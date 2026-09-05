#!/usr/bin/env sh
# Stop the Postgres container. Pass --volumes to also delete the data volume.
set -eu
cd "$(dirname "$0")/.."
docker compose down "$@"
