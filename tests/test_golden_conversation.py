"""The phase 3 exit criterion.

    ``packs/acme_billing`` has a ``root.yaml`` with a classify node and a ``small_talk`` path,
    and a scripted conversation runs through it with the fake provider. - BACKLOG.md, phase 3

"With the fake provider" is the strict one: :class:`~support_core.llm.fake.FakeProvider` replays
by request fingerprint and refuses anything it has not seen, so this test passing means the
engine sent, byte for byte, the prompts that were recorded. A change anywhere in prompt assembly
breaks it loudly and ``python -m tests.cassettes.build_cassettes`` fixes it.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.llm.fake import FakeProvider, ScriptedProvider
from support_core.llm.recording import Cassette, RecordingProvider
from tests.cassettes.scenarios import SCENARIOS, Scenario, play
from tests.engine_support import messages, outbound_texts, path, run_row


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_the_scenario_replays_against_the_recorded_responses(
    scenario: Scenario, engine: AsyncEngine
) -> None:
    cassette = Cassette.load(scenario.cassette_path)
    provider = FakeProvider(cassette)
    conversation_id, _ = await play(scenario, engine, provider)

    row = await run_row(engine, conversation_id)
    assert row["status"] == "done"
    assert len(provider.calls) == len(cassette.interactions)

    if cassette.recorded_with == "scripted":
        # A live recording may legitimately take a different path - that is what asking a real
        # model means - so the exact path is asserted only for the deterministic recording.
        assert await path(engine, row["id"]) == list(scenario.expected_path)

    transcript = await messages(engine, conversation_id)
    assert [row["author"] for row in transcript] == [
        "customer",
        "agent",
        "agent",
        "customer",
    ]
    assert all(row["status"] in {"sent", "received"} for row in transcript)


async def test_the_small_talk_path_answers_and_asks_again(engine: AsyncEngine) -> None:
    """The classify node's ``small_talk`` edge, end to end (the exit criterion's own example)."""
    scenario = SCENARIOS[0]
    conversation_id, _ = await play(
        scenario, engine, FakeProvider(Cassette.load(scenario.cassette_path))
    )
    spoken = await outbound_texts(engine, conversation_id)
    assert spoken[0].startswith("Hello.")
    assert spoken[1] == "Is there anything else I can help you with?"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_the_committed_cassette_is_what_the_builder_produces(
    scenario: Scenario, engine: AsyncEngine
) -> None:
    """Drift, caught where it happens rather than as a mysterious miss later.

    Re-recording the scenario offline must reproduce the committed file exactly: the same
    fingerprints in the same order, with the same responses. If prompt assembly changed, this
    fails with a clear instruction rather than leaving a stale cassette to rot.
    """
    committed = Cassette.load(scenario.cassette_path)
    recorder = RecordingProvider(ScriptedProvider(list(scenario.rules)), Cassette())
    await play(scenario, engine, recorder)

    assert list(recorder.cassette.interactions) == list(committed.interactions), (
        "prompt assembly changed; re-record with `python -m tests.cassettes.build_cassettes`"
    )
    for key, recorded in recorder.cassette.interactions.items():
        assert recorded.response == committed.interactions[key].response
        assert recorded.request == committed.interactions[key].request


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_the_cassette_records_what_produced_it(scenario: Scenario) -> None:
    cassette = Cassette.load(scenario.cassette_path)
    assert cassette.recorded_with in {"scripted", "anthropic"}
    assert cassette.recorded_at is not None
    assert all(key == interaction.key for key, interaction in cassette.interactions.items())
