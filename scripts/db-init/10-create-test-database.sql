-- Runs once, when the Postgres data volume is first initialised (docker-entrypoint-initdb.d).
-- The test suite rebuilds the schema of the database it runs against, so it gets its own
-- database next to the development one. scripts/db-up.sh creates it as well for volumes that
-- predate this file. See tests/conftest.py and support_core/storage/config.py.
CREATE DATABASE support_test;
