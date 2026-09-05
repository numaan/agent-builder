# customer-support-agent

`support-core` is a Python library for running graph-driven customer support conversations, plus
a sample domain pack (`packs/acme_billing`). The architecture is in [DESIGN.md](DESIGN.md), the
per-phase workflow in [PLAN.md](PLAN.md), and status in [BACKLOG.md](BACKLOG.md). Each phase's
plan, self-critique and independent review live in `reviews/`.

## Layout

```
support_core/        the library; one subpackage per DESIGN.md section 18 entry
  storage/           SQLAlchemy models, Alembic migrations (support_core/storage/migrations)
  graph/             pack manifest and validator (graphs arrive in phase 1)
  cli/               the `support` command
packs/acme_billing/  sample domain pack (DESIGN.md section 5 layout)
tests/               pytest suite; database tests run against real Postgres
scripts/             db-up.sh, db-down.sh, db-psql.sh (POSIX sh, Git Bash and Linux)
docker-compose.yml   Postgres 16 + pgvector
```

## Setup

Requirements: Python 3.12+ (3.13 is used in CI), Docker with Compose v2.

```sh
python -m venv .venv
# Windows (Git Bash):      .venv/Scripts/python.exe -m pip install -e ".[dev]"
# Linux/macOS:             .venv/bin/python -m pip install -e ".[dev]"
```

All commands below assume the virtual environment's `python` is first on your PATH (activate it,
or spell out `.venv/Scripts/python.exe` / `.venv/bin/python`).

## Database

Start Postgres 16 with pgvector and wait until it accepts connections:

```sh
sh scripts/db-up.sh        # or: make db-up
```

The container listens on `localhost:5432` with user, password and database all `support`. Every
component reads the connection string from one place:

```sh
export SUPPORT_DATABASE_URL=postgresql+asyncpg://support:support@localhost:5432/support   # the default
```

Apply the schema:

```sh
python -m alembic upgrade head      # or: make migrate
python -m alembic check             # confirms models and migrations agree
```

Other useful commands:

```sh
sh scripts/db-psql.sh                 # psql inside the container (no local client needed)
sh scripts/db-down.sh                 # stop; add --volumes to delete the data as well
```

If the database ends up in a state the migrations cannot move from (for example after editing a
migration in place), `sh scripts/db-down.sh --volumes` then `sh scripts/db-up.sh` gives a clean one.

## Checks

```sh
python -m ruff check . && python -m ruff format --check .   # make lint
python -m mypy                                              # make typecheck (strict)
python -m pytest                                            # make test
```

`pytest` needs the database from the previous section. The fixtures in `tests/conftest.py`
downgrade to base and upgrade to head once per session (so the downgrade path is exercised on
every run) and truncate every table before each test. Point `SUPPORT_DATABASE_URL` elsewhere to
use a different Postgres. Do not run the suite in parallel against one database.

`make check` runs lint, typecheck, tests and the pack validation in sequence. CI
(`.github/workflows/ci.yml`) runs the same steps against a `pgvector/pgvector:pg16` service.

## CLI

```sh
support pack validate packs/acme_billing
```

Prints one line per finding (`ERROR`, `WARNING`, `INFO` with a stable rule id such as
`layout.missing_file`) and a summary. Exit status is 0 when the pack is well-formed, 1 when it has
errors; `--strict` also fails on warnings, `--quiet` prints only the summary. In phase 0 the checks
cover the manifest (`pack.yaml`, DESIGN.md 5.1) and the directory layout (5); graph validation
(5.2) is phase 1 and the sample pack currently reports `empty but well-formed`.

`support pack knowledge sync`, `support pack eval` and `support replay` exist but exit with
status 3 and name the phase that delivers them.

## Conventions

- Every module under `support_core/` starts with a docstring naming the DESIGN.md section it
  implements (`tests/test_package_layout.py` enforces this).
- The database is never mocked in tests.
- Commits are prefixed with the phase, for example `phase-0: alembic initial migration`.
- Ruff excludes `*.md` on purpose: it would otherwise reformat the Python snippets in DESIGN.md.
