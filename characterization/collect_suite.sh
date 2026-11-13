#!/usr/bin/env bash
# Collect perf-cost measurements for the characterization suite.
#
# Dry-run by default. Pass --execute to deploy and invoke.
#   characterization/collect_suite.sh aws
#   characterization/collect_suite.sh aws --execute
#   characterization/collect_suite.sh openwhisk --execute
#   characterization/collect_suite.sh local --execute --size large
#
# --size large writes characterization/results/raw-large so the small-input
# sample in characterization/results/raw is left in place.
#
# AWS credentials come from AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.
# The checked-in config files do not contain keys.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -n "${PYTHON:-}" ]]; then
  PYTHON="$PYTHON"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="python3"
fi

PLATFORM="${1:-}"
shift || true
EXECUTE=0
SIZE="small"
ONLY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute) EXECUTE=1 ;;
    --size)
      SIZE="${2:-}"
      shift
      ;;
    --benchmark)
      ONLY="${2:-}"
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
  shift
done

if [[ "$PLATFORM" != "aws" && "$PLATFORM" != "openwhisk" && "$PLATFORM" != "local" ]]; then
  echo "Usage: characterization/collect_suite.sh {aws|openwhisk|local} [--execute] [--size small|large|test] [--benchmark ID]" >&2
  exit 1
fi
if [[ "$SIZE" != "small" && "$SIZE" != "large" && "$SIZE" != "test" ]]; then
  echo "Input size must be test, small, or large." >&2
  exit 1
fi

if [[ "$SIZE" == "small" ]]; then
  RAW="$ROOT/characterization/results/raw"
  REPS=30
else
  RAW="$ROOT/characterization/results/raw-${SIZE}"
  # The large uploader stores a fresh copy of a 98 MB object on every call.
  REPS=10
fi

if [[ "$PLATFORM" == "local" ]]; then
  echo "python -m characterization.collect_local --results $RAW --size $SIZE --repetitions $REPS --resume"
  echo "Layout: $RAW/local/<benchmark>/{papi,memory,disk-io,time,package.json}"
  echo "Needs: x86_64 Linux, docker group, kernel.perf_event_paranoid <= 1,"
  echo "       git submodules benchmarks-data and third-party/pypapi (and its papi submodule)."
  if [[ "$EXECUTE" -eq 1 ]]; then
    "$PYTHON" -m characterization.collect_local --results "$RAW" --size "$SIZE" --repetitions "$REPS" --resume
  fi
  echo "Raw tree: $RAW"
  if [[ "$SIZE" == "small" ]]; then
    FEATURES="characterization/results/features.csv"
  else
    FEATURES="characterization/results/features-${SIZE}.csv"
  fi
  echo "Next: python -m characterization.ingest --results $RAW --out $FEATURES"
  exit 0
fi

TEMPLATE="$ROOT/characterization/configs/${PLATFORM}-perf-cost.json"
if [[ ! -f "$TEMPLATE" ]]; then
  echo "Missing config template $TEMPLATE" >&2
  exit 1
fi

if [[ "$PLATFORM" == "openwhisk" && "$EXECUTE" -eq 1 ]]; then
  "$PYTHON" - "$TEMPLATE" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1]))
address = payload["deployment"]["openwhisk"]["storage"]["object"]["minio"]["address"]
if not address:
    raise SystemExit(
        "Fill deployment.openwhisk.storage in characterization/configs/openwhisk-perf-cost.json "
        "before --execute. See docs/usage.md for the storage start and jq merge."
    )
PY
fi

if [[ "$EXECUTE" -eq 1 ]]; then
  mkdir -p "$RAW"
  "$PYTHON" - "$RAW/manifest.json" "$PLATFORM" "$SIZE" <<'PY'
import json, sys
from pathlib import Path
dest = Path(sys.argv[1])
platform = sys.argv[2]
manifest = {}
if dest.is_file():
    manifest = json.loads(dest.read_text())
manifest.update({"language": "python", "input_size": sys.argv[3], "memory_mb": 2048, "origin": "measured"})
platforms = set(manifest.get("platforms", []))
platforms.add(platform)
manifest["platforms"] = sorted(platforms)
dest.write_text(json.dumps(manifest, indent=2) + "\n")
PY
fi

while IFS= read -r BENCH; do
  if [[ -n "$ONLY" && "$BENCH" != "$ONLY" ]]; then
    continue
  fi
  OUT="$RAW/$PLATFORM/$BENCH"
  echo "# $PLATFORM $BENCH -> $OUT"
  echo "sebs experiment invoke perf-cost --config $TEMPLATE --output-dir $OUT"
  echo "sebs experiment process perf-cost --config $TEMPLATE --output-dir $OUT"
  echo "# benchmark field rewritten to $BENCH, input size $SIZE, at execute time"
  if [[ "$EXECUTE" -eq 1 ]]; then
    CONFIG="$(mktemp "${TMPDIR:-/tmp}/sebs-${PLATFORM}.XXXXXX.json")"
    "$PYTHON" - "$TEMPLATE" "$CONFIG" "$BENCH" "$SIZE" "$PLATFORM" "$REPS" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1]))
benchmark, size, platform, reps = sys.argv[3], sys.argv[4], sys.argv[5], int(sys.argv[6])
perf = payload["experiments"]["perf-cost"]
perf["benchmark"] = benchmark
perf["input-size"] = size
perf["repetitions"] = reps
# Stay under the OpenWhisk blocking wait (360s) and the action cap (300s).
timeouts = {
    "010.sleep": 300,
    "110.dynamic-html": 120,
    "120.uploader": 180,
    "130.crud-api": 60,
    "220.video-processing": 180,
    "501.graph-pagerank": 180,
    "502.graph-mst": 180,
    "503.graph-bfs": 180,
}
if size == "large" and benchmark in timeouts:
    perf["timeout"] = timeouts[benchmark]
json.dump(payload, open(sys.argv[2], "w"), indent=2)
PY
    if "$PYTHON" - "$OUT" "$REPS" <<'PY'
import json, sys
from pathlib import Path
out = Path(sys.argv[1]) / "perf-cost"
expected = int(sys.argv[2])
for name in ("cold_results_2048-processed.json", "warm_results_2048-processed.json"):
    path = out / name
    if not path.is_file():
        raise SystemExit(1)
    stats = json.loads(path.read_text()).get("statistics") or {}
    # A larger finished sample, including the copied size-invariant benchmarks, still counts.
    if int(stats.get("samples_generated") or 0) < expected or stats.get("failures_count") != 0:
        raise SystemExit(1)
PY
    then
      echo "$BENCH already has at least $REPS cold and $REPS warm, skipping"
    else
      sebs experiment invoke perf-cost --config "$CONFIG" --output-dir "$OUT"
      sebs experiment process perf-cost --config "$CONFIG" --output-dir "$OUT"
      "$PYTHON" - "$ROOT" "$PLATFORM" "$BENCH" "$OUT" <<'PY'
import json, subprocess, sys
from pathlib import Path
root, platform, bench, out = sys.argv[1:]
sys.path.insert(0, root)
from characterization.ingest import container_image_uri, package_size_for_deployment
cache_path = Path(root) / "cache" / bench / "config.json"
if not cache_path.is_file():
    raise SystemExit(0)
cache = json.loads(cache_path.read_text())
size = None
uri = container_image_uri(cache, platform)
if uri:
    probe = subprocess.run(
        ["docker", "image", "inspect", "-f", "{{.Size}}", uri],
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0 and probe.stdout.strip().isdigit():
        size = int(probe.stdout.strip())
if size is None:
    size = package_size_for_deployment(cache, platform)
if size is None:
    raise SystemExit(0)
Path(out, "package.json").write_text(json.dumps({"package_size_bytes": size}) + "\n")
print(f"package_size_bytes={size}")
PY
    fi
    rm -f "$CONFIG"
  fi
done < <("$PYTHON" -m characterization.suite)

echo "Raw tree: $RAW"
if [[ "$SIZE" == "small" ]]; then
  FEATURES="characterization/results/features.csv"
else
  FEATURES="characterization/results/features-${SIZE}.csv"
fi
echo "Next: python -m characterization.ingest --results $RAW --out $FEATURES"
