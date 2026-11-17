import argparse
from contextlib import closing
import csv
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re

if __package__:
    from .python import contract
else:
    from python import contract

_UPPER = str.maketrans(contract.ASCII_LOWERCASE, contract.ASCII_UPPERCASE)


def _csv_rows(path, headers):
    """Read CSV rows and check their format."""
    try:
        with path.open(encoding="utf-8", newline="") as source:
            reader = csv.reader(source, strict=True)
            if next(reader, None) != headers:
                raise ValueError(f"Incorrect CSV header: {path}")
            for values in reader:
                if len(values) != len(headers):
                    raise ValueError(f"Incorrect CSV field count: {path}")
                yield dict(zip(headers, values))
    except (csv.Error, UnicodeError) as error:
        raise ValueError(f"Malformed UTF-8 CSV: {path}") from error


def _manifest(dataset):
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict) or not {"products.csv", "stores.csv"} <= inputs.keys():
        raise ValueError("Input manifest must declare both reference files")
    if not any(name.startswith("transactions/") for name in inputs):
        raise ValueError("Input manifest must declare transaction CSVs")
    for name in inputs:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Invalid input path: {name}")
        if name not in ("products.csv", "stores.csv") and not (
            len(path.parts) == 2 and path.parts[0] == "transactions" and path.suffix == ".csv"
        ):
            raise ValueError(f"Unexpected input file: {name}")
    return manifest


def validate_inputs(dataset):
    dataset = Path(dataset)
    manifest = _manifest(dataset)
    input_events = 0
    for name, info in manifest["inputs"].items():
        path = dataset / name
        if (not isinstance(info, dict) or type(info.get("rows")) is not int or info["rows"] < 0
                or type(info.get("bytes")) is not int or info["bytes"] <= 0):
            raise ValueError(f"Invalid input manifest counts: {name}")
        if path.stat().st_size != info["bytes"]:
            raise ValueError(f"Input byte size differs from manifest: {name}")
        digest = hashlib.sha256()
        last_byte = b""
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                last_byte = chunk[-1:]
        if digest.hexdigest() != info.get("sha256"):
            raise ValueError(f"Input checksum differs from manifest: {name}")
        if last_byte != b"\n":
            raise ValueError(f"CSV must end with LF: {name}")
        headers = (contract.PRODUCT_HEADERS if name == "products.csv" else
                   contract.STORE_HEADERS if name == "stores.csv" else contract.EVENT_HEADERS)
        if sum(1 for _ in _csv_rows(path, headers)) != info["rows"]:
            raise ValueError(f"Input row count differs from manifest: {name}")
        if name.startswith("transactions/"):
            input_events += info["rows"]
    return {"input_files": len(manifest["inputs"]), "input_events": input_events}


def _valid_event(raw):
    """Parse an event and reject invalid values."""
    row = {name: value.strip(contract.ASCII_WHITESPACE) for name, value in raw.items()}
    for name in ("status", "currency"):
        row[name] = row[name].translate(_UPPER)
    for name, pattern in (
        ("event_id", contract.EVENT_ID_PATTERN), ("transaction_id", contract.TRANSACTION_ID_PATTERN),
        ("product_id", contract.PRODUCT_ID_PATTERN), ("store_id", contract.STORE_ID_PATTERN),
        ("event_time", contract.TIMESTAMP_PATTERN), ("transaction_time", contract.TIMESTAMP_PATTERN),
    ):
        if re.fullmatch(pattern, row[name]) is None:
            return None
    try:
        for name in ("transaction_time", "event_time"):
            row[name] = datetime.strptime(row[name], contract.TIMESTAMP_FORMAT)
    except ValueError:
        return None
    if (row["transaction_time"] > row["event_time"] or row["status"] not in contract.STATUSES
            or row["currency"] != contract.CURRENCY):
        return None
    for name, low, high in (
        ("quantity", contract.QUANTITY_MIN, contract.QUANTITY_MAX),
        ("unit_price_cents", contract.PRICE_CENTS_MIN, contract.PRICE_CENTS_MAX),
        ("discount_bps", contract.DISCOUNT_BPS_MIN, contract.DISCOUNT_BPS_MAX),
    ):
        digits = row[name].lstrip("0") or "0"
        if not re.fullmatch(r"[0-9]+", row[name]) or len(digits) > len(str(high)):
            return None
        row[name] = int(digits)
        if not low <= row[name] <= high:
            return None
    return row


def _events(dataset, manifest):
    for name in manifest["inputs"]:
        if name.startswith("transactions/"):
            for raw in _csv_rows(dataset / name, contract.EVENT_HEADERS):
                row = _valid_event(raw)
                if row is not None:
                    yield row


def _latest_events(events):
    """Select the latest event for each transaction."""
    latest = {}
    for row in events:
        key = row["transaction_id"]
        previous = latest.get(key)
        rank = (row["event_time"], row["event_id"])
        if previous is None or rank > (previous["event_time"], previous["event_id"]):
            latest[key] = row
    while latest:
        yield latest.popitem()[1]


def _reference_rows(dataset, manifest):
    """Calculate expected results in Python."""
    products = {row["product_id"]: (row["product_name"], row["category"])
                for row in _csv_rows(dataset / "products.csv", contract.PRODUCT_HEADERS)}
    stores = {row["store_id"]: (row["store_name"], row["region"])
              for row in _csv_rows(dataset / "stores.csv", contract.STORE_HEADERS)}
    for row in _latest_events(_events(dataset, manifest)):
        if row["status"] != contract.STATUS_COMPLETED:
            continue
        if row["product_id"] not in products or row["store_id"] not in stores:
            continue
        gross = row["quantity"] * row["unit_price_cents"]
        discount, remainder = divmod(gross * row["discount_bps"], contract.BASIS_POINTS)
        discount += remainder >= contract.BASIS_POINTS // 2
        row["product_name"], row["category"] = products[row["product_id"]]
        row["store_name"], row["region"] = stores[row["store_id"]]
        row.update(transaction_date=row["transaction_time"].date(), gross_cents=gross,
                   discount_cents=discount, total_cents=gross - discount)
        yield tuple(row[name] for name in contract.OUTPUT_HEADERS)


def reference_result(dataset):
    """Return expected rows and audit counts."""
    dataset = Path(dataset)
    counts = validate_inputs(dataset)
    rows = sorted(_reference_rows(dataset, _manifest(dataset)), key=lambda row: row[1])
    return rows, {"input_events": counts["input_events"], "output_rows": len(rows)}


def _parquet_rows(directory, partitions):
    """Read Parquet rows and check their schema and partitions."""
    import duckdb

    date_index = contract.OUTPUT_HEADERS.index("transaction_date")
    region_index = contract.OUTPUT_HEADERS.index("region")
    date_column, region_column = contract.PARTITION_COLUMNS
    with duckdb.connect(":memory:", config={"memory_limit": "256MB", "threads": "1"}) as decoder:
        for path in directory.rglob("*.parquet"):
            relative = path.relative_to(directory)
            if len(relative.parts) != 3:
                raise ValueError(f"Invalid partition path: {relative}")
            day_part, region_part, _ = relative.parts
            if not day_part.startswith(date_column + "=") or region_part not in [
                region_column + "=" + region for region in contract.REGIONS
            ]:
                raise ValueError(f"Invalid partition path: {relative}")
            day = date.fromisoformat(day_part.split("=", 1)[1])
            if day_part != date_column + "=" + day.isoformat():
                raise ValueError(f"Invalid partition date: {relative}")
            region = region_part.split("=", 1)[1]
            schema = decoder.execute("DESCRIBE SELECT * FROM read_parquet(?, hive_partitioning=false)", [str(path)]).fetchall()
            if [(r[0], r[1]) for r in schema] != list(zip(contract.OUTPUT_HEADERS, contract.OUTPUT_TYPES)):
                raise ValueError(f"Incorrect Parquet schema: {relative}")
            cursor = decoder.execute("SELECT * FROM read_parquet(?, hive_partitioning=false)", [str(path)])
            while batch := cursor.fetchmany(1024):
                for row in batch:
                    if row[date_index] != day or row[region_index] != region:
                        raise ValueError(f"Row disagrees with partition: {relative}")
                    key = path.parent.relative_to(directory).as_posix()
                    partitions[key] = partitions.get(key, 0) + 1
                    yield row


def _compare_memory(actual, expected):
    count = len(expected)
    for row in actual:
        if expected.pop(row[1], None) != row:
            raise ValueError(f"Incorrect, duplicate or unexpected output transaction: {row[1]}")
    if expected:
        raise ValueError(f"Missing output transaction: {next(iter(expected))}")
    return count


def validate_output(directory, dataset):
    directory, dataset = Path(directory), Path(dataset)
    counts = validate_inputs(dataset)
    manifest, partitions = _manifest(dataset), {}
    expected = {row[1]: row for row in _reference_rows(dataset, manifest)}
    with closing(_parquet_rows(directory, partitions)) as actual:
        output_rows = _compare_memory(actual, expected)
    expected_audit = {"input_events": counts["input_events"], "output_rows": output_rows}
    return {"audit": expected_audit, "partitions": partitions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--inputs-only", action="store_true", help="Check input schemas, counts and checksums only")
    args = parser.parse_args()
    if args.inputs_only:
        if args.output is not None:
            parser.error("--inputs-only does not accept an output directory")
        result = validate_inputs(args.dataset)
    else:
        if args.output is None:
            parser.error("output directory is required unless --inputs-only is used")
        result = validate_output(args.output, args.dataset)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
