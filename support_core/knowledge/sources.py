"""``knowledge/sources.yaml``, typed. Implements DESIGN.md section 9.1.

Section 9.1 shows the file and names three sections: ``documents`` (each with an ``id``, a
``type``, and the location that type needs), ``knowledge_graph`` and ``live_lookups``. Phase 0's
validator checked that the top level was a mapping of those three keys onto lists and nothing
else; everything inside was unread until a sync tripped over it.

This module is the schema, and it is shared: :func:`support_core.graph.validator.validate_pack`
uses it, so a source file with a missing path or an unknown type is a *load-time finding*
against the pack rather than a traceback in a scheduled job at three in the morning. That is the
same argument phase 1 made for type-checking tool arguments at load.

``knowledge_graph`` is parsed and not loaded: entities and relations are phase 9's. Parsing it
anyway is deliberate - a pack that declares one gets its typos caught now, and phase 9 inherits
a schema instead of inventing one.
"""

import ipaddress
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

SOURCE_FILE = Path("knowledge") / "sources.yaml"

ID_PATTERN = r"^[a-z][a-z0-9-]*$"
"""Source ids reach a Qdrant collection name and a trace, so they are constrained the way node
ids are. A dash rather than an underscore because that is what DESIGN.md 9.1's own example
uses (``help-center``, ``policy-docs``)."""


class SourceError(ValueError):
    """``knowledge/sources.yaml`` is missing, unreadable, or not what the schema allows."""


class MarkdownDirSource(BaseModel):
    """A directory of markdown files, walked recursively (DESIGN.md section 9.1)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    type: Literal["markdown_dir"]
    path: str
    """Relative to the pack directory. An absolute path is refused: a pack has to be the same
    thing on a developer's laptop and in a container, and a source that reads from outside the
    pack is not part of the pack."""

    refresh: Literal["manual", "hourly", "daily", "weekly"] = "manual"
    """What a scheduler would do with it (section 9.3, "runs on a schedule in production").
    Nothing in core schedules anything yet; the field is here so the pack can say, and so the
    scheduler phase 7 owns finds the answer rather than a new key to invent."""

    def resolve(self, pack_path: Path) -> Path:
        return (pack_path / self.path).resolve()


class HtmlCrawlSource(BaseModel):
    """A web page and the pages under it, normalised to markdown (DESIGN.md section 9.1)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    type: Literal["html_crawl"]
    url: str
    depth: int = Field(default=1, ge=1, le=3)
    """How many links deep to follow, counting the start page as one.

    Bounded in the schema rather than by a comment, because an unbounded crawl of a help centre
    is a way to turn a pack into an unpredictably large index and a scheduled job into an
    outage. Three is already more than the design's example needs."""

    max_pages: int = Field(default=50, ge=1, le=2000)
    refresh: Literal["manual", "hourly", "daily", "weekly"] = "daily"


DocumentSource = Annotated[MarkdownDirSource | HtmlCrawlSource, Field(discriminator="type")]


class KnowledgeGraphSource(BaseModel):
    """Parsed here, loaded in phase 9 (DESIGN.md section 9.1's ``knowledge_graph``)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    type: Literal["yaml"]
    path: str


class LiveLookup(BaseModel):
    """A READ tool the retriever may call for a dynamic fact (DESIGN.md section 9.1).

    ``triggers`` is not in the design and is what makes this implementable without a second
    model call. Section 9.1 routes "entity-specific questions" to a tool; deciding *which*
    question is entity-specific, and what arguments the tool needs, is a classification problem
    with nothing constraining its answer. A pack that says "call ``get_plan_details`` when the
    question mentions any of these words" has made that decision itself, in a line a reviewer can
    read. The narrower shape is recorded as a deviation in reviews/phase-5.md.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str
    triggers: list[str] = Field(default_factory=list)
    """Lowercase terms; the lookup fires when the query contains one. Empty means every query,
    which is what a pack with a single always-relevant lookup wants."""

    args: dict[str, str] = Field(default_factory=dict)
    """Argument name to a ``ctx.`` expression, evaluated against the conversation context. Only
    ``ctx`` - not ``state``, because a retriever runs for a node in any frame and has no business
    reading another workflow's state, and not a literal from the query, because that is the
    extraction step this shape exists to avoid."""


class KnowledgeSources(BaseModel):
    """The whole file (DESIGN.md section 9.1)."""

    model_config = ConfigDict(extra="forbid")

    documents: list[DocumentSource] = Field(default_factory=list)
    knowledge_graph: list[KnowledgeGraphSource] = Field(default_factory=list)
    live_lookups: list[LiveLookup] = Field(default_factory=list)

    def document(self, source_id: str) -> DocumentSource:
        for source in self.documents:
            if source.id == source_id:
                return source
        msg = f"no document source {source_id!r} in {SOURCE_FILE.as_posix()}"
        raise SourceError(msg)

    def source_ids(self) -> list[str]:
        return [source.id for source in self.documents]


def parse_sources(raw: Any) -> KnowledgeSources:
    """Validate an already-parsed mapping. Raises :class:`SourceError` with a readable message."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        msg = "top level must be a mapping"
        raise SourceError(msg)
    normalised = {key: ([] if value is None else value) for key, value in raw.items()}
    try:
        sources = KnowledgeSources.model_validate(normalised)
    except ValidationError as exc:
        raise SourceError(_readable(exc)) from exc
    _check_unique(sources)
    return sources


def _check_unique(sources: KnowledgeSources) -> None:
    """One id per source, across documents and graphs alike.

    They share the id namespace because a ``Passage.source_id`` names one of them and a trace
    that cannot tell a document from a graph is not a trace. Phase 9 adds the graphs; the
    constraint is cheaper to state now than to retrofit onto packs that already exist.
    """
    seen: set[str] = set()
    for source_id in [s.id for s in sources.documents] + [s.id for s in sources.knowledge_graph]:
        if source_id in seen:
            msg = f"duplicate source id {source_id!r}"
            raise SourceError(msg)
        seen.add(source_id)


def _readable(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"]) or "(top level)"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def load_sources(pack_path: Path) -> KnowledgeSources:
    """Read and validate ``<pack>/knowledge/sources.yaml``.

    A missing file is an empty set of sources rather than an error: DESIGN.md section 5.2 makes
    the file part of a pack's layout and the validator already reports it missing, and a second
    complaint from the sync would say nothing new.
    """
    path = pack_path / SOURCE_FILE
    if not path.is_file():
        return KnowledgeSources()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"{SOURCE_FILE.as_posix()} is unreadable: {exc}"
        raise SourceError(msg) from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        msg = f"{SOURCE_FILE.as_posix()} is invalid YAML: {exc}"
        raise SourceError(msg) from exc
    except RecursionError as exc:
        # The same guard every other YAML load in this repository carries: a deeply nested
        # document is a parser bomb, and a pack is not necessarily written by a friend.
        msg = f"{SOURCE_FILE.as_posix()} is nested too deeply to parse"
        raise SourceError(msg) from exc
    return parse_sources(raw)


def contained_path(pack_path: Path, source: MarkdownDirSource) -> Path:
    """Resolve ``source.path`` under ``pack_path``, refusing an absolute or escaping path.

    This is the one place that decides "is this path inside the pack", and it is called from two
    places that must never disagree: :func:`path_findings` (``support pack validate``) and
    :func:`~support_core.knowledge.ingest.read_markdown_dir` (``support pack knowledge sync``,
    the command a pack's own README documents for routine knowledge updates). Before this
    function existed the two checked the boundary separately and only the validator's copy
    actually refused anything - a pack whose ``sources.yaml`` was edited to point outside the
    pack directory *after* being validated would sync and index whatever it now named. Raising
    here, at the point the directory is about to be walked, closes that whether or not an
    operator ever ran ``pack validate`` again.

    Raises :class:`SourceError` with no source id in the message, because the two callers name it
    differently (a ``(id, problem)`` pair here, an id-prefixed exception there).
    """
    if Path(source.path).is_absolute():
        msg = f"path {source.path!r} must be relative to the pack directory"
        raise SourceError(msg)
    resolved = source.resolve(pack_path)
    root = pack_path.resolve()
    if resolved != root and root not in resolved.parents:
        msg = f"path {source.path!r} escapes the pack directory"
        raise SourceError(msg)
    return resolved


_LOOPBACK_HOSTNAMES = frozenset({"localhost"})


def validated_crawl_url(source: HtmlCrawlSource) -> str:
    """``source.url``, refusing a non-http(s) scheme or a loopback/link-local host.

    The same argument as :func:`contained_path`, for the other kind of source: ``crawl()`` opens
    a socket, and the scheme check that guarded it previously lived only in ``path_findings``.
    Loopback and link-local hosts are refused too - ``path_findings`` used to accept any
    http(s) URL, which would pass ``http://169.254.169.254/...`` (a cloud metadata endpoint) or
    ``http://localhost:6333/...`` straight into a crawl a scheduler runs unattended.

    A crawl never leaves the start URL's origin (``crawl()``'s own boundary rule), so checking
    the start URL once is checking every page the crawl could ever fetch.
    """
    parsed = urlparse(source.url)
    if parsed.scheme not in ("http", "https"):
        msg = f"url {source.url!r} must be http or https"
        raise SourceError(msg)
    host = parsed.hostname
    if not host:
        msg = f"url {source.url!r} has no host"
        raise SourceError(msg)
    if host.lower() in _LOOPBACK_HOSTNAMES:
        msg = f"url {source.url!r} names a loopback host"
        raise SourceError(msg)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (address.is_loopback or address.is_link_local):
        msg = f"url {source.url!r} names a loopback or link-local address"
        raise SourceError(msg)
    return source.url


def path_findings(pack_path: Path, sources: KnowledgeSources) -> Iterable[tuple[str, str]]:
    """``(source id, problem)`` for every source whose location cannot be read.

    Separate from :func:`parse_sources` because the schema can be checked anywhere and this needs
    the pack on disk. The validator turns each pair into a finding. Delegates the boundary checks
    to :func:`contained_path` and :func:`validated_crawl_url`, which are also what the sync path
    calls, so a pack that passes validation and a pack that syncs are checked the same way.
    """
    for source in sources.documents:
        if isinstance(source, MarkdownDirSource):
            try:
                resolved = contained_path(pack_path, source)
            except SourceError as exc:
                yield source.id, str(exc)
                continue
            if not resolved.is_dir():
                yield source.id, f"path {source.path!r} is not a directory in this pack"
        else:
            try:
                validated_crawl_url(source)
            except SourceError as exc:
                yield source.id, str(exc)
    for graph in sources.knowledge_graph:
        if not (pack_path / graph.path).is_file():
            yield graph.id, f"path {graph.path!r} is not a file in this pack"
