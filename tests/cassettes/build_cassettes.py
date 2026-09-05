"""Rewrite the committed cassettes.

    python -m tests.cassettes.build_cassettes           # offline, scripted answers
    python -m tests.cassettes.build_cassettes --live    # against the real API

Both modes drive exactly the same scenarios through exactly the same engine, and both write the
same file format: request fingerprint, the canonical request, the response. That is what makes
"regenerate the fixtures against the live API" a one-command operation that touches no test.

Needs the test database (``sh scripts/db-up.sh``): a scenario is a real conversation through the
real executor, because a recording of anything less would not be a recording of what the engine
sends.
"""

import argparse
import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from support_core.llm.anthropic_provider import AnthropicProvider, api_key_present
from support_core.llm.fake import ScriptedProvider
from support_core.llm.provider import LLMProvider
from support_core.llm.recording import Cassette, RecordingProvider
from support_core.storage.config import test_database_url
from support_core.storage.models import ALL_TABLES
from tests.cassettes.scenarios import SCENARIOS, Scenario, play


async def record(scenario: Scenario, *, live: bool) -> None:
    engine = create_async_engine(test_database_url(), poolclass=NullPool)
    inner: LLMProvider = AnthropicProvider() if live else ScriptedProvider(list(scenario.rules))
    recorder = RecordingProvider(inner, Cassette(description=scenario.name))
    try:
        table_list = ", ".join(f'"{table.name}"' for table in ALL_TABLES)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {table_list} RESTART IDENTITY CASCADE"))
        await play(scenario, engine, recorder)
    finally:
        await engine.dispose()
    path = recorder.cassette.save(scenario.cassette_path)
    print(f"{scenario.name}: {len(recorder.cassette.interactions)} interaction(s) -> {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="record against the real API")
    args = parser.parse_args()
    if args.live and not api_key_present():
        print("ANTHROPIC_API_KEY is not set; nothing to record against.", file=sys.stderr)
        return 2
    for scenario in SCENARIOS:
        asyncio.run(record(scenario, live=args.live))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
