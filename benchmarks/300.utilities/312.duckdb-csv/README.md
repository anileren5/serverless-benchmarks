# 312.duckdb-csv - Retail Transaction ETL

**Type:** Utilities
**Languages:** Python
**Architecture:** x64

## Description

The benchmark processes synthetic retail transaction events using DuckDB. It downloads CSV transaction shards and product/store reference tables from object storage, cleans and deduplicates the events, joins reference data, and calculates monetary amounts. Results are uploaded as partitioned Parquet files. The response lists every output path and storage key alongside audit counts, resource settings, and phase measurements.

See [WORKLOAD.md](WORKLOAD.md) for the input/output layout, runtime configuration, and offline test commands. The Python implementation pins DuckDB 1.4.2.
