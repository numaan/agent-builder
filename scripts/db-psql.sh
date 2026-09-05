#!/usr/bin/env sh
# Open psql inside the running container (no local psql client needed).
set -eu
cd "$(dirname "$0")/.."
exec docker compose exec db psql -U support -d support "$@"
