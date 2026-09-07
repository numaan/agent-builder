"""``knowledge/sources.yaml``, its schema, and the load-time findings it produces. Phase 5.

DESIGN.md section 9.1 shows the file; phase 0's validator checked only that the top level was a
mapping of three known keys onto lists, so a source with a missing path or an unknown type was
discovered by a scheduled sync at three in the morning. This phase widens the check to the schema
the sync itself reads, which is the same argument phase 1 made for type-checking tool arguments
at load: a pack that cannot work should say so when it is loaded.

Also here: the live-lookup retriever of section 9.1, because what it may call is declared in this
file and enforced by phase 4's gateway.
"""

from pathlib import Path
from typing import Any

import pytest

from support_core import load_pack
from support_core.graph.loader import PackValidationError
from support_core.graph.validator import validate_pack
from support_core.knowledge.live import LiveLookupRetriever, declared_tools
from support_core.knowledge.sources import (
    HtmlCrawlSource,
    LiveLookup,
    MarkdownDirSource,
    SourceError,
    load_sources,
    parse_sources,
    path_findings,
)
from support_core.llm.tool_loop import ModelToolSpec, ReadOnlyToolGateway, ToolOutcome
from support_core.tools.risk import Risk
from tests.engine_support import PACKS
from tests.knowledge_support import request

SAMPLE = Path("packs/acme_billing")
KNOWLEDGE_PACK = PACKS / "knowledge_pack"


# -- the schema --------------------------------------------------------------------------------


def test_the_design_s_own_example_parses() -> None:
    """Section 9.1's file, verbatim apart from the paths."""
    parsed = parse_sources(
        {
            "documents": [
                {
                    "id": "help-center",
                    "type": "html_crawl",
                    "url": "https://help.acme.com/billing",
                    "refresh": "daily",
                },
                {"id": "policy-docs", "type": "markdown_dir", "path": "./docs/policies"},
            ],
            "knowledge_graph": [{"id": "products", "type": "yaml", "path": "./kg/products.yaml"}],
            "live_lookups": [{"tool": "get_plan_details"}],
        }
    )
    assert parsed.source_ids() == ["help-center", "policy-docs"]
    assert isinstance(parsed.document("policy-docs"), MarkdownDirSource)
    assert parsed.knowledge_graph[0].id == "products"
    assert parsed.live_lookups[0].tool == "get_plan_details"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"documents": [{"id": "x", "type": "unknown_kind"}]}, "type"),
        ({"documents": [{"id": "x", "type": "markdown_dir"}]}, "path"),
        ({"documents": [{"id": "x", "type": "html_crawl"}]}, "url"),
        ({"documents": [{"id": "Bad Id", "type": "markdown_dir", "path": "./d"}]}, "id"),
        ({"documents": [{"id": "x", "type": "markdown_dir", "path": "./d", "oops": 1}]}, "oops"),
        ({"unknown_section": []}, "unknown_section"),
        ({"documents": "not a list"}, "documents"),
        ("not a mapping", "mapping"),
    ],
)
def test_a_source_file_that_cannot_work_is_refused_with_a_readable_message(
    raw: Any, message: str
) -> None:
    with pytest.raises(SourceError) as caught:
        parse_sources(raw)
    assert message in str(caught.value)


def test_two_sources_may_not_share_an_id() -> None:
    """A ``Passage.source_id`` names one of them, and a trace that cannot tell a document from a
    graph is not a trace."""
    with pytest.raises(SourceError, match="duplicate source id"):
        parse_sources(
            {
                "documents": [{"id": "x", "type": "markdown_dir", "path": "./d"}],
                "knowledge_graph": [{"id": "x", "type": "yaml", "path": "./k.yaml"}],
            }
        )


def test_a_crawl_is_bounded_by_the_schema_rather_than_by_a_comment() -> None:
    """An unbounded crawl turns a pack into an unpredictably large index and a scheduled job into
    an outage."""
    with pytest.raises(SourceError):
        parse_sources(
            {"documents": [{"id": "x", "type": "html_crawl", "url": "http://a", "depth": 9}]}
        )
    source = HtmlCrawlSource(id="x", type="html_crawl", url="http://a")
    assert source.depth == 1
    assert source.max_pages == 50


def test_an_absent_or_empty_file_is_an_empty_set_of_sources(tmp_path: Path) -> None:
    """A pack with no knowledge is a legitimate pack; the validator already reports the file
    missing, and a second complaint from the sync would say nothing new."""
    assert load_sources(tmp_path).documents == []
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / "sources.yaml").write_text("", encoding="utf-8")
    assert load_sources(tmp_path).documents == []


def test_a_yaml_bomb_is_a_readable_error_and_not_a_stack_overflow(tmp_path: Path) -> None:
    """The same guard every other YAML load in this repository carries: a pack is not necessarily
    written by a friend."""
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / "sources.yaml").write_text("[" * 5000, encoding="utf-8")
    with pytest.raises(SourceError):
        load_sources(tmp_path)


# -- the paths ---------------------------------------------------------------------------------


def test_a_source_path_outside_the_pack_is_a_finding(tmp_path: Path) -> None:
    """A pack has to be the same thing on a laptop and in a container, and a source reading from
    outside the pack is not part of the pack."""
    escaping = parse_sources(
        {"documents": [{"id": "x", "type": "markdown_dir", "path": "../elsewhere"}]}
    )
    assert [problem for _, problem in path_findings(tmp_path, escaping)] == [
        "path '../elsewhere' escapes the pack directory"
    ]

    # An absolute path is refused too. Which of the two messages it gets is a platform detail -
    # ``/etc`` has no drive letter, so Windows calls it relative-but-escaping and Linux calls it
    # absolute - and both are the same refusal, so the test asserts the refusal rather than the
    # wording. The pack must load the same way on a laptop and in CI, which is the point.
    absolute = parse_sources(
        {"documents": [{"id": "x", "type": "markdown_dir", "path": "/etc"}]}
    )
    problem = next(iter(path_findings(tmp_path, absolute)))[1]
    assert "must be relative" in problem or "escapes the pack directory" in problem


def test_a_missing_directory_is_a_finding(tmp_path: Path) -> None:
    sources = parse_sources(
        {"documents": [{"id": "x", "type": "markdown_dir", "path": "./nowhere"}]}
    )
    assert "is not a directory" in next(iter(path_findings(tmp_path, sources)))[1]


def test_the_validator_reports_a_broken_source_file_at_load(tmp_path: Path) -> None:
    """The point of sharing the schema: a bad source file fails ``support pack validate``."""
    import shutil  # noqa: PLC0415

    pack = tmp_path / "pack"
    shutil.copytree(KNOWLEDGE_PACK, pack)
    (pack / "knowledge" / "sources.yaml").write_text(
        "documents:\n  - id: policy-docs\n    type: markdown_dir\n", encoding="utf-8"
    )
    report = validate_pack(pack)
    assert not report.ok
    assert any(finding.rule == "knowledge.sources_invalid" for finding in report.findings)
    with pytest.raises(PackValidationError, match="sources_invalid"):
        load_pack(pack)


def test_the_validator_warns_about_a_source_with_no_documents_in_it(tmp_path: Path) -> None:
    """A node with a ``knowledge:`` block over an empty corpus produces an empty layer 7, and the
    citation guardrail then turns its first factual claim into a handoff - correct behaviour
    arrived at for a reason nobody can see from outside."""
    import shutil  # noqa: PLC0415

    pack = tmp_path / "pack"
    shutil.copytree(KNOWLEDGE_PACK, pack)
    for path in (pack / "knowledge" / "docs").glob("*.md"):
        path.unlink()
    report = validate_pack(pack)
    assert report.ok, "an empty corpus is a warning, not an error"
    assert any(finding.rule == "knowledge.source_empty" for finding in report.findings)


def test_the_sample_pack_declares_the_documents_its_graphs_cite() -> None:
    """The pack the demo runs: a real source, pointing at documents that exist."""
    pack = load_pack(SAMPLE)
    assert pack.knowledge.source_ids() == ["policy-docs"]
    source = pack.knowledge.document("policy-docs")
    assert isinstance(source, MarkdownDirSource)
    files = sorted(path.name for path in source.resolve(SAMPLE).glob("*.md"))
    assert files == ["processing-times.md", "refund-policy.md"]
    assert list(path_findings(SAMPLE, pack.knowledge)) == []


def test_the_pack_carries_its_parsed_sources() -> None:
    """One reading of one file, shared by the sync, the composition root and the validator."""
    pack = load_pack(KNOWLEDGE_PACK)
    assert pack.knowledge.source_ids() == ["policy-docs"]


# -- live lookups ------------------------------------------------------------------------------


class Runner:
    """A tool runner that answers, so the gateway is what decides whether a call happens."""

    def __init__(self, risk: Risk = Risk.READ) -> None:
        self.specs = [
            ModelToolSpec(
                name="get_plan_details",
                description="What the plan includes.",
                input_schema={"type": "object", "properties": {"customer_ref": {"type": "string"}}},
                risk=risk,
            )
        ]
        self.invoked: list[tuple[str, dict[str, Any]]] = []

    async def describe(self, names: Any) -> Any:
        return [spec for spec in self.specs if spec.name in names]

    async def invoke(self, name: str, arguments: Any, *, call_id: str, step_id: str) -> ToolOutcome:
        self.invoked.append((name, dict(arguments)))
        return ToolOutcome(content='{"plan": "Pro", "seats": 5}')


def gateway(runner: Runner, *, declared: Any = ("get_plan_details",)) -> ReadOnlyToolGateway:
    return ReadOnlyToolGateway(
        runner=runner,
        declared=tuple(declared),
        manifest_risk={"get_plan_details": Risk.READ},
        max_calls=5,
    )


LOOKUP = LiveLookup(
    tool="get_plan_details",
    triggers=["plan", "included"],
    args={"customer_ref": "ctx.customer.ref"},
)


def context(ref: str | None = "cust_1") -> Any:
    from support_core.graph.context import ConversationContext  # noqa: PLC0415

    return ConversationContext.model_validate({"customer": {"ref": ref}} if ref else {})


async def test_a_declared_lookup_fires_on_its_trigger_and_becomes_a_passage() -> None:
    runner = Runner()
    retriever = LiveLookupRetriever([LOOKUP], gateway(runner))
    found = list(
        await retriever.retrieve(
            request("what is included in my plan").model_copy(update={"ctx": context()})
        )
    )
    assert runner.invoked == [("get_plan_details", {"customer_ref": "cust_1"})]
    assert len(found) == 1
    assert found[0].source_id == "tool:get_plan_details"
    assert found[0].source_version == "live"
    assert "Pro" in found[0].text
    assert "cust_1" in found[0].locator


async def test_a_lookup_does_not_fire_on_an_unrelated_question() -> None:
    runner = Runner()
    retriever = LiveLookupRetriever([LOOKUP], gateway(runner))
    assert (
        list(
            await retriever.retrieve(
                request("why was I charged twice").model_copy(update={"ctx": context()})
            )
        )
        == []
    )
    assert runner.invoked == []


async def test_a_lookup_whose_argument_the_conversation_does_not_have_is_skipped() -> None:
    """A pack saying "call this when you can". A retrieval that raised would turn a missing
    optional fact into a handoff."""
    runner = Runner()
    retriever = LiveLookupRetriever([LOOKUP], gateway(runner))
    found = await retriever.retrieve(
        request("what is in my plan").model_copy(update={"ctx": context(ref=None)})
    )
    assert list(found) == []
    assert runner.invoked == []


async def test_a_lookup_may_only_read_ctx() -> None:
    """Not ``state``: a retriever runs for a node in any frame and has no business reading
    another workflow's state. Not a literal from the query either - that is the extraction step
    this narrow shape exists to avoid."""
    runner = Runner()
    bad = LiveLookup(tool="get_plan_details", args={"customer_ref": "state.charge_id"})
    retriever = LiveLookupRetriever([bad], gateway(runner))
    assert (
        list(
            await retriever.retrieve(
                request("anything").model_copy(update={"ctx": context()})
            )
        )
        == []
    )
    assert runner.invoked == []


async def test_the_gateway_refuses_a_lookup_the_pack_did_not_declare_to_it() -> None:
    """The tool runtime is not bypassed. Everything phase 4's gateway refuses, this refuses."""
    runner = Runner()
    retriever = LiveLookupRetriever([LOOKUP], gateway(runner, declared=()))
    assert (
        list(
            await retriever.retrieve(
                request("what is in my plan").model_copy(update={"ctx": context()})
            )
        )
        == []
    )
    assert runner.invoked == []


async def test_a_write_tier_tool_can_never_be_reached_through_a_lookup() -> None:
    """PLAN.md's standing rule, on the surface this phase added: no WRITE or HIGH tool without an
    ``ActionApproval``. Here it holds because a WRITE tool cannot be called at all."""
    runner = Runner(risk=Risk.WRITE)
    retriever = LiveLookupRetriever([LOOKUP], gateway(runner))
    assert (
        list(
            await retriever.retrieve(
                request("what is in my plan").model_copy(update={"ctx": context()})
            )
        )
        == []
    )
    assert runner.invoked == []


def test_the_declared_tool_list_is_what_the_executor_builds_a_gateway_from() -> None:
    """The gateway is built before the retriever, so the list has to be readable without one."""
    lookups = [LOOKUP, LiveLookup(tool="get_plan_details"), LiveLookup(tool="other")]
    assert declared_tools(lookups) == ("get_plan_details", "other")
    assert LiveLookupRetriever(lookups, None).declared() == ("get_plan_details", "other")


async def test_a_retriever_with_no_gateway_answers_nothing_rather_than_failing() -> None:
    assert (
        list(await LiveLookupRetriever([LOOKUP], None).retrieve(request("what is in my plan")))
        == []
    )
