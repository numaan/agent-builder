#!/usr/bin/env sh
# Start the Postgres + pgvector and Qdrant containers and wait until they accept connections.
# Works in Git Bash on Windows and in Linux CI. Requires Docker Compose v2.
#
# Two services, not one: Qdrant holds the ColBERT multivectors of DESIGN.md section 9.1. The
# script keeps its name because every document and every Makefile target already uses it, and
# because Postgres is still the one store a turn cannot run without.
set -eu
cd "$(dirname "$0")/.."
docker compose up -d --wait db qdrant

# The test suite needs its own database (it drops and recreates every table). A fresh data
# volume gets it from scripts/db-init/; older volumes get it here. Idempotent.
psql_db() { docker compose exec -T db psql -U support -d support -v ON_ERROR_STOP=1 -tA "$@"; }
if [ "$(psql_db -c "SELECT 1 FROM pg_database WHERE datname = 'support_test'")" != "1" ]; then
  psql_db -c "CREATE DATABASE support_test" >/dev/null
  echo "Created test database support_test."
fi

port="${SUPPORT_DB_PORT:-5432}"
echo "Postgres is ready on localhost:${port} (user/password: support; databases: support, support_test)."
echo "export SUPPORT_DATABASE_URL=postgresql+asyncpg://support:support@localhost:${port}/support"
echo "pytest uses postgresql+asyncpg://support:support@localhost:${port}/support_test (override: SUPPORT_TEST_DATABASE_URL)"

qport="${SUPPORT_QDRANT_PORT:-6333}"
echo "Qdrant is ready on localhost:${qport} (override: SUPPORT_QDRANT_URL)."
