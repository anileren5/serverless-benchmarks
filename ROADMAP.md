# SPEC-style characterization and redundancy analysis of SeBS

This study characterizes the SeBS function suite the way Phansalkar, Joshi, and John characterized SPEC CPU2006: measure how the programs behave, project those measurements with principal component analysis (PCA), and use the resulting space to find redundant benchmarks and regions the suite does not cover.

The paper claims are about the workloads. Platform effects (cold start, gateway latency, Lambda CPU scaling with memory) are measured separately and are not folded into the same PCA as the hardware-counter features.

## Question

Which SeBS benchmarks exercise different CPU, memory, I/O, network, packaging, and startup behavior, which ones repeat each other, and which combinations of those behaviors does the suite leave empty?

## Suite

Python is the only language that implements every application benchmark, so it is the primary characterization language. Node.js and C++ are a later sensitivity check on the benchmarks that exist in those languages.

| Id | Benchmark | Why it is in the suite |
| --- | --- | --- |
| 010 | sleep | Near-idle control. Anchors the low-compute corner. |
| 110 | dynamic-html | Small CPU, string and template work, no storage. |
| 120 | uploader | Network download plus object-storage write. |
| 130 | crud-api | Small CPU plus NoSQL round trips. |
| 210 | thumbnailer | Image decode and resize. |
| 220 | video-processing | Heavier media I/O and CPU. |
| 311 | compression | Read many objects, compress, write one object. |
| 411 | image-recognition | Model load, floating-point inference, large package. |
| 501 | graph-pagerank | Graph kernel, likely close to the other graph codes. |
| 502 | graph-mst | Graph kernel. |
| 503 | graph-bfs | Graph kernel. |
| 504 | dna-visualisation | Scientific code with a large memory setting (2048 MB). |

Internal helpers `020.network-benchmark`, `030.clock-synchronization`, and `040.server-reply` stay out of the suite PCA. `020` is still run on its own when we want a pure network-latency profile of a platform.

Input size for the reported run is `small`. `test` is only a smoke size. `large` is a sensitivity run after the `small` matrix is stable, because video, inference, and DNA can approach provider timeouts.

## Where each metric comes from

Hardware counters exist on the local Docker deployment. PAPI is wired through `dockerfiles/local/python/papi-runner.py` and the event sets in `scripts/run_experiments.py`. AWS Lambda and OpenWhisk do not expose those counters. Cloud runs supply the metrics that only a real FaaS platform can: provider initialization time, provider execution time, billed memory, and client-visible latency.

| Behavior | Primary source | What goes into the workload PCA |
| --- | --- | --- |
| CPU mix and intensity | Local PAPI | IPC, load/store fraction, branch fraction, L1/L2/L3 misses per instruction, stall fraction, single- and double-precision FLOPs per instruction |
| Memory | Local `/proc` sampler (`scripts/proc_analyzer.py`) and container cgroup samples | Peak RSS, PSS, and USS |
| Disk I/O | Local `/proc/<pid>/io` | Bytes and syscalls read and written during the run |
| CPU vs wait | Local `time-in-proc` (user, system, wall) | `(user + system) / wall`. Absolute wall time stays out of the PCA because it is machine-specific speed, not program shape |
| Package size | SeBS code-package `size` for the Python zip built for local and AWS | Bytes of the deployment artifact |
| Initialization vs execution | AWS `Init Duration` and OpenWhisk init annotations, plus SeBS `times.benchmark` and `provider_times` | Used in the deployment analysis, not in the workload PCA |
| Network | Client time minus provider time, and `measurement.download_time` / `upload_time` when the benchmark records them | Deployment analysis. Bytes moved by storage benchmarks also show up in the local I/O counters |

Ratios use counters from the same PAPI event-set run. IPC is `PAPI_TOT_INS / PAPI_TOT_CYC` from the cycles run. Miss rates use `PAPI_TOT_INS` from the cache run. That avoids mixing instruction totals from different processes.

`scripts/run_experiments.py` still lists these experiments, and it still imports `CodePackage`, which is no longer exported (`sebs/__init__.py` exports `Benchmark`). Local counter collection is blocked on a driver that uses the current package API and the existing PAPI runner image. Cloud collection is not blocked: `sebs experiment invoke perf-cost` is the supported path.

Collection runs on Linux. PAPI and the privileged perf counters the local images expect are not available from macOS Docker. Analysis of a finished feature table runs on any machine with the Python packages in `characterization/requirements.txt`.

## Protocol choices that keep the comparison interpretable

These are easy to get wrong, and they change the paper if they are silent.

- One memory size on every cloud run: **2048 MB**. That is the minimum that covers `504.dna-visualisation`. On Lambda, memory allocation changes the vCPU share, so a benchmark run at 128 MB is not comparable with one run at 2048 MB. OpenWhisk's default per-function cap is often 512 MB and has to be raised before this protocol (`docs/platforms.md`).
- One invocation at a time (`concurrent-invocations: 1`). Burst and sequential experiments measure platform scaling. They are out of this PCA.
- Cold and warm are separate. SeBS already drops warm samples inside a cold experiment and the reverse. Initialization time is the provider field (`provider_times.initialization`, microseconds). The init fraction is `init / (init + provider execution)` on cold invocations, so both terms come from the provider clock.
- The workload PCA is computed on local rows only. A table that also contains AWS or OpenWhisk rows is filtered to `platform=local` before that analysis. The deployment PCA requires an explicit platform when more than one is present.
- Incomplete columns are dropped. Missing counters are left missing rather than imputed, because an imputed IPC would be a fictional program.
- Size-like columns (bytes, resident memory, package size, microseconds) can be re-run with `log1p` as a sensitivity. The reported analysis uses the raw values after z-scoring, which is the closer match to the SPEC study.

## PCA and redundancy method

The method follows Phansalkar, Joshi, and John, "Analysis of Redundancy and Application Balance in the SPEC CPU2006 Benchmark Suite" (ISCA 2007), and the earlier similarity method in Phansalkar et al. (ISPASS 2005).

1. Build one row per benchmark. Cloud timings use the median over repetitions. Counter totals are summed across PAPI samples, then turned into ratios.
2. Drop constant columns and columns that are missing for any remaining benchmark.
3. Z-score each column with the sample standard deviation (the correlation matrix).
4. PCA by singular value decomposition. Component sign is fixed so the largest-magnitude loading on that component is positive. Signs of principal components are otherwise arbitrary, and unfixed signs make two runs of the same matrix look different.
5. Keep components with eigenvalue greater than 1 (Kaiser). This is the primary space used for clustering. Also cut at 80% cumulative variance and compare the two clusterings with the Rand index. A low Rand index means the redundancy conclusion depends on the cutoff and has to be stated that way.
6. K-means in the retained space. The number of clusters is the `k` with the highest Bayesian information criterion under a spherical Gaussian with a shared variance (Pelleg and Moore, X-means, 2000). The variance is floored at 0.1% of the total variance so a near-zero residual cannot make one cluster per benchmark look optimal. `k` is searched from 1 to `min(6, n - 1)` with k-means++ and a fixed seed. Ties prefer the smaller `k`.
7. Average-linkage hierarchical clustering (Euclidean) is cut at the same `k`. Agreement with k-means is reported. Phansalkar et al. use both the cluster view and the distance view; a pair that only appears in one of them is not described as redundant.
8. A pair is called redundant only when `k >= 2`, both members fall in the same k-means cluster, and their distance is at or below the 25th percentile of all pairwise distances. If BIC selects a single cluster, the report lists nearest neighbors and does not declare the whole suite redundant.
9. Empty regions are interior cells of a 4-by-4 grid over the PC1–PC2 bounding box that contain no benchmark. Each empty cell is described by the loadings of the side of PC1 and PC2 it sits on, so the hole is a statement about program behavior ("high stall fraction and low FLOPs per instruction") rather than a statement about a plot quadrant.

PAPI counters are microarchitecture-dependent. Phansalkar et al. preferred microarchitecture-independent metrics for that reason. We use ratios rather than raw event counts, and the report records the host CPU from `/proc/cpuinfo` or `lscpu` in the run manifest. Repeating the local collection on a second CPU is the check that a redundancy pair is a property of the programs.

## What the paper can show

Figures and tables the pipeline writes:

- Scree plot and a table of variance explained, eigenvalues, and the Kaiser cutoff.
- Loading table (feature by component), which is the text that says what each axis means.
- PC1–PC2 scatter with one point per benchmark.
- Cluster membership, BIC curve, and the k-means vs hierarchical Rand index.
- Nearest-neighbor table and, when the rule above fires, the redundant pairs.
- Empty-cell table with the loading interpretation.
- A second, smaller PCA per cloud platform on init fraction, cold and warm provider time, client time, provider memory, and package size.
- Side-by-side cold init vs warm execution, which is the serverless-specific plot SPEC does not have.

Hypotheses the measurements are set up to test. They are hypotheses until a measured feature table is published under `characterization/published/`.

- The three graph kernels sit close together on the CPU ratios and are the most likely redundancy group. Memory and I/O may still separate BFS from PageRank.
- `411.image-recognition` is an outlier on floating-point intensity, resident memory, and package size. If the suite has a sparse region, it is likely between this point and the web apps.
- `010.sleep` and `110.dynamic-html` occupy the low-compute corner. A hole between that corner and the scientific codes means the suite jumps from trivial handlers to heavy kernels.
- `120.uploader`, `130.crud-api`, and `311.compression` should separate from the CPU kernels on I/O and on `(user + system) / wall`, even when their IPC looks ordinary.
- Package size and init fraction should reorder similarity relative to the CPU PCA. Two kernels that match on IPC can differ sharply in cold-start cost. That comparison is the deployment PCA, and it is the result that is specific to FaaS.
- OpenWhisk's historical 48 MB zip limit makes packaging itself a finding. Container deployment is the configuration that can actually run the larger benchmarks; the report records any benchmark that fails to deploy.

## Reproducing the results

From a clone of this branch, on a machine that can install Python packages:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r characterization/requirements.txt
python -m unittest characterization.tests.test_analysis
```

The unit tests build a feature matrix with two known groups and check that PCA, BIC, and the redundancy rule recover those groups. They also check that PAPI ratios, the shifted disk-I/O header written by `scripts/proc_analyzer.py`, and SeBS perf-cost JSON are parsed into the columns above. A passing test run means the statistics match this document. It does not mean a cloud measurement has been taken.

Measured runs:

```bash
# AWS. Credentials from the environment (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY).
# Dry-run prints the sebs commands. --execute deploys functions and spends quota.
characterization/collect_suite.sh aws
characterization/collect_suite.sh aws --execute

# OpenWhisk, after storage is filled in characterization/configs/openwhisk-perf-cost.json
# and the function memory cap is at least 2048 MB.
characterization/collect_suite.sh openwhisk --execute

# Turn a finished results tree into the feature table and the report.
python -m characterization.ingest --results characterization/results/raw --out characterization/results/features.csv
python -m characterization.analyze --features characterization/results/features.csv --feature-set workload --out characterization/results/workload
python -m characterization.analyze --features characterization/results/features.csv --feature-set deployment --platform aws --out characterization/results/aws
```

`characterization/reproduce.sh` runs the tests and then the analyze step on a feature CSV.

Raw results stay in `characterization/results/` and are gitignored. After a run is the one cited in the paper:

```bash
characterization/publish.sh characterization/results/workload characterization/published/workload-small
```

`publish.sh` copies the report, the feature table, scores, and loadings. It refuses the copy when the report is marked synthetic, and it refuses the copy when the tree still contains credential fields (`access_key`, `secret_key`, `AWS_SECRET`, `password`). Reviewers regenerate figures from the published CSV with `characterization/reproduce.sh`.

Each report records the git revision, the analysis seed (`0`), the feature set, the retention rule, the Python package versions, and whether the table was measured or synthetic.

## Work plan

1. **Analysis pipeline (this branch, done with the tests).** Feature schema, ingest of PAPI CSV, memory CSV, disk-I/O CSV, local time CSV, and SeBS perf-cost JSON, PCA, BIC k-means, hierarchical check, empty regions, markdown report.
2. **Repair local counter collection.** Replace the `CodePackage` driver in `scripts/run_experiments.py` with the current `Benchmark` API, keep the existing PAPI event sets and runner image, and write one directory per benchmark in the layout `ingest` already reads. Confirm on a Linux host that `PAPI_TOT_INS` and `PAPI_TOT_CYC` increment inside the container.
3. **Shakeout, then the reported sample size.** Three repetitions of `110.dynamic-html` and `501.graph-pagerank` on local, AWS, and OpenWhisk. Then the full suite at 30 cold and 30 warm invocations, input size `small`, 2048 MB, concurrency 1.
4. **Publish the measured tables** with `publish.sh`, write the loading interpretation into the paper from `report.md`, and run the `log1p` and 80% variance sensitivities. A redundancy pair that disappears under either sensitivity is reported as unstable.
5. **Optional second host** for the local PCA, and a Node.js rerun of the overlapping benchmarks, to see whether a redundancy pair is specific to CPython on one CPU.

## Cost and safety

AWS commands are dry-run unless `--execute` is passed. Credentials stay in the environment or in an untracked local config. The checked-in JSON files have empty credential fields. Function URLs on AWS are created with auth type `NONE`, which is the SeBS default and makes the URL public; `sebs resource clean` removes the functions after the run. OpenWhisk storage keys in a filled-in config must not be committed.
