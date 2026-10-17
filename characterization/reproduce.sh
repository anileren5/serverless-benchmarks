#!/usr/bin/env bash
# Re-run the characterization tests, then the PCA report for a feature table.
#
#   characterization/reproduce.sh
#   characterization/reproduce.sh characterization/published/workload-small/features.csv workload characterization/results/workload

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

"$PYTHON" -m unittest characterization.tests.test_analysis

FEATURES="${1:-}"
if [[ -z "$FEATURES" ]]; then
  echo "Tests passed. Pass a features.csv to write a report."
  exit 0
fi

FEATURE_SET="${2:-workload}"
OUT="${3:-characterization/results/${FEATURE_SET}}"
PLATFORM_ARGS=()
if [[ "$FEATURE_SET" == "deployment" ]]; then
  if [[ -z "${4:-}" ]]; then
    echo "Deployment reproduction needs the platform as the fourth argument." >&2
    exit 1
  fi
  PLATFORM_ARGS=(--platform "$4")
fi

"$PYTHON" -m characterization.analyze \
  --features "$FEATURES" \
  --feature-set "$FEATURE_SET" \
  --out "$OUT" \
  --seed 0 \
  "${PLATFORM_ARGS[@]}"

echo "Report: $OUT/report.md"
