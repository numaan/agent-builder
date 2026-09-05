# customer-support-agent

`support-core` is a Python library for running graph-driven customer support conversations, plus
a sample domain pack (`packs/acme_billing`). The architecture is in [DESIGN.md](DESIGN.md), the
per-phase workflow in [PLAN.md](PLAN.md), and status in [BACKLOG.md](BACKLOG.md). Each phase's
plan, self-critique and independent review live in `reviews/`.

## Layout

```
support_core/        the library; one subpackage per DESIGN.md section 18 entry
  storage/           SQLAlchemy models, Alembic migrations (support_core/storage/migrations)
  graph/             pack manifest, graph schema, expression language, templates, validator
  cli/               the `support` command
packs/acme_billing/  sample domain pack (DESIGN.md section 5 layout)
tests/               pytest suite; database tests run against real Postgres
  packs/             reference packs the validator and stepper tests load
  stepper.py         test-only in-memory graph stepper (the engine is phase 2)
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

The `scripts/*.sh` files are the canonical way to do things; the `Makefile` only wraps them for
convenience and needs GNU make, which Git Bash on Windows does not ship.

The container listens on `localhost:5432` with user and password `support` and two databases:
`support` for development and `support_test`, which only the test suite uses. Every component
reads the connection string from one place:

```sh
export SUPPORT_DATABASE_URL=postgresql+asyncpg://support:support@localhost:5432/support   # the default
```

Apply the schema:

```sh
python -m alembic upgrade head      # or: make migrate
python -m alembic check             # confirms models and migrations agree
```

The initial migration runs `CREATE EXTENSION IF NOT EXISTS vector`. On the
`pgvector/pgvector:pg16` image that needs a superuser (the compose user `support` is one);
against a managed Postgres, have the extension created by an administrator first and the
migration will find it. `alembic downgrade base` also drops the extension, so do not run
the downgrade against an instance that shares it with other databases.

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

`pytest` needs the database from the previous section and runs against `support_test`, never
`support`. The fixtures in `tests/conftest.py` downgrade to base and upgrade to head once per
session (so the downgrade path is exercised on every run) and truncate every table before each
test, so the suite must own the database it is pointed at. `tests/conftest.py` therefore refuses
to start (a usage error before collection) unless the database name ends in `_test`:

- With nothing set, the suite uses `postgresql+asyncpg://support:support@localhost:5432/support_test`.
- If `SUPPORT_DATABASE_URL` is exported it is used only when its database name ends in `_test`;
  the development URL above is rejected with a message that says so.
- `SUPPORT_TEST_DATABASE_URL` overrides both and is used as-is (any name); this is the explicit
  opt-in for CI or a throwaway instance.

`scripts/db-up.sh` creates `support_test` if it is missing, so a data volume created before the
test database existed only needs `sh scripts/db-up.sh` run again. Do not run the suite in
parallel against one database.

`make check` runs lint, typecheck, tests and the pack validation in sequence. CI
(`.github/workflows/ci.yml`) runs the same steps against a `pgvector/pgvector:pg16` service.

## CLI

```sh
support pack validate packs/acme_billing
```

Prints one line per finding and a summary. A finding is a severity (`ERROR`, `WARNING`, `INFO`),
a stable rule id (`layout.missing_file`, `graph.unconfirmed_write`), the file it points at and,
for graph rules, the node id:

```
ERROR   graph.unconfirmed_write [graphs/refund.yaml:issue_refund]: tool 'issue_refund' is high risk but ...
```

Exit status is 0 when the pack is well-formed, 1 when it has errors; `--strict` also fails on
warnings, `--quiet` prints only the summary. The checks cover the manifest (`pack.yaml`,
DESIGN.md 5.1), the directory layout (5), and every graph rule in 5.2. `packs/acme_billing` has no
graphs yet, so it reports `empty but well-formed`; `tests/packs/refund_pack` is the worked
DESIGN.md 6.4 example and reports `well-formed` with warnings.

`support pack knowledge sync`, `support pack eval` and `support replay` exist but exit with
status 3 and name the phase that delivers them.

## Writing a pack's graphs

A graph file declares `id`, `start`, `nodes`, and optionally `description`, `inputs`, `outputs`
and `state` (DESIGN.md 6.4). Edges live inside the nodes (`next`, `edges`, `on_error`, `default`).
Three things are worth knowing before the first one:

- **Expressions** (`state.x`, `ctx.customer.y`, `result.z`) are a small sandboxed language, not
  Python: attribute access, `== != < <= > >=`, `and` / `or` / `not`, literals, parentheses, and
  the four filters `money`, `lower`, `len`, `default`. Anything else is a load-time error naming
  the offending token and its position. There is no `eval` anywhere in the implementation.
- **Templates** in `say`, `ask` and `confirm` are sandboxed Jinja with the same four filters.
  `{% if %}` is allowed; loops, assignment, calls and subscripts are not. Every variable is
  type-checked against the graph's declared `state` at load time.
- **Tools** are declared as data in `tools/tools.yaml` (name, `risk`, `input`, `output`,
  `confirm_exempt`, ...) so the validator can enforce DESIGN.md 5.2's confirm-on-all-paths rule
  before the phase 4 tool registry exists. Phase 4 replaces this file as the source of truth.

The rule that matters most: a `write` or `high` risk tool node must have a `confirm` node on every
path from the last customer input, and must name that confirm in `requires_approval` with matching
arguments (DESIGN.md 5.2 and 8.2). The validator proves this with a dataflow analysis over the
graph, across sub-graph calls, not with a pattern match.

## Conventions

- Every module under `support_core/` starts with a docstring naming the DESIGN.md section it
  implements (`tests/test_package_layout.py` enforces this).
- The database is never mocked in tests.
- Commits are prefixed with the phase, for example `phase-0: alembic initial migration`.
- Ruff excludes `*.md` on purpose: it would otherwise reformat the Python snippets in DESIGN.md.
