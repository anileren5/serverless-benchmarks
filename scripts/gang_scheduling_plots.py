#!/usr/bin/env python3
"""Figures for runs of the gang-scheduling experiment.

Usage: scripts/gang_scheduling_plots.py --output <dir> <run-directory>...

The predicted concurrency cap is the AWS account's concurrent executions (--aws-concurrency)
or, on OpenWhisk, the invoker's action memory divided by the action memory (--openwhisk-memory;
tools/openwhisk_standalone.sh reserves 8192 MB for actions).
"""

import argparse
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402


def load(directories: list) -> pd.DataFrame:
    frames = []
    for directory in directories:
        with open(os.path.join(directory, "config.json")) as f:
            config = json.load(f)
        summary = pd.read_csv(os.path.join(directory, "summary.csv"))
        summary["platform"] = config["deployment"]["name"]
        summary["memory"] = config["experiment"]["memory"]
        frames.append(summary)
    return pd.concat(frames, ignore_index=True)


def predicted_cap(platform: str, memory: int, args) -> int:
    if platform == "aws":
        return args.aws_concurrency
    return args.openwhisk_memory // memory


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--output", required=True)
    parser.add_argument("--aws-concurrency", type=int, default=10)
    parser.add_argument("--openwhisk-memory", type=int, default=8192)
    args = parser.parse_args()
    os.makedirs(args.output, exist_ok=True)
    data = load(args.runs)
    cells = data.groupby(["platform", "memory", "world_size"])

    fig, (completion, waiting) = plt.subplots(1, 2, figsize=(10, 4))
    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    for color, ((platform, memory), _) in zip(colors, data.groupby(["platform", "memory"])):
        label = f"{platform}, {memory} MB"
        rows = cells.agg(
            completed=("run_completed", "mean"),
            wait=("wait_fraction_median", "median"),
            makespan=("makespan_seconds", "median"),
        ).loc[(platform, memory)]
        cap = predicted_cap(platform, memory, args)
        completion.plot(rows.index, rows["completed"], "o-", color=color, label=label)
        completion.axvline(cap + 0.5, color=color, ls=":", lw=1)
        done = rows[rows["completed"] == 1]
        waiting.plot(done.index, done["wait"], "o-", color=color, label=label)
    completion.set_xlabel("Ranks launched together (N)")
    completion.set_ylabel("Fraction of runs that completed")
    completion.set_title("Runs complete only up to the concurrency cap (dotted)", fontsize=9)
    completion.legend(fontsize=7)
    waiting.set_xlabel("Ranks (N), completed runs")
    waiting.set_ylabel("Median fraction of rank time at barriers")
    waiting.set_ylim(0, 1)
    waiting.set_title("All-reduce through object storage", fontsize=9)
    waiting.legend(fontsize=7)
    fig.tight_layout()
    for extension in ("pdf", "png"):
        fig.savefig(os.path.join(args.output, f"gang_scheduling.{extension}"), dpi=150)
    print("Wrote gang_scheduling")

    table = cells.agg(
        launches=("run_completed", "size"),
        runs_completed=("run_completed", "sum"),
        completed_ranks_mean=("completed_ranks", "mean"),
        throttled_ranks_mean=("invocation_errors", "mean"),
        start_skew_median=("start_skew_seconds", "median"),
        wait_fraction_median=("wait_fraction_median", "median"),
        makespan_median=("makespan_seconds", "median"),
    ).reset_index()
    table["predicted_cap"] = [
        predicted_cap(p, m, args) for p, m in zip(table["platform"], table["memory"])
    ]
    table.round(3).to_csv(os.path.join(args.output, "gang_scheduling.csv"), index=False)
    print("Wrote gang_scheduling.csv")


if __name__ == "__main__":
    main()
