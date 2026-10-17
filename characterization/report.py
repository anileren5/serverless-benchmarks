"""Assemble the PCA, clustering, and markdown report for one feature table."""

from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from characterization.cluster import ClusterResult, cluster_space, empty_regions, pairwise_distances
from characterization.features import PreparedMatrix, prepare_matrix
from characterization.pca import PCAFit, fit_pca, kaiser_components, variance_components


@dataclass
class Analysis:
    """One finished characterization run."""

    matrix: PreparedMatrix
    pca: PCAFit
    primary: list[int]
    variance_cutoff: list[int]
    clusters: ClusterResult
    origin: str
    feature_set: str
    seed: int
    log_size: bool


def analyze_frame(
    frame: pd.DataFrame,
    columns: tuple[str, ...] | list[str],
    seed: int = 0,
    log_size: bool = False,
    variance_threshold: float = 0.80,
) -> Analysis:
    """Z-score ``frame``, fit PCA, and cluster the retained components."""
    matrix = prepare_matrix(frame, columns, log_size=log_size)
    pca = fit_pca(matrix.standardized, matrix.columns)
    primary = kaiser_components(pca.eigenvalues)
    cutoff = variance_components(pca.variance_ratio, variance_threshold)
    primary_scores = pca.scores[:, primary]
    sensitivity = pca.scores[:, cutoff]
    same_space = primary == cutoff
    clusters = cluster_space(
        primary_scores,
        matrix.labels,
        seed,
        None if same_space else sensitivity,
    )
    if pca.scores.shape[1] >= 2:
        clusters.empty_cells = empty_regions(pca.scores[:, :2], pca.loadings, pca.columns)
    return Analysis(
        matrix=matrix,
        pca=pca,
        primary=primary,
        variance_cutoff=cutoff,
        clusters=clusters,
        origin="measured",
        feature_set="",
        seed=seed,
        log_size=log_size,
    )


def _git_revision(cwd: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=cwd, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _versions() -> dict[str, str]:
    import scipy

    versions = {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "scipy": scipy.__version__,
    }
    try:
        import matplotlib

        versions["matplotlib"] = matplotlib.__version__
    except ImportError:
        versions["matplotlib"] = "not installed"
    return versions


def _cluster_members(labels: np.ndarray, names: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for label, name in zip(labels, names):
        groups.setdefault(str(int(label)), []).append(name)
    # Order clusters by their first sorted member so ids do not depend on k-means init order.
    ordered = {}
    for index, (_label, members) in enumerate(
        sorted(groups.items(), key=lambda item: sorted(item[1])[0])
    ):
        ordered[str(index)] = sorted(members)
    return ordered


def _markdown(analysis: Analysis, provenance: dict) -> str:
    pca = analysis.pca
    clusters = analysis.clusters
    lines = [
        "# SeBS characterization report",
        "",
        f"Feature set: `{analysis.feature_set}`. Origin: `{analysis.origin}`. Seed: `{analysis.seed}`.",
        "",
    ]
    if analysis.origin != "measured":
        lines.extend(
            [
                "This table is synthetic. It checks the pipeline. It is not a result about SeBS.",
                "",
            ]
        )
    lines.extend(["## Provenance", ""])
    for key, value in provenance.items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Preparation", ""])
    for note in analysis.matrix.notes:
        lines.append(f"- {note}")
    if not analysis.matrix.notes:
        lines.append("- Every requested column that was present survived filtering.")
    lines.extend(
        [
            "",
            "## Principal components",
            "",
            "Components with eigenvalue greater than 1 are the clustering space "
            "(Kaiser). The 80% variance prefix is the sensitivity check.",
            "",
            "| Component | Eigenvalue | Variance | Cumulative |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    cumulative = 0.0
    for index, (eigenvalue, share) in enumerate(zip(pca.eigenvalues, pca.variance_ratio)):
        cumulative += float(share)
        marker = " retained" if index in analysis.primary else ""
        lines.append(
            f"| PC{index + 1}{marker} | {eigenvalue:.3f} | {100 * share:.1f}% | {100 * cumulative:.1f}% |"
        )
    lines.extend(["", "### Loadings", ""])
    shown = analysis.primary[: min(4, len(analysis.primary))] or [0]
    header = "| Feature | " + " | ".join(f"PC{index + 1}" for index in shown) + " |"
    rule = "| --- | " + " | ".join("---:" for _ in shown) + " |"
    lines.extend([header, rule])
    for row, name in enumerate(pca.columns):
        cells = " | ".join(f"{pca.loadings[row, index]:.3f}" for index in shown)
        lines.append(f"| {name} | {cells} |")

    lines.extend(["", "## Clusters", ""])
    lines.append("| k | BIC | Inertia |")
    lines.append("| ---: | ---: | ---: |")
    for model in clusters.models:
        chosen = " selected" if model.k == clusters.selected.k else ""
        lines.append(f"| {model.k}{chosen} | {model.bic:.2f} | {model.inertia:.3f} |")
    lines.extend(["", "Membership (k-means, labels ordered by benchmark name):", ""])
    for label, members in _cluster_members(clusters.selected.labels, analysis.matrix.labels).items():
        lines.append(f"- Cluster {label}: {', '.join(members)}")
    lines.extend(
        [
            "",
            f"Rand index between k-means and average-linkage at the same k: {clusters.rand_index:.3f}.",
        ]
    )
    if clusters.sensitivity_rand is not None:
        lines.append(
            "Rand index between the Kaiser space and the 80% variance space, "
            f"same k: {clusters.sensitivity_rand:.3f}."
        )
    else:
        lines.append("The Kaiser cutoff and the 80% variance cutoff selected the same components.")

    lines.extend(["", "## Redundancy", "", clusters.redundancy.note, ""])
    lines.append("| Benchmark | Nearest neighbor | Distance | Same cluster |")
    lines.append("| --- | --- | ---: | --- |")
    for item in clusters.redundancy.nearest:
        lines.append(
            f"| {item['benchmark']} | {item['neighbor']} | {item['distance']:.3f} | {item['same_cluster']} |"
        )
    if clusters.redundancy.pairs:
        lines.extend(["", "Pairs under the redundancy rule:", ""])
        for pair in clusters.redundancy.pairs:
            lines.append(f"- {pair['a']} and {pair['b']} (distance {pair['distance']:.3f})")
    else:
        lines.extend(["", "No pair passed the redundancy rule.", ""])

    lines.extend(["", "## Empty regions on PC1–PC2", ""])
    if not clusters.empty_cells:
        lines.append(
            "No interior empty cell on the 4-by-4 grid, or the first two components "
            "do not both vary across the suite."
        )
    else:
        lines.append(
            "Each cell is an interior grid cell with no benchmark. The features are the "
            "strongest loadings on that side of the component."
        )
        lines.append("")
        for cell in clusters.empty_cells:
            pc1 = ", ".join(cell.pc1_features) or "no positive loading on this side"
            pc2 = ", ".join(cell.pc2_features) or "no positive loading on this side"
            lines.append(
                f"- Grid ({cell.row}, {cell.col}) at PC1={cell.pc1_center:.2f} ({cell.pc1_side}: {pc1}), "
                f"PC2={cell.pc2_center:.2f} ({cell.pc2_side}: {pc2})."
            )
    lines.append("")
    return "\n".join(lines)


def _write_plots(analysis: Analysis, directory: Path) -> str | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return "matplotlib is not installed; figures were not written."

    pca = analysis.pca
    scores = pca.scores
    figure, axis = plt.subplots(figsize=(7, 5))
    axis.scatter(scores[:, 0], scores[:, 1] if scores.shape[1] > 1 else np.zeros(len(scores)))
    y_values = scores[:, 1] if scores.shape[1] > 1 else np.zeros(len(scores))
    for label, x_value, y_value in zip(analysis.matrix.labels, scores[:, 0], y_values):
        axis.annotate(label, (x_value, y_value), fontsize=7, xytext=(4, 4), textcoords="offset points")
    axis.set_xlabel("PC1")
    axis.set_ylabel("PC2" if scores.shape[1] > 1 else "PC2 unavailable")
    axis.set_title(f"SeBS {analysis.feature_set} PCA")
    figure.tight_layout()
    figure.savefig(directory / "pca_scatter.png", dpi=150)
    figure.savefig(directory / "pca_scatter.pdf")
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(6, 4))
    ranks = np.arange(1, len(pca.variance_ratio) + 1)
    axis.bar(ranks, pca.variance_ratio * 100)
    axis.set_xlabel("Component")
    axis.set_ylabel("Variance explained (%)")
    axis.set_title("Scree")
    figure.tight_layout()
    figure.savefig(directory / "scree.png", dpi=150)
    plt.close(figure)
    return None


def write_report(analysis: Analysis, directory: Path, repo_root: Path | None = None) -> None:
    """Write JSON, CSV, markdown, and plots for one analysis."""
    directory.mkdir(parents=True, exist_ok=True)
    root = repo_root or Path(__file__).resolve().parents[1]
    provenance = {
        "git_revision": _git_revision(root),
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seed": analysis.seed,
        "feature_set": analysis.feature_set,
        "log_size": analysis.log_size,
        "origin": analysis.origin,
        "kaiser_components": [index + 1 for index in analysis.primary],
        "variance_80_components": [index + 1 for index in analysis.variance_cutoff],
        **{f"version_{name}": version for name, version in _versions().items()},
    }
    payload = {
        "provenance": provenance,
        "notes": analysis.matrix.notes,
        "columns": analysis.matrix.columns,
        "benchmarks": analysis.matrix.labels,
        "eigenvalues": analysis.pca.eigenvalues.tolist(),
        "variance_ratio": analysis.pca.variance_ratio.tolist(),
        "kaiser_components": analysis.primary,
        "variance_80_components": analysis.variance_cutoff,
        "bic": [{"k": model.k, "bic": model.bic, "inertia": model.inertia} for model in analysis.clusters.models],
        "selected_k": analysis.clusters.selected.k,
        "clusters": _cluster_members(analysis.clusters.selected.labels, analysis.matrix.labels),
        "hierarchical_rand": analysis.clusters.rand_index,
        "sensitivity_rand": analysis.clusters.sensitivity_rand,
        "redundancy": {
            "note": analysis.clusters.redundancy.note,
            "threshold": analysis.clusters.redundancy.threshold,
            "nearest": analysis.clusters.redundancy.nearest,
            "pairs": analysis.clusters.redundancy.pairs,
        },
        "empty_regions": [asdict(cell) for cell in analysis.clusters.empty_cells],
    }
    (directory / "report.json").write_text(json.dumps(payload, indent=2) + "\n")
    (directory / "report.md").write_text(_markdown(analysis, provenance))
    analysis.matrix.raw.to_csv(directory / "features_used.csv")
    pd.DataFrame(
        analysis.pca.scores,
        index=analysis.matrix.labels,
        columns=[f"PC{index + 1}" for index in range(analysis.pca.n_components)],
    ).to_csv(directory / "scores.csv")
    pd.DataFrame(
        analysis.pca.loadings,
        index=analysis.matrix.columns,
        columns=[f"PC{index + 1}" for index in range(analysis.pca.n_components)],
    ).to_csv(directory / "loadings.csv")
    distances = pairwise_distances(analysis.pca.scores[:, analysis.primary])
    pd.DataFrame(distances, index=analysis.matrix.labels, columns=analysis.matrix.labels).to_csv(
        directory / "distances.csv"
    )
    plot_note = _write_plots(analysis, directory)
    if plot_note:
        (directory / "plots.txt").write_text(plot_note + "\n")
