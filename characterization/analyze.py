"""Run the PCA redundancy analysis on a feature table.

Example::

    python -m characterization.analyze \\
        --features characterization/results/features.csv \\
        --feature-set workload \\
        --out characterization/results/workload
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from characterization.features import columns_for, subset_frame
from characterization.report import analyze_frame, write_report


def load_table(path: Path) -> pd.DataFrame:
    """Read the canonical feature CSV."""
    frame = pd.read_csv(path)
    if "benchmark" not in frame.columns:
        raise ValueError(f"{path} has no benchmark column.")
    return frame


def infer_origin(frame: pd.DataFrame, requested: str) -> str:
    """A synthetic row forces the report to say so, even if the CLI says measured."""
    if "origin" in frame.columns and (frame["origin"].astype(str) == "synthetic").any():
        return "synthetic"
    return requested


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="PCA redundancy analysis for a SeBS feature table.")
    parser.add_argument("--features", required=True, type=Path, help="Feature table CSV.")
    parser.add_argument(
        "--feature-set",
        default="workload",
        choices=["workload", "deployment"],
        help="Which column group to analyze.",
    )
    parser.add_argument("--platform", default=None, help="Keep one platform. Required for a mixed deployment PCA.")
    parser.add_argument("--out", required=True, type=Path, help="Directory for the report.")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--log-size",
        action="store_true",
        help="Apply log1p to size-like columns before z-scoring (sensitivity run).",
    )
    parser.add_argument("--origin", default="measured", choices=["measured", "synthetic"])
    args = parser.parse_args(argv)

    frame = load_table(args.features)
    frame, notes = subset_frame(frame, args.feature_set, args.platform)
    analysis = analyze_frame(
        frame,
        columns_for(args.feature_set),
        seed=args.seed,
        log_size=args.log_size,
    )
    analysis.matrix.notes = notes + analysis.matrix.notes
    analysis.feature_set = args.feature_set
    analysis.origin = infer_origin(frame, args.origin)
    analysis.seed = args.seed
    analysis.log_size = args.log_size
    write_report(analysis, args.out)
    print(f"Wrote {args.out / 'report.md'}")
    print(f"Selected k={analysis.clusters.selected.k} on components {[i + 1 for i in analysis.primary]}")


if __name__ == "__main__":
    main()
