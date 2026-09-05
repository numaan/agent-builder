"""``support pack validate`` and the layout validator behind it (DESIGN.md sections 5, 5.2).

Phase 0 exit criterion: the sample pack is reported as "empty but well-formed". The other tests
pin the finding rule names so phase 1 can extend the validator without breaking the CLI contract.
"""

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from support_core.cli.main import EXIT_INVALID, EXIT_NOT_IMPLEMENTED, EXIT_OK, cli
from support_core.graph.manifest import ManifestError, load_manifest
from support_core.graph.validator import Severity, validate_pack
from tests.conftest import SAMPLE_PACK


@pytest.fixture
def pack_copy(tmp_path: Path) -> Path:
    """A writable copy of the sample pack for breaking things."""
    target = tmp_path / "acme_billing"
    shutil.copytree(SAMPLE_PACK, target)
    return target


def _rules(pack: Path, severity: Severity | None = None) -> set[str]:
    report = validate_pack(pack)
    return {f.rule for f in report.findings if severity is None or f.severity is severity}


def test_sample_pack_is_empty_but_well_formed_via_cli() -> None:
    result = CliRunner().invoke(cli, ["pack", "validate", str(SAMPLE_PACK)])
    assert result.exit_code == EXIT_OK, result.output
    assert "acme-billing: empty but well-formed" in result.output
    assert "pack.empty" in result.output


def test_sample_pack_report() -> None:
    report = validate_pack(SAMPLE_PACK)
    assert report.ok
    assert report.empty
    assert report.manifest is not None
    assert report.manifest.id == "acme-billing"
    assert report.manifest.entry_graph == "root"
    assert report.manifest.channels == ["web_chat", "email"]
    assert report.manifest.handoff.queue == "billing-tier-1"
    assert report.manifest.limits.max_nodes_per_turn == 25
    assert {f.rule for f in report.findings} == {"pack.empty"}


def test_sample_manifest_matches_design_section_5_1() -> None:
    manifest = load_manifest(SAMPLE_PACK)
    assert manifest.llm.default_model == "claude-sonnet-5"
    assert manifest.llm.escalation_model == "claude-opus-5"
    assert manifest.interrupts.allowed_from == ["root", "refund", "update_address"]
    assert manifest.interrupts.blocked_in == ["verify_identity", "payment_capture"]
    assert manifest.handoff.sla_minutes == 30
    assert manifest.limits.max_llm_cost_per_conversation_usd == 2.0
    assert manifest.core_compatible()


def test_missing_path_is_an_error(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["pack", "validate", str(tmp_path / "nope")])
    assert result.exit_code == EXIT_INVALID
    assert "layout.not_a_directory" in result.output


def test_missing_required_files_and_dirs(pack_copy: Path) -> None:
    (pack_copy / "policies.md").unlink()
    shutil.rmtree(pack_copy / "evals" / "nodes")
    report = validate_pack(pack_copy)
    assert not report.ok
    by_rule = {(f.rule, f.location) for f in report.errors}
    assert ("layout.missing_file", "policies.md") in by_rule
    assert ("layout.missing_dir", "evals/nodes") in by_rule

    result = CliRunner().invoke(cli, ["pack", "validate", str(pack_copy)])
    assert result.exit_code == EXIT_INVALID
    assert "2 errors; pack is not valid" in result.output


def _drop_top_level_key(yaml_text: str, key: str) -> str:
    """Remove a top-level ``key:`` line and every indented line that belongs to it."""
    kept: list[str] = []
    dropping = False
    for line in yaml_text.splitlines(keepends=True):
        if line.startswith(f"{key}:"):
            dropping = True
            continue
        if dropping and (line.startswith((" ", "\t")) or not line.strip()):
            continue
        dropping = False
        kept.append(line)
    return "".join(kept)


@pytest.mark.parametrize(
    "key", ["id", "version", "core", "entry_graph", "channels", "llm", "handoff"]
)
def test_manifest_required_keys(pack_copy: Path, key: str) -> None:
    manifest_path = pack_copy / "pack.yaml"
    original = manifest_path.read_text(encoding="utf-8")
    mutated = _drop_top_level_key(original, key)
    assert mutated != original, f"fixture did not contain top-level key {key!r}"
    manifest_path.write_text(mutated, encoding="utf-8")
    with pytest.raises(ManifestError, match=rf"{key}: Field required"):
        load_manifest(pack_copy)
    assert "manifest.invalid" in _rules(pack_copy, Severity.ERROR)


@pytest.mark.parametrize("key", ["language", "interrupts", "limits"])
def test_manifest_optional_keys_have_defaults(pack_copy: Path, key: str) -> None:
    manifest_path = pack_copy / "pack.yaml"
    manifest_path.write_text(
        _drop_top_level_key(manifest_path.read_text(encoding="utf-8"), key), encoding="utf-8"
    )
    manifest = load_manifest(pack_copy)
    assert manifest.language == "en"
    assert manifest.limits.max_tool_calls_per_turn == 10
    assert validate_pack(pack_copy).ok


def test_manifest_rejects_unknown_keys_and_channels(pack_copy: Path) -> None:
    manifest_path = pack_copy / "pack.yaml"
    manifest_path.write_text(manifest_path.read_text() + "surprise: true\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="surprise"):
        load_manifest(pack_copy)

    manifest_path.write_text(
        manifest_path.read_text()
        .replace("surprise: true\n", "")
        .replace("channels: [web_chat, email]", "channels: [carrier_pigeon]"),
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="channels"):
        load_manifest(pack_copy)


def test_manifest_core_compatibility(pack_copy: Path) -> None:
    manifest_path = pack_copy / "pack.yaml"
    manifest_path.write_text(
        manifest_path.read_text().replace('core: ">=0.0.1,<1"', 'core: ">=1.4,<2"'),
        encoding="utf-8",
    )
    assert not load_manifest(pack_copy).core_compatible()
    assert "manifest.core_incompatible" in _rules(pack_copy, Severity.ERROR)


@pytest.mark.parametrize("value", ['""', '"   "'])
def test_manifest_rejects_empty_core_specifier(pack_copy: Path, value: str) -> None:
    """An empty specifier matches every version, silently disabling the compatibility check."""
    manifest_path = pack_copy / "pack.yaml"
    manifest_path.write_text(
        manifest_path.read_text().replace('core: ">=0.0.1,<1"', f"core: {value}"),
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match=r"core.*at least one version constraint"):
        load_manifest(pack_copy)
    assert "manifest.invalid" in _rules(pack_copy, Severity.ERROR)


def test_manifest_interrupt_lists_must_not_overlap(pack_copy: Path) -> None:
    manifest_path = pack_copy / "pack.yaml"
    manifest_path.write_text(
        manifest_path.read_text().replace("blocked_in: [verify_identity", "blocked_in: [refund"),
        encoding="utf-8",
    )
    assert "manifest.interrupts_conflict" in _rules(pack_copy, Severity.ERROR)


def test_malformed_yaml_is_reported_not_raised(pack_copy: Path) -> None:
    (pack_copy / "pack.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    report = validate_pack(pack_copy)
    assert not report.ok
    assert {f.rule for f in report.errors} == {"manifest.invalid"}


@pytest.mark.parametrize(
    ("rel", "rule"),
    [
        ("pack.yaml", "manifest.unreadable"),
        ("policies.md", "policies.unreadable"),
        ("knowledge/sources.yaml", "knowledge.sources_unreadable"),
        ("tools/__init__.py", "tools.unreadable"),
    ],
)
def test_non_utf8_files_are_reported_not_raised(pack_copy: Path, rel: str, rule: str) -> None:
    """validate_pack promises never to raise for a bad pack; a latin-1 byte must not break it."""
    (pack_copy / rel).write_bytes(b"\xff\xfe not utf-8\n")
    report = validate_pack(pack_copy)
    assert not report.ok
    assert rule in {f.rule for f in report.errors}

    result = CliRunner().invoke(cli, ["pack", "validate", str(pack_copy)])
    assert result.exit_code == EXIT_INVALID
    assert rule in result.output, "the CLI must print the finding, not a traceback"


def test_tools_module_must_export_tools(pack_copy: Path) -> None:
    (pack_copy / "tools" / "__init__.py").write_text('"""no tools"""\n', encoding="utf-8")
    assert "tools.no_export" in _rules(pack_copy, Severity.ERROR)


def test_knowledge_sources_shape(pack_copy: Path) -> None:
    sources = pack_copy / "knowledge" / "sources.yaml"
    sources.write_text("documents: {}\nrandom_section: []\n", encoding="utf-8")
    report = validate_pack(pack_copy)
    messages = [f.message for f in report.errors if f.rule == "knowledge.sources_invalid"]
    assert len(messages) == 2
    assert any("must be a list" in m for m in messages)
    assert any("random_section" in m for m in messages)

    sources.write_text("", encoding="utf-8")
    assert "knowledge.sources_invalid" not in _rules(pack_copy)


def test_policies_length_warning(pack_copy: Path) -> None:
    (pack_copy / "policies.md").write_text(
        "\n".join(f"- rule {i}" for i in range(60)), encoding="utf-8"
    )
    report = validate_pack(pack_copy)
    assert report.ok, "a warning must not fail the pack"
    assert "policies.too_long" in {f.rule for f in report.warnings}

    strict = CliRunner().invoke(cli, ["pack", "validate", "--strict", str(pack_copy)])
    assert strict.exit_code == EXIT_INVALID
    lenient = CliRunner().invoke(cli, ["pack", "validate", str(pack_copy)])
    assert lenient.exit_code == EXIT_OK


def test_graph_files_hit_the_phase_1_hook(pack_copy: Path) -> None:
    (pack_copy / "graphs" / "root.yaml").write_text("id: root\n", encoding="utf-8")
    report = validate_pack(pack_copy)
    assert report.ok
    assert not report.empty
    assert report.graph_files == ["graphs/root.yaml"]
    assert "graph.not_validated" in {f.rule for f in report.warnings}
    assert report.summary() == "acme-billing: well-formed (1 warning(s))"


def test_entry_graph_must_exist_once_graphs_are_present(pack_copy: Path) -> None:
    (pack_copy / "graphs" / "refund.yaml").write_text("id: refund\n", encoding="utf-8")
    assert "graph.entry_missing" in _rules(pack_copy, Severity.ERROR)


@pytest.mark.parametrize(
    "args",
    [
        ["pack", "knowledge", "sync", "packs/acme_billing"],
        ["pack", "eval", "packs/acme_billing"],
        ["replay", "some-conversation-id"],
    ],
)
def test_future_commands_say_which_phase(args: list[str]) -> None:
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == EXIT_NOT_IMPLEMENTED
    assert "not implemented yet" in result.output
    assert "phase" in result.output
