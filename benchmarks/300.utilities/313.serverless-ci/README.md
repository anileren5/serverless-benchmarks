# 313.serverless-ci — Serverless CI Builds (`ts-pattern` & `NestJS`)

## Overview
`313.serverless-ci` evaluates whether serverless FaaS platforms (**Apache OpenWhisk** and **AWS Lambda**) can execute realistic, multi-stage Continuous Integration (CI) build and unit-test jobs for pinned TypeScript/Node.js open-source repositories without outbound network access during invocation.

## Candidate Repositories & Workload Sizes

| Profile / Size | Repository | Tag / Commit | License | Pipeline Stages | Unit Tests |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `test` | [`gvergnaud/ts-pattern`](https://github.com/gvergnaud/ts-pattern) | `v5.0.5` (`5edaca4`) | MIT | `tsc --noEmit` + `tsc --outDir dist --declaration` + `jest tests/tuples.test.ts` | 9 passed |
| `small` | [`gvergnaud/ts-pattern`](https://github.com/gvergnaud/ts-pattern) | `v5.0.5` (`5edaca4`) | MIT | `tsc --noEmit` + `tsc --outDir dist --declaration` + full `jest --runInBand` (46 suites) | 387 passed |
| `nestjs-single` | [`nestjs/nest`](https://github.com/nestjs/nest) | `v10.3.3` (`d658942`) | MIT | `tsc -b packages/tsconfig.single.json` (`@nestjs/common`) + `mocha` on `packages/common` | 359 passed |
| `large` | [`nestjs/nest`](https://github.com/nestjs/nest) | `v10.3.3` (`d658942`) | MIT | `tsc -b packages/tsconfig.core-subset.json` (`common`, `core`, `platform-express`, `testing`) + `mocha` | 931 passed |
| `nestjs-full` | [`nestjs/nest`](https://github.com/nestjs/nest) | `v10.3.3` (`d658942`) | MIT | `tsc -b packages/tsconfig.json` (all 9 packages) + full workspace `mocha` unit suite | 1,616 passed |
| `nestjs-endurance` | [`nestjs/nest`](https://github.com/nestjs/nest) | `v10.3.3` (`d658942`) | MIT | 12-pass clean `tsc -b packages/tsconfig.json` + 4-pass workspace `mocha` unit suite (multi-minute endurance run) | 1,616 passed / pass |

## Stage-Decomposed Metrics (`measurement`)
Every invocation returns microsecond-granularity stage timings and resource high-water marks:
- `download_time`: Downloading source & dependency tarballs from S3/MinIO and verifying SHA-256 checksums.
- `extract_time`: Extracting the pinned source archive into `/tmp`.
- `deps_prep_time`: Extracting offline `node_modules` into `/tmp` (or warm cache preparation).
- `typecheck_time`: Running strict TypeScript typechecking (`tsc --noEmit`).
- `compile_time`: Emitting `.js` and `.d.ts` artifacts via `tsc` / `tsc -b`.
- `test_time`: Running Jest or Mocha unit tests (`--runInBand` / sequential).
- `upload_time`: Uploading the JSON build/test report to object storage.
- `peak_rss_bytes`: Peak RSS across the parent handler and child compiler/test processes.
- `peak_tmp_bytes`: Peak `/tmp` workspace usage in bytes.

## Generating Fixtures
Fixtures are generated deterministically via:
```bash
python3 benchmarks/300.utilities/313.serverless-ci/prepare_fixtures.py
```

## Environment Variables for Specialized Studies
- `SEBS_CI_PROFILE`: Override the profile (`test`, `small`, `nestjs-single`, `large`, `nestjs-full`, `nestjs-endurance`).
- `SEBS_CI_REUSE_CACHE=1`: Enable warm `/tmp/sebs-ci-cache` workspace reuse across warm container invocations.
- `SEBS_CI_INCREMENTAL=1`: Mutate a single TypeScript file on warm invocations to measure incremental `tsc -b` rebuild latency.
- `SEBS_CI_BUILD_PASSES=<N>`: Override the number of clean compilation passes for duration scaling.
