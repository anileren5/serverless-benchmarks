"""Principal component analysis on a z-scored feature matrix.

Components are computed from the singular value decomposition of the z-scored
matrix. Eigenvalues are those of the sample correlation matrix,
``S**2 / (n - 1)``. The sign of each component is fixed so the loading with
the largest magnitude is positive.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class PCAFit:
    """Scores, loadings, and variance for one standardized matrix."""

    eigenvalues: np.ndarray
    variance_ratio: np.ndarray
    loadings: np.ndarray
    scores: np.ndarray
    columns: list[str]

    @property
    def n_components(self) -> int:
        return int(self.eigenvalues.shape[0])


def fit_pca(standardized: np.ndarray, columns: list[str]) -> PCAFit:
    """Fit PCA. ``standardized`` is ``(n_benchmarks, n_features)`` and mean-centered."""
    if standardized.ndim != 2:
        raise ValueError("PCA input must be a 2-D matrix.")
    n_rows, n_cols = standardized.shape
    if n_rows < 3 or n_cols < 2:
        raise ValueError(f"PCA needs at least 3 rows and 2 columns, got {standardized.shape}.")

    _u, singular, vt = np.linalg.svd(standardized, full_matrices=False)
    eigenvalues = (singular ** 2) / (n_rows - 1)
    total = float(eigenvalues.sum())
    variance_ratio = eigenvalues / total if total > 0 else np.zeros_like(eigenvalues)
    loadings = vt.T.copy()
    scores = standardized @ loadings

    for component in range(loadings.shape[1]):
        pivot = int(np.argmax(np.abs(loadings[:, component])))
        if loadings[pivot, component] < 0:
            loadings[:, component] *= -1
            scores[:, component] *= -1

    return PCAFit(
        eigenvalues=eigenvalues,
        variance_ratio=variance_ratio,
        loadings=loadings,
        scores=scores,
        columns=list(columns),
    )


def kaiser_components(eigenvalues: np.ndarray) -> list[int]:
    """Return component indexes with eigenvalue greater than 1.

    Falls back to the first component when every eigenvalue is at most 1,
    which happens when the benchmarks differ along a single weak direction.
    """
    selected = [index for index, value in enumerate(eigenvalues) if value > 1.0]
    if selected:
        return selected
    return [0]


def variance_components(variance_ratio: np.ndarray, threshold: float = 0.80) -> list[int]:
    """Return the shortest prefix of components that reaches ``threshold`` variance."""
    cumulative = 0.0
    selected: list[int] = []
    for index, share in enumerate(variance_ratio):
        selected.append(index)
        cumulative += float(share)
        if cumulative >= threshold:
            break
    return selected or [0]
