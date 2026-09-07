"""The ``support`` command line. Implements DESIGN.md section 18 (``cli/``).

Command shape follows the design: ``support pack validate | knowledge sync | eval | replay``.
Only ``pack validate`` does anything in phase 0; the other commands exist so the shape is
fixed early and each one says which phase delivers it.
"""

import asyncio
import sys
from pathlib import Path

import click

from support_core import __version__
from support_core.graph.validator import validate_pack
from support_core.knowledge.ingest import SyncResult
from support_core.knowledge.sources import SourceError, load_sources
from support_core.knowledge.wiring import build_ingestor
from support_core.storage.session import make_engine, make_session_factory

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_NOT_IMPLEMENTED = 3


@click.group()
@click.version_option(__version__, prog_name="support")
def cli() -> None:
    """support-core: run and validate customer support domain packs."""


@cli.group()
def pack() -> None:
    """Domain pack commands."""


@pack.command("validate")
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--strict", is_flag=True, help="Exit 1 on warnings as well as errors.")
@click.option("--quiet", "-q", is_flag=True, help="Print only the summary line.")
def pack_validate(path: Path, strict: bool, quiet: bool) -> None:
    """Validate the domain pack at PATH (DESIGN.md section 5.2).

    Exit status 0 when the pack is well-formed, 1 when it has errors (or warnings with
    --strict). Every finding is printed in both modes; --strict changes only the exit
    status.
    """
    report = validate_pack(path)
    if not quiet:
        for finding in report.findings:
            click.echo(finding.render())
    click.echo(report.summary())
    failed = not report.ok or (strict and report.warnings)
    sys.exit(EXIT_INVALID if failed else EXIT_OK)


@pack.group()
def knowledge() -> None:
    """Knowledge source commands."""


@knowledge.command("sync")
@click.argument("path", type=click.Path(path_type=Path))
@click.option(
    "--force",
    is_flag=True,
    help="Re-index even when the corpus hashes to what the last sync saw.",
)
@click.option("--source", "only", multiple=True, help="Sync only these source ids.")
def knowledge_sync(path: Path, force: bool, only: tuple[str, ...]) -> None:
    """Fetch, chunk, embed and index the pack's knowledge sources (DESIGN.md section 9.3).

    "Fetch, normalize to markdown, chunk by headings, embed, upsert, and mark stale chunks",
    which is section 9.3's own sentence. Old chunks are marked stale rather than deleted, and the
    vector index is built as a new collection whose alias is flipped at the end, so a trace taken
    before this ran still names - and can still be shown - the version that produced it.

    Exit status 0 when every source synced, 1 when a source could not be read at all. A Qdrant
    that could not be reached is *not* a failure: the lexical and dense halves are committed in
    Postgres and answer without it, and a pack that could not correct its knowledge because a
    secondary index was down would be a worse system than one that degrades.
    """
    sources = load_sources(path)
    wanted = [s for s in sources.documents if not only or s.id in only]
    unknown = sorted(set(only) - {s.id for s in sources.documents})
    if unknown:
        click.echo(f"no such source(s) in {path}: {', '.join(unknown)}", err=True)
        sys.exit(EXIT_INVALID)
    if not wanted:
        click.echo(f"{path} declares no document sources; nothing to sync.")
        sys.exit(EXIT_OK)

    async def run() -> list[SyncResult]:
        engine = make_engine()
        try:
            ingestor = build_ingestor(path, make_session_factory(engine))
            if ingestor.store is not None and not await ingestor.store.ping():
                click.echo(
                    "Qdrant is not answering; the lexical and dense halves will be indexed and "
                    "the vector collection will not. Re-run when it is back.",
                    err=True,
                )
            return [await ingestor.sync_source(source, force=force) for source in wanted]
        finally:
            await engine.dispose()

    try:
        results = asyncio.run(run())
    except (FileNotFoundError, SourceError) as exc:
        click.echo(f"sync failed: {exc}", err=True)
        sys.exit(EXIT_INVALID)
    for result in results:
        click.echo(result.line())
    sys.exit(EXIT_OK)


@pack.command("eval")
@click.argument("path", type=click.Path(path_type=Path))
def pack_eval(path: Path) -> None:
    """Run the pack's node evals, golden conversations and the adversarial suite (phase 8)."""
    _not_implemented("support pack eval", phase=8)


@cli.command("replay")
@click.argument("conversation_id")
def replay(conversation_id: str) -> None:
    """Render the frame stack of a conversation over time (phase 7)."""
    _not_implemented("support replay", phase=7)


def _not_implemented(command: str, *, phase: int) -> None:
    click.echo(f"{command} is not implemented yet; it arrives in phase {phase}.", err=True)
    sys.exit(EXIT_NOT_IMPLEMENTED)


if __name__ == "__main__":  # pragma: no cover
    cli()
