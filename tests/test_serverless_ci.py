from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_DIR = REPO_ROOT / "benchmarks" / "300.utilities" / "313.serverless-ci"
DATA_DIR = REPO_ROOT / "benchmarks-data" / "300.utilities" / "313.serverless-ci"


def load_input_module():
    spec = importlib.util.spec_from_file_location(
        "serverless_ci_input", str(BENCHMARK_DIR / "input.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LocalMockStorage:
    def __init__(self, root_dir: Path):
        self.root_dir = root_dir

    def upload(self, bucket: str, key: str, src_path: str) -> None:
        dest = self.root_dir / bucket / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest)

    def download(self, bucket: str, key: str, dest_path: str) -> None:
        src = self.root_dir / bucket / key
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest_path)


class TestServerlessCIBenchmark(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.input_mod = load_input_module()

    def test_buckets_count(self) -> None:
        self.assertEqual(self.input_mod.buckets_count(), (1, 1))

    def test_generate_input_profiles(self) -> None:
        uploaded = []

        def mock_upload(bucket_idx: int, key: str, filepath: str) -> None:
            self.assertTrue(os.path.exists(filepath), f"Missing fixture file: {filepath}")
            uploaded.append((bucket_idx, key, filepath))

        for size, expected_project in [
            ("test", "ts-pattern"),
            ("small", "ts-pattern"),
            ("large", "nestjs"),
        ]:
            uploaded.clear()
            cfg = self.input_mod.generate_input(
                data_dir=str(DATA_DIR),
                size=size,
                benchmarks_bucket="test-bucket",
                input_paths=["in-prefix"],
                output_paths=["out-prefix"],
                upload_func=mock_upload,
            )
            self.assertEqual(cfg["bucket"]["bucket"], "test-bucket")
            self.assertEqual(cfg["object"]["project"], expected_project)
            self.assertEqual(len(uploaded), 2)

    def test_openwhisk_storage_wrapper_get_instance(self) -> None:
        wrapper_path = REPO_ROOT / "benchmarks" / "wrappers" / "openwhisk" / "nodejs" / "storage.js"
        content = wrapper_path.read_text(encoding="utf-8")
        self.assertIn("new minio_storage()", content)
        self.assertNotIn("new storage()", content)

    def test_openwhisk_trigger_polling_on_long_running_action(self) -> None:
        import sys
        import types
        from unittest.mock import MagicMock

        for mod_name in (
            "pycurl",
            "docker",
            "docker.errors",
            "docker.models",
            "docker.models.containers",
            "requests",
            "rich",
            "rich.progress",
            "yaml",
            "colorama",
        ):
            if mod_name not in sys.modules:
                sys.modules[mod_name] = MagicMock()

        for pkg_name, rel_path in (
            ("sebs", "sebs"),
            ("sebs.faas", "sebs/faas"),
            ("sebs.openwhisk", "sebs/openwhisk"),
        ):
            if pkg_name not in sys.modules:
                pkg = types.ModuleType(pkg_name)
                pkg.__path__ = [str(REPO_ROOT / rel_path)]
                sys.modules[pkg_name] = pkg

        from sebs.openwhisk.triggers import LibraryTrigger

        trigger = LibraryTrigger(fname="313.serverless-ci", wsk_cmd=["wsk", "--insecure"])

        timeout_err = subprocess.CalledProcessError(
            returncode=1,
            cmd=["wsk", "--insecure", "action", "invoke", "--result", "313.serverless-ci"],
            output=b"ok: invoked /_/313.serverless-ci, but the request has not yet finished, with id 0123456789abcdef0123456789abcdef\n",
        )
        activation_doc = {
            "activationId": "0123456789abcdef0123456789abcdef",
            "end": 1700000120000,
            "duration": 115000,
            "annotations": [
                {"key": "waitTime", "value": 45},
                {"key": "initTime", "value": 320},
            ],
            "response": {
                "result": {
                    "begin": "1700000005.000",
                    "end": "1700000120.000",
                    "request_id": "0123456789abcdef0123456789abcdef",
                    "is_cold": True,
                    "result": {
                        "result": {"status": "SUCCESS", "tests_passed": 931},
                        "measurement": {"total_time": 115000000},
                    },
                }
            },
        }

        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                timeout_err,
                 subprocess.CompletedProcess(
                    args=["wsk"],
                    returncode=0,
                    stdout=(
                        "ok: got activation 0123456789abcdef0123456789abcdef\n"
                        + json.dumps(activation_doc)
                    ).encode("utf-8"),
                ),
            ]
            result = trigger.sync_invoke(payload={"bucket": {}, "object": {}})
            self.assertEqual(result.output["result"]["result"]["status"], "SUCCESS")
            self.assertEqual(result.output["result"]["result"]["tests_passed"], 931)
            self.assertTrue(result.stats.cold_start)

    def _invoke_node_handler(
        self,
        size: str,
        profile_override: str | None = None,
        reuse_cache: bool = False,
        incremental: bool = False,
        storage_root: Path | None = None,
    ) -> tuple[dict, dict, LocalMockStorage]:
        own_tmp = storage_root is None
        tmp_dir = Path(tempfile.mkdtemp(prefix="sebs-ci-test-")) if own_tmp else storage_root
        assert tmp_dir is not None
        try:
            mock_storage = LocalMockStorage(tmp_dir / "storage")

            def upload_func(bucket_idx: int, key: str, filepath: str) -> None:
                mock_storage.upload("ci-bucket", f"input/{key}", filepath)

            env_patch = {}
            if profile_override:
                env_patch["SEBS_CI_PROFILE"] = profile_override
            if reuse_cache:
                env_patch["SEBS_CI_REUSE_CACHE"] = "1"
            if incremental:
                env_patch["SEBS_CI_INCREMENTAL"] = "1"

            with mock.patch.dict(os.environ, env_patch, clear=False):
                input_cfg = self.input_mod.generate_input(
                    data_dir=str(DATA_DIR),
                    size=size,
                    benchmarks_bucket="ci-bucket",
                    input_paths=["input"],
                    output_paths=["output"],
                    upload_func=upload_func,
                )

            harness_dir = tmp_dir / "harness"
            harness_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BENCHMARK_DIR / "nodejs" / "function.js", harness_dir / "function.js")

            storage_shim = """
const fs = require('fs');
const path = require('path');
class LocalStorage {
  constructor() {
    this.root = process.env.SEBS_MOCK_STORAGE_ROOT;
  }
  async download(bucket, key, destPath) {
    const src = path.join(this.root, bucket, key);
    fs.mkdirSync(path.dirname(destPath), { recursive: true });
    fs.copyFileSync(src, destPath);
  }
  upload(bucket, key, srcPath) {
    const parsed = path.parse(key);
    const uniqueKey = path.posix.join(parsed.dir, `${parsed.name}.deadbeef${parsed.ext}`);
    const dst = path.join(this.root, bucket, uniqueKey);
    fs.mkdirSync(path.dirname(dst), { recursive: true });
    const p = Promise.resolve().then(() => {
      fs.copyFileSync(srcPath, dst);
    });
    return [uniqueKey, p];
  }
}
module.exports.storage = LocalStorage;
"""
            (harness_dir / "storage.js").write_text(storage_shim, encoding="utf-8")
            event_file = harness_dir / "event.json"
            out_file = harness_dir / "output.json"
            event_file.write_text(json.dumps(input_cfg), encoding="utf-8")

            runner_js = """
const fs = require('fs');
const fn = require('./function');
const event = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
fn.handler(event).then((res) => {
  fs.writeFileSync(process.argv[3], JSON.stringify(res, null, 2));
}).catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
            (harness_dir / "runner.js").write_text(runner_js, encoding="utf-8")
            env = os.environ.copy()
            env["SEBS_MOCK_STORAGE_ROOT"] = str(tmp_dir / "storage")
            subprocess.run(
                ["node", str(harness_dir / "runner.js"), str(event_file), str(out_file)],
                cwd=str(harness_dir),
                env=env,
                check=True,
            )
            output = json.loads(out_file.read_text(encoding="utf-8"))
            return input_cfg, output, mock_storage
        finally:
            if own_tmp and tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_end_to_end_ts_pattern_and_warm_incremental(self) -> None:
        storage_root = Path(tempfile.mkdtemp(prefix="sebs-ci-e2e-"))
        cache_dir = Path("/tmp/sebs-ci-cache/ts-pattern-v5.0.5")
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
        try:
            # 1. Clean run + cache population
            input_cfg1, output1, mock_storage1 = self._invoke_node_handler(
                size="test", reuse_cache=True, incremental=False, storage_root=storage_root
            )
            err1 = self.input_mod.validate_output(
                str(DATA_DIR), input_cfg1, output1, "nodejs", storage=mock_storage1
            )
            self.assertIsNone(err1, f"Validation failed on clean ts-pattern run: {err1}")
            self.assertFalse(output1["result"]["cache_hit"])
            self.assertEqual(output1["result"]["tests_passed"], 9)
            self.assertEqual(output1["result"]["emitted_files_count"], 32)

            # 2. Warm incremental run (reuses /tmp cache + mutates 1 file)
            input_cfg2, output2, mock_storage2 = self._invoke_node_handler(
                size="test", reuse_cache=True, incremental=True, storage_root=storage_root
            )
            err2 = self.input_mod.validate_output(
                str(DATA_DIR), input_cfg2, output2, "nodejs", storage=mock_storage2
            )
            self.assertIsNone(err2, f"Validation failed on warm incremental run: {err2}")
            self.assertTrue(output2["result"]["cache_hit"])
            self.assertTrue(output2["result"]["incremental_change"])
            self.assertEqual(output2["measurement"]["download_time"], 0)
        finally:
            shutil.rmtree(storage_root, ignore_errors=True)
            if cache_dir.exists():
                shutil.rmtree(cache_dir, ignore_errors=True)

    def test_end_to_end_nestjs_large(self) -> None:
        storage_root = Path(tempfile.mkdtemp(prefix="sebs-ci-nestjs-"))
        try:
            input_cfg, output, mock_storage = self._invoke_node_handler(
                size="large", storage_root=storage_root
            )
            err = self.input_mod.validate_output(
                str(DATA_DIR), input_cfg, output, "nodejs", storage=mock_storage
            )
            self.assertIsNone(err, f"Validation failed on NestJS large run: {err}")
            self.assertEqual(output["result"]["status"], "SUCCESS")
            self.assertEqual(output["result"]["tests_passed"], 931)
            self.assertEqual(output["result"]["tests_failed"], 0)
            self.assertGreaterEqual(output["result"]["emitted_files_count"], 650)
        finally:
            shutil.rmtree(storage_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
