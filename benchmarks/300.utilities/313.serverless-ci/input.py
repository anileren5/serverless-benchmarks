from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple


def buckets_count() -> Tuple[int, int]:
    return (1, 1)


def _ensure_data_dir(data_dir: Optional[str]) -> str:
    if data_dir is None:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        data_dir = os.path.join(repo_root, "benchmarks-data", "300.utilities", "313.serverless-ci")
    manifest_path = os.path.join(data_dir, "fixtures_manifest.json")
    if not os.path.exists(manifest_path):
        import subprocess
        import sys

        prep_script = os.path.join(os.path.dirname(__file__), "prepare_fixtures.py")
        subprocess.run(
            [sys.executable, prep_script, "--output-dir", data_dir],
            check=True,
        )
    return data_dir


def _load_manifest(data_dir: str) -> Dict[str, Any]:
    manifest_path = os.path.join(data_dir, "fixtures_manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


def generate_input(
    data_dir: Optional[str],
    size: str,
    benchmarks_bucket: Optional[str],
    input_paths: List[str],
    output_paths: List[str],
    upload_func: Optional[Callable[[int, str, str], None]],
    nosql_func: Optional[
        Callable[[str, str, dict, Tuple[str, str], Optional[Tuple[str, str]]], None]
    ] = None,
) -> Dict[str, Any]:
    assert upload_func is not None, "Benchmark 313.serverless-ci requires an upload function"
    resolved_data_dir = _ensure_data_dir(data_dir)
    manifest = _load_manifest(resolved_data_dir)
    profiles = manifest["profiles"]
    projects = manifest["projects"]

    profile_override = os.environ.get("SEBS_CI_PROFILE")
    effective_profile = profile_override if profile_override in profiles else size
    if effective_profile not in profiles:
        effective_profile = "test"

    profile_cfg = profiles[effective_profile]
    project_name = profile_cfg["project"]
    project_meta = projects[project_name]

    source_archive = project_meta["source_archive"]
    deps_archive = project_meta["deps_archive"]

    upload_func(0, source_archive, os.path.join(resolved_data_dir, source_archive))
    upload_func(0, deps_archive, os.path.join(resolved_data_dir, deps_archive))

    reuse_cache = os.environ.get("SEBS_CI_REUSE_CACHE", "").lower() in ("1", "true", "yes")
    incremental_change = os.environ.get("SEBS_CI_INCREMENTAL", "").lower() in ("1", "true", "yes")
    build_passes_override = os.environ.get("SEBS_CI_BUILD_PASSES")
    test_passes_override = os.environ.get("SEBS_CI_TEST_PASSES")

    build_passes = (
        int(build_passes_override)
        if build_passes_override
        else int(profile_cfg.get("build_passes", 1))
    )
    test_passes = (
        int(test_passes_override)
        if test_passes_override
        else int(profile_cfg.get("test_passes", 1))
    )

    output_dirs = ["dist"] if project_name == "ts-pattern" else []

    return {
        "bucket": {
            "bucket": benchmarks_bucket,
            "input": input_paths[0],
            "output": output_paths[0],
        },
        "object": {
            "profile": effective_profile,
            "project": project_meta["project"],
            "version": project_meta["version"],
            "commit": project_meta["commit"],
            "source_archive": source_archive,
            "source_sha256": project_meta["source_sha256"],
            "deps_archive": deps_archive,
            "deps_sha256": project_meta["deps_sha256"],
            "typecheck_args": profile_cfg.get("typecheck_args", []),
            "build_args": profile_cfg.get("build_args", []),
            "clean_args": profile_cfg.get("clean_args", []),
            "test_args": profile_cfg.get("test_args", []),
            "build_passes": build_passes,
            "test_passes": test_passes,
            "reuse_cache": reuse_cache,
            "incremental_change": incremental_change,
            "output_dirs": output_dirs,
            "expected_tests_passed": profile_cfg["expected_tests_passed"],
            "expected_emitted_files_min": profile_cfg["expected_emitted_files_min"],
        },
    }


def validate_output(
    data_dir: str | None,
    input_config: dict,
    output: dict,
    language: str,
    storage: Optional[Any] = None,
) -> str | None:
    result = output.get("result", output)
    if not isinstance(result, dict):
        return f"Expected dict result in output, got: {type(result)}"

    status = result.get("status")
    if status != "SUCCESS":
        failure_class = result.get("failure_class", "UNKNOWN")
        failed_stage = result.get("failed_stage", "unknown")
        err_msg = result.get("error_message", "no message")
        return (
            f"CI pipeline status is {status} "
            f"(failure_class={failure_class}, stage={failed_stage}): {err_msg}"
        )

    obj = input_config.get("object", {})
    expected_source_sha = obj.get("source_sha256")
    if expected_source_sha and result.get("source_sha256") != expected_source_sha:
        return (
            f"Source SHA-256 mismatch: expected {expected_source_sha}, "
            f"got {result.get('source_sha256')}"
        )

    expected_deps_sha = obj.get("deps_sha256")
    if expected_deps_sha and result.get("deps_sha256") != expected_deps_sha:
        return (
            f"Deps SHA-256 mismatch: expected {expected_deps_sha}, "
            f"got {result.get('deps_sha256')}"
        )

    tests_failed = result.get("tests_failed", -1)
    if tests_failed != 0:
        return f"Expected 0 failed unit tests, got {tests_failed}"

    expected_passed = obj.get("expected_tests_passed", 1)
    tests_passed = result.get("tests_passed", 0)
    if tests_passed < expected_passed:
        return (
            f"Expected at least {expected_passed} passing unit tests for profile "
            f"'{obj.get('profile')}', got {tests_passed}"
        )

    expected_files_min = obj.get("expected_emitted_files_min", 1)
    emitted_files = result.get("emitted_files_count", 0)
    if emitted_files < expected_files_min:
        return (
            f"Expected at least {expected_files_min} emitted build files, "
            f"got {emitted_files}"
        )

    build_sha = result.get("build_output_sha256", "")
    if not isinstance(build_sha, str) or len(build_sha) != 64:
        return f"Invalid build_output_sha256 in result: {build_sha!r}"

    report_key = result.get("report_key")
    if not report_key:
        return "Missing report_key in CI result"

    if storage is not None:
        bucket = input_config["bucket"]["bucket"]
        with tempfile.TemporaryDirectory() as tmp_dir:
            local_report = os.path.join(tmp_dir, "report.json")
            storage.download(bucket, report_key, local_report)
            if not os.path.exists(local_report):
                return f"Uploaded CI report not found in storage at {report_key}"
            with open(local_report, "r", encoding="utf-8") as f:
                report_data = json.load(f)
            if report_data.get("build_output_sha256") != build_sha:
                return (
                    f"Storage report build_output_sha256 ({report_data.get('build_output_sha256')}) "
                    f"does not match handler output ({build_sha})"
                )

    return None
