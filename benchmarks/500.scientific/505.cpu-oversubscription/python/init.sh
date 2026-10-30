#!/bin/bash

DIR=$1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

cp ${SCRIPT_DIR}/*.c ${DIR}
