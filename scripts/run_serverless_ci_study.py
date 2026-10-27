#!/usr/bin/env python3
"""Cross-platform study runner and analyzer for 313.serverless-ci (OpenWhisk & AWS Lambda).

Supports:
1. Automated execution of 313.serverless-ci across memory sizes, workload profiles,
   and build modes (clean cold, warm cached, warm incremental) on OpenWhisk, AWS, or local.
2. Aggregation of stage-decomposed latencies, peak RSS / /tmp disk usage, OpenWhisk
   controller waitTime / initTime, AWS billed duration, cost vs. GitHub Actions ($0.008/min),
   and feasibility/failure classification matrices.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]

AWS_PRICE_PER_GB_SEC = 0.0000166667
AWS_PRICE_PER_REQUEST = 0.0000002
GITHUB_ACTIONS_LINUX_PRICE_PER_SEC = 0.008 / 60.0

DEFAULT_PROFILES = ["test", "small", "nestjs-single", "large", "nestjs-full", "nestjs-endurance"]
DEFAULT_MEMORY_MB = [512, 1024, 2048, 3008, 4096]


def compute_aws_cost_usd(memory_mb: int, duration_sec: float) -> float:
    gb_sec = (memory_mb / 1024.0) * duration_sec
    return gb_sec * AWS_PRICE_PER_GB_SEC + AWS_PRICE_PER_REQUEST


def compute_gha_cost_usd(duration_sec: float) -> float:
    return duration_sec * GITHUB_ACTIONS_LINUX_PRICE_PER_SEC


def run_sebs_invocation(
    deployment: str,
    config_path: Path,
    profile: str,
    memory_mb: int,
    repetitions: int,
    output_dir: Path,
    reuse_cache: bool = False,
    incremental: bool = False,
) -> Path:
    mode_tag = "incremental" if incremental else ("warm-cache" if reuse_cache else "clean")
    run_dir = output_dir / f"{deployment}_{profile}_{memory_mb}MB_{mode_tag}"
    run_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["SEBS_CI_PROFILE"] = profile
    env["SEBS_CI_REUSE_CACHE"] = "1" if reuse_cache else "0"
    env["SEBS_CI_INCREMENTAL"] = "1" if incremental else "0"

    size_arg = profile if profile in ("test", "small", "large") else "large"
    cmd = [
        sys.executable,
        str(REPO_ROOT / "sebs.py"),
        "benchmark",
        "invoke",
        "313.serverless-ci",
        size_arg,
        "--config",
        str(config_path),
        "--deployment",
        deployment,
        "--memory",
        str(memory_mb),
        "--repetitions",
        str(repetitions),
        "--output-dir",
        str(run_dir),
    ]
    subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, check=False)
    return run_dir / "experiments.json"


def summarize_results(results_files: List[Path], summary_csv: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for res_file in results_files:
        if not res_file.exists():
            continue
        with open(res_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        for fn_name, fn_data in data.get("invocations", {}).items():
            for inv_id, inv in fn_data.items():
                output = inv.get("output", {})
                res = output.get("result", {})
                meas = output.get("measurement", {})
                times = inv.get("times", {})
                provider_times = inv.get("provider_times", {})

                total_sec = float(meas.get("total_time", times.get("benchmark", 0))) / 1e6
                memory_mb = int(inv.get("memory", 1024))
                row = {
                    "function": fn_name,
                    "invocation_id": inv_id,
                    "profile": res.get("profile", "unknown"),
                    "project": res.get("project", "unknown"),
                    "memory_mb": memory_mb,
                    "status": res.get("status", "FAILURE"),
                    "failure_class": res.get("failure_class") or "NONE",
                    "cache_hit": res.get("cache_hit", False),
                    "incremental_change": res.get("incremental_change", False),
                    "tests_passed": res.get("tests_passed", 0),
                    "emitted_files_count": res.get("emitted_files_count", 0),
                    "build_output_sha256": res.get("build_output_sha256", ""),
                    "download_s": float(meas.get("download_time", 0)) / 1e6,
                    "extract_s": float(meas.get("extract_time", 0)) / 1e6,
                    "deps_prep_s": float(meas.get("deps_prep_time", 0)) / 1e6,
                    "typecheck_s": float(meas.get("typecheck_time", 0)) / 1e6,
                    "compile_s": float(meas.get("compile_time", 0)) / 1e6,
                    "test_s": float(meas.get("test_time", 0)) / 1e6,
                    "upload_s": float(meas.get("upload_time", 0)) / 1e6,
                    "total_s": total_sec,
                    "client_s": float(times.get("client", 0)) / 1e6,
                    "provider_exec_s": float(provider_times.get("execution", 0)) / 1e6,
                    "provider_init_s": float(provider_times.get("initialization", 0)) / 1e6,
                    "peak_rss_mb": float(meas.get("peak_rss_bytes", 0)) / (1024 * 1024),
                    "peak_tmp_mb": float(meas.get("peak_tmp_bytes", 0)) / (1024 * 1024),
                    "aws_est_cost_usd": round(compute_aws_cost_usd(memory_mb, total_sec), 6),
                    "gha_est_cost_usd": round(compute_gha_cost_usd(total_sec), 6),
                }
                rows.append(row)

    if rows:
        summary_csv.parent.mkdir(parents=True, exist_ok=True)
        with open(summary_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Run or summarize 313.serverless-ci study.")
    parser.add_argument(
        "--deployment",
        choices=["openwhisk", "aws", "local"],
        default="openwhisk",
        help="Target FaaS platform",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "configs" / "serverless-ci-openwhisk.json",
        help="SeBS experiment configuration JSON",
    )
    parser.add_argument(
        "--profiles",
        nargs="+",
        default=DEFAULT_PROFILES,
        help="CI workload profiles to evaluate",
    )
    parser.add_argument(
        "--memory-sizes",
        nargs="+",
        type=int,
        default=DEFAULT_MEMORY_MB,
        help="Memory allocations in MB",
    )
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "experiments_serverless_ci",
    )
    parser.add_argument(
        "--summarize-only",
        nargs="*",
        type=Path,
        help="Summarize existing experiments.json files without invoking",
    )
    args = parser.parse_args()

    if args.summarize_only is not None:
        files = args.summarize_only or list(args.output_dir.rglob("experiments.json"))
        rows = summarize_results(files, args.output_dir / "serverless_ci_summary.csv")
        print(f"Summarized {len(rows)} invocations into {args.output_dir / 'serverless_ci_summary.csv'}")
        return

    experiment_files: List[Path] = []
    for profile in args.profiles:
        for memory_mb in args.memory_sizes:
            experiment_files.append(
                run_sebs_invocation(
                    deployment=args.deployment,
                    config_path=args.config,
                    profile=profile,
                    memory_mb=memory_mb,
                    repetitions=args.repetitions,
                    output_dir=args.output_dir,
                    reuse_cache=False,
                    incremental=False,
                )
            )
            experiment_files.append(
                run_sebs_invocation(
                    deployment=args.deployment,
                    config_path=args.config,
                    profile=profile,
                    memory_mb=memory_mb,
                    repetitions=args.repetitions,
                    output_dir=args.output_dir,
                    reuse_cache=True,
                    incremental=False,
                )
            )
            experiment_files.append(
                run_sebs_invocation(
                    deployment=args.deployment,
                    config_path=args.config,
                    profile=profile,
                    memory_mb=memory_mb,
                    repetitions=args.repetitions,
                    output_dir=args.output_dir,
                    reuse_cache=True,
                    incremental=True,
                )
            )

    rows = summarize_results(experiment_files, args.output_dir / "serverless_ci_summary.csv")
    print(f"Completed study with {len(rows)} recorded invocations.")


if __name__ == "__main__":
    main()
