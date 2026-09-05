"""The ``support`` command line. Implements DESIGN.md section 18 (``cli/``).

Command shape follows the design: ``support pack validate | knowledge sync | eval | replay``.
Only ``pack validate`` does anything in phase 0; the other commands exist so the shape is
fixed early and each one says which phase delivers it.
"""

import sys
from pathlib import Path

import click

from support_core import __version__
from support_core.graph.validator import validate_pack

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
def knowledge_sync(path: Path) -> None:
    """Fetch, chunk, embed and index the pack's knowledge sources (phase 5)."""
    _not_implemented("support pack knowledge sync", phase=5)


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
