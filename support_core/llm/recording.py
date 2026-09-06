"""Recorded model responses: the storage format behind DESIGN.md section 11.1's fake provider.
Implements PLAN.md's "Phases 3+ use a recorded-response fake provider in tests; live calls only
in a marked ``live`` test group".

A cassette is a JSON file holding, for each interaction, the *whole canonical request* beside
the SHA-256 of it (:meth:`~support_core.llm.types.CompletionRequest.fingerprint`) and the
response. Storing the request is the point of the format: it is what lets a machine that has an
``ANTHROPIC_API_KEY`` regenerate the file against the live API without a single test changing.
The generator re-drives the same scenarios through
``RecordingProvider(AnthropicProvider(...))`` instead of
``RecordingProvider(ScriptedProvider(...))`` and rewrites the file; the tests keep asking the
same questions and keep looking up by fingerprint.

Two properties follow from keying on the fingerprint rather than on a label:

* A change to prompt assembly - a new layer, a re-ordered one, a different fence - changes every
  key, and the replay fails loudly with the missing fingerprint rather than quietly answering a
  question nobody asked.
* Two calls that assemble to the same bytes share one recording, which is what makes a golden
  conversation reproducible turn after turn.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from support_core.llm.provider import LLMProvider, StructuredByCompletion
from support_core.llm.types import CompletionRequest, CompletionResponse, LLMError

CASSETTE_VERSION = 1


class CassetteMiss(LLMError):
    """The cassette has no recording for this request.

    Carries the fingerprint and the request so a developer can see *what* was asked, not only
    that something was. Almost always means prompt assembly changed and the cassettes need
    regenerating - which is one command, and a test says so.
    """

    def __init__(self, request: CompletionRequest, path: Path | None) -> None:
        self.request = request
        self.fingerprint = request.fingerprint()
        where = f" (cassette {path})" if path else ""
        super().__init__(
            f"no recorded response for {request.purpose!r} request {self.fingerprint}{where}; "
            f"re-record the cassettes with `python -m tests.cassettes.build_cassettes` "
            f"(add --live to record against the real API)"
        )


@dataclass(slots=True)
class Interaction:
    """One recorded request and its response."""

    key: str
    purpose: str
    model: str
    request: dict[str, Any]
    response: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "purpose": self.purpose,
            "model": self.model,
            "request": self.request,
            "response": self.response,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Interaction":
        return cls(
            key=str(raw["key"]),
            purpose=str(raw.get("purpose", "node")),
            model=str(raw.get("model", "")),
            request=dict(raw.get("request") or {}),
            response=dict(raw["response"]),
        )


@dataclass(slots=True)
class Cassette:
    """A file of recorded interactions, keyed by request fingerprint."""

    description: str = ""
    recorded_with: str = "scripted"
    """Which provider produced the responses: ``scripted`` offline, ``anthropic`` from a live
    run. Recorded so a reader of the file knows whether it reflects a real model."""

    recorded_at: str | None = None
    interactions: dict[str, Interaction] = field(default_factory=dict)
    path: Path | None = None

    def put(self, request: CompletionRequest, response: CompletionResponse) -> None:
        key = request.fingerprint()
        self.interactions[key] = Interaction(
            key=key,
            purpose=request.purpose,
            model=request.model,
            request=request.canonical(),
            response=response.model_dump(mode="json"),
        )

    def get(self, request: CompletionRequest) -> CompletionResponse | None:
        found = self.interactions.get(request.fingerprint())
        return CompletionResponse.model_validate(found.response) if found else None

    def requests(self) -> Iterator[CompletionRequest]:
        """Every recorded request, for a regeneration pass against a live provider."""
        for interaction in self.interactions.values():
            yield CompletionRequest.model_validate(interaction.request)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": CASSETTE_VERSION,
            "description": self.description,
            "recorded_with": self.recorded_with,
            "recorded_at": self.recorded_at,
            "interactions": [i.to_json() for i in self.interactions.values()],
        }

    def dumps(self) -> str:
        return json.dumps(self.to_json(), indent=2, ensure_ascii=False, sort_keys=False) + "\n"

    def save(self, path: Path | None = None) -> Path:
        target = path or self.path
        if target is None:
            msg = "a cassette needs a path to be saved to"
            raise ValueError(msg)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.dumps(), encoding="utf-8")
        self.path = target
        return target

    @classmethod
    def load(cls, path: Path) -> "Cassette":
        raw = json.loads(path.read_text(encoding="utf-8"))
        version = raw.get("version")
        if version != CASSETTE_VERSION:
            msg = f"{path}: cassette version {version!r}, expected {CASSETTE_VERSION}"
            raise ValueError(msg)
        cassette = cls(
            description=str(raw.get("description", "")),
            recorded_with=str(raw.get("recorded_with", "scripted")),
            recorded_at=raw.get("recorded_at"),
            path=path,
        )
        for entry in raw.get("interactions", []):
            interaction = Interaction.from_json(entry)
            cassette.interactions[interaction.key] = interaction
        return cassette


def load_cassettes(directory: Path) -> Cassette:
    """Every ``*.json`` cassette in a directory, merged into one.

    What a *service* replaying recorded conversations needs, as against a test, which replays
    one: a person typing at the running application picks which recorded conversation to have,
    and the provider has to hold all of them. Merging is safe because the key is the request
    fingerprint - two recordings of the same request are the same recording - and a genuine
    disagreement about one fingerprint is refused rather than resolved by directory order.
    """
    merged = Cassette(description=f"every cassette in {directory}", path=directory)
    origin: dict[str, Path] = {}
    for path in sorted(directory.glob("*.json")):
        cassette = Cassette.load(path)
        for key, interaction in cassette.interactions.items():
            seen = merged.interactions.get(key)
            if seen is not None and seen.response != interaction.response:
                msg = (
                    f"{path} and {origin[key]} record different responses for the same request "
                    f"{key}; one of them is stale"
                )
                raise ValueError(msg)
            merged.interactions[key] = interaction
            origin[key] = path
    if not merged.interactions:
        msg = f"no cassettes in {directory}: nothing to replay"
        raise ValueError(msg)
    return merged


class RecordingProvider(StructuredByCompletion):
    """Wrap any provider and write everything it answers into a cassette.

    Wrapping ``complete`` alone is enough because
    :class:`~support_core.llm.provider.StructuredByCompletion` routes ``structured`` through it,
    so a structured call is recorded with the schema it was made under - which is part of the
    fingerprint, and therefore part of what a replay has to match.
    """

    def __init__(self, inner: LLMProvider, cassette: Cassette | None = None) -> None:
        self.inner = inner
        self.cassette = cassette or Cassette(recorded_with=inner.name)
        self.cassette.recorded_with = inner.name

    @property
    def name(self) -> str:
        return f"recording({self.inner.name})"

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        response = await self.inner.complete(req)
        self.cassette.put(req, response)
        self.cassette.recorded_at = datetime.now(UTC).isoformat(timespec="seconds")
        return response
