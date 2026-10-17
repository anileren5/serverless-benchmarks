"""Clustering, redundancy pairs, and empty regions in PCA space.

K-means uses a spherical Gaussian BIC (Pelleg and Moore, 2000) to choose
``k``. Hierarchical clustering is average linkage with Euclidean distance,
matching the distance view used alongside k-means in the SPEC CPU2006 study.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage


@dataclass
class ClusterModel:
    """K-means result for one value of ``k``."""

    k: int
    labels: np.ndarray
    centers: np.ndarray
    inertia: float
    bic: float


@dataclass
class Redundancy:
    """Nearest neighbors and pairs that pass the redundancy rule."""

    nearest: list[dict]
    pairs: list[dict]
    distance_quantile: float
    threshold: float
    note: str


@dataclass
class EmptyCell:
    """One interior grid cell that contains no benchmark."""

    row: int
    col: int
    pc1_center: float
    pc2_center: float
    pc1_side: str
    pc2_side: str
    pc1_features: list[str]
    pc2_features: list[str]


@dataclass
class ClusterResult:
    """Everything the report needs from the clustering stage."""

    models: list[ClusterModel]
    selected: ClusterModel
    hierarchical_labels: np.ndarray
    rand_index: float
    sensitivity_labels: np.ndarray | None
    sensitivity_rand: float | None
    redundancy: Redundancy
    empty_cells: list[EmptyCell] = field(default_factory=list)


def _kmeans_pp(data: np.ndarray, k: int, rng: np.random.Generator) -> np.ndarray:
    """Choose ``k`` initial centers with k-means++."""
    n_rows = data.shape[0]
    first = int(rng.integers(0, n_rows))
    centers = [data[first]]
    for _ in range(1, k):
        current = np.vstack(centers)
        distances = ((data[:, None, :] - current[None, :, :]) ** 2).sum(axis=2)
        nearest = distances.min(axis=1)
        total = float(nearest.sum())
        if total <= 0:
            centers.append(data[int(rng.integers(0, n_rows))])
            continue
        probs = nearest / total
        centers.append(data[int(rng.choice(n_rows, p=probs))])
    return np.vstack(centers)


def kmeans(
    data: np.ndarray, k: int, seed: int, n_init: int = 10, max_iter: int = 100
) -> tuple[np.ndarray, np.ndarray, float]:
    """Run k-means with several k-means++ initializations. Returns the lowest inertia."""
    n_rows = data.shape[0]
    if k < 1 or k > n_rows:
        raise ValueError(f"k must be between 1 and {n_rows}, got {k}.")

    best: tuple[np.ndarray, np.ndarray, float] | None = None
    for init in range(n_init):
        rng = np.random.default_rng(seed + init)
        centers = _kmeans_pp(data, k, rng)
        labels = np.zeros(n_rows, dtype=int)
        for _ in range(max_iter):
            distances = ((data[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
            labels = distances.argmin(axis=1).astype(int)
            updated = centers.copy()
            for cluster in range(k):
                members = data[labels == cluster]
                if len(members) == 0:
                    updated[cluster] = data[int(rng.integers(0, n_rows))]
                else:
                    updated[cluster] = members.mean(axis=0)
            if np.allclose(updated, centers):
                centers = updated
                break
            centers = updated
        inertia = float(((data - centers[labels]) ** 2).sum())
        if best is None or inertia < best[2]:
            best = (labels.copy(), centers.copy(), inertia)
    assert best is not None
    return best


def bic_spherical(data: np.ndarray, labels: np.ndarray, centers: np.ndarray) -> float:
    """BIC for a spherical Gaussian mixture with one shared variance.

    Parameters are the ``k * d`` center coordinates and one variance. The
    log-likelihood uses the maximum-likelihood variance ``RSS / (n * d)``,
    floored at 0.1% of the total variance.
    """
    n_rows, n_dims = data.shape
    k = int(centers.shape[0])
    if len(np.unique(labels)) < k:
        return float("-inf")
    rss = float(((data - centers[labels]) ** 2).sum())
    total = float(((data - data.mean(axis=0)) ** 2).sum())
    # A near-zero residual sends the Gaussian likelihood to infinity and BIC
    # then prefers one cluster per benchmark. Floor the variance at 0.1% of
    # the total variance. Models that have already explained that much share
    # a likelihood, and the parameter penalty keeps the smaller k.
    variance = rss / (n_rows * n_dims)
    floor = 1e-3 * max(total, 1e-12) / (n_rows * n_dims)
    variance = max(variance, floor)
    loglik = -0.5 * n_rows * n_dims * np.log(2.0 * np.pi * variance) - 0.5 * n_rows * n_dims
    n_params = k * n_dims + 1
    return float(loglik - 0.5 * n_params * np.log(n_rows))


def select_k(data: np.ndarray, seed: int, k_max: int | None = None) -> list[ClusterModel]:
    """Fit k-means for ``k = 1 .. k_max`` and score each model with BIC."""
    n_rows = data.shape[0]
    limit = k_max if k_max is not None else min(6, n_rows - 1)
    limit = max(1, min(limit, n_rows - 1))
    models: list[ClusterModel] = []
    for k in range(1, limit + 1):
        labels, centers, inertia = kmeans(data, k, seed)
        models.append(
            ClusterModel(
                k=k,
                labels=labels,
                centers=centers,
                inertia=inertia,
                bic=bic_spherical(data, labels, centers),
            )
        )
    return models


def hierarchical_labels(data: np.ndarray, k: int) -> np.ndarray:
    """Average-linkage clusters, cut into ``k`` groups. Labels start at 0."""
    if k <= 1 or data.shape[0] < 2:
        return np.zeros(data.shape[0], dtype=int)
    linked = linkage(data, method="average", metric="euclidean")
    raw = fcluster(linked, t=k, criterion="maxclust")
    return raw - 1


def pair_rand(left: np.ndarray, right: np.ndarray) -> float:
    """Rand index: fraction of benchmark pairs that agree on same-cluster membership."""
    n_rows = len(left)
    if n_rows < 2:
        return 1.0
    agree = 0
    total = 0
    for i in range(n_rows):
        for j in range(i + 1, n_rows):
            total += 1
            same_left = bool(left[i] == left[j])
            same_right = bool(right[i] == right[j])
            if same_left == same_right:
                agree += 1
    return agree / total


def pairwise_distances(data: np.ndarray) -> np.ndarray:
    """Euclidean distance matrix."""
    diff = data[:, None, :] - data[None, :, :]
    return np.sqrt((diff ** 2).sum(axis=2))


def redundancy_report(
    data: np.ndarray, labels: np.ndarray, names: list[str], k: int
) -> Redundancy:
    """Nearest neighbor of every benchmark, and redundant pairs when ``k`` >= 2."""
    distances = pairwise_distances(data)
    np.fill_diagonal(distances, np.inf)
    order = np.argsort(names)
    nearest: list[dict] = []
    for index in order:
        neighbor = int(np.argmin(distances[index]))
        nearest.append(
            {
                "benchmark": names[index],
                "neighbor": names[neighbor],
                "distance": float(distances[index, neighbor]),
                "same_cluster": bool(labels[index] == labels[neighbor]),
            }
        )

    upper = distances[np.triu_indices(len(names), k=1)]
    threshold = float(np.quantile(upper, 0.25)) if len(upper) else 0.0
    pairs: list[dict] = []
    note = (
        "BIC selected one cluster, so no subset is marked redundant. "
        "Nearest neighbors are still listed."
    )
    if k >= 2:
        note = (
            "A pair is redundant when both benchmarks share a k-means cluster and "
            "their distance is at or below the 25th percentile of pairwise distances "
            f"({threshold:.4f})."
        )
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                if labels[i] != labels[j]:
                    continue
                distance = float(distances[i, j])
                if distance <= threshold:
                    left, right = sorted((names[i], names[j]))
                    pairs.append({"a": left, "b": right, "distance": distance})
        pairs.sort(key=lambda item: (item["distance"], item["a"], item["b"]))
    return Redundancy(
        nearest=nearest,
        pairs=pairs,
        distance_quantile=0.25,
        threshold=threshold,
        note=note,
    )


def _side_features(loadings: np.ndarray, columns: list[str], component: int, sign: float) -> list[str]:
    """Features that pull a component toward ``sign``, strongest first."""
    weighted = loadings[:, component] * sign
    ranked = np.argsort(-weighted)
    return [columns[index] for index in ranked if weighted[index] > 0][:3]


def empty_regions(
    scores: np.ndarray,
    loadings: np.ndarray,
    columns: list[str],
    grid: int = 4,
) -> list[EmptyCell]:
    """Interior cells of a grid on PC1–PC2 that contain no benchmark.

    Border cells are the outside of the sample, so they are not reported as
    holes in the suite. A degenerate axis (every benchmark shares a coordinate)
    yields no cells.
    """
    if scores.shape[0] == 0 or scores.shape[1] < 2 or grid < 3:
        return []
    x = scores[:, 0]
    y = scores[:, 1]
    if np.allclose(x.min(), x.max()) or np.allclose(y.min(), y.max()):
        return []

    x_edges = np.linspace(float(x.min()), float(x.max()), grid + 1)
    y_edges = np.linspace(float(y.min()), float(y.max()), grid + 1)
    x_edges[-1] = np.nextafter(x_edges[-1], np.inf)
    y_edges[-1] = np.nextafter(y_edges[-1], np.inf)
    x_bin = np.clip(np.searchsorted(x_edges, x, side="right") - 1, 0, grid - 1)
    y_bin = np.clip(np.searchsorted(y_edges, y, side="right") - 1, 0, grid - 1)
    occupied = np.zeros((grid, grid), dtype=bool)
    for i, j in zip(x_bin, y_bin):
        occupied[int(i), int(j)] = True

    cells: list[EmptyCell] = []
    for i in range(1, grid - 1):
        for j in range(1, grid - 1):
            if occupied[i, j]:
                continue
            cx = 0.5 * (float(x_edges[i]) + float(x_edges[i + 1]))
            cy = 0.5 * (float(y_edges[j]) + float(y_edges[j + 1]))
            pc1_sign = 1.0 if cx >= 0 else -1.0
            pc2_sign = 1.0 if cy >= 0 else -1.0
            cells.append(
                EmptyCell(
                    row=i,
                    col=j,
                    pc1_center=cx,
                    pc2_center=cy,
                    pc1_side="positive" if pc1_sign > 0 else "negative",
                    pc2_side="positive" if pc2_sign > 0 else "negative",
                    pc1_features=_side_features(loadings, columns, 0, pc1_sign),
                    pc2_features=_side_features(loadings, columns, 1, pc2_sign),
                )
            )
    return cells


def cluster_space(
    primary_scores: np.ndarray,
    names: list[str],
    seed: int,
    sensitivity_scores: np.ndarray | None = None,
) -> ClusterResult:
    """Choose ``k`` on the primary scores and compare with hierarchical clustering."""
    models = select_k(primary_scores, seed)
    selected = max(models, key=lambda model: (model.bic, -model.k))
    hier = hierarchical_labels(primary_scores, selected.k)
    rand = pair_rand(selected.labels, hier)
    sensitivity_labels = None
    sensitivity_rand = None
    if sensitivity_scores is not None and sensitivity_scores.shape[1] >= 1:
        # Same k, so the Rand index compares partitions rather than two BIC choices.
        sensitivity_labels, _, _ = kmeans(sensitivity_scores, selected.k, seed)
        sensitivity_rand = pair_rand(selected.labels, sensitivity_labels)
    return ClusterResult(
        models=models,
        selected=selected,
        hierarchical_labels=hier,
        rand_index=rand,
        sensitivity_labels=sensitivity_labels,
        sensitivity_rand=sensitivity_rand,
        redundancy=redundancy_report(primary_scores, selected.labels, names, selected.k),
    )
