import argparse
import csv
import hashlib
import json
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from . import contract

EVENT_COUNTS = {"test": 1000, "small": 10_000, "medium": 30_000, "large": 100_000}
PRODUCT_COUNTS = {"test": 8, "small": 1000, "medium": 3000, "large": 10_000}
PRODUCT_NAMES = ("Café mug", "Desk lamp", "Cotton shirt", "Tea gift set")
START_TIME = datetime.fromisoformat(contract.TRANSACTION_DATE)


def _event_counts(config):
    """Calculate the number of events of each kind."""
    invalid = EVENT_COUNTS[config.size] // contract.INVALID_RECORD_EVERY
    valid = EVENT_COUNTS[config.size] - invalid
    extras = valid * config.duplicate_bps // contract.BASIS_POINTS
    return valid - extras, extras, invalid


@dataclass(frozen=True)
class DatasetConfig:
    size: str = "test"
    file_count: int = 4
    # Keep derived values in serialized configurations for existing datasets.
    products: int = field(init=False)
    referenced_products: int = field(init=False)
    duplicate_bps: int = field(init=False, default=contract.DEFAULT_DUPLICATE_BPS)

    def __post_init__(self):
        if self.size not in EVENT_COUNTS:
            raise ValueError(f"Unknown size: {self.size}")
        if type(self.file_count) is not int or not 1 <= self.file_count <= 256:
            raise ValueError("file_count must be an integer in [1, 256]")
        object.__setattr__(self, "products", PRODUCT_COUNTS[self.size])
        object.__setattr__(self, "referenced_products", self.products)


PROFILES = {
    size: DatasetConfig(size, files)
    for size, files in (
        ("test", 4), ("small", 8), ("medium", 16), ("large", 32),
    )
}


def _base_event(index, referenced_products, timestamp):
    event = {
        "event_id": "",
        "transaction_id": f"T{index:012d}",
        "event_time": (timestamp + timedelta(hours=1)).strftime(contract.TIMESTAMP_FORMAT),
        "transaction_time": timestamp.strftime(contract.TIMESTAMP_FORMAT),
        "product_id": f"P{index % referenced_products:08d}",
        "store_id": f"S{(index // 4) % len(contract.REGIONS):08d}",
        "quantity": str(1 + index % 5),
        "unit_price_cents": str(99 + index * 113 % 100_000),
        "discount_bps": str((0, 500, 1000)[index % 3]),
        "status": contract.STATUS_PENDING if index % 10 == 9 else contract.STATUS_COMPLETED,
        "currency": contract.CURRENCY,
    }
    if index % 20 == 0:
        event["status"] = f" {contract.STATUS_COMPLETED.lower()} "
    return event


def _duplicate_event(base, index, timestamp):
    """Create an update to an existing transaction."""
    event = base.copy()
    kind = index % 3
    if kind == 0:
        event.update(event_time=base["transaction_time"], status=contract.STATUS_PENDING)
    else:
        event["event_time"] = (timestamp + timedelta(hours=2)).strftime(contract.TIMESTAMP_FORMAT)
        event["status"] = contract.STATUS_COMPLETED if kind == 1 else contract.STATUS_CANCELLED
        if kind == 1:
            event["unit_price_cents"] = str(int(base["unit_price_cents"]) + 100)
    return event


def _invalid_events(count, first_id, referenced_products):
    """Create events with invalid quantities."""
    for i in range(count):
        yield {
            "event_id": f"E{first_id + i:012d}",
            "transaction_id": f"BAD{i:012d}",
            "event_time": f"{contract.TRANSACTION_DATE}T12:00:00Z",
            "transaction_time": f"{contract.TRANSACTION_DATE}T10:00:00Z",
            "product_id": f"P{i % referenced_products:08d}",
            "store_id": f"S{i % len(contract.REGIONS):08d}",
            "quantity": "0", "unit_price_cents": "100", "discount_bps": "0",
            "status": contract.STATUS_COMPLETED, "currency": contract.CURRENCY,
        }


def transaction_events(config):
    """Generate the configured number of transaction events."""
    bases, extras, invalid = _event_counts(config)
    event_id = extra_index = 0
    for i in range(bases):
        timestamp = START_TIME + timedelta(seconds=i * 37 % 86400)
        event = _base_event(i, config.referenced_products, timestamp)
        event["event_id"] = f"E{event_id:012d}"
        yield event
        event_id += 1
        if (i + 1) * extras // bases > i * extras // bases:
            extra = _duplicate_event(event, extra_index, timestamp)
            extra["event_id"] = f"E{event_id:012d}"
            yield extra
            event_id += 1
            extra_index += 1
    yield from _invalid_events(invalid, event_id, config.referenced_products)


def _product_rows(count):
    for i in range(count):
        yield [f"P{i:08d}", f"{PRODUCT_NAMES[i % len(PRODUCT_NAMES)]} {i}", contract.CATEGORIES[i % len(contract.CATEGORIES)]]


def _store_rows():
    for i, region in enumerate(contract.REGIONS):
        yield [f"S{i:08d}", f"Online {region}", region]


def _csv_writer(stack, path, headers):
    target = stack.enter_context(path.open("w", encoding="utf-8", newline=""))
    writer = csv.writer(target, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow(headers)
    return writer


def _write_inputs(directory, config):
    paths = [f"transactions/part-{i:05d}.csv" for i in range(config.file_count)]
    counts = dict.fromkeys(paths, 0)
    with ExitStack() as stack:
        writers = [_csv_writer(stack, directory / name, contract.EVENT_HEADERS) for name in paths]
        for i, row in enumerate(transaction_events(config)):
            shard = i % config.file_count
            writers[shard].writerow(row[name] for name in contract.EVENT_HEADERS)
            counts[paths[shard]] += 1
        products = _csv_writer(stack, directory / "products.csv", contract.PRODUCT_HEADERS)
        products.writerows(_product_rows(config.products))
        stores = _csv_writer(stack, directory / "stores.csv", contract.STORE_HEADERS)
        stores.writerows(_store_rows())
    counts.update({"products.csv": config.products, "stores.csv": len(contract.REGIONS)})
    return counts


def _create_manifest(directory, config, counts):
    inputs = {}
    for name, rows in counts.items():
        path = directory / name
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        inputs[name] = {"rows": rows, "bytes": path.stat().st_size, "sha256": digest.hexdigest()}
    manifest = {"config": asdict(config), "inputs": inputs}
    with (directory / "manifest.json").open("w", encoding="utf-8") as target:
        json.dump(manifest, target, indent=2, sort_keys=True)
        target.write("\n")
    return manifest


def generate_dataset(directory, config=PROFILES["test"]):
    if not isinstance(config, DatasetConfig):
        raise TypeError("config must be a DatasetConfig")
    directory = Path(directory)
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise ValueError("Destination must be a new or empty directory")
    (directory / "transactions").mkdir(parents=True)
    counts = _write_inputs(directory, config)
    return _create_manifest(directory, config, counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("size", choices=PROFILES)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--file-count", type=int)
    args = parser.parse_args()
    try:
        config = PROFILES[args.size]
        if args.file_count is not None:
            config = replace(config, file_count=args.file_count)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(generate_dataset(args.directory, config), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
