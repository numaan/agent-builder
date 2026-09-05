# Execution Plan

Source of truth for scope: [DESIGN.md](DESIGN.md). Source of truth for status: [BACKLOG.md](BACKLOG.md).

## Goal

Implement `support-core` and a sample domain pack (`packs/acme_billing`) so that the worked example in DESIGN.md section 19 runs end to end, all nine phases of section 22 are complete, and every phase has passed self-critique and an independent review.

## Repository layout

```
customer-support-agent/
├── DESIGN.md, PLAN.md, BACKLOG.md
├── support_core/            the library (DESIGN.md section 18)
├── packs/acme_billing/      sample domain pack (DESIGN.md section 5)
├── tests/                   core unit and integration tests
├── reviews/                 one file per phase: self-critique + independent review + resolution
├── pyproject.toml           single project for now; split into two packages in phase 9
└── docker-compose.yml       Postgres 16 + pgvector for local and CI
```

## Per-phase workflow (mandatory)

Every backlog phase goes through the same five steps. A phase is not done until step 5 is recorded in `reviews/phase-N.md` and the BACKLOG.md status is `done`.

1. **Plan the phase.** The implementing agent reads DESIGN.md sections named in the backlog item, writes a short task breakdown at the top of `reviews/phase-N.md`, and lists any deviation from the design it intends to make and why.
2. **Implement with tests.** Code plus tests. Tests run against real Postgres via docker-compose where the design needs Postgres behaviour (advisory locks, JSONB, pgvector). No mocking of the database.
3. **Self-critique.** The implementing agent re-reads DESIGN.md for the phase and writes a critique section answering: What did I skip or simplify? Where does the code diverge from the design? Which tests are weak? What would break under concurrency or a crash mid-step? It fixes what it can and records the rest.
4. **Independent review.** A separate agent that did not write the code reviews the diff against the design and the exit criterion. It writes findings ranked by severity into `reviews/phase-N.md` under "Independent review." It must attempt to break the phase: run the tests, try the adversarial cases relevant to the phase, and check the validator rules.
5. **Resolve and close.** The implementing agent (or a fresh one) addresses every finding marked `must-fix`, records the resolution, re-runs the full test suite, and marks the phase `done` in BACKLOG.md. `should-fix` findings that are deferred become new backlog items under "Deferred findings."

## Conventions

- Python 3.12+ (3.13 is fine), Pydantic v2, FastAPI, SQLAlchemy 2 async, asyncpg, Alembic, pytest + pytest-asyncio.
- `ruff` for lint and format, `mypy --strict` on `support_core` from phase 1 onward.
- Every module under `support_core/` starts with a docstring naming the DESIGN.md section it implements.
- Commit per phase step, message prefixed with the phase, for example `phase-2: engine checkpointing`.
- No code path may execute a WRITE or HIGH tool without an `ActionApproval` (DESIGN.md 8.2). This is the one rule reviewers check on every phase regardless of scope.

## Environment needs

| Need | Provided by |
|------|-------------|
| Postgres 16 + pgvector | `docker compose up -d db` |
| Anthropic API key | `ANTHROPIC_API_KEY` env var. Phases 3+ use a recorded-response fake provider in tests; live calls only in a marked `live` test group. |
| Email provider | Not required. Phase 7 email adapter is tested against a fake webhook payload. |

## Definition of done for the whole effort

- All backlog phases `done` with reviews recorded.
- `pytest` green including the adversarial suite.
- `support pack validate packs/acme_billing` passes.
- The section 19 worked example runs as a golden conversation against the fake provider, and once against the live provider if a key is present.
- A final integration review across all phases is recorded in `reviews/final.md`.
