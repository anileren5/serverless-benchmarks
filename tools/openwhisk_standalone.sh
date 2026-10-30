#!/bin/bash
# Starts a single-node OpenWhisk (standalone, Docker container factory) and a local image registry.
#
# Usage: tools/openwhisk_standalone.sh [user-cpus]
#   user-cpus: optional total CPUs for all actions; each action then also gets a hard
#              --cpus quota proportional to its memory. Without it, actions only get
#              --cpu-shares proportional to their memory.
#
# Configure SeBS with config/example.json and:
#   deployment.openwhisk.wskExec = path to wsk, docker_registry.registry = "localhost:5000"
# and run SeBS with --container-deployment.

set -e

USER_CPUS=$1
# 8 GB for benchmark actions plus 512 MB for the two Node.js containers standalone prewarms.
USER_MEMORY=${OPENWHISK_USER_MEMORY:-8704m}
MAX_ACTION_MEMORY=${OPENWHISK_MAX_ACTION_MEMORY:-4096m}
DOCKER_CLI_VERSION=28.5.1
STATE_DIR=${HOME}/.local/share/openwhisk-standalone

# Docker >= 29 rejects the API version of the Docker client bundled in the standalone image.
mkdir -p "${STATE_DIR}"
if [[ ! -x "${STATE_DIR}/docker" ]]; then
  curl -sfL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
    | tar xz -C "${STATE_DIR}" --strip-components=1 docker/docker
fi

if [[ -z "$(docker ps -aq --filter name=^registry$)" ]]; then
  docker run -d --restart unless-stopped -p 5000:5000 --name registry registry:2
fi

JVM_ARGS="-Dwhisk.memory.max=${MAX_ACTION_MEMORY}"
JVM_ARGS+=" -Dwhisk.container-pool.user-memory=${USER_MEMORY}"
JVM_ARGS+=" -Dwhisk.config.limits-actions-invokes-perMinute=100000"
JVM_ARGS+=" -Dwhisk.config.limits-actions-invokes-concurrent=1000"
if [[ -n "${USER_CPUS}" ]]; then
  JVM_ARGS+=" -Dwhisk.container-pool.user-cpus=${USER_CPUS}"
fi

docker rm -f openwhisk >/dev/null 2>&1 || true
# Action containers of a previous instance would otherwise keep running.
ACTION_CONTAINERS=$(docker ps -aq --filter "name=^wsk[0-9]+_")
if [[ -n "${ACTION_CONTAINERS}" ]]; then
  docker rm -f ${ACTION_CONTAINERS} >/dev/null
fi
docker run -d --name openwhisk --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "${STATE_DIR}/docker:/usr/bin/docker:ro" \
  -e JVM_EXTRA_ARGS="${JVM_ARGS}" \
  openwhisk/standalone:nightly

API_HOST="http://$(hostname -i | awk '{print $1}'):3233"
echo "Waiting for OpenWhisk at ${API_HOST}"
until curl -sf "${API_HOST}/ping" >/dev/null; do sleep 2; done

# Default credentials of the guest namespace in standalone mode.
wsk property set --apihost "${API_HOST}" \
  --auth 23bc46b1-71f6-4ed5-8c54-816aa4f8c502:123zO3xZCLrMN6v2BKK1dXYFpXlPkccOFqm12CdAsMgRU4VrNZ9lyGVCGuMDGIwP
echo "OpenWhisk is up. Actions are kept in memory: remove the SeBS cache entries after a restart."
