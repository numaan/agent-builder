# Convenience targets. Every target has a script or command equivalent documented in README.md.
PY ?= python

.PHONY: db-up db-down db-psql migrate lint format typecheck test validate check

db-up:
	sh scripts/db-up.sh

db-down:
	sh scripts/db-down.sh

db-psql:
	sh scripts/db-psql.sh

migrate:
	$(PY) -m alembic upgrade head

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format:
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

typecheck:
	$(PY) -m mypy

test:
	$(PY) -m pytest

validate:
	support pack validate packs/acme_billing

check: lint typecheck test validate
