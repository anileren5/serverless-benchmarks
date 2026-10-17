#!/usr/bin/env bash
# Copy a finished measured report into characterization/published/.
# Refuses synthetic reports and trees that still contain credential fields.
#
#   characterization/publish.sh characterization/results/workload characterization/published/workload-small

set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: characterization/publish.sh <report-dir> <destination>" >&2
  exit 1
fi

SRC="$1"
DEST="$2"

if [[ ! -f "$SRC/report.json" ]]; then
  echo "No report.json in $SRC" >&2
  exit 1
fi

if [[ -n "${PYTHON:-}" ]]; then
  PYTHON="$PYTHON"
elif [[ -x ".venv/bin/python" ]]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="python3"
fi

"$PYTHON" - "$SRC" <<'PY'
import json, sys
from pathlib import Path
report = json.loads(Path(sys.argv[1], "report.json").read_text())
origin = report.get("provenance", {}).get("origin")
if origin != "measured":
    raise SystemExit(f"Refusing to publish origin={origin!r}. The cited run has to be measured.")
PY

if grep -R -n -E -i 'access_key|secret_key|aws_secret|aws_access_key|"password"[[:space:]]*:[[:space:]]*"[^"]+' "$SRC" >/dev/null; then
  echo "Refusing to publish $SRC because it matches a credential pattern." >&2
  exit 1
fi

mkdir -p "$DEST"
cp "$SRC/report.json" "$SRC/report.md" "$SRC/features_used.csv" "$SRC/scores.csv" "$SRC/loadings.csv" "$SRC/distances.csv" "$DEST/"
if [[ -f "$SRC/pca_scatter.pdf" ]]; then
  cp "$SRC/pca_scatter.pdf" "$SRC/pca_scatter.png" "$SRC/scree.png" "$DEST/"
fi
echo "Published $DEST"
