#!/usr/bin/env sh
# Start the Postgres + pgvector container and wait until it accepts connections.
# Works in Git Bash on Windows and in Linux CI. Requires Docker Compose v2.
set -eu
cd "$(dirname "$0")/.."
docker compose up -d --wait db

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
