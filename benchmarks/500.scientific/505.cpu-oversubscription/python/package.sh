#!/bin/bash

# Runs inside the build image, with the function package mounted at /mnt/function.
# Function runtimes (local run image, AWS Lambda) do not ship libgomp,
# so we bundle it next to the kernel library and resolve it through $ORIGIN.
# Fail the build rather than ship a package without the kernels.
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
CUR_DIR=$(pwd)
cd ${SCRIPT_DIR}

cp "$(gcc -print-file-name=libgomp.so.1)" .

for C_FILE in *.c
do
    gcc -O3 -fopenmp -fPIC -shared -Wl,-rpath,'$ORIGIN' -o ${C_FILE%%.*}.so ${C_FILE}
    rm ${C_FILE}
done

cd ${CUR_DIR}
