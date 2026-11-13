#!/bin/bash

# Adds the ResNet-50 v1 model of the ONNX model zoo to the function package.
# The download is cached and verified, since builds run often and the file is 98 MB.
set -euo pipefail

DIR=$1
MODEL=resnet50-v1-7.onnx
URL=https://github.com/onnx/models/raw/main/validated/vision/classification/resnet/model/${MODEL}
SHA256=af16a04a6ec48ac494065d4439fe9dea590d337b9ca6dc328160ccf04a217b9c
CACHE=${XDG_CACHE_HOME:-${HOME}/.cache}/sebs/412.image-recognition-onnx

mkdir -p "${CACHE}"
if ! echo "${SHA256}  ${CACHE}/${MODEL}" | sha256sum --check --status 2>/dev/null; then
  curl --fail --silent --show-error --location --output "${CACHE}/${MODEL}.part" "${URL}"
  echo "${SHA256}  ${CACHE}/${MODEL}.part" | sha256sum --check --status
  mv "${CACHE}/${MODEL}.part" "${CACHE}/${MODEL}"
fi
cp "${CACHE}/${MODEL}" "${DIR}"
