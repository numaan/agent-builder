"""``support pack knowledge sync``. DESIGN.md section 9.3, and the CLI shape of section 18.

    ``support pack knowledge sync`` is a CLI in core: fetch, normalize to markdown, chunk by
    headings, embed, upsert, and mark stale chunks. ... Runs on a schedule in production and in
    CI on pack changes. - DESIGN.md 9.3

"Runs on a schedule" is what decides most of what is tested here. A scheduled job is a thing
nobody watches, so what it does when something is wrong matters more than what it prints when
everything is right: an unreadable source file must fail loudly, an unreachable Qdrant must not
fail at all, and a corpus nobody edited must do nothing rather than rebuild an index.

The command is driven through Click's runner against the *test* database, by pointing
``SUPPORT_DATABASE_URL`` at it - the same variable a deployment sets, so the command under test
is the command a deployment runs.
"""

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.cli.main import cli
from support_core.storage.config import test_database_url as _test_database_url
from tests.engine_support import PACKS
from tests.knowledge_support import write_corpus

SAMPLE = Path("packs/acme_billing")
KNOWLEDGE_PACK = PACKS / "knowledge_pack"

POLICY = """# Refund policy

## Refund window

A charge can be refunded within 60 days of the payment date.
"""


@pytest.fixture
def run(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> object:
    """Invoke the CLI against the test database, with no Qdrant unless a test wants one.

    ``engine`` is depended on rather than used: it is what truncates the tables and guarantees
    the schema is migrated, and the command opens its own connection exactly as a cron job would.
    """
    monkeypatch.setenv("SUPPORT_DATABASE_URL", _test_database_url())
    monkeypatch.setenv("SUPPORT_QDRANT_URL", "http://127.0.0.1:1")

    def invoke(*args: str) -> object:
        return CliRunner().invoke(cli, ["pack", "knowledge", "sync", *args])

    return invoke


def pack_at(tmp_path: Path, documents: dict[str, str]) -> Path:
    pack = tmp_path / "pack"
    shutil.copytree(KNOWLEDGE_PACK, pack)
    for existing in (pack / "knowledge" / "docs").glob("*.md"):
        existing.unlink()
    write_corpus(pack, documents)
    return pack


def test_a_sync_reports_what_it_wrote(run: object, tmp_path: Path) -> None:
    pack = pack_at(tmp_path, {"policy.md": POLICY})
    result = run(str(pack))  # type: ignore[operator]
    assert result.exit_code == 0, result.output
    assert "policy-docs: r1-" in result.output
    assert "1 document(s)" in result.output
    assert "1 chunk(s)" in result.output


def test_a_second_sync_of_the_same_corpus_says_it_did_nothing(run: object, tmp_path: Path) -> None:
    """What a daily job prints on the days nobody edited anything."""
    pack = pack_at(tmp_path, {"policy.md": POLICY})
    first = run(str(pack))  # type: ignore[operator]
    again = run(str(pack))  # type: ignore[operator]
    assert again.exit_code == 0
    assert "unchanged at" in again.output
    assert first.output.split()[1].rstrip(",") in again.output


def test_force_re_indexes_a_corpus_that_did_not_change(run: object, tmp_path: Path) -> None:
    pack = pack_at(tmp_path, {"policy.md": POLICY})
    run(str(pack))  # type: ignore[operator]
    forced = run(str(pack), "--force")  # type: ignore[operator]
    assert forced.exit_code == 0
    assert "unchanged" not in forced.output
    assert "policy-docs: r2-" in forced.output


def test_an_unreachable_qdrant_is_reported_and_is_not_a_failure(
    run: object, tmp_path: Path
) -> None:
    """The whole argument for splitting the two stores, at the CLI.

    A pack that could not correct its knowledge because a *secondary* index was down would be
    worse than one that degrades: the lexical and dense halves are in Postgres and answer without
    it.
    """
    pack = pack_at(tmp_path, {"policy.md": POLICY})
    result = run(str(pack))  # type: ignore[operator]
    assert result.exit_code == 0
    assert "Qdrant is not answering" in result.output
    assert "vectors NOT indexed" in result.output
    assert "1 chunk(s)" in result.output


def test_an_unreadable_source_file_fails_loudly(run: object, tmp_path: Path) -> None:
    pack = pack_at(tmp_path, {"policy.md": POLICY})
    (pack / "knowledge" / "sources.yaml").write_text("documents: [oops", encoding="utf-8")
    result = run(str(pack))  # type: ignore[operator]
    assert result.exit_code == 1
    assert "invalid YAML" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        "a scheduled job's failure has to be a message, not a traceback"
    )


def test_a_missing_source_directory_fails_rather_than_reporting_success(
    run: object, tmp_path: Path
) -> None:
    pack = pack_at(tmp_path, {"policy.md": POLICY})
    shutil.rmtree(pack / "knowledge" / "docs")
    result = run(str(pack))  # type: ignore[operator]
    assert result.exit_code == 1
    assert "sync failed" in result.output


def test_a_pack_with_no_sources_says_so_and_succeeds(run: object, tmp_path: Path) -> None:
    pack = pack_at(tmp_path, {"policy.md": POLICY})
    (pack / "knowledge" / "sources.yaml").write_text("documents: []\n", encoding="utf-8")
    result = run(str(pack))  # type: ignore[operator]
    assert result.exit_code == 0
    assert "nothing to sync" in result.output


def test_naming_a_source_that_does_not_exist_is_an_error_not_a_silent_no_op(
    run: object, tmp_path: Path
) -> None:
    """A scheduled job invoked with a stale ``--source`` would otherwise report success for ever
    while indexing nothing."""
    pack = pack_at(tmp_path, {"policy.md": POLICY})
    result = run(str(pack), "--source", "not-a-source")  # type: ignore[operator]
    assert result.exit_code == 1
    assert "no such source" in result.output


def test_the_sample_pack_syncs(run: object) -> None:
    """The command a demo runs, on the pack a demo serves."""
    result = run(str(SAMPLE))  # type: ignore[operator]
    assert result.exit_code == 0, result.output
    assert "policy-docs: r1-" in result.output
    assert "2 document(s)" in result.output
