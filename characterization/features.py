"""Feature sets for the workload PCA and the deployment PCA.

Absolute wall-clock time is stored on the feature table and left out of the
workload PCA. It measures the host, not the shape of the program. Rates,
counter ratios, resident memory, I/O volume, and package size are the
workload features. Provider initialization and client latency are deployment
features and are analyzed per platform.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# Ratios from one PAPI event-set run each. See ROADMAP.md.
WORKLOAD_FEATURES = (
    "ipc",
    "load_store_frac",
    "branch_frac",
    "l1d_miss_per_ins",
    "l2_miss_per_ins",
    "l3_miss_per_ins",
    "stall_frac",
    "sp_flop_per_ins",
    "dp_flop_per_ins",
    "cpu_util",
    "peak_rss_kb",
    "peak_pss_kb",
    "peak_uss_kb",
    "read_bytes",
    "write_bytes",
    "read_syscalls",
    "write_syscalls",
    "package_size_bytes",
)

DEPLOYMENT_FEATURES = (
    "cold_init_us",
    "cold_provider_exec_us",
    "warm_provider_exec_us",
    "cold_exec_us",
    "warm_exec_us",
    "cold_client_us",
    "warm_client_us",
    "init_fraction",
    "provider_mem_mb",
    "package_size_bytes",
)

FEATURE_SETS = {
    "workload": WORKLOAD_FEATURES,
    "deployment": DEPLOYMENT_FEATURES,
}

# Heavy-tailed quantities. The reported run z-scores the raw values.
# ``--log-size`` applies log1p to these columns before z-scoring.
SIZE_LIKE = frozenset(
    {
        "peak_rss_kb",
        "peak_pss_kb",
        "peak_uss_kb",
        "read_bytes",
        "write_bytes",
        "read_syscalls",
        "write_syscalls",
        "package_size_bytes",
        "cold_init_us",
        "cold_provider_exec_us",
        "warm_provider_exec_us",
        "cold_exec_us",
        "warm_exec_us",
        "cold_client_us",
        "warm_client_us",
        "provider_mem_mb",
    }
)

ID_COLUMNS = ("benchmark", "platform", "language", "input_size", "origin")


@dataclass
class PreparedMatrix:
    """Numeric matrix ready for PCA, plus the bookkeeping the report needs."""

    labels: list[str]
    columns: list[str]
    raw: pd.DataFrame
    standardized: np.ndarray
    notes: list[str] = field(default_factory=list)


def columns_for(feature_set: str) -> tuple[str, ...]:
    """Return the column order for a named feature set."""
    if feature_set not in FEATURE_SETS:
        known = ", ".join(sorted(FEATURE_SETS))
        raise ValueError(f"Unknown feature set {feature_set!r}. Known sets: {known}")
    return FEATURE_SETS[feature_set]


def subset_frame(
    frame: pd.DataFrame, feature_set: str, platform: str | None
) -> tuple[pd.DataFrame, list[str]]:
    """Keep the rows that belong in one PCA.

    The workload PCA uses local hardware-counter rows when the table also
    contains cloud platforms. The deployment PCA is computed for one platform
    at a time, because init and client time are platform measurements.
    """
    notes: list[str] = []
    data = frame.copy()
    if "platform" not in data.columns:
        return data, notes

    present = sorted(str(value) for value in data["platform"].dropna().unique())
    if platform:
        data = data[data["platform"].astype(str) == platform]
        if data.empty:
            raise ValueError(f"No rows for platform {platform!r}. Present: {present}")
        return data, notes

    if feature_set == "workload" and "local" in present and len(present) > 1:
        data = data[data["platform"].astype(str) == "local"]
        notes.append(
            "Workload PCA kept platform=local. Cloud timings stay in the deployment PCA."
        )
    elif feature_set == "deployment" and len(present) > 1:
        raise ValueError(
            "Deployment PCA needs --platform when several platforms are in the table. "
            f"Present: {present}"
        )
    return data, notes


def row_labels(frame: pd.DataFrame) -> list[str]:
    """Build a stable label per row. Append the platform when several remain."""
    if "benchmark" not in frame.columns:
        raise ValueError("Feature table needs a benchmark column.")
    names = frame["benchmark"].astype(str).tolist()
    if "platform" not in frame.columns:
        return names
    platforms = frame["platform"].astype(str).tolist()
    if len(set(platforms)) <= 1:
        return names
    return [f"{name} [{plat}]" for name, plat in zip(names, platforms)]


def prepare_matrix(
    frame: pd.DataFrame,
    columns: tuple[str, ...] | list[str],
    log_size: bool = False,
) -> PreparedMatrix:
    """Drop incomplete or constant columns and z-score the rest.

    A column with a missing value on any remaining row is dropped. Imputing it
    would invent a program behavior. A column with sample standard deviation
    zero has nothing for PCA to explain.
    """
    notes: list[str] = []
    requested = [column for column in columns if column in frame.columns]
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        notes.append("Columns absent from the table: " + ", ".join(missing))
    if len(requested) < 2:
        raise ValueError(
            "Need at least two feature columns in the table. "
            f"Found {requested or 'none'}."
        )

    numeric = frame[requested].apply(pd.to_numeric, errors="coerce")
    numeric.index = pd.Index(row_labels(frame), name="benchmark")
    kept_rows = numeric.dropna(how="all")
    dropped_rows = sorted(set(numeric.index) - set(kept_rows.index))
    if dropped_rows:
        notes.append("Dropped rows with no measured features: " + ", ".join(dropped_rows))
    if kept_rows.shape[0] < 3:
        raise ValueError(
            f"Need at least 3 benchmarks with measurements, found {kept_rows.shape[0]}."
        )

    incomplete = [column for column in kept_rows.columns if kept_rows[column].isna().any()]
    if incomplete:
        notes.append(
            "Dropped columns with missing values on a remaining benchmark: "
            + ", ".join(incomplete)
        )
        kept_rows = kept_rows.drop(columns=incomplete)

    if log_size:
        for column in list(kept_rows.columns):
            if column not in SIZE_LIKE:
                continue
            values = kept_rows[column].to_numpy(dtype=float)
            if np.any(values < 0):
                notes.append(f"Left {column} untransformed because it contains negatives.")
                continue
            kept_rows[column] = np.log1p(values)
        notes.append("Applied log1p to nonnegative size-like columns before z-scoring.")

    constant = [
        column
        for column in kept_rows.columns
        if float(kept_rows[column].std(ddof=1)) == 0.0 or kept_rows[column].nunique() <= 1
    ]
    if constant:
        notes.append("Dropped constant columns: " + ", ".join(constant))
        kept_rows = kept_rows.drop(columns=constant)

    if kept_rows.shape[1] < 2:
        raise ValueError(
            "Fewer than two varying columns remained after dropping incomplete "
            f"and constant features. Remaining: {list(kept_rows.columns)}"
        )

    raw = kept_rows.astype(float)
    mean = raw.mean(axis=0).to_numpy()
    std = raw.std(axis=0, ddof=1).to_numpy()
    standardized = (raw.to_numpy() - mean) / std
    return PreparedMatrix(
        labels=list(raw.index),
        columns=list(raw.columns),
        raw=raw,
        standardized=standardized,
        notes=notes,
    )
