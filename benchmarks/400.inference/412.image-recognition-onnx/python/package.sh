#!/bin/bash

# Keeps the package below the 250 MB limit of Lambda zip deployments (the model is 98 MB).
# onnxruntime depends on sympy, coloredlogs, and its offline tools, none of which inference imports.
PACKAGE_DIR=${1:-}
# The OpenWhisk image runs this script before installing dependencies, without a directory.
if [[ -z "${PACKAGE_DIR}" ]]; then
  exit 0
fi
echo "Original size $(du -sh "${PACKAGE_DIR}" | cut -f1)"

cd "${PACKAGE_DIR}"
rm -rf sympy* mpmath* humanfriendly* coloredlogs*
rm -rf onnxruntime/tools onnxruntime/transformers onnxruntime/quantization
find . -type d -name tests -prune -exec rm -rf {} +
find . -name "*.so*" -type f -exec strip {} + 2>/dev/null || true
find . -name "*.pyc" -delete

echo "Stripped size $(du -sh "${PACKAGE_DIR}" | cut -f1)"
