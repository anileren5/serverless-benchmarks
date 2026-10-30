import unittest

import pandas as pd

from sebs.experiments.cpu_oversubscription import benchmark_result, flatten, summarize


def jacobi_run(threads: int, wall_seconds: float, position: int) -> dict:
    return {
        "threads": threads,
        "position": position,
        "wall_seconds": wall_seconds,
        "cpu_seconds": wall_seconds * threads,
        "checksum": 1.0,
        "cgroup_cpu_stat_delta": {"nr_periods": 10, "nr_throttled": 5, "throttled_usec": 1000},
        "idle_cpu_seconds_after": 0.0,
    }


def benchmark(runs: list) -> dict:
    return {
        "environment": {"os_cpu_count": 32, "omp_wait_policy": None, "cgroup": {}},
        "effective_cpus": {"effective_cpus": 2.0},
        "jacobi": runs,
    }


def record(runs: list, policy=None, phase="measurement") -> dict:
    return {
        "configuration": "test",
        "resources": "1024MB",
        "omp_wait_policy_requested": policy,
        "cold_starts": False,
        "phase": phase,
        "failure": False,
        "benchmark": benchmark(runs),
    }


class CPUOversubscriptionProcessing(unittest.TestCase):
    def test_benchmark_result_unwraps_platform_outputs(self):
        result = benchmark({"jacobi": []})
        # AWS: result.output; local: result.output.result; OpenWhisk: result.result
        self.assertIs(benchmark_result({"result": {"output": result}}), result)
        self.assertIs(benchmark_result({"result": {"output": {"result": result}}}), result)
        self.assertIs(benchmark_result({"result": {"result": result}}), result)
        self.assertEqual(benchmark_result({"result": "Error"}), {})

    def test_flatten_one_row_per_kernel_run(self):
        rows = flatten(record([jacobi_run(1, 1.0, 1), jacobi_run(4, 0.5, 0)]))
        self.assertEqual([row["threads"] for row in rows], [1, 4])
        self.assertEqual(rows[1]["position"], 0)
        self.assertEqual(rows[0]["throttled_fraction"], 0.5)
        self.assertEqual(rows[1]["cpu_utilization"], 4)

    def test_flatten_keeps_failures(self):
        rows = flatten({"configuration": "test", "failure": True, "error": "timeout"})
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["failure"])

    def test_summarize_speedup_and_penalty_per_cell(self):
        rows = []
        for policy in (None, "passive"):
            for wall in (1.0, 1.2):
                runs = [
                    jacobi_run(1, wall, 0),
                    jacobi_run(2, wall / 2, 1),
                    jacobi_run(32, wall * 4, 2),
                ]
                rows += flatten(record(runs, policy))
        rows += flatten(record([jacobi_run(1, 100.0, 0)], phase="warmup"))

        summary = summarize(pd.DataFrame(rows))
        self.assertEqual(len(summary), 6)
        default = summary[summary["omp_wait_policy_requested"].isna()].set_index("threads")
        self.assertAlmostEqual(default.loc[1, "wall_median"], 1.1)
        self.assertAlmostEqual(default.loc[2, "speedup"], 2.0)
        self.assertAlmostEqual(default.loc[32, "penalty_vs_best"], 8.0)


if __name__ == "__main__":
    unittest.main()
