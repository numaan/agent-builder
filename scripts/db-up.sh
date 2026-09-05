#!/usr/bin/env sh
# Start the Postgres + pgvector container and wait until it accepts connections.
# Works in Git Bash on Windows and in Linux CI. Requires Docker Compose v2.
set -eu
cd "$(dirname "$0")/.."
docker compose up -d --wait db
echo "Postgres is ready on localhost:${SUPPORT_DB_PORT:-5432} (user/password/db: support)."
echo "export SUPPORT_DATABASE_URL=postgresql+asyncpg://support:support@localhost:${SUPPORT_DB_PORT:-5432}/support"
