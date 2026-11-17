import csv
import hashlib
import json
from pathlib import Path

from python.contract import EVENT_HEADERS, PRODUCT_HEADERS, STORE_HEADERS


def write_dataset(directory, events):
    directory = Path(directory)
    (directory / "transactions").mkdir(parents=True)
    products = [["P00000000", "Mug", "home"], ["P00000001", "Shirt", "apparel"]]
    stores = [["S00000000", "Online North", "north"], ["S00000001", "Online South", "south"]]
    inputs = {}
    for name, headers, rows in (("transactions/part-00000.csv", EVENT_HEADERS, events),
                                ("products.csv", PRODUCT_HEADERS, products), ("stores.csv", STORE_HEADERS, stores)):
        with (directory / name).open("w", encoding="utf-8", newline="") as target:
            writer = csv.writer(target, quoting=csv.QUOTE_ALL, lineterminator="\n")
            writer.writerow(headers)
            writer.writerows([row[h] for h in headers] if isinstance(row, dict) else row for row in rows)
        content = (directory / name).read_bytes()
        inputs[name] = {"rows": len(rows), "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    manifest = {"config": {"fixture": True}, "inputs": inputs}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


def event(transaction_id, **changes):
    row = dict(zip(EVENT_HEADERS, ["", transaction_id, "2025-01-02T12:00:00Z", "2025-01-01T10:00:00Z",
                                   "P00000000", "S00000000", "2", "1999", "500", "COMPLETED", "USD"]))
    row.update(changes)
    return row


def sample_events():
    rows = [
        event("NORMAL"),
        event("UPDATE"),
        event("UPDATE", event_time="2025-01-03T12:00:00Z", transaction_time="2025-01-02T23:59:59Z",
              product_id="P00000001", store_id="S00000001", quantity="3"),
        event("CANCEL"), event("CANCEL", event_time="2025-01-02T13:00:00Z", status="CANCELLED"),
        event("INVALID", quantity="0"),
        event("UNKNOWN_PRODUCT", product_id="P99999999"),
        event("UNKNOWN_STORE", store_id="S99999999"),
        event("PENDING", status="PENDING"),
        event("ROUND", quantity="1", unit_price_cents="1", discount_bps="5000"),
        event("TIE"), event("TIE", unit_price_cents="2501"),
        event("NO_FALLBACK"), event("NO_FALLBACK", event_time="2025-01-02T13:00:00Z", product_id="P99999999"),
    ]
    for i, row in enumerate(rows):
        row["event_id"] = f"E{i:012d}"
    return rows
