"""Turn SeBS measurement files into one feature row per benchmark and platform.

The raw tree is::

    raw/<platform>/<benchmark>/papi/*.csv
    raw/<platform>/<benchmark>/memory/*.csv
    raw/<platform>/<benchmark>/disk-io/*.csv
    raw/<platform>/<benchmark>/time/*.csv
    raw/<platform>/<benchmark>/package.json
    raw/<platform>/<benchmark>/perf-cost/*.json

``platform`` is one of ``local``, ``aws``, ``openwhisk``. A tree whose first
level is already the benchmark name is treated as ``local``.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np
import pandas as pd

PLATFORMS = ("local", "aws", "openwhisk")
SKIP_DIRS = {"report", "published", "results", "__pycache__"}

# scripts/proc_analyzer.py writes this header for disk I/O, then a row that
# starts with timestamp and a pid count. The /proc/<pid>/io fields follow:
# rchar, wchar, syscr, syscw, read_bytes, write_bytes, cancelled_write_bytes.
BROKEN_DISK_HEADER = [
    "Timestamp",
    "WriteChars",
    "ReadChars",
    "ReadSysCalls",
    "WriteSysCalls",
    "ReadBytes",
    "WriteBytes",
]


def _read_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    """Read a CSV, skipping blank lines and ``#`` comments."""
    with path.open(newline="") as handle:
        rows = [
            row
            for row in csv.reader(handle)
            if row and not (row[0].startswith("#") or row[0] == "")
        ]
    if not rows:
        return [], []
    return rows[0], rows[1:]


def _sums(header: list[str], rows: list[list[str]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for column, name in enumerate(header):
        if not name.startswith("PAPI_"):
            continue
        values = []
        for row in rows:
            if column >= len(row) or row[column] == "":
                continue
            values.append(float(row[column]))
        if values:
            totals[name] = float(np.sum(values))
    return totals


def classify_papi(counters: dict[str, float]) -> str | None:
    """Map one PAPI CSV onto the event-set that produced it."""
    if "PAPI_TOT_CYC" in counters:
        return "cycles"
    if "PAPI_L1_DCM" in counters or "PAPI_L2_TCM" in counters:
        return "cache"
    if "PAPI_SP_OPS" in counters or "PAPI_VEC_SP" in counters:
        return "sp_flops"
    if "PAPI_DP_OPS" in counters or "PAPI_VEC_DP" in counters:
        return "dp_flops"
    if "PAPI_LST_INS" in counters or "PAPI_BR_INS" in counters:
        return "inscount"
    if "PAPI_TOT_INS" in counters:
        return "inscount"
    return None


def ratios_from_papi(groups: dict[str, dict[str, float]]) -> dict[str, float]:
    """Build CPU ratios. Each ratio uses counters from a single event-set run."""
    features: dict[str, float] = {}

    def ratio(group: str, numerator: str, denominator: str, name: str) -> None:
        counters = groups.get(group, {})
        denom = counters.get(denominator, 0.0)
        if denom:
            features[name] = counters.get(numerator, 0.0) / denom

    cycles = groups.get("cycles", {})
    if cycles.get("PAPI_TOT_CYC"):
        features["ipc"] = cycles.get("PAPI_TOT_INS", 0.0) / cycles["PAPI_TOT_CYC"]
        features["stall_frac"] = cycles.get("PAPI_STL_ICY", 0.0) / cycles["PAPI_TOT_CYC"]
    ratio("inscount", "PAPI_LST_INS", "PAPI_TOT_INS", "load_store_frac")
    ratio("inscount", "PAPI_BR_INS", "PAPI_TOT_INS", "branch_frac")
    ratio("cache", "PAPI_L1_DCM", "PAPI_TOT_INS", "l1d_miss_per_ins")
    ratio("cache", "PAPI_L2_TCM", "PAPI_TOT_INS", "l2_miss_per_ins")
    ratio("cache", "PAPI_L3_TCM", "PAPI_TOT_INS", "l3_miss_per_ins")
    ratio("sp_flops", "PAPI_SP_OPS", "PAPI_TOT_INS", "sp_flop_per_ins")
    ratio("dp_flops", "PAPI_DP_OPS", "PAPI_TOT_INS", "dp_flop_per_ins")
    return features


def papi_features(paths: list[Path]) -> dict[str, float]:
    """Sum PAPI sample rows and convert each event set into ratios."""
    groups: dict[str, dict[str, float]] = {}
    for path in paths:
        header, rows = _read_rows(path)
        totals = _sums(header, rows)
        kind = classify_papi(totals)
        if kind is None:
            continue
        bucket = groups.setdefault(kind, {})
        for name, value in totals.items():
            bucket[name] = bucket.get(name, 0.0) + value
    return ratios_from_papi(groups)


def memory_features(paths: list[Path]) -> dict[str, float]:
    """Peak USS, PSS, and RSS across memory-sampler CSVs. Units are kB."""
    peaks = {"peak_uss_kb": 0.0, "peak_pss_kb": 0.0, "peak_rss_kb": 0.0}
    found = False
    for path in paths:
        header, rows = _read_rows(path)
        index = {name: position for position, name in enumerate(header)}
        needed = {"USS": "peak_uss_kb", "PSS": "peak_pss_kb", "RSS": "peak_rss_kb"}
        if not set(needed).issubset(index):
            continue
        for row in rows:
            for source, target in needed.items():
                position = index[source]
                if position >= len(row) or row[position] == "":
                    continue
                peaks[target] = max(peaks[target], float(row[position]))
                found = True
    return peaks if found else {}


def _counter_delta(values: list[float]) -> float:
    """Difference of a cumulative counter. Falls back to positive steps if it resets."""
    if len(values) < 2:
        return values[0] if values else 0.0
    if all(values[i] <= values[i + 1] for i in range(len(values) - 1)):
        return values[-1] - values[0]
    delta = 0.0
    for earlier, later in zip(values, values[1:]):
        if later >= earlier:
            delta += later - earlier
    return delta


def disk_features(paths: list[Path]) -> dict[str, float]:
    """Bytes and syscall counts from ``/proc/<pid>/io`` samples.

    The analyzer header does not match the row layout, so rows that are longer
    than that header are read by position. A header that already contains
    ``rchar`` is read by name.
    """
    read_bytes: list[float] = []
    write_bytes: list[float] = []
    read_syscalls: list[float] = []
    write_syscalls: list[float] = []
    for path in paths:
        header, rows = _read_rows(path)
        if not rows:
            continue
        named = {name: position for position, name in enumerate(header)}
        if {"rchar", "read_bytes", "write_bytes"}.issubset(named):
            positions = {
                "read_bytes": named["read_bytes"],
                "write_bytes": named["write_bytes"],
                "read_syscalls": named.get("syscr", named.get("read_syscalls")),
                "write_syscalls": named.get("syscw", named.get("write_syscalls")),
            }
        elif header[: len(BROKEN_DISK_HEADER)] == BROKEN_DISK_HEADER or (
            header and header[0] == "Timestamp" and len(rows[0]) >= 8
        ):
            # timestamp, pid count, rchar, wchar, syscr, syscw, read_bytes, write_bytes
            positions = {
                "read_bytes": 6,
                "write_bytes": 7,
                "read_syscalls": 4,
                "write_syscalls": 5,
            }
        else:
            continue
        for row in rows:
            def cell(key: str, row: list[str] = row) -> float | None:
                position = positions[key]
                if position is None or position >= len(row) or row[position] == "":
                    return None
                return float(row[position])

            read_value = cell("read_bytes")
            write_value = cell("write_bytes")
            if read_value is None or write_value is None:
                continue
            read_bytes.append(read_value)
            write_bytes.append(write_value)
            read_call = cell("read_syscalls")
            write_call = cell("write_syscalls")
            if read_call is not None:
                read_syscalls.append(read_call)
            if write_call is not None:
                write_syscalls.append(write_call)
    if not read_bytes:
        return {}
    features = {
        "read_bytes": _counter_delta(read_bytes),
        "write_bytes": _counter_delta(write_bytes),
    }
    if read_syscalls:
        features["read_syscalls"] = _counter_delta(read_syscalls)
    if write_syscalls:
        features["write_syscalls"] = _counter_delta(write_syscalls)
    return features


def time_features(paths: list[Path]) -> dict[str, float]:
    """Median wall, user, and system time from the local in-process timer."""
    durations: list[float] = []
    users: list[float] = []
    systems: list[float] = []
    for path in paths:
        header, rows = _read_rows(path)
        index = {name: position for position, name in enumerate(header)}
        if not {"Duration", "User", "Sys"}.issubset(index):
            continue
        for row in rows:
            durations.append(float(row[index["Duration"]]))
            users.append(float(row[index["User"]]))
            systems.append(float(row[index["Sys"]]))
    if not durations:
        return {}
    wall = float(np.median(durations))
    user = float(np.median(users))
    system = float(np.median(systems))
    features = {"wall_us": wall, "user_us": user, "sys_us": system}
    if wall > 0:
        features["cpu_util"] = (user + system) / wall
    return features


def _median(values: list[float]) -> float:
    return float(np.median(np.asarray(values, dtype=float)))


def _invocation_records(payload: dict) -> list[dict]:
    """Flatten SeBS ``_invocations`` into one dict per successful invocation."""
    records = []
    invocations = payload.get("_invocations", {})
    if not isinstance(invocations, dict):
        return records
    for function_calls in invocations.values():
        if not isinstance(function_calls, dict):
            continue
        for call in function_calls.values():
            if not isinstance(call, dict):
                continue
            stats = call.get("stats") or {}
            if stats.get("failure"):
                continue
            times = call.get("times") or {}
            provider = call.get("provider_times") or {}
            records.append(
                {
                    "cold": bool(stats.get("cold_start")),
                    "exec_us": times.get("benchmark"),
                    "client_us": times.get("client"),
                    "provider_exec_us": provider.get("execution"),
                    "init_us": provider.get("initialization"),
                    "mem_mb": stats.get("memory_used"),
                }
            )
    return records


def perf_cost_features(paths: list[Path]) -> dict[str, float]:
    """Median cold and warm timings from SeBS perf-cost JSON."""
    cold_init: list[float] = []
    cold_provider: list[float] = []
    warm_provider: list[float] = []
    cold_exec: list[float] = []
    warm_exec: list[float] = []
    cold_client: list[float] = []
    warm_client: list[float] = []
    memory: list[float] = []
    for path in paths:
        if path.suffix != ".json":
            continue
        payload = json.loads(path.read_text())
        if "_invocations" not in payload:
            continue
        for record in _invocation_records(payload):
            cold = record["cold"]
            if record["init_us"] not in (None, 0) and cold:
                cold_init.append(float(record["init_us"]))
            if record["provider_exec_us"] not in (None, 0):
                (cold_provider if cold else warm_provider).append(float(record["provider_exec_us"]))
            if record["exec_us"] not in (None, 0):
                (cold_exec if cold else warm_exec).append(float(record["exec_us"]))
            if record["client_us"] not in (None, 0):
                (cold_client if cold else warm_client).append(float(record["client_us"]))
            if record["mem_mb"] not in (None, 0):
                memory.append(float(record["mem_mb"]))
    features: dict[str, float] = {}
    if cold_init:
        features["cold_init_us"] = _median(cold_init)
    if cold_provider:
        features["cold_provider_exec_us"] = _median(cold_provider)
    if warm_provider:
        features["warm_provider_exec_us"] = _median(warm_provider)
    if cold_exec:
        features["cold_exec_us"] = _median(cold_exec)
    if warm_exec:
        features["warm_exec_us"] = _median(warm_exec)
    if cold_client:
        features["cold_client_us"] = _median(cold_client)
    if warm_client:
        features["warm_client_us"] = _median(warm_client)
    if memory:
        features["provider_mem_mb"] = _median(memory)
    if features.get("cold_init_us") and features.get("cold_provider_exec_us"):
        total = features["cold_init_us"] + features["cold_provider_exec_us"]
        features["init_fraction"] = features["cold_init_us"] / total
    return features


def package_features(paths: list[Path]) -> dict[str, float]:
    """Read ``package_size_bytes`` from a sidecar JSON file."""
    for path in paths:
        if path.suffix != ".json":
            continue
        payload = json.loads(path.read_text())
        if isinstance(payload, dict) and "package_size_bytes" in payload:
            return {"package_size_bytes": float(payload["package_size_bytes"])}
    return {}


def package_size_for_deployment(payload: dict, deployment: str) -> int | None:
    """Return the SeBS cache ``size`` for one deployment, if it was recorded."""
    node = payload.get(deployment)
    sizes = package_sizes_in_cache_config(node) if isinstance(node, dict) else []
    if not sizes:
        sizes = package_sizes_in_cache_config(payload)
    return sizes[-1] if sizes else None


def package_sizes_in_cache_config(payload: dict) -> list[int]:
    """Collect integer ``size`` fields from a SeBS cache ``config.json``."""
    found: list[int] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            size = node.get("size")
            if isinstance(size, int):
                found.append(size)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


def _classify_file(path: Path) -> str | None:
    if path.suffix == ".csv":
        header, _rows = _read_rows(path)
        if any(name.startswith("PAPI_") for name in header):
            return "papi"
        if {"USS", "PSS", "RSS"}.issubset(header):
            return "memory"
        if {"Duration", "User", "Sys"}.issubset(header):
            return "time"
        if header[: len(BROKEN_DISK_HEADER)] == BROKEN_DISK_HEADER or "read_bytes" in header:
            return "disk"
        if header and header[0] == "Timestamp" and "RSS" not in header:
            return "disk"
    if path.suffix == ".json":
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict) and "_invocations" in payload:
            return "perf"
        if isinstance(payload, dict) and "package_size_bytes" in payload:
            return "package"
    return None


def features_from_benchmark_dir(directory: Path) -> dict[str, float]:
    """Parse every recognized measurement file under one benchmark directory."""
    grouped: dict[str, list[Path]] = {}
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        kind = _classify_file(path)
        if kind:
            grouped.setdefault(kind, []).append(path)
    features: dict[str, float] = {}
    features.update(papi_features(grouped.get("papi", [])))
    features.update(memory_features(grouped.get("memory", [])))
    features.update(disk_features(grouped.get("disk", [])))
    features.update(time_features(grouped.get("time", [])))
    features.update(perf_cost_features(grouped.get("perf", [])))
    features.update(package_features(grouped.get("package", [])))
    return features


def iter_benchmark_dirs(root: Path):
    """Yield ``(platform, benchmark, directory)`` for a raw results tree."""
    children = [
        path
        for path in root.iterdir()
        if path.is_dir() and path.name not in SKIP_DIRS and not path.name.startswith(".")
    ]
    names = {path.name for path in children}
    if names & set(PLATFORMS):
        for platform_dir in children:
            if platform_dir.name not in PLATFORMS:
                continue
            for benchmark_dir in sorted(platform_dir.iterdir()):
                if benchmark_dir.is_dir():
                    yield platform_dir.name, benchmark_dir.name, benchmark_dir
        return
    for benchmark_dir in sorted(children):
        yield "local", benchmark_dir.name, benchmark_dir


def ingest_tree(root: Path, manifest: dict | None = None) -> pd.DataFrame:
    """Build the feature table for a raw results tree."""
    meta = manifest or {}
    if (root / "manifest.json").is_file():
        meta = {**json.loads((root / "manifest.json").read_text()), **meta}
    rows = []
    for platform, benchmark, directory in iter_benchmark_dirs(root):
        measured = features_from_benchmark_dir(directory)
        if not measured:
            continue
        row = {
            "benchmark": benchmark,
            "platform": platform,
            "language": meta.get("language", "python"),
            "input_size": meta.get("input_size", "small"),
            "origin": meta.get("origin", "measured"),
        }
        row.update(measured)
        rows.append(row)
    if not rows:
        raise ValueError(f"No measurement files recognized under {root}.")
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> None:
    """CLI: read a raw tree and write ``features.csv``."""
    parser = argparse.ArgumentParser(description="Build a SeBS characterization feature table.")
    parser.add_argument("--results", required=True, type=Path, help="Raw results directory.")
    parser.add_argument("--out", required=True, type=Path, help="Destination features.csv.")
    parser.add_argument("--language", default=None)
    parser.add_argument("--input-size", default=None)
    args = parser.parse_args(argv)
    manifest = {}
    if args.language:
        manifest["language"] = args.language
    if args.input_size:
        manifest["input_size"] = args.input_size
    frame = ingest_tree(args.results, manifest or None)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.out, index=False)
    print(f"Wrote {len(frame)} rows to {args.out}")


if __name__ == "__main__":
    main()
