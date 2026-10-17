"""Checks that PCA, clustering, and ingest recover known structure."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from characterization.analyze import infer_origin, main as analyze_main
from characterization.cluster import cluster_space, empty_regions, pair_rand
from characterization.features import prepare_matrix, subset_frame
from characterization.ingest import (
    disk_features,
    ingest_tree,
    package_sizes_in_cache_config,
    papi_features,
    perf_cost_features,
    ratios_from_papi,
)
from characterization.pca import fit_pca, kaiser_components
from characterization.report import analyze_frame


class PCATests(unittest.TestCase):
    """Two separated groups must survive standardization and PCA."""

    def _two_groups(self) -> pd.DataFrame:
        rng = np.random.default_rng(0)
        rows = []
        # Every feature moves with the group. Independent noise columns become
        # full principal components after z-scoring, which is a different fixture.
        for name, base in (
            ("a1", 0.0),
            ("a2", 0.0),
            ("a3", 0.0),
            ("a4", 0.0),
            ("b1", 10.0),
            ("b2", 10.0),
            ("b3", 10.0),
            ("b4", 10.0),
        ):
            noise = rng.normal(0.0, 0.05, size=4)
            values = np.array([base, base, 0.5 * base, 1.0 + 0.2 * base]) + noise
            rows.append(
                {
                    "benchmark": name,
                    "f0": values[0],
                    "f1": values[1],
                    "f2": values[2],
                    "f3": values[3],
                }
            )
        return pd.DataFrame(rows)

    def test_recovers_two_clusters_and_positive_loading(self):
        frame = self._two_groups()
        matrix = prepare_matrix(frame, ["f0", "f1", "f2", "f3"])
        pca = fit_pca(matrix.standardized, matrix.columns)
        primary = kaiser_components(pca.eigenvalues)
        self.assertIn(0, primary)
        self.assertGreater(pca.variance_ratio[0], 0.5)
        # The dominant signal is f0 and f1 rising together, and the sign fix
        # makes the larger of those loadings positive.
        self.assertGreater(max(pca.loadings[0, 0], pca.loadings[1, 0]), 0)

        result = cluster_space(pca.scores[:, primary], matrix.labels, seed=0)
        self.assertEqual(result.selected.k, 2)
        members = {}
        for label, name in zip(result.selected.labels, matrix.labels):
            members.setdefault(int(label), set()).add(name[0])
        self.assertEqual(
            {frozenset(group) for group in members.values()},
            {frozenset({"a"}), frozenset({"b"})},
        )
        self.assertGreaterEqual(result.rand_index, 0.9)
        pair_names = {(item["a"][0], item["b"][0]) for item in result.redundancy.pairs}
        self.assertTrue(pair_names)
        self.assertTrue(all(left == right for left, right in pair_names))

    def test_constant_and_incomplete_columns_are_dropped(self):
        frame = pd.DataFrame(
            {
                "benchmark": ["a", "b", "c"],
                "keep": [1.0, 2.0, 3.0],
                "also": [3.0, 2.0, 1.0],
                "flat": [5.0, 5.0, 5.0],
                "gap": [1.0, None, 3.0],
            }
        )
        matrix = prepare_matrix(frame, ["keep", "also", "flat", "gap", "missing"])
        self.assertEqual(matrix.columns, ["keep", "also"])
        self.assertTrue(any("constant" in note for note in matrix.notes))
        self.assertTrue(any("missing values" in note for note in matrix.notes))

    def test_workload_rows_stay_on_the_local_platform(self):
        frame = pd.DataFrame(
            {
                "benchmark": ["110.dynamic-html", "110.dynamic-html", "501.graph-pagerank"],
                "platform": ["local", "aws", "local"],
                "ipc": [1.0, 9.0, 0.5],
            }
        )
        kept, notes = subset_frame(frame, "workload", None)
        self.assertEqual(sorted(kept["platform"]), ["local", "local"])
        self.assertTrue(notes)
        with self.assertRaises(ValueError):
            subset_frame(frame, "deployment", None)

    def test_empty_interior_cell(self):
        scores = np.array(
            [
                [0.0, 0.0],
                [0.1, 0.0],
                [3.0, 3.0],
                [2.9, 3.0],
            ]
        )
        loadings = np.array([[0.8, 0.1], [0.2, 0.9]])
        cells = empty_regions(scores, loadings, ["ipc", "stall_frac"], grid=4)
        self.assertTrue(cells)
        self.assertTrue(all(cell.pc1_features for cell in cells))

    def test_rand_index_extremes(self):
        labels = np.array([0, 0, 1, 1])
        self.assertEqual(pair_rand(labels, labels), 1.0)
        opposite = np.array([0, 1, 0, 1])
        self.assertLess(pair_rand(labels, opposite), 1.0)


class IngestTests(unittest.TestCase):
    """Parsers follow the files SeBS already writes."""

    def test_papi_ratios_use_one_event_set(self):
        groups = {
            "cycles": {"PAPI_TOT_CYC": 200.0, "PAPI_TOT_INS": 100.0, "PAPI_STL_ICY": 50.0},
            "inscount": {"PAPI_TOT_INS": 80.0, "PAPI_LST_INS": 20.0, "PAPI_BR_INS": 8.0},
            "cache": {"PAPI_TOT_INS": 40.0, "PAPI_L1_DCM": 4.0, "PAPI_L2_TCM": 2.0, "PAPI_L3_TCM": 1.0},
        }
        ratios = ratios_from_papi(groups)
        self.assertAlmostEqual(ratios["ipc"], 0.5)
        self.assertAlmostEqual(ratios["stall_frac"], 0.25)
        self.assertAlmostEqual(ratios["load_store_frac"], 0.25)
        self.assertAlmostEqual(ratios["branch_frac"], 0.1)
        self.assertAlmostEqual(ratios["l1d_miss_per_ins"], 0.1)
        self.assertAlmostEqual(ratios["l3_miss_per_ins"], 0.025)

    def test_disk_header_shift_and_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bench = root / "local" / "110.dynamic-html"
            (bench / "papi").mkdir(parents=True)
            (bench / "disk-io").mkdir()
            (bench / "time").mkdir()
            (bench / "perf-cost").mkdir()
            (bench / "papi" / "inscount_papi.csv").write_text(
                "Time,RelativeTime,PAPI_TOT_INS,PAPI_LST_INS,PAPI_BR_INS\n"
                "1,0,100,40,10\n"
                "2,1,50,10,5\n"
            )
            (bench / "papi" / "cycles_papi.csv").write_text(
                "Time,RelativeTime,PAPI_TOT_CYC,PAPI_TOT_INS,PAPI_STL_ICY\n"
                "1,0,300,150,30\n"
            )
            (bench / "disk-io" / "disk-io.csv").write_text(
                "Timestamp,WriteChars,ReadChars,ReadSysCalls,WriteSysCalls,ReadBytes,WriteBytes\n"
                "t0,1,100,200,3,4,10,20,0\n"
                "t1,1,150,260,5,8,40,70,0\n"
            )
            (bench / "time" / "time_warm.csv").write_text(
                "#Seconds from epoch.microseconds; CPU times are in microseconds\n"
                "Begin,End,Duration,User,Sys\n"
                "0,1,1000,800,100\n"
                "0,1,3000,2400,300\n"
            )
            (bench / "package.json").write_text(json.dumps({"package_size_bytes": 4096}))
            (bench / "perf-cost" / "cold.json").write_text(
                json.dumps(
                    {
                        "_invocations": {
                            "fn": {
                                "cold": {
                                    "times": {"benchmark": 1000, "client": 5000},
                                    "provider_times": {"initialization": 8000, "execution": 2000},
                                    "stats": {"cold_start": True, "memory_used": 64, "failure": False},
                                },
                                "failed": {
                                    "times": {"benchmark": 1, "client": 1},
                                    "provider_times": {"initialization": 1, "execution": 1},
                                    "stats": {"cold_start": True, "memory_used": 1, "failure": True},
                                },
                            }
                        }
                    }
                )
            )
            frame = ingest_tree(root)
            row = frame.iloc[0]
            self.assertEqual(row["benchmark"], "110.dynamic-html")
            self.assertEqual(row["platform"], "local")
            self.assertAlmostEqual(row["load_store_frac"], 50 / 150)
            self.assertAlmostEqual(row["ipc"], 150 / 300)
            self.assertAlmostEqual(row["read_bytes"], 30)
            self.assertAlmostEqual(row["write_bytes"], 50)
            self.assertAlmostEqual(row["cpu_util"], (1600 + 200) / 2000)
            self.assertAlmostEqual(row["package_size_bytes"], 4096)
            self.assertAlmostEqual(row["cold_init_us"], 8000)
            self.assertAlmostEqual(row["init_fraction"], 8000 / (8000 + 2000))
            self.assertAlmostEqual(row["provider_mem_mb"], 64)

    def test_direct_parsers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "papi.csv"
            path.write_text("Time,RelativeTime,PAPI_TOT_INS,PAPI_LST_INS\n1,0,10,4\n")
            self.assertAlmostEqual(papi_features([path])["load_store_frac"], 0.4)
            disk = Path(tmp) / "disk.csv"
            disk.write_text(
                "Timestamp,WriteChars,ReadChars,ReadSysCalls,WriteSysCalls,ReadBytes,WriteBytes\n"
                "t,1,0,0,1,1,5,6,0\n"
                "t,1,0,0,2,2,8,9,0\n"
            )
            parsed = disk_features([disk])
            self.assertAlmostEqual(parsed["read_bytes"], 3)
            self.assertAlmostEqual(parsed["write_syscalls"], 1)

    def test_cache_size_and_perf_median(self):
        sizes = package_sizes_in_cache_config({"aws": {"python": {"code_package": {"size": 12}}}})
        self.assertEqual(sizes, [12])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "perf.json"
            path.write_text(
                json.dumps(
                    {
                        "_invocations": {
                            "fn": {
                                "a": {
                                    "times": {"benchmark": 10, "client": 30},
                                    "provider_times": {"initialization": 100, "execution": 40},
                                    "stats": {"cold_start": True, "memory_used": 10},
                                },
                                "b": {
                                    "times": {"benchmark": 30, "client": 50},
                                    "provider_times": {"initialization": 300, "execution": 80},
                                    "stats": {"cold_start": True, "memory_used": 30},
                                },
                            }
                        }
                    }
                )
            )
            features = perf_cost_features([path])
            self.assertAlmostEqual(features["cold_init_us"], 200)
            self.assertAlmostEqual(features["provider_mem_mb"], 20)


class ReportTests(unittest.TestCase):
    """The written report records the method and refuses to hide synthetic data."""

    def test_report_round_trip(self):
        frame = pd.DataFrame(
            {
                "benchmark": ["a", "b", "c", "d", "e", "f"],
                "platform": ["local"] * 6,
                "origin": ["synthetic"] * 6,
                "ipc": [0.20, 0.22, 0.21, 2.00, 2.02, 1.98],
                "stall_frac": [0.80, 0.78, 0.79, 0.10, 0.11, 0.12],
                "package_size_bytes": [1000, 1100, 1050, 80000, 79000, 81000],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            csv_path = directory / "features.csv"
            frame.to_csv(csv_path, index=False)
            out = directory / "report"
            analyze_main(
                [
                    "--features",
                    str(csv_path),
                    "--feature-set",
                    "workload",
                    "--out",
                    str(out),
                    "--origin",
                    "measured",
                ]
            )
            payload = json.loads((out / "report.json").read_text())
            self.assertEqual(payload["provenance"]["origin"], "synthetic")
            self.assertEqual(payload["selected_k"], 2)
            text = (out / "report.md").read_text()
            self.assertIn("synthetic", text)
            self.assertIn("Eigenvalue", text)
            self.assertTrue((out / "loadings.csv").is_file())
            self.assertTrue((out / "scores.csv").is_file())

    def test_infer_origin(self):
        frame = pd.DataFrame({"origin": ["measured", "synthetic"]})
        self.assertEqual(infer_origin(frame, "measured"), "synthetic")


class EndToEndMatrixTests(unittest.TestCase):
    """analyze_frame on a hand-built deployment table selects two groups."""

    def test_deployment_split(self):
        frame = pd.DataFrame(
            {
                "benchmark": ["web", "web2", "web3", "ml", "ml2", "ml3"],
                "cold_init_us": [100, 120, 110, 9000, 8800, 9100],
                "cold_provider_exec_us": [50, 60, 55, 4000, 4200, 4100],
                "warm_provider_exec_us": [40, 50, 45, 3000, 3100, 2900],
                "init_fraction": [0.66, 0.66, 0.66, 0.69, 0.68, 0.69],
                "package_size_bytes": [1000, 1100, 900, 80000, 79000, 81000],
            }
        )
        from characterization.features import DEPLOYMENT_FEATURES

        analysis = analyze_frame(frame, DEPLOYMENT_FEATURES, seed=0)
        self.assertGreaterEqual(analysis.clusters.selected.k, 2)
        self.assertEqual(len(analysis.matrix.labels), 6)


if __name__ == "__main__":
    unittest.main()
