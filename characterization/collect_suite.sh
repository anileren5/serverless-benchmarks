#!/usr/bin/env bash
# Collect perf-cost measurements for the characterization suite.
#
# Dry-run by default. Pass --execute to deploy and invoke.
#   characterization/collect_suite.sh aws
#   characterization/collect_suite.sh aws --execute
#   characterization/collect_suite.sh openwhisk --execute
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
EXECUTE=0
if [[ "${2:-}" == "--execute" ]]; then
  EXECUTE=1
fi

if [[ "$PLATFORM" != "aws" && "$PLATFORM" != "openwhisk" && "$PLATFORM" != "local" ]]; then
  echo "Usage: characterization/collect_suite.sh {aws|openwhisk|local} [--execute]" >&2
  exit 1
fi

if [[ "$PLATFORM" == "local" ]]; then
  cat <<'EOF'
Local hardware-counter collection is not runnable from this script yet.

scripts/run_experiments.py still imports sebs.CodePackage. The current package
exports Benchmark, so that driver fails before it starts the PAPI container.
The runner image and event sets are unchanged (dockerfiles/local/python/papi-runner.py,
inscount / flops / cache / cycles). The next collection task is a driver that
uses Benchmark and writes:

  characterization/results/raw/local/<benchmark>/{papi,memory,disk-io,time,package.json}

Analysis of a finished tree already works:
  python -m characterization.ingest --results characterization/results/raw --out characterization/results/features.csv
EOF
  if [[ "$EXECUTE" -eq 1 ]]; then
    exit 2
  fi
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

RAW="$ROOT/characterization/results/raw"
if [[ "$EXECUTE" -eq 1 ]]; then
  mkdir -p "$RAW"
  "$PYTHON" - "$RAW/manifest.json" "$PLATFORM" <<'PY'
import json, sys
from pathlib import Path
dest = Path(sys.argv[1])
platform = sys.argv[2]
manifest = {}
if dest.is_file():
    manifest = json.loads(dest.read_text())
manifest.update({"language": "python", "input_size": "small", "memory_mb": 2048, "origin": "measured"})
platforms = set(manifest.get("platforms", []))
platforms.add(platform)
manifest["platforms"] = sorted(platforms)
dest.write_text(json.dumps(manifest, indent=2) + "\n")
PY
fi

while IFS= read -r BENCH; do
  OUT="$RAW/$PLATFORM/$BENCH"
  echo "# $PLATFORM $BENCH -> $OUT"
  echo "sebs experiment invoke perf-cost --config $TEMPLATE --output-dir $OUT"
  echo "sebs experiment process perf-cost --config $TEMPLATE --output-dir $OUT"
  echo "# benchmark field rewritten to $BENCH at execute time"
  if [[ "$EXECUTE" -eq 1 ]]; then
    CONFIG="$(mktemp "${TMPDIR:-/tmp}/sebs-${PLATFORM}.XXXXXX.json")"
    "$PYTHON" - "$TEMPLATE" "$CONFIG" "$BENCH" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1]))
payload["experiments"]["perf-cost"]["benchmark"] = sys.argv[3]
json.dump(payload, open(sys.argv[2], "w"), indent=2)
PY
    sebs experiment invoke perf-cost --config "$CONFIG" --output-dir "$OUT"
    sebs experiment process perf-cost --config "$CONFIG" --output-dir "$OUT"
    rm -f "$CONFIG"
    "$PYTHON" - "$ROOT" "$PLATFORM" "$BENCH" "$OUT" <<'PY'
import json, sys
from pathlib import Path
root, platform, bench, out = sys.argv[1:]
sys.path.insert(0, root)
from characterization.ingest import package_size_for_deployment
cache = Path(root) / "cache" / bench / "config.json"
if not cache.is_file():
    raise SystemExit(0)
size = package_size_for_deployment(json.loads(cache.read_text()), platform)
if size is None:
    raise SystemExit(0)
Path(out, "package.json").write_text(json.dumps({"package_size_bytes": size}) + "\n")
    print(f"package_size_bytes={size}")
PY
  fi
done < <("$PYTHON" -m characterization.suite)

echo "Raw tree: $RAW"
echo "Next: python -m characterization.ingest --results $RAW --out characterization/results/features.csv"
