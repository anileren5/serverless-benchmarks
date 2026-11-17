import argparse
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns

import duckdb

from .contract import (
    ASCII_LOWERCASE, ASCII_UPPERCASE, ASCII_WHITESPACE, BASIS_POINTS,
    BYTES_PER_MIB, CURRENCY, DISCOUNT_BPS_MAX, DISCOUNT_BPS_MIN, DUCKDB_MEMORY_PERCENT,
    EVENT_HEADERS, EVENT_ID_PATTERN, LAMBDA_MAX_MEMORY_MB, LAMBDA_MAX_THREADS,
    LAMBDA_MB_PER_VCPU, LAMBDA_MIN_MEMORY_MB, OUTPUT_HEADERS, PARQUET_COMPRESSION,
    PARTITION_COLUMNS, PRICE_CENTS_MAX, PRICE_CENTS_MIN, PRODUCT_HEADERS,
    PRODUCT_ID_PATTERN, QUANTITY_MAX, QUANTITY_MIN, STATUSES, STATUS_COMPLETED,
    STORE_HEADERS, STORE_ID_PATTERN, TIMESTAMP_FORMAT, TIMESTAMP_PATTERN,
    TRANSACTION_ID_PATTERN,
)

from .storage_io import relative_path


def _literal(value):
    """Quote a SQL string value."""
    return "'" + str(value).replace("'", "''") + "'"


def _identifier(name):
    """Quote a SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def resource_config(memory_mb, threads=None, memory_limit=None):
    """Choose DuckDB thread and memory settings."""
    if type(memory_mb) is not int or not LAMBDA_MIN_MEMORY_MB <= memory_mb <= LAMBDA_MAX_MEMORY_MB:
        raise ValueError(f"memory_mb must be an integer in [{LAMBDA_MIN_MEMORY_MB}, {LAMBDA_MAX_MEMORY_MB}]")
    if threads is None:
        threads = min(LAMBDA_MAX_THREADS, (memory_mb + LAMBDA_MB_PER_VCPU - 1) // LAMBDA_MB_PER_VCPU)
    if type(threads) is not int or threads < 1:
        raise ValueError("threads must be a positive integer")
    if memory_limit is None:
        memory_limit = f"{memory_mb * BYTES_PER_MIB * DUCKDB_MEMORY_PERCENT // 100}B"
    if not isinstance(memory_limit, str) or not memory_limit:
        raise ValueError("memory_limit must be a DuckDB memory-size string")
    return {"threads": str(threads), "memory_limit": memory_limit}


def _load_inputs(directory):
    with (directory / "manifest.json").open(encoding="utf-8") as source:
        manifest = json.load(source)
    entries = {}
    root = directory.resolve()
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("Input manifest must contain a file mapping")
    for name, info in inputs.items():
        relative = relative_path(name)
        if name not in ("products.csv", "stores.csv") and not (
            len(relative.parts) == 2 and relative.parts[0] == "transactions" and relative.suffix == ".csv"
        ):
            raise ValueError(f"Unexpected input file: {name}")
        if not isinstance(info, dict) or type(info.get("rows")) is not int or info["rows"] < 0:
            raise ValueError(f"Invalid declared input row count: {name}")
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Input path escapes the dataset directory: {name}")
        if not path.is_file():
            raise FileNotFoundError(f"Missing input file: {path}")
        entries[name] = dict(info, path=str(path))
    for name in ("products.csv", "stores.csv"):
        if name not in entries:
            raise ValueError(f"Missing reference input in manifest: {name}")
    return entries


def _input_view(con, table, entries, headers):
    """Create a view over CSV input files."""
    paths = [Path(entry["path"]) for entry in entries]
    if not paths or len({path.resolve() for path in paths}) != len(paths):
        raise ValueError(f"Input file list for {table} must be nonempty and contain no repeated files")
    columns = "{" + ", ".join(f"{_literal(name)}: 'VARCHAR'" for name in headers) + "}"
    fields = ",\n                ".join(f"coalesce({_identifier(name)}, '') AS {_identifier(name)}" for name in headers)
    files = "[" + ", ".join(_literal(path) for path in paths) + "]"
    con.execute(f"""
        CREATE VIEW {_identifier(table)} AS
        SELECT {fields}
        FROM read_csv(
            {files}, columns={columns}, auto_detect=false, header=true,
            delim=',', quote='"', escape='"', nullstr='', strict_mode=true,
            null_padding=false, ignore_errors=false, hive_partitioning=false
        )
    """)


def _create_input_views(con, entries):
    events = [info for name, info in entries.items() if name.startswith("transactions/")]
    _input_view(con, "events", events, EVENT_HEADERS)
    _input_view(con, "products", [entries["products.csv"]], PRODUCT_HEADERS)
    _input_view(con, "stores", [entries["stores.csv"]], STORE_HEADERS)
    return sum(entry["rows"] for entry in events)


def _pipeline(con):
    """Define the retail data transformations."""
    normalized = []
    for name in EVENT_HEADERS:
        value = f"trim({_identifier(name)}, {_literal(ASCII_WHITESPACE)})"
        if name in ("status", "currency"):
            value = f"translate({value}, {_literal(ASCII_LOWERCASE)}, {_literal(ASCII_UPPERCASE)})"
        normalized.append(f"{value} AS {_identifier(name)}")
    fields = ",\n               ".join(normalized)
    con.execute(f"""
        CREATE VIEW clean AS
        SELECT {fields}
        FROM events
    """)
    statuses = ", ".join(_literal(status) for status in STATUSES)
    con.execute(f"""
        CREATE VIEW valid AS
        WITH parsed AS (
            SELECT event_id, transaction_id,
                   try_strptime(event_time, {_literal(TIMESTAMP_FORMAT)}) AS event_time,
                   try_strptime(transaction_time, {_literal(TIMESTAMP_FORMAT)}) AS transaction_time,
                   product_id, store_id,
                   try_cast(quantity AS BIGINT) AS quantity,
                   try_cast(unit_price_cents AS BIGINT) AS unit_price_cents,
                   try_cast(discount_bps AS BIGINT) AS discount_bps,
                   status, currency
            FROM clean
            WHERE regexp_full_match(event_id, {_literal(EVENT_ID_PATTERN)})
              AND regexp_full_match(transaction_id, {_literal(TRANSACTION_ID_PATTERN)})
              AND regexp_full_match(product_id, {_literal(PRODUCT_ID_PATTERN)})
              AND regexp_full_match(store_id, {_literal(STORE_ID_PATTERN)})
              AND regexp_full_match(event_time, {_literal(TIMESTAMP_PATTERN)})
              AND regexp_full_match(transaction_time, {_literal(TIMESTAMP_PATTERN)})
              AND regexp_full_match(quantity, '[0-9]+')
              AND regexp_full_match(unit_price_cents, '[0-9]+')
              AND regexp_full_match(discount_bps, '[0-9]+')
        )
        SELECT * FROM parsed
        WHERE transaction_time <= event_time
          AND quantity BETWEEN {QUANTITY_MIN} AND {QUANTITY_MAX}
          AND unit_price_cents BETWEEN {PRICE_CENTS_MIN} AND {PRICE_CENTS_MAX}
          AND discount_bps BETWEEN {DISCOUNT_BPS_MIN} AND {DISCOUNT_BPS_MAX}
          AND status IN ({statuses}) AND currency = {_literal(CURRENCY)}
    """)
    con.execute("""
        CREATE VIEW selected AS
        SELECT * FROM valid
        QUALIFY row_number() OVER (
            PARTITION BY transaction_id ORDER BY event_time DESC, event_id DESC
        ) = 1
    """)
    con.execute(f"""
        CREATE VIEW output AS
        WITH enriched AS (
            SELECT s.*, p.product_name, p.category, r.store_name, r.region,
                   cast(s.transaction_time AS DATE) AS transaction_date,
                   s.quantity * s.unit_price_cents AS gross_cents
            FROM selected s
            JOIN products p USING (product_id)
            JOIN stores r USING (store_id)
            WHERE s.status = {_literal(STATUS_COMPLETED)}
        ), discounted AS (
            SELECT *, ((gross_cents * discount_bps + {BASIS_POINTS // 2}) // {BASIS_POINTS})::BIGINT AS discount_cents
            FROM enriched
        )
        SELECT *, gross_cents - discount_cents AS total_cents FROM discounted
    """)


def _write_parquet(con, directory):
    fields = ", ".join(_identifier(name) for name in OUTPUT_HEADERS)
    partitions = ", ".join(_identifier(name) for name in PARTITION_COLUMNS)
    return con.execute(f"""
        COPY (SELECT {fields} FROM output) TO {_literal(directory)} (
            FORMAT PARQUET, PARTITION_BY ({partitions}),
            WRITE_PARTITION_COLUMNS true, COMPRESSION {_literal(PARQUET_COMPRESSION)}
        )
    """).fetchone()[0]


def _execute_copy(con, directory, profile_path):
    """Write the results and measure execution time."""
    if profile_path is not None:
        con.execute(f"PRAGMA profiling_output={_literal(profile_path)}")
        con.execute("PRAGMA enable_profiling='json'")
    execution_begin = perf_counter_ns()
    output_count = _write_parquet(con, directory)
    execution_done = perf_counter_ns()
    if profile_path is not None:
        con.execute("PRAGMA disable_profiling")
    return output_count, execution_begin, execution_done


def _profile_metrics(path):
    """Read memory and disk usage from the query profile."""
    with path.open(encoding="utf-8") as source:
        data = json.load(source)
    metrics = {"duckdb_version": duckdb.__version__, "missing_metrics": []}
    for name in ("SYSTEM_PEAK_TEMP_DIR_SIZE", "SYSTEM_PEAK_BUFFER_MEMORY"):
        value = data.get(name.lower())
        if type(value) is not int or value < 0:
            value = None
            metrics["missing_metrics"].append(name)
        metrics[name.lower() + "_bytes"] = value
    return metrics


def _prepare_output_paths(output_directory, profile, profile_path):
    output_directory = Path(output_directory)
    if profile_path is not None:
        profile_path = Path(profile_path)
        if not profile:
            raise ValueError("profile_path requires profile=True")
        if profile_path.resolve().is_relative_to(output_directory.resolve()):
            raise ValueError("profile_path must be outside the ETL output directory")
        if profile_path.exists():
            raise ValueError("profile_path must be new")
    if output_directory.exists() and (not output_directory.is_dir() or any(output_directory.iterdir())):
        raise ValueError("Output directory must be new or empty")
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    return output_directory, profile_path


def transform(dataset_directory, output_directory, *, memory_mb, threads=None, memory_limit=None,
              measurements=None, profile=False, profile_path=None):
    """Convert retail CSV data to partitioned Parquet."""
    config = resource_config(memory_mb, threads, memory_limit)
    if type(profile) is not bool:
        raise ValueError("profile must be a boolean")
    entries = _load_inputs(Path(dataset_directory))
    output_directory, profile_path = _prepare_output_paths(output_directory, profile, profile_path)
    with TemporaryDirectory(prefix=".retail-etl-", dir=output_directory.parent) as temporary:
        work = Path(temporary)
        staged = work / "output"
        staged.mkdir()
        raw_profile = work / "duckdb-profile.json"
        initialization_begin = perf_counter_ns()
        with duckdb.connect(":memory:", config={
            **config, "temp_directory": str(work / "spill"), "preserve_insertion_order": "false",
        }) as con:
            initialized = perf_counter_ns()
            declared_events = _create_input_views(con, entries)
            _pipeline(con)
            setup_done = perf_counter_ns()
            output_count, execution_begin, execution_done = _execute_copy(
                con, staged, raw_profile if profile else None,
            )
            effective = con.execute("SELECT current_setting('threads'), current_setting('memory_limit')").fetchone()
        processed = perf_counter_ns()
        audit = {"input_events": declared_events, "output_rows": output_count}
        resources = {
            "memory_mb": memory_mb, "threads": effective[0], "memory_limit": config["memory_limit"],
            "effective_memory_limit": effective[1],
        }
        if measurements is not None:
            measurements.update({
                "initialization_time": (initialized - initialization_begin) / 1000,
                "compute_time": (processed - initialized) / 1000,
                "sql_setup_time": (setup_done - initialized) / 1000,
                "query_execution_time": (execution_done - execution_begin) / 1000,
                "resources": resources,
            })
            if profile:
                measurements["profiling"] = _profile_metrics(raw_profile)
        if profile_path is not None:
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(raw_profile, profile_path)
        staged.replace(output_directory)
    return audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--memory-mb", type=int, required=True, help="Declared function/container allocation")
    parser.add_argument("--threads", type=int, help="Override workers for controlled experiments")
    parser.add_argument("--memory-limit", help="Override DuckDB budget, e.g. 128MB, for spill investigations")
    parser.add_argument("--profile", action="store_true", help="Report COPY peak memory and temporary disk metrics")
    parser.add_argument("--profile-output", type=Path, help="Also retain raw JSON outside OUTPUT; enables profiling")
    args = parser.parse_args()
    profile = args.profile or args.profile_output is not None
    measurements = {}
    audit = transform(args.dataset, args.output, memory_mb=args.memory_mb,
                      threads=args.threads, memory_limit=args.memory_limit,
                      measurements=measurements, profile=profile, profile_path=args.profile_output)
    print(json.dumps({"audit": audit, "measurement": measurements}, indent=2))


if __name__ == "__main__":
    main()
