# Retail transaction ETL

## Dataset generation

Inputs are UTF-8 CSVs with headers and fully quoted fields. Timestamps use
`YYYY-MM-DDTHH:MM:SSZ`. Each event is a full transaction snapshot.

| File | Columns, in order |
| --- | --- |
| `transactions/*.csv` | `event_id, transaction_id, event_time, transaction_time, product_id, store_id, quantity, unit_price_cents, discount_bps, status, currency` |
| `products.csv` | `product_id, product_name, category` |
| `stores.csv` | `store_id, store_name, region` |

| Profile | Events | Transaction CSVs | Products |
| --- | ---: | ---: | ---: |
| test | 1,000 | 4 | 8 |
| small | 10,000 | 8 | 1,000 |
| medium | 30,000 | 16 | 3,000 |
| large | 100,000 | 32 | 10,000 |

The same generator and configuration produce identical inputs. Transaction times
are on 2025-01-01. Reference data contains four north/south/east/west stores.
Base snapshots have completed or pending statuses, including padded lowercase
values such as `" completed "`, and currency `USD`.

Each profile includes 1% events with `quantity="0"`, the generator's only
invalid-event case. Of the remaining events, 20% are additional snapshots sharing
a base transaction ID but carrying distinct event IDs. These cycle through older
pending snapshots, later completed updates with a 100-cent price increase, and
later cancellations. Relative to transaction time, base event times are one hour
later. Older snapshots use transaction time itself, and updates/cancellations are
two hours later.

`--file-count` changes only sharding, preserving event count and contents.
`manifest.json` records `config` and `inputs`, mapping CSV paths to `rows`,
`bytes`, and `sha256`. It contains no expected output.

## ETL transformation

The logical operations are:

1. Read and normalize

   Read transaction shards as `events`, `products.csv` as
   `products`, and `stores.csv` as `stores`, with all CSV fields initially text.
   Empty fields become empty strings. Trim spaces, tabs, CR and LF from event
   fields and uppercase ASCII letters in status and currency. For example,
   `" completed "` becomes `COMPLETED`.

2. Filter invalid events

   Require the contract's ID formats: `E` plus 12 digits
   for events, `P`/`S` plus eight digits for products/stores, and 1–32 uppercase
   letters, digits, underscores or hyphens for transaction IDs. Parse timestamps
   in the documented format and require `transaction_time <= event_time`.
   Numeric fields must contain digits and fit their ranges: quantity 1–1000,
   unit price 0–10,000,000 cents, and discount 0–10,000 basis points. Currency must
   be `USD`, and status must be `PENDING`, `COMPLETED` or `CANCELLED`. Generated
   zero-quantity events fail here. Pending and cancelled events remain valid.

3. Select the latest snapshot

   Keep one valid row per `transaction_id`, ordered
   by descending `event_time`, then descending `event_id` to break time ties.
   File order does not determine the selected snapshot.

4. Filter status

   Keep only selected `COMPLETED` snapshots. A latest pending
   snapshot or cancellation produces no output. Older completed rows are not used.

5. Join reference data

   Inner-join selected transactions to `products` on
   `product_id` and to `stores` on `store_id`, adding product name/category and
   store name/region. Unmatched selected snapshots are dropped without falling
   back to older ones. Derive `transaction_date` from `transaction_time`.

6. Calculate money

   Use integer cents:

   ```text
   gross_cents = quantity * unit_price_cents
   discount_cents = (gross_cents * discount_bps + 5000) // 10000
   total_cents = gross_cents - discount_cents
   ```

   Here `//` is integer division: discounts round to the nearest cent, with
   half-cent ties rounded up.

7. Write Parquet

   Write ZSTD-compressed files partitioned by
   `transaction_date=YYYY-MM-DD/region=REGION/`, retaining partition columns in
   each file. SeBS returns fragments as `{path, key}` in `result.files`. Empty
   output has an empty file list.

This is the logical order. DuckDB may choose a different physical execution plan,
including pushing filters down or reordering joins.

Output columns are the transaction columns above, followed by
`product_name, category, store_name, region, transaction_date, gross_cents, discount_cents, total_cents`.
`quantity`, `unit_price_cents`, `discount_bps`, `gross_cents`, `discount_cents`
and `total_cents` are `BIGINT`. `event_time` and `transaction_time` are
`TIMESTAMP`, `transaction_date` is `DATE`, and all other columns are `VARCHAR`.

The independent validator checks input integrity, recomputes expected rows in
Python without workload SQL, and compares exact output rows, schemas and partitions.

## Running

From the repository root, with SeBS and DuckDB 1.4.2 installed in `.venv`:

```bash
source .venv/bin/activate
cd benchmarks/300.utilities/312.duckdb-csv
DATA_DIR=$(mktemp -d /tmp/retail-demo.XXXXXX)
python -m python.generator test "$DATA_DIR/input" --file-count 8
python -m python.etl_workload "$DATA_DIR/input" "$DATA_DIR/output" --memory-mb 1024
python -m validation "$DATA_DIR/input" "$DATA_DIR/output"
```

Destinations must be new or empty. For SeBS file-count overrides, create
`benchmarks-data/300.utilities/312.duckdb-csv/retail-config.json` with, for example,
`{"test": {"file_count": 8}}`. Use `--update-storage` after changing it. Preparation
generates all four profiles and submits their files to the storage uploader.
`sebs benchmark invoke 312.duckdb-csv test --validate` checks successful
invocations outside their measurements, including file mappings and audit counts.
Supply the appropriate SeBS deployment configuration when invoking it.

## SeBS runtime settings

Optionally create `benchmarks-data/300.utilities/312.duckdb-csv/retail-runtime.json`
in the benchmark's SeBS data directory:

```json
{
  "resources": {"threads": 2, "memory_limit": "256MB"},
  "profile": false
}
```

| Parameter | Meaning | Default |
| --- | --- | --- |
| `resources.threads` | DuckDB workers (positive integer). | Allocation / 1769 MiB, rounded up, capped at six. |
| `resources.memory_limit` | DuckDB budget (nonempty memory-size string checked by DuckDB). | 60% of allocation. |
| `profile` | Enable COPY profiling (boolean). | `false`. |

All fields are optional. Unknown keys are rejected. Allocation comes from Lambda
or, elsewhere, `SEBS_MEMORY_MB` (default 1024). The runtime file controls DuckDB's
budget, not Lambda's allocation.

The input hook reads the file on each preparation and puts `resources` and
`profile` in the invocation payload, separate from dataset configuration,
manifests and dataset hashes. The same payload reaches warm-ups and measured
calls. Changing only this file needs no `--update-code` or `--update-storage`.
Preparation still generates all four datasets. Existing AWS storage caching
skips their uploads when the storage cache is valid and replacement is disabled.
Dataset changes through `retail-config.json` still require `--update-storage`.
Changes to generator content also require a storage refresh: SeBS's storage
cache does not compare dataset hashes or file contents before skipping uploads.

SeBS uses `retail-runtime.json` to prepare invocation settings but does not
automatically save the file with results. A copy can optionally be preserved
alongside experiment configurations and raw results in a separate output directory
for each setting, with `{}` documenting default settings. Invocation results also
report effective runtime settings, plus profiling measurements when profiling is
enabled.

## Measurements and offline checks

Handler duration fields are in microseconds. `total_time` covers the handler,
including download, compute, upload and temporary-directory cleanup. It excludes
module imports and provider startup. `compute_time` includes SQL setup, query
execution and connection shutdown. `sql_setup_time` and `query_execution_time`
are components of it, not additional phases. Profiling covers the Parquet COPY
query and adds overhead, so keep profiled diagnostics separate from timing runs.
DuckDB's memory limit is not a limit on whole-process memory.

From the repository root, run the existing offline tests:

```bash
(cd benchmarks/300.utilities/312.duckdb-csv && ../../../.venv/bin/python -B -m unittest discover -s tests -t . -v)
```

SeBS experiments select the workflow with the positional argument, for example
`sebs experiment invoke perf-cost --config /path/to/duckdb-experiment.json`.
For an AWS deployment, that command is not an offline check.
