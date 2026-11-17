from dataclasses import replace
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from python import generator as retail


def read_events(directory):
    rows = []
    for path in (directory / "transactions").glob("*.csv"):
        with path.open(encoding="utf-8", newline="") as source:
            rows.extend(csv.DictReader(source))
    return sorted(rows, key=lambda row: row["event_id"])


class RetailGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_deterministic_generation(self):
        a, b = self.root / "a", self.root / "b"
        retail.generate_dataset(a)
        retail.generate_dataset(b)
        self.assertEqual({p.relative_to(a): p.read_bytes() for p in a.rglob("*") if p.is_file()},
                         {p.relative_to(b): p.read_bytes() for p in b.rglob("*") if p.is_file()})

    def test_file_count_changes_only_layout(self):
        for size, config in retail.PROFILES.items():
            with self.subTest(size=size):
                a, b = self.root / (size + "-one"), self.root / (size + "-many")
                retail.generate_dataset(a, replace(config, file_count=1))
                retail.generate_dataset(b, replace(config, file_count=17))
                self.assertEqual(read_events(a), read_events(b))
                for filename in ("products.csv", "stores.csv"):
                    self.assertEqual((a / filename).read_bytes(), (b / filename).read_bytes())

    def test_runtime_settings_reach_payload_without_changing_dataset_identity(self):
        spec = importlib.util.spec_from_file_location(
            "retail_input", Path(__file__).resolve().parents[1] / "input.py")
        inputs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inputs)

        def prepare():
            uploads = {}

            def upload(index, key, path):
                uploads[key] = Path(path).read_bytes()

            event = inputs.generate_input(
                str(self.root), "test", "bucket", ["input"], ["output"], upload, None)
            return event, uploads

        original, original_uploads = prepare()
        settings = {"resources": {"threads": 2, "memory_limit": "128MB"}, "profile": True}
        (self.root / "retail-runtime.json").write_text(json.dumps(settings), encoding="utf-8")
        configured, configured_uploads = prepare()

        self.assertEqual(original.pop("resources"), {})
        self.assertFalse(original.pop("profile"))
        self.assertEqual(configured.pop("resources"), settings["resources"])
        self.assertTrue(configured.pop("profile"))
        self.assertEqual(original, configured)
        self.assertEqual(original_uploads, configured_uploads)


if __name__ == "__main__":
    unittest.main()
