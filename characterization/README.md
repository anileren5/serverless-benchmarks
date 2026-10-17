# Characterization pipeline

This directory is the reproducible analysis for the SPEC-style study described in `ROADMAP.md`. The statistics live here. The study design and the claims we are willing to make live in the roadmap.

## Layout

| Path | Role |
| --- | --- |
| `suite.py` | The 12 benchmarks, the 2048 MB iso-memory setting, input size `small`, 30 repetitions |
| `features.py` | Workload columns vs deployment columns, z-scoring, row filters |
| `ingest.py` | PAPI, memory, disk I/O, local time, and SeBS perf-cost JSON to one CSV |
| `pca.py` | SVD PCA, Kaiser cutoff, 80% variance cutoff |
| `cluster.py` | BIC k-means, average linkage, redundancy pairs, empty PC1–PC2 cells |
| `analyze.py` | Report writer CLI |
| `configs/` | AWS and OpenWhisk perf-cost templates. No credentials |
| `results/` | Local runs. Gitignored |
| `published/` | Measured reports that the paper cites. Committed after `publish.sh` |

## Run the tests and rebuild a report

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r characterization/requirements.txt
characterization/reproduce.sh path/to/features.csv workload
```

`reproduce.sh` with no CSV runs the unit tests and stops. The tests construct a matrix with two known groups and check that the clustering recovers them. They also check the SeBS file formats, including the disk-I/O header written by `scripts/proc_analyzer.py`, whose column names do not match the row layout.

## Collect

Cloud collection is dry-run until `--execute`:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
characterization/collect_suite.sh aws
characterization/collect_suite.sh aws --execute
python -m characterization.ingest --results characterization/results/raw --out characterization/results/features.csv
python -m characterization.analyze --features characterization/results/features.csv --feature-set workload --out characterization/results/workload
python -m characterization.analyze --features characterization/results/features.csv --feature-set deployment --platform aws --out characterization/results/aws
```

OpenWhisk needs the storage object in `configs/openwhisk-perf-cost.json` filled in first, and a per-function memory cap of at least 2048 MB. The template uses container images because the OpenWhisk zip limit is 48 MB.

Local PAPI collection is described in `ROADMAP.md`. `scripts/run_experiments.py` still imports `CodePackage`, which this tree no longer exports, so `collect_suite.sh local --execute` exits with status 2 instead of launching a broken driver.

## Publish a measured run

```bash
characterization/publish.sh characterization/results/workload characterization/published/workload-small
```

The script copies the report, the feature matrix, scores, loadings, and distances. It refuses a synthetic origin and refuses a tree that still matches credential field names.
