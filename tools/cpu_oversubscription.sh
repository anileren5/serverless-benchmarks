#!/bin/bash

# Runs the cpu-oversubscription experiment with one of the plans in config/cpu-oversubscription.
#
# Usage: tools/cpu_oversubscription.sh <local|aws|openwhisk> <plan> [label]
#
# Environment:
#   REGION        AWS region (default: eu-north-1). Other regions use their own SeBS cache,
#                 since the cache pins the region and the region's resources.
#   STORAGE       output of `sebs.py storage start object ...` (default: out_storage.json),
#                 needed by local and openwhisk.
#   OUTPUT_DIR    results directory (default: results).
#   REPETITIONS, CONCURRENCY
#                 override all configurations of the plan, e.g. for a pilot run.
#   WSK, REGISTRY wsk binary and image registry for openwhisk
#                 (default: wsk, localhost:5000, matching tools/openwhisk_standalone.sh).
#
# Requires jq, an activated SeBS environment, and for AWS the credentials in the environment.

set -euo pipefail

if [[ $# -lt 2 ]]; then
  sed -n '3,18p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
fi

PLATFORM=$1
PLAN=$(realpath "$2")
LABEL=${3:-}
REGION=${REGION:-eu-north-1}
STORAGE=${STORAGE:+$(realpath "${STORAGE}")}
OUTPUT_DIR=${OUTPUT_DIR:+$(realpath -m "${OUTPUT_DIR}")}
LANGUAGE_VERSION=3.11
ARCHITECTURE=x64

# Paths given by the user are relative to the caller, defaults to the repository.
cd "$(dirname "${BASH_SOURCE[0]}")/.."
STORAGE=${STORAGE:-out_storage.json}
OUTPUT_DIR=${OUTPUT_DIR:-results}

CONFIG=$(mktemp --suffix .json)
trap 'rm -f "${CONFIG}"' EXIT

FILTER='.experiments["cpu-oversubscription"] = $plan[0]'
ARGS=(--slurpfile plan "${PLAN}")
if [[ -n "${LABEL}" ]]; then
  FILTER+=' | .experiments["cpu-oversubscription"].label = $label'
  ARGS+=(--arg label "${LABEL}")
fi
if [[ -n "${REPETITIONS:-}" ]]; then
  FILTER+=' | .experiments["cpu-oversubscription"].configurations[].repetitions = ($reps | tonumber)'
  ARGS+=(--arg reps "${REPETITIONS}")
fi
if [[ -n "${CONCURRENCY:-}" ]]; then
  FILTER+=' | .experiments["cpu-oversubscription"].configurations[]["concurrent-invocations"] = ($conc | tonumber)'
  ARGS+=(--arg conc "${CONCURRENCY}")
fi

SEBS_ARGS=(--deployment "${PLATFORM}" --architecture "${ARCHITECTURE}" --language-version "${LANGUAGE_VERSION}")
case "${PLATFORM}" in
  aws)
    FILTER+=' | .deployment.aws.region = $region'
    ARGS+=(--arg region "${REGION}")
    if [[ "${REGION}" != "eu-north-1" ]]; then
      SEBS_ARGS+=(--cache "cache-aws-${REGION}")
    fi
    ;;
  local)
    FILTER+=' | .deployment.local.storage = $storage[0]'
    ARGS+=(--slurpfile storage "${STORAGE}")
    ;;
  openwhisk)
    FILTER+=' | .deployment.openwhisk.storage = $storage[0]'
    FILTER+=' | .deployment.openwhisk.wskExec = $wsk | .deployment.openwhisk.docker_registry.registry = $registry'
    ARGS+=(--slurpfile storage "${STORAGE}" --arg wsk "$(command -v "${WSK:-wsk}")")
    ARGS+=(--arg registry "${REGISTRY:-localhost:5000}")
    SEBS_ARGS+=(--container-deployment)
    ;;
  *)
    echo "Unknown platform ${PLATFORM}" >&2
    exit 1
    ;;
esac

jq "${ARGS[@]}" "${FILTER}" config/example.json > "${CONFIG}"
./sebs.py experiment invoke cpu-oversubscription --config "${CONFIG}" "${SEBS_ARGS[@]}" \
  --output-dir "${OUTPUT_DIR}"
