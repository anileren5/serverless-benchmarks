#!/usr/bin/env python3
"""Figures and tables for runs of the cpu-oversubscription experiment.

Usage: scripts/cpu_oversubscription_plots.py --output <dir> <run-directory>...

Reads processed.csv and summary.csv of each run (see docs/experiments.md) and requires matplotlib.
"""

import argparse
import json
import os
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

# Lambda x86 price per GB-second, eu-north-1 (first pricing tier).
LAMBDA_GB_SECOND_USD = 0.0000166667
# Memory at which Lambda grants one full vCPU.
LAMBDA_MB_PER_VCPU = 1769


def load_runs(directories: list) -> tuple:
    processed, summaries = [], []
    for directory in directories:
        with open(os.path.join(directory, "config.json")) as f:
            config = json.load(f)
        run = {
            "run": os.path.basename(os.path.normpath(directory)),
            "platform": config["deployment"]["name"],
            "label": config["experiment"].get("label", ""),
        }
        for name, frames in (("processed.csv", processed), ("summary.csv", summaries)):
            path = os.path.join(directory, name)
            if os.path.exists(path):
                frames.append(pd.read_csv(path).assign(**run))
    summary = pd.concat(summaries, ignore_index=True)
    summary["memory"] = summary["resources"].map(memory_of)
    return pd.concat(processed, ignore_index=True), summary


def memory_of(resources: str) -> float:
    match = re.search(r"(\d+)MB$", str(resources))
    return float(match.group(1)) if match else np.nan


def resource_order(resources: pd.Series) -> list:
    unique = list(dict.fromkeys(resources))
    return sorted(unique, key=lambda r: (np.isnan(memory_of(r)), memory_of(r)))


def default_policy(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[frame["omp_wait_policy_requested"].isna()]


def title(run: str, configuration: str) -> str:
    return f"{configuration}\n{run}"


def save(fig, output: str, name: str):
    fig.tight_layout()
    for extension in ("pdf", "png"):
        fig.savefig(os.path.join(output, f"{name}.{extension}"), dpi=150)
    plt.close(fig)
    print(f"Wrote {name}")


def plot_effective_cpus(processed: pd.DataFrame, output: str):
    e1 = processed[
        (processed["configuration"] == "e1-resources")
        & (processed["phase"] == "measurement")
        & ~processed["failure"]
    ]
    if e1.empty:
        return
    fig, ax = plt.subplots(figsize=(7, 4.5))
    rng = np.random.default_rng(0)
    for model, rows in e1.groupby("cpu_model"):
        jitter = rng.uniform(-0.03, 0.03, len(rows))
        ax.scatter(
            rows["memory"] * (1 + jitter), rows["effective_cpus"], s=8, alpha=0.6, label=model
        )
    memories = np.array(sorted(e1["memory"].unique()))
    ax.plot(memories, memories / LAMBDA_MB_PER_VCPU, "k--", lw=1, label="memory / 1769 MB")
    reported = e1["os_cpu_count"].max()
    ax.axhline(reported, color="tab:red", lw=1, label=f"reported CPUs ({reported:.0f})")
    ax.set_xscale("log", base=2)
    ax.set_xticks(memories, [f"{m:.0f}" for m in memories], rotation=45)
    ax.set_xlabel("Function memory [MB]")
    ax.set_ylabel("Effective CPUs (CPU time / wall time)")
    ax.legend(fontsize=7)
    ax.set_title("AWS Lambda: reported versus effective CPUs")
    save(fig, output, "aws_effective_cpus")

    table = e1.groupby(["memory", "cpu_model"])["effective_cpus"].describe(
        percentiles=[0.05, 0.5, 0.95]
    )
    table.to_csv(os.path.join(output, "aws_effective_cpus.csv"))


def plot_speedup(summary: pd.DataFrame, output: str):
    for (run, configuration), cell in default_policy(summary).groupby(["run", "configuration"]):
        if cell["threads"].nunique() < 3:
            continue
        fig, ax = plt.subplots(figsize=(6, 4.5))
        for resources in resource_order(cell["resources"]):
            rows = cell[cell["resources"] == resources].sort_values("threads")
            single = rows.loc[rows["threads"] == 1, "wall_median"]
            if single.empty:
                continue
            ax.plot(rows["threads"], rows["speedup"], marker="o", ms=3, label=resources)
            ax.fill_between(
                rows["threads"],
                single.iloc[0] / rows["wall_p75"],
                single.iloc[0] / rows["wall_p25"],
                alpha=0.15,
            )
        ax.axhline(1, color="grey", lw=0.8)
        ax.set_xscale("log", base=2)
        ax.set_yscale("log", base=2)
        ax.set_xlabel("OpenMP threads")
        ax.set_ylabel("Speedup over one thread (median, IQR)")
        ax.legend(fontsize=7, title="resources")
        ax.set_title(title(run, configuration), fontsize=9)
        save(fig, output, f"speedup_{run}_{configuration}")


def plot_penalty_heatmaps(summary: pd.DataFrame, output: str):
    for (run, configuration), cell in default_policy(summary).groupby(["run", "configuration"]):
        if cell["threads"].nunique() < 3:
            continue
        matrix = cell.pivot_table(index="resources", columns="threads", values="penalty_vs_best")
        matrix = matrix.loc[resource_order(matrix.index)]
        fig, ax = plt.subplots(
            figsize=(1 + 0.7 * matrix.shape[1], max(2.5, 1 + 0.45 * matrix.shape[0]))
        )
        levels = np.log2(matrix.values)
        image = ax.imshow(levels, cmap="magma_r", vmin=0, aspect="auto")
        dark = 0.5 * np.nanmax(levels)
        for (i, j), value in np.ndenumerate(matrix.values):
            if not np.isnan(value):
                color = "white" if np.log2(value) > dark else "black"
                ax.text(j, i, f"{value:.1f}", ha="center", va="center", fontsize=7, color=color)
        ax.set_xticks(range(matrix.shape[1]), [f"{t:.0f}" for t in matrix.columns])
        ax.set_yticks(range(matrix.shape[0]), matrix.index)
        ax.set_xlabel("OpenMP threads")
        fig.colorbar(image, ax=ax, label="log2(time / best time)")
        ax.set_title(title(run, configuration), fontsize=9)
        save(fig, output, f"penalty_{run}_{configuration}")


def plot_aws_against_model(summary: pd.DataFrame, output: str):
    aws = default_policy(summary[summary["configuration"] == "e2-thread-sweep-small"])
    model = default_policy(summary[summary["configuration"] == "gt-aws-model"])
    if aws.empty or model.empty:
        return
    fig, ax = plt.subplots(figsize=(6.5, 4.5))
    for i, resources in enumerate(resource_order(aws["resources"])):
        color = f"C{i}"
        for frame, style, source in ((aws, "-", "AWS"), (model, "--", "local model")):
            name = resources if frame is aws else f"model-{resources}"
            rows = frame[frame["resources"] == name].sort_values("threads")
            if not rows.empty:
                ax.plot(
                    rows["threads"],
                    rows["penalty_vs_best"],
                    style,
                    color=color,
                    marker="o",
                    ms=3,
                    label=f"{resources} {source}",
                )
    ax.set_xscale("log", base=2)
    ax.set_xlabel("OpenMP threads")
    ax.set_ylabel("Time / best time")
    ax.legend(fontsize=7, ncol=2)
    ax.set_title("AWS Lambda against 2 CPUs with the measured quota (local)", fontsize=9)
    save(fig, output, "aws_against_local_model")


def plot_wait_policies(summary: pd.DataFrame, output: str):
    policies = summary.assign(policy=summary["omp_wait_policy_requested"].fillna("default"))
    for (run, configuration), cell in policies.groupby(["run", "configuration"]):
        if cell["policy"].nunique() < 2:
            continue
        cell = cell[cell["threads"].isin([2, 32]) | (cell["threads"].nunique() <= 2)]
        groups = [
            (resources, threads)
            for resources in resource_order(cell["resources"])
            for threads in sorted(cell["threads"].unique())
        ]
        names = sorted(cell["policy"].unique())
        width = 0.8 / len(names)
        fig, ax = plt.subplots(figsize=(1.5 + 0.9 * len(groups), 4))
        for k, policy in enumerate(names):
            rows = cell[cell["policy"] == policy].set_index(["resources", "threads"])
            values = [rows["wall_median"].get(group, np.nan) for group in groups]
            ax.bar(np.arange(len(groups)) + k * width, values, width, label=policy)
        ax.set_xticks(
            np.arange(len(groups)) + width * (len(names) - 1) / 2,
            [f"{r}\n{t:.0f} thr" for r, t in groups],
            fontsize=7,
        )
        ax.set_yscale("log")
        ax.set_ylabel("Kernel time [s] (median)")
        ax.legend(title="OMP_WAIT_POLICY", fontsize=7)
        ax.set_title(title(run, configuration), fontsize=9)
        save(fig, output, f"wait_policy_{run}_{configuration}")


def plot_cost(summary: pd.DataFrame, output: str):
    aws = default_policy(
        summary[(summary["platform"] == "aws") & summary["configuration"].str.startswith("e2")]
    )
    if aws.empty:
        return
    fig, ax = plt.subplots(figsize=(6, 4))
    for configuration, cell in aws.groupby("configuration"):
        cost = cell.assign(
            cost=cell["wall_median"] * cell["memory"] / 1024 * LAMBDA_GB_SECOND_USD * 1e6
        )
        by_memory = cost.groupby("memory")
        best = by_memory["cost"].min()
        single = cost[cost["threads"] == 1].set_index("memory")["cost"]
        worst = by_memory["cost"].max()
        ax.plot(best.index, best, "o-", label=f"{configuration}: best thread count")
        ax.plot(single.index, single, "s--", label=f"{configuration}: 1 thread")
        ax.plot(worst.index, worst, "^:", label=f"{configuration}: worst thread count")
    ax.set_xscale("log", base=2)
    ax.set_yscale("log")
    ax.set_xlabel("Function memory [MB]")
    ax.set_ylabel("Cost per kernel run [µUSD]")
    ax.legend(fontsize=7)
    ax.set_title("AWS Lambda: cost of one kernel run", fontsize=9)
    save(fig, output, "aws_cost")


def plot_throttling(summary: pd.DataFrame, output: str):
    local = default_policy(summary[summary["platform"] == "local"])
    for configuration, cell in local.groupby("configuration"):
        if cell["throttled_fraction_median"].notna().sum() == 0 or cell["threads"].nunique() < 3:
            continue
        fig, ax = plt.subplots(figsize=(6, 4))
        for resources in resource_order(cell["resources"]):
            rows = cell[cell["resources"] == resources].sort_values("threads")
            if rows["throttled_fraction_median"].notna().any():
                ax.plot(
                    rows["threads"],
                    rows["throttled_fraction_median"],
                    marker="o",
                    ms=3,
                    label=resources,
                )
        ax.set_xscale("log", base=2)
        ax.set_ylim(0, 1.05)
        ax.set_xlabel("OpenMP threads")
        ax.set_ylabel("Throttled CFS periods (median fraction)")
        ax.legend(fontsize=7)
        ax.set_title(f"Local: {configuration}", fontsize=9)
        save(fig, output, f"throttling_{configuration}")


def write_tables(processed: pd.DataFrame, output: str):
    invocations = processed.drop_duplicates(["run", "request_id", "invocation_begin"])
    models = (
        invocations[invocations["cpu_model"].notna()]
        .groupby(["platform", "run", "configuration", "resources", "cpu_model"])
        .size()
        .rename("invocations")
    )
    models.to_csv(os.path.join(output, "cpu_models.csv"))

    timeouts = invocations[invocations["configuration"].str.startswith("e4")]
    if not timeouts.empty:
        columns = [
            "run",
            "configuration",
            "omp_wait_policy_requested",
            "batch",
            "failure",
            "error",
            "client_time_us",
            "billed_time_ms",
            "wall_seconds",
            "threads",
        ]
        timeouts[columns].to_csv(os.path.join(output, "timeouts.csv"), index=False)

    failures = invocations.groupby(["run", "configuration"])["failure"].agg(["sum", "size"])
    failures.rename(columns={"sum": "failed", "size": "invocations"}).to_csv(
        os.path.join(output, "failures.csv")
    )
    print("Wrote tables")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", help="run directories")
    parser.add_argument("--output", required=True, help="directory for figures and tables")
    args = parser.parse_args()
    os.makedirs(args.output, exist_ok=True)

    processed, summary = load_runs(args.runs)
    plot_effective_cpus(processed, args.output)
    plot_speedup(summary, args.output)
    plot_penalty_heatmaps(summary, args.output)
    plot_aws_against_model(summary, args.output)
    plot_wait_policies(summary, args.output)
    plot_cost(summary, args.output)
    plot_throttling(summary, args.output)
    write_tables(processed, args.output)


if __name__ == "__main__":
    main()
