from datetime import date
import tempfile
from pathlib import Path
import unittest

import duckdb

from python import generator as retail
from python.contract import OUTPUT_HEADERS
from python.etl_workload import transform
from validation import reference_result, validate_output
from .retail_fixtures import sample_events, write_dataset


class RetailETLTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.input = write_dataset(self.root / "input", sample_events())
        self.output = self.root / "output"

    def tearDown(self):
        self.temporary.cleanup()

    def test_end_to_end_profiles(self):
        for size, config in retail.PROFILES.items():
            with self.subTest(size=size):
                dataset, output = self.root / size, self.root / (size + "-out")
                retail.generate_dataset(dataset, config)
                audit = transform(dataset, output, memory_mb=1024)
                self.assertEqual(audit["input_events"], retail.EVENT_COUNTS[size])
                self.assertEqual(validate_output(output, dataset)["audit"], audit)

    def test_snapshot_filtering_joins_and_money(self):
        rows, audit = reference_result(self.input)
        self.assertEqual(audit, {"input_events": 14, "output_rows": 4})
        by_id = {row[1]: dict(zip(OUTPUT_HEADERS, row)) for row in rows}
        self.assertEqual(set(by_id), {"NORMAL", "UPDATE", "TIE", "ROUND"})
        self.assertEqual(by_id["TIE"]["unit_price_cents"], 2501)
        self.assertEqual(by_id["UPDATE"]["quantity"], 3)
        self.assertEqual(by_id["UPDATE"]["product_name"], "Shirt")
        self.assertEqual(by_id["UPDATE"]["region"], "south")
        self.assertEqual(by_id["UPDATE"]["transaction_date"], date(2025, 1, 2))
        self.assertEqual(tuple(by_id["NORMAL"][name] for name in ("gross_cents", "discount_cents", "total_cents")),
                         (3998, 200, 3798))
        self.assertEqual(by_id["ROUND"]["discount_cents"], 1)
        self.assertEqual(transform(self.input, self.output, memory_mb=1024), audit)
        self.assertEqual(validate_output(self.output, self.input)["audit"], audit)

    def test_profiling_and_resource_controls(self):
        phases = {}
        audit = transform(self.input, self.output, memory_mb=512, threads=2,
                          memory_limit="128MB", profile=True, measurements=phases)
        self.assertEqual(validate_output(self.output, self.input)["audit"], audit)
        resources = phases["resources"]
        self.assertEqual(resources["memory_mb"], 512)
        self.assertEqual(resources["threads"], 2)
        self.assertEqual(resources["memory_limit"], "128MB")
        self.assertTrue(resources["effective_memory_limit"])
        for name in ("initialization_time", "compute_time", "sql_setup_time", "query_execution_time"):
            self.assertGreater(phases[name], 0)
        self.assertGreaterEqual(phases["compute_time"], phases["sql_setup_time"] + phases["query_execution_time"])
        profile = phases["profiling"]
        self.assertEqual(profile["duckdb_version"], duckdb.__version__)
        self.assertEqual(profile["missing_metrics"], [])
        self.assertGreater(profile["system_peak_buffer_memory_bytes"], 0)
        self.assertGreaterEqual(profile["system_peak_temp_dir_size_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
