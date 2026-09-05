"""The Alembic migrations and the SQLAlchemy models must describe the same schema.

Alembic's autogenerate comparison is run against the migrated database; any difference
(a column, type, nullability, index, unique constraint or foreign key present on one side
only) fails the test and prints the diff so the fix is obvious.
"""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.storage.models import ALL_TABLES, Base

EXPECTED_TABLES = {
    "conversation",
    "message",
    "run",
    "trace_step",
    "tool_call",
    "action_approval",
    "handoff",
    "customer_memory",
    "doc_source",
    "doc_chunk",
    "kg_entity",
    "kg_relation",
    "eval_run",
}
"""Every table named in DESIGN.md section 17."""


def _schema_diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "compare_server_default": True}
    )
    return list(compare_metadata(context, Base.metadata))


async def test_models_match_migrations(engine: AsyncEngine) -> None:
    """Columns, types, nullability, server defaults, FKs (incl. ON DELETE), uniques, indexes.

    Known blind spots of Alembic's comparison, covered by the tests below instead: the index
    access method (GIN) and the generated-column expression.
    """
    async with engine.connect() as conn:
        diff = await conn.run_sync(_schema_diff)
    assert diff == [], f"models and migrations differ:\n{diff}"


async def test_doc_chunk_search_indexes(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        indexdef = (
            await conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_doc_chunk_tsv'")
            )
        ).scalar_one()
        generated = (
            await conn.execute(
                text(
                    "SELECT generation_expression FROM information_schema.columns "
                    "WHERE table_name = 'doc_chunk' AND column_name = 'tsv'"
                )
            )
        ).scalar_one()
    assert "USING gin" in indexdef
    assert generated == "to_tsvector('english'::regconfig, text)"


async def test_every_design_table_exists(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        names = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    assert names >= EXPECTED_TABLES
    assert {t.name for t in ALL_TABLES} == EXPECTED_TABLES


async def test_design_unique_constraints_present(engine: AsyncEngine) -> None:
    """Section 17 names two unique keys: trace_step.step_id and tool_call.idempotency_key."""

    def uniques(connection: Connection, table: str) -> set[str]:
        return {u["name"] for u in inspect(connection).get_unique_constraints(table) if u["name"]}

    async with engine.connect() as conn:
        assert "uq_trace_step_step_id" in await conn.run_sync(uniques, "trace_step")
        assert "uq_tool_call_idempotency_key" in await conn.run_sync(uniques, "tool_call")
