"""The exit criterion of phase 5, whole.

    Test: change a policy document, re-sync, and show the trace of a new answer cites the new
    ``source_version`` while an old trace still names the old one. - BACKLOG.md, phase 5

    Traces record which passage versions were used, so a wrong answer can be traced to a
    specific source version. - DESIGN.md 9.2

The word that decides how this file is written is **still**. It is easy to write a test that
passes because the old trace holds an old string; that proves the string was not overwritten and
nothing else. What the criterion is worth having is the stronger property, so this file checks all
four parts of it:

1. the old trace names version A and the new trace names version B, and A is not B;
2. the *text* the old trace names is still readable, and still says what it said - so an
   investigation that starts from the old trace reaches the sentence that caused the wrong
   answer rather than the sentence that replaced it;
3. the customer's new answer rests on B, not on A - a re-sync is picked up "on the next
   retrieval" (section 9.2) with no restart;
4. it holds for the vector index as well, whose versioning is by collection: the collection that
   produced the old answer still exists and still contains what it contained.

The wrong answer is a real one. The corpus says 60 days, a customer is told 60 days, the document
turns out to have been wrong, somebody fixes it - and the question a week later is which sentence
the agent was reading when it said 60. That is the question this answers.
"""

import dataclasses
import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core import load_pack
from support_core.engine import Executor
from support_core.knowledge.embedding import DeterministicEncoder
from support_core.knowledge.qdrant import alias_name, collection_name
from support_core.knowledge.wiring import build_retriever
from support_core.llm.fake import Rule, ScriptedProvider
from support_core.llm.wiring import service_for_pack
from support_core.storage import knowledge_repo as repo
from support_core.storage.session import make_session_factory
from tests.engine_support import PACKS, Recorder, outbound_texts, run_row
from tests.knowledge_support import ingestor, markdown_source, sources_for, write_corpus

KNOWLEDGE_PACK = PACKS / "knowledge_pack"
CLASSIFY = "Classify what the customer is asking about"
ANSWER = "Answer the customer's question about the refund policy"

WRONG = """# Refund policy

## Refund window

A charge can be refunded within 60 days of the payment date.
"""

CORRECTED = WRONG.replace("60 days", "90 days")


def decision(
    label: str, *, message: str | None = None, citations: Any = (), updates: Any = None
) -> dict[str, Any]:
    return {
        "message_to_customer": message,
        "decision": label,
        "state_updates": dict(updates or {}),
        "citations": list(citations),
        "confidence": 0.9,
        "needs_handoff": False,
    }


async def _no_sleep(seconds: float) -> None: ...


def build(engine: AsyncEngine, answer: str, *, store: Any = None) -> Executor:
    """A pack whose corpus is whatever the last sync wrote, with a scripted model.

    The retriever is rebuilt per turn exactly as a running service does not: a *service* holds
    one for the life of the process, which is the point - it must pick up a re-sync with no
    restart. That is asserted below with a single long-lived executor as well.
    """
    pack = load_pack(KNOWLEDGE_PACK)
    encoder = DeterministicEncoder()
    retriever = build_retriever(
        sources_for(),
        make_session_factory(engine),
        embedder=encoder,
        encoder=encoder,
        store=store,
        use_qdrant=store is not None,
    )
    provider = ScriptedProvider(
        [
            Rule(when=CLASSIFY, respond=decision("answer", updates={"topic": "refund window"})),
            Rule(when=ANSWER, respond=decision("done", message=answer, citations=["k1"])),
        ]
    )
    return Executor(
        pack,
        engine,
        hooks=dataclasses.replace(Recorder().hooks()),
        llm=service_for_pack(pack, provider, sleep=_no_sleep),
        retriever=retriever,
    )


async def retrieval_in_trace(engine: AsyncEngine, run_id: uuid.UUID) -> list[dict[str, Any]]:
    """The ``retrieval`` block of the answering node's trace step (DESIGN.md 9.2, 15)."""
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                sql_text(
                    "SELECT node_id, llm_response FROM trace_step "
                    "WHERE run_id = :r ORDER BY seq"
                ),
                {"r": run_id},
            )
        ).mappings()
    for row in rows:
        response = row["llm_response"]
        if isinstance(response, str):
            response = json.loads(response)
        if row["node_id"] == "answer" and response and "retrieval" in response:
            return list(response["retrieval"])
    return []


async def text_of_version(engine: AsyncEngine, version: str) -> list[str]:
    """What that ``source_version`` says, read back from the rows the sync left behind."""
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                sql_text(
                    "SELECT text FROM doc_chunk WHERE source_version = :v ORDER BY chunk_index"
                ),
                {"v": version},
            )
        ).scalars()
    return list(rows)


async def test_a_wrong_answer_is_traceable_to_the_source_version_that_caused_it(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The exit criterion, end to end, with one executor across the re-sync.

    One executor on purpose: DESIGN.md 9.2 says in-flight conversations "pick up the new version
    on their next retrieval" and "no service restart", so rebuilding the service between the two
    turns would test the test instead of the claim.
    """
    write_corpus(tmp_path, {"policy.md": WRONG})
    first_sync = await ingestor(engine, tmp_path).sync_source(markdown_source())

    executor = build(engine, "A charge can be refunded within 60 days.")
    conversation = await executor.start_conversation()
    await executor.on_inbound(conversation, "how long do I have to ask for a refund?")

    wrong_run = await run_row(engine, conversation)
    wrong_trace = await retrieval_in_trace(engine, wrong_run["id"])
    assert wrong_trace, "the answering node's trace recorded no retrieval"
    assert {row["source_version"] for row in wrong_trace} == {first_sync.version}
    assert await outbound_texts(engine, conversation) == [
        "A charge can be refunded within 60 days."
    ]

    # Somebody notices the document is wrong and corrects it. A data change, not a deploy.
    write_corpus(tmp_path, {"policy.md": CORRECTED})
    second_sync = await ingestor(engine, tmp_path).sync_source(markdown_source())
    assert second_sync.version != first_sync.version

    # (3) the next retrieval on the *same* executor picks the new version up, with no restart.
    later = await executor.start_conversation()
    await executor.on_inbound(later, "how long do I have to ask for a refund?")
    right_run = await run_row(engine, later)
    right_trace = await retrieval_in_trace(engine, right_run["id"])
    assert {row["source_version"] for row in right_trace} == {second_sync.version}

    # (1) the old trace is unchanged and still names the old version.
    assert await retrieval_in_trace(engine, wrong_run["id"]) == wrong_trace
    assert {row["source_version"] for row in wrong_trace} != {
        row["source_version"] for row in right_trace
    }

    # (2) and the version it names can still be read, and still says 60.
    old_text = await text_of_version(engine, first_sync.version)
    assert old_text, "the version the old trace names has nothing behind it"
    assert any("60 days" in chunk for chunk in old_text)
    assert not any("90 days" in chunk for chunk in old_text)
    new_text = await text_of_version(engine, second_sync.version)
    assert any("90 days" in chunk for chunk in new_text)


async def test_the_trace_names_the_document_and_the_heading_not_only_the_version(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """A version identifies a corpus; an investigation needs the sentence.

    So the trace step carries the locator and the id the model was shown as well, which is what
    turns "revision 1 was wrong" into "revision 1 of this heading of this file was wrong".
    """
    write_corpus(tmp_path, {"policy.md": WRONG})
    await ingestor(engine, tmp_path).sync_source(markdown_source())
    executor = build(engine, "Within 60 days.")
    conversation = await executor.start_conversation()
    await executor.on_inbound(conversation, "how long?")

    row = await run_row(engine, conversation)
    entry = (await retrieval_in_trace(engine, row["id"]))[0]
    assert set(entry) == {"id", "source_id", "source_version", "locator", "score", "backend"}
    assert entry["id"] == "k1"
    assert entry["locator"] == "policy.md#Refund policy > Refund window"
    assert entry["source_id"] == "policy-docs"


async def test_the_trace_records_that_an_answer_was_given_with_a_backend_missing(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """"These are the best passages available *because Qdrant was down*" is a materially
    different fact from "these are the best there are", when somebody is later asked why an
    answer was wrong (DESIGN.md 15)."""
    from tests.knowledge_support import unreachable_store  # noqa: PLC0415

    write_corpus(tmp_path, {"policy.md": WRONG})
    await ingestor(engine, tmp_path).sync_source(markdown_source())
    executor = build(engine, "Within 60 days.", store=unreachable_store())
    conversation = await executor.start_conversation()
    await executor.on_inbound(conversation, "how long?")

    row = await run_row(engine, conversation)
    async with engine.connect() as connection:
        response = (
            await connection.execute(
                sql_text(
                    "SELECT llm_response FROM trace_step WHERE run_id = :r AND node_id = 'answer'"
                ),
                {"r": row["id"]},
            )
        ).scalar_one()
    if isinstance(response, str):
        response = json.loads(response)
    assert response["retrieval_degraded"] == ["colbert"]
    assert response["retrieval"], "the answer was still grounded on the Postgres halves"


async def test_the_old_collection_still_holds_what_produced_the_old_answer(
    engine: AsyncEngine, tmp_path: Path, qdrant: Any
) -> None:
    """The vector half of the same criterion.

    Versions are collections and a sync flips an alias, so "the index that produced that answer"
    is a thing that still exists rather than a description of a thing that was overwritten. This
    is why the decisions log chose Qdrant over an index directory the deployment rebuilds in
    place.
    """
    write_corpus(tmp_path, {"policy.md": WRONG})
    first = await ingestor(engine, tmp_path, store=qdrant).sync_source(markdown_source())
    write_corpus(tmp_path, {"policy.md": CORRECTED})
    second = await ingestor(engine, tmp_path, store=qdrant).sync_source(markdown_source())

    old = collection_name("policy-docs", 1, prefix=qdrant.prefix)
    new = collection_name("policy-docs", 2, prefix=qdrant.prefix)
    assert await qdrant.collection_exists(old)
    assert await qdrant.collection_exists(new)

    encoder = DeterministicEncoder()
    query = "refund window"
    dense = await encoder.embed_query(query)
    colbert = await encoder.encode_query(query)

    def versions(hits: Any) -> set[str]:
        return {hit.payload["source_version"] for hit in hits}

    assert versions(
        await qdrant.search_colbert(old, dense=dense, colbert=colbert, limit=5)
    ) == {first.version}
    assert versions(
        await qdrant.search_colbert(new, dense=dense, colbert=colbert, limit=5)
    ) == {second.version}
    # The alias - what a live query names - points at the new one and nothing else.
    assert versions(
        await qdrant.search_colbert(
            alias_name("policy-docs", prefix=qdrant.prefix),
            dense=dense,
            colbert=colbert,
            limit=5,
        )
    ) == {second.version}


async def test_a_version_named_by_a_trace_can_be_read_back_by_version(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The operation an investigation actually performs: given the string in the trace, show me
    the corpus. It is one query, and it works because the rows were marked stale rather than
    deleted."""
    write_corpus(tmp_path, {"policy.md": WRONG})
    first = await ingestor(engine, tmp_path).sync_source(markdown_source())
    write_corpus(tmp_path, {"policy.md": CORRECTED})
    await ingestor(engine, tmp_path).sync_source(markdown_source())

    async with make_session_factory(engine)() as session, session.begin():
        live = await repo.chunk_count(session, "policy-docs", stale=False)
        stale = await repo.chunk_count(session, "policy-docs", stale=True)
    assert live == 1
    assert stale == 1
    assert any("60 days" in chunk for chunk in await text_of_version(engine, first.version))
