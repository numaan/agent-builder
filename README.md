# customer-support-agent

`support-core` is a Python library for running graph-driven customer support conversations, plus
a sample domain pack (`packs/acme_billing`). The architecture is in [DESIGN.md](DESIGN.md), the
per-phase workflow in [PLAN.md](PLAN.md), and status in [BACKLOG.md](BACKLOG.md). Each phase's
plan, self-critique and independent review live in `reviews/`.

## Layout

```
support_core/        the library; one subpackage per DESIGN.md section 18 entry
  storage/           SQLAlchemy models, Alembic migrations, the engine's repositories
  graph/             pack manifest, graph schema, expression language, templates, validator
  engine/            the turn loop, frame stack, checkpoints, advisory lock, node runners
  channels/          the channel adapter protocol and the web chat adapter (DESIGN.md 12)
  api/               create_app, the health endpoint, the web chat endpoints, the demo client
  cli/               the `support` command
app.py               the service, as DESIGN.md 4.1 writes it: create_app(load_pack(...))
demo/                configuration for the demo: which pack, which provider, which customer
packs/acme_billing/  sample domain pack (DESIGN.md section 5 layout)
tests/               pytest suite; database tests run against real Postgres
  packs/             reference packs the validator and engine tests load
  engine_child.py    a separate OS process the durability and concurrency tests drive
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

No test calls a model. Model responses come from recorded cassettes under `tests/cassettes/`,
keyed by the SHA-256 of the request, so a test that passes is a test whose prompt matched the
recording to the byte. Two commands matter:

```sh
python -m tests.cassettes.build_cassettes          # re-record offline, from scripted answers
python -m tests.cassettes.build_cassettes --live   # re-record against the real API
python -m pytest -m live                           # the opt-in live group (skips with no key)
```

The `live` group is excluded from a plain `pytest` run (`addopts = ["-m", "not live"]`) and skips
cleanly when `ANTHROPIC_API_KEY` is unset. A change to prompt assembly changes every fingerprint,
which `tests/test_golden_conversation.py` catches with a message telling you to re-record; no test
file contains a hash, so re-recording - offline or live - touches no test.

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
DESIGN.md 5.1), the directory layout (5), and every graph rule in 5.2. **Validating a pack
imports its `tools/` package**, because a risk tier that a file the pack author writes could
misstate governs nothing (DESIGN.md 8.3); an import that raises is a finding, not a crash.
`packs/acme_billing` reports `well-formed` with nine warnings - the two workflows phase 6 still
adds, the two `confirm_exempt` passcode tools with the reason each gave, and five notes about
optional values and a state field typed as a pack model.

`support pack knowledge sync`, `support pack eval` and `support replay` exist but exit with
status 3 and name the phase that delivers them.

## Running the demo

A browser, a conversation, and a refund that stops for your approval before it moves any money.
Three commands from a clean checkout, run from the repository root:

```sh
sh scripts/db-up.sh                                       # Postgres 16 + pgvector on 5432
python -m alembic upgrade head                            # create the schema
SUPPORT_APP_CONFIG=demo/acme_web_chat.json python -m uvicorn app:app --port 8000
```

Then open <http://127.0.0.1:8000/>. On Windows use `.venv/Scripts/python.exe` in place of
`python` (Git Bash accepts the `VAR=value command` prefix); on Linux or macOS use `.venv/bin/python`.

The page offers the recorded messages as buttons. Send the first four in order:

1. *I got charged twice for the Pro Plan this month; can I have one of them back?*
2. *The code is 581139. Also, can you change my address while we are at it?*
3. *Yes please, go ahead and refund it.*
4. *Yes please - it is 4 Elm Row, Edinburgh, EH7 4AH, United Kingdom.*

What to watch for:

- **The identity check comes first.** The workflow verifies the customer **before** it looks at
  the account, because that is a gate in the graph rather than an instruction in a prompt.
- **The topic change is not lost.** Message two answers the passcode question *and* asks for
  something else. Identity verification is in the pack's `interrupts.blocked_in`, so the agent
  finishes it, says it has made a note, and comes back to the address change afterwards - which
  is what it does at message four, without being asked again (DESIGN.md section 6.6).
- **Nothing moves until you say so.** The proposed refund appears in a bordered panel naming the
  tool it would run (`issue_refund`) and quoting the exact proposal the approval is bound to.
- **A reload changes nothing.** The transcript comes back and the conversation carries on: it is
  identified by a session key, not by the connection.

Send the fifth message - *Why was I charged 40 dollars on the 3rd?* - in a fresh conversation to
see the other half of phase 6. This pack has no workflow for looking up a charge, so it hands the
conversation to a person: the agent says so, the run parks, and a packet lands on the
`billing-tier-1` queue. Read it, answer the customer, and give the workflow back:

The desk is **off unless you turn it on, and it needs a credential when you do**. It lists every
conversation in the deployment and writes into any of them, so start the app with a token - add
`"serve_desk": true` to the configuration file and export `SUPPORT_DESK_TOKEN` - and send that
token on every desk request:

```sh
export SUPPORT_DESK_TOKEN=$(python -c 'import secrets; print(secrets.token_hex(16))')
desk() { curl -H "Authorization: Bearer $SUPPORT_DESK_TOKEN" "$@"; }

desk localhost:8000/desk/handoffs                                   # the queue
desk localhost:8000/desk/handoffs/<id>                              # the whole packet
desk -X POST localhost:8000/desk/handoffs/<id>/reply \
     -H 'content-type: application/json' \
     -d '{"text": "That was the annual renewal."}'
desk -X POST localhost:8000/desk/handoffs/<id>/resume -H 'content-type: application/json' -d '{}'
```

Turning `serve_desk` on without a token stops the application at startup rather than serving an
open desk, and a request without the token is a 401 rather than an answer. Both are deliberate:
phase W's review found this API mounted beside the customer chat, on by default and open, and
reproduced an anonymous browser listing every conversation, reading another customer's transcript
and packet, and writing into it (reviews/phase-w.md, finding W1). What the token is *not* is
per-operator identity or rotation; the desk takes a `human_id` on every action for the audit, and
real operator accounts belong with phase 7.

The reply appears in the customer's open browser tab, attributed to a person rather than to the
agent. The packet carries the reason, an LLM-written summary, whether identity was verified,
where the conversation stopped, its state, every WRITE and HIGH tool call the conversation made,
any pending action, suggested next steps and a transcript link.

Two things worth knowing:

- **Say the recorded messages.** With no `ANTHROPIC_API_KEY` the app answers from the cassettes
  in `tests/cassettes/`, which are keyed by the SHA-256 of the whole prompt: an unrecorded
  message is refused rather than guessed at, and the conversation ends up waiting for a human.
  Use the buttons. With `ANTHROPIC_API_KEY` set, the same configuration (`"provider": "auto"`)
  uses the live model instead and you can type whatever you like.
- **Restart the app to run the refund again, and run one process.** The sample pack's billing
  system and passcodes are in-memory fakes seeded at startup (`packs/acme_billing/tools`), so a
  charge that has been refunded stays refunded until the process restarts, and a second worker
  would have a second, different account.

`GET /healthz` reports the pack, its fingerprint, the provider in use and whether the database is
reachable. The channel endpoints are `WS /channels/web_chat/ws` and
`POST /channels/web_chat/messages` with `{"session": "...", "text": "..."}`; the POST answers
`202` with `"queued": true` when another turn holds that conversation's lock, rather than holding
the connection until it frees.

The socket's first frame names the conversation - `{"type": "hello", "session": "<key>"}`, or
`{"type": "hello"}` to be given a new key - and the server answers `ready`. The key travels in a
frame rather than in the query string because it is the whole of this channel's access control,
and a URL is written verbatim into uvicorn's access log and into every proxy in front of it
(finding W9). Connecting creates nothing: the first message creates the conversation (W8).

## Using GLM instead of Claude

The model provider is configuration, not code (DESIGN.md section 11.1). Z.ai serves GLM through
an endpoint that speaks the Anthropic message API, so GLM needs no second client: it is the same
provider pointed at a different base URL, with prompt caching and `strict` tool schemas turned
off because that endpoint does not implement them. Neither is load-bearing - the answer is still
a tool call carrying the node's own schema, and it is still validated against the Pydantic model
on the way back, so dropping `strict` costs a retry rather than a guarantee.

```sh
export GLM_API_KEY=your-key                               # ZAI_API_KEY also accepted
sh scripts/db-up.sh
python -m alembic upgrade head
SUPPORT_APP_CONFIG=demo/acme_web_chat_glm.json python -m uvicorn app:app --port 8000
```

Unlike the recorded demo, this one answers whatever you type.

`demo/acme_web_chat_glm.json` differs from the recorded configuration in three lines:

```json
{ "provider": "glm", "models": { "default": "glm-4.6", "escalation": "glm-4.6" } }
```

The `models` override exists because `pack.yaml` names Claude model ids, and a pack should not
have to be edited to serve a different vendor. Set it per deployment, or with `SUPPORT_MODEL`
and `SUPPORT_ESCALATION_MODEL`.

| Variable | Meaning |
|---|---|
| `GLM_API_KEY` or `ZAI_API_KEY` | The key. Either name works. |
| `GLM_BASE_URL` | A regional or self-hosted endpoint. Defaults to `https://api.z.ai/api/anthropic`. |
| `SUPPORT_MODEL` | Overrides the pack's default model id. |
| `SUPPORT_ESCALATION_MODEL` | Overrides the pack's escalation model id. |

With `provider: "auto"` the app prefers Anthropic when `ANTHROPIC_API_KEY` is set, then GLM when
a GLM key is set, then the recorded cassettes, then no provider at all.

**Not yet verified against a live GLM endpoint.** No key was available when this was written, so
the payload shape is tested but the round trip is not. The two things to watch on a first live
run are whether the forced `tool_choice` is honoured and whether the tool-call answer comes back
in the content blocks the parser expects.

## Running a conversation

```python
from support_core import load_pack
from support_core.engine import Executor
from support_core.storage.session import make_engine

pack = load_pack("packs/acme_billing")
service = service_for_pack(pack, AnthropicProvider())          # DESIGN.md 11.1
hooks = EngineHooks(
    extract_slots=StructuredSlotExtractor(service),            # DESIGN.md 6.2
    confirm_decision=StructuredConfirmClassifier(service),     # DESIGN.md 6.2, 8.2
    summarize=LlmSummarizer(service),                          # DESIGN.md 10
)
executor = Executor(pack, make_engine(), hooks=hooks, llm=service)
conversation_id = await executor.start_conversation(channel="web_chat")
await executor.on_inbound(conversation_id, "I was charged twice")
```

(`service_for_pack`, `StructuredSlotExtractor` and `StructuredConfirmClassifier` are in
`support_core.llm.wiring`,
`LlmSummarizer` in `support_core.memory`. An `Executor` built without `llm=` still runs a pack
with no `llm` nodes; one that reaches an `llm` node hands off with reason `llm_unavailable`
rather than walking past it.)

What the engine guarantees (DESIGN.md 7.1 to 7.3, 17), and what it does not yet do:

- **One writer per conversation.** Each turn is run under `pg_advisory_xact_lock`. A message
  that arrives while the lock is held is stored with `status = pending` and processed, in order,
  by whichever process next holds the lock. Nothing is lost and nothing overtakes.
- **A checkpoint after every node**, writing the frame stack, the trace step and the node's
  outbound messages in one transaction. A process that dies anywhere resumes from the last
  checkpoint to the same outcome; the step id (`run_id:frame_seq:node_id:attempt`) is the same
  on the retry, and is the idempotency key of every tool call.
- **Suspension** into `waiting_customer`, `waiting_human`, `waiting_async_tool` and
  `waiting_timer`, with per-status and per-channel timeouts from `pack.yaml`'s `timeouts:` block.
  Resume with `on_inbound`, `resume_human`, `resume_async_tool`, `resume_timer`; a sweep of
  expired deadlines is `sweep_timeouts`.
- **Gates fire on every entry to a frame**, so a customer cannot suspend after a gate, let the
  precondition lapse, and come back to the protected node.
- **A prompted step chooses only among the graph's edges.** An `llm` node's structured output
  has `decision` typed as a `Literal` over exactly that node's declared edge labels, and the
  answer is validated against it. An undeclared edge, a malformed answer or a state update
  outside the node's `output_schema` is retried once - with the reason stated back to the model -
  and then handed off; a confidence below the pack's `llm.confidence_threshold` takes the node's
  `unclear` edge, or hands off if it has none. Nothing is guessed at.
- **Untrusted text is data.** Customer messages, tool results, retrieved passages, state values
  and the rolling summary are rendered inside `-----BEGIN UNTRUSTED DATA (...)-----` fences in a
  fixed nine-layer prompt (DESIGN.md 11.2) that a pack can fill but cannot reorder or escape.
- **Only READ-tier tools, and only the ones the node declared,** can be reached from a prompted
  step: the node holds a gateway, the gateway holds the runtime, the runtime refuses a
  non-READ tier a second time, and a refusal is fed back to the model rather than executed.
- **Nothing runs a `write` or `high` tool without a matching approval.** A `confirm` node hashes
  `sha256(tool + canonical_json(args))` over the arguments it *shows the customer*, records an
  `action_approval` on an explicit yes, and the `tool` node re-computes the hash and consumes
  that approval - which is bound to the run, the frame, the confirm node and the arguments, and
  is good for exactly one call. A mismatch is refused and routed to `on_error`. The check is in
  the runtime and assumes nothing about the validator having run.
- **A tool call is claimed before it runs**, under the step id. A completed call replays instead
  of running again; a call whose process died with the outcome unknown is retried only if the
  tool declared itself `idempotent`, and otherwise refused - at-most-once, in the one place where
  the difference is one refund or two.
- **A rolling summary every K turns** (`memory.summarize_every_turns`), stored on the
  conversation with the turn it covers. It is prompt context only: nothing a turn depends on is
  read from it, and a lost summary changes no durable outcome.
- **A message reaches the customer only after the checkpoint that wrote it commits.** A channel
  adapter (DESIGN.md 12) is given committed messages and nothing else, so nobody sees text that
  an outbound guardrail (phase 7) would have stopped; delivery towards a channel is
  at-least-once, and a transport that fails cannot abort the turn behind it. `create_app` serves
  the web chat channel; the email adapter and the desk are phase 7 and implement the same
  protocol.
- Everything a later phase owns is a hook on `EngineHooks` with a default that does nothing
  surprising: the interrupt check answers `continue` (phase 6), handoff records nothing but the
  run still parks for a human (phase 6), and no summary is written unless one is wired up. A
  `handoff` node refuses to run and names its phase.

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
- **Tools** are Python. `tools/__init__.py` exports `TOOLS: list[Tool]` (DESIGN.md 8.3), and
  that list is the only source of a risk tier: the validator type-checks argument expressions
  against the real input models and the runtime enforces the real tiers. The declarative
  `tools/tools.yaml` from phase 1 is now optional, and is compared against the export - a
  different tier is an error, a different shape a warning. A pack that declares tools and
  exports none is warned that it cannot run any of them.
- **`confirm_exempt`** (DESIGN.md 8.2, for a side effect the customer cannot be asked about,
  such as a passcode) is available on `write` tools only, requires a written reason, and is
  reported as a warning so `--strict` fails until somebody has looked at it.
- **`patches_context`** names the `ctx.customer` fields a tool may change, and it is empty by
  default. A tool that patches a field it did not declare fails the call. `verify_otp` declares
  `identity_verified` because setting it is what the tool is *for*; nothing else in the sample
  pack declares anything.

**A pack author is inside the trust boundary, so reviewing a pack is a security review.**
Everything above constrains the model, the customer and the graph - the model cannot act, the
customer's yes is bound to exact arguments, the graph cannot route around the runtime. None of
it constrains the pack's Python. A tool runs in-process with the service's credentials, and the
tier it declares is the tier it gets: a tool that says `read` and moves money is callable from a
model loop with no confirmation, and core cannot tell. That is the boundary DESIGN.md 4.1 draws
(one domain, one repository, one image, one service) and it is a deliberate one, but it means
`tools/` deserves the same attention as the code that reads it.

The rule that matters most: a `write` or `high` risk tool node must have a `confirm` node on every
path from the last customer input, and must name that confirm in `requires_approval` with matching
arguments (DESIGN.md 5.2 and 8.2). The validator proves this with a dataflow analysis over the
graph, across sub-graph calls, not with a pattern match - and refuses, besides, a node between
the confirm and the call that rewrites what the approved arguments read, and a second tool node
that would spend the same approval. All of it is checked again at run time.

## Conventions

- Every module under `support_core/` starts with a docstring naming the DESIGN.md section it
  implements (`tests/test_package_layout.py` enforces this).
- The database is never mocked in tests.
- Commits are prefixed with the phase, for example `phase-0: alembic initial migration`.
- Ruff excludes `*.md` on purpose: it would otherwise reformat the Python snippets in DESIGN.md.
