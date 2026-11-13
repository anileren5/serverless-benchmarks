#!/usr/bin/env python3
"""Slide figures for the "Pushing the envelope" results (TA meeting, 2026-11-13).

Usage: slide_figures.py <results-dir> <output-dir>

Reads the final runs in <results-dir>/cpu-oversubscription and <results-dir>/gang-scheduling
and writes one PNG (and PDF) per slide, sized to be placed 1:1 on a 10 x 5.625 in slide.
"""

import json
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1"
LAMBDA_MB_PER_VCPU = 1769

plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 13,
        "axes.labelsize": 13,
        "axes.labelcolor": INK2,
        "axes.edgecolor": MUTED,
        "axes.linewidth": 0.8,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "xtick.color": INK2,
        "ytick.color": INK2,
        "xtick.labelsize": 12,
        "ytick.labelsize": 12,
        "legend.frameon": False,
        "legend.fontsize": 12,
        "lines.linewidth": 2.2,
        "lines.markersize": 7,
        "savefig.facecolor": "white",
    }
)


def save(fig, output, name):
    for extension in ("png", "pdf"):
        fig.savefig(os.path.join(output, f"{name}.{extension}"), dpi=200)
    plt.close(fig)
    print(f"Wrote {name}")


def summary(results, run):
    frame = pd.read_csv(os.path.join(results, "cpu-oversubscription", run, "summary.csv"))
    return frame[~frame["cold_starts"].astype(bool)]


def effective_cpus(results, output):
    table = pd.read_csv(os.path.join(results, "figures", "aws_effective_cpus.csv"))
    fig, ax = plt.subplots(figsize=(6.2, 3.9))
    memory = np.array([128, 3008])
    ax.plot(memory, [2, 2], color=ORANGE, lw=2.2)
    ax.text(3008, 2.05, "CPUs the function sees: 2", color=INK, ha="right", va="bottom")
    ax.plot(memory, memory / LAMBDA_MB_PER_VCPU, color=MUTED, lw=1.2, ls="--", zorder=1,
            label="memory / 1769 MB")
    hosts = [
        ("Intel(R) Xeon(R) Processor @ 2.50GHz", BLUE, "measured, most hosts"),
        ("Intel(R) Xeon(R) Processor", AQUA, "measured, other hosts"),
    ]
    for model, color, label in hosts:
        rows = table[table["cpu_model"] == model].sort_values("memory")
        ax.plot(rows["memory"], rows["50%"], "o-", color=color, label=label, ms=6,
                markeredgecolor="white", markeredgewidth=1.5)
    first = table[(table["memory"] == 128) & (table["cpu_model"] == hosts[0][0])]["50%"].iloc[0]
    ax.annotate(f"{first:.2f} CPUs\nat 128 MB", xy=(128, first), xytext=(420, 0.75),
                fontsize=12, color=INK, arrowprops={"arrowstyle": "-", "color": MUTED, "lw": 1})
    ax.set_xlim(0, 3150)
    ax.set_ylim(0, 2.4)
    ax.set_xticks([128, 1024, 1769, 3008])
    ax.set_xlabel("Function memory (MB)")
    ax.set_ylabel("CPUs")
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles[1:] + handles[:1], labels[1:] + labels[:1], loc="upper left",
              bbox_to_anchor=(0.0, 0.8), fontsize=11.5)
    fig.tight_layout()
    save(fig, output, "slide_effective_cpus")


def barrier_storm(results, output):
    aws = summary(results, "20261113-111143_aws_storm").assign(platform="aws")
    local = summary(results, "20261113-113030_local_storm").assign(platform="local")
    storm = pd.concat([aws, local])
    storm["grid"] = storm["configuration"].str.extract(r"storm-g(\d+)$")[0].astype(int)
    single = storm[storm["threads"] == 1].set_index(["platform", "resources", "grid"])["wall_median"]
    storm["slowdown"] = storm.apply(
        lambda r: r["wall_median"] / single[(r["platform"], r["resources"], r["grid"])], axis=1
    )
    series = [
        ("aws", "128MB", 2, BLUE, "128 MB (0.08 CPUs)"),
        ("aws", "512MB", 2, AQUA, "512 MB (0.3 CPUs)"),
        ("aws", "1769MB", 2, ORANGE, "1769 MB (1 CPU)"),
    ]
    fig, ax = plt.subplots(figsize=(6.2, 3.9))
    for platform, resources, threads, color, label in series:
        rows = storm[
            (storm["platform"] == platform)
            & (storm["resources"] == resources)
            & (storm["threads"] == threads)
        ].sort_values("grid", ascending=False)
        ax.plot(rows["grid"] ** 2, rows["slowdown"], "o-", color=color, label=label,
                markeredgecolor="white", markeredgewidth=1.5)
        last = rows.iloc[-1]
        value = last["slowdown"]
        ax.text(last["grid"] ** 2 / 1.35, value, f"{value:.0f}x" if value >= 10 else f"{value:.1f}x",
                ha="left", va="center", color=INK, fontsize=13, fontweight="bold")
    ax.axhline(1, color=MUTED, lw=1)
    ax.set_xscale("log", base=2)
    ax.set_yscale("log")
    grids = [2048, 1024, 512, 256, 128]
    ax.set_xticks([g * g for g in grids])
    ax.set_xticklabels([f"{g}²" for g in grids])
    ax.set_xlim(2048**2 * 1.6, 128**2 / 3.2)
    ax.set_yticks([1, 2, 5, 10, 20, 50])
    ax.set_yticklabels(["1x", "2x", "5x", "10x", "20x", "50x"])
    ax.minorticks_off()
    ax.set_xlabel("Grid points per barrier (same total work)  →  more barriers")
    ax.set_ylabel("Time vs 1 thread")
    ax.legend(loc="upper left", title="Lambda, 2 threads vs 1", title_fontsize=12,
              alignment="left")
    fig.tight_layout()
    save(fig, output, "slide_barrier_storm")


def onnx(results, output):
    frame = summary(results, "20261113-103204_aws_onnx")
    frame = frame.assign(policy=frame["omp_wait_policy_requested"].fillna("spinning"))
    fig, ax = plt.subplots(figsize=(6.2, 3.9))
    for policy, color, label in (
        ("spinning", ORANGE, "spinning workers (default)"),
        ("passive", BLUE, "allow_spinning = 0"),
    ):
        rows = frame[frame["policy"] == policy]
        stats = rows.groupby("threads")["penalty_vs_best"].agg(["min", "median", "max"])
        ax.fill_between(stats.index, stats["min"], stats["max"], color=color, alpha=0.15, lw=0)
        ax.plot(stats.index, stats["median"], "o-", color=color, label=label,
                markeredgecolor="white", markeredgewidth=1.5)
        ax.text(8.3, stats["median"].iloc[-1], f"{stats['median'].iloc[-1]:.1f}x",
                va="center", color=INK, fontsize=13, fontweight="bold")
    ax.axhline(1, color=MUTED, lw=1)
    ax.axvline(2, color=MUTED, lw=1, ls=":")
    ax.text(2.1, 6.4, "ONNX Runtime's\ndefault on Lambda", color=INK2, fontsize=11, va="top")
    ax.set_xticks([1, 2, 3, 4, 8])
    ax.set_xlim(0.7, 9.2)
    ax.set_ylim(0.7, 6.8)
    ax.set_xlabel("Intra-op threads")
    ax.set_ylabel("Inference time vs best")
    ax.legend(loc="lower right", bbox_to_anchor=(1.0, 0.13))
    fig.tight_layout()
    save(fig, output, "slide_onnx")


def gang(results, output):
    runs = [
        ("20261113-120703_aws", "Lambda\n1024 MB", 10, "cap 10"),
        ("20261113-120711_openwhisk_1024MB", "OpenWhisk\n1024 MB", 8, "cap 8"),
        ("20261113-121519_openwhisk_2048MB", "OpenWhisk\n2048 MB", 4, "cap 4"),
    ]
    fig, (outcome, timeline) = plt.subplots(
        1, 2, figsize=(9.2, 3.7), gridspec_kw={"width_ratios": [1.15, 1]}
    )
    for row, (run, label, cap, cap_label) in enumerate(runs):
        frame = pd.read_csv(os.path.join(results, "gang-scheduling", run, "summary.csv"))
        cells = frame.groupby("world_size")["run_completed"].mean()
        y = len(runs) - 1 - row
        ok = cells[cells == 1].index
        bad = cells[cells < 1].index
        outcome.scatter(ok, [y] * len(ok), s=90, color=BLUE, zorder=3,
                        edgecolor="white", linewidth=1.5)
        outcome.scatter(bad, [y] * len(bad), s=90, color=ORANGE, marker="X", zorder=3,
                        edgecolor="white", linewidth=1)
        outcome.plot([cap + 0.5] * 2, [y - 0.38, y + 0.38], color=INK, lw=1.5)
        outcome.text(cap + 0.7, y + 0.27, cap_label, fontsize=10.5, color=INK2, va="center")
    outcome.scatter([], [], s=90, color=BLUE, label="all 3 runs completed")
    outcome.scatter([], [], s=90, color=ORANGE, marker="X", label="all 3 runs deadlocked")
    outcome.set_yticks(range(len(runs)))
    outcome.set_yticklabels([r[1] for r in reversed(runs)])
    outcome.set_ylim(-0.6, len(runs) - 0.3)
    outcome.set_xlim(1, 17)
    outcome.set_xticks([2, 4, 6, 8, 10, 12, 14, 16])
    outcome.set_xlabel("Ranks launched together (N)")
    outcome.grid(axis="y", visible=False)
    outcome.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=2, fontsize=11,
                   handletextpad=0.2, columnspacing=1.0)

    # One deadlocked launch per platform: when each rank ran.
    examples = [("20261113-120703_aws", 12, "Lambda, N = 12"),
                ("20261113-120711_openwhisk_1024MB", 10, "OpenWhisk, N = 10")]
    y = 0
    ticks = []
    for run, world_size, label in examples:
        with open(os.path.join(results, "gang-scheduling", run, "raw.jsonl")) as f:
            records = [json.loads(line) for line in f]
        ranks = [r for r in records if r["world_size"] == world_size and r["repetition"] == 0]
        ranks.sort(key=lambda r: (r["rank_result"] or {}).get("begin", 0) if r.get("rank_result") else -1)
        start = y
        for r in ranks:
            result = r.get("rank_result")
            if result:
                begin, end = result["begin"] - r["launch"], result["end"] - r["launch"]
                late = begin > 1
                timeline.barh(y, end - begin, left=begin, height=0.7,
                              color=ORANGE if late else BLUE, lw=0)
            else:
                timeline.scatter(0.6, y, marker="X", s=55, color=ORANGE, zorder=3)
            y += 1
        ticks.append(((start + y - 1) / 2, label))
        y += 1.2
    timeline.text(2.8, 0.5, "rejected: TooManyRequests", fontsize=10.5, color=INK2, va="center")
    timeline.text(31.5, 6.5, "wait for peers\nuntil 30 s timeout", fontsize=10.5,
                  color=INK2, va="center")
    timeline.text(21.5, 16.0, "wait, give up\nafter 20 s", fontsize=10.5, color=INK2,
                  va="center")
    timeline.text(41.5, y - 2.5, "queued, run\nafter the others", fontsize=10.5,
                  color=INK2, va="center")
    timeline.set_yticks([t[0] for t in ticks])
    timeline.set_yticklabels([t[1].replace(", ", "\n") for t in ticks])
    timeline.invert_yaxis()
    timeline.set_xlim(0, 58)
    timeline.set_xticks([0, 10, 20, 30, 40])
    timeline.set_xlabel("Seconds since launch (bar = rank)")
    timeline.grid(axis="y", visible=False)
    timeline.tick_params(axis="y", length=0)
    fig.tight_layout(w_pad=2.5)
    save(fig, output, "slide_gang_scheduling")


def main():
    results, output = sys.argv[1], sys.argv[2]
    os.makedirs(output, exist_ok=True)
    effective_cpus(results, output)
    barrier_storm(results, output)
    onnx(results, output)
    gang(results, output)


if __name__ == "__main__":
    main()
