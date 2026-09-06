"""The loaded pack object and its graph version pin. Implements DESIGN.md sections 5 and 6.7.

DESIGN.md section 6.7: "Graphs are versioned with the pack. A running conversation pins the
graph version it started with. ... A migration hook lets a pack declare how to map old state to
new when a graph changes incompatibly; if none exists and the shapes differ, the conversation is
handed off."

Phase 1 delivers the *data structure* for that and nothing else: a :class:`PackPin` recording,
per graph, a content hash and a hash of the declared state shape. A conversation started under
one pin can be compared with a later pin by :meth:`PackPin.state_compatible`, which is the
"shapes differ" test the design's migration hook needs. Loading two pack versions side by side,
routing in-flight conversations to the old one, and the migration hook itself are phase 2 and
later; nothing here has run-time behaviour.
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, ConfigDict

from support_core.graph.manifest import PackManifest
from support_core.graph.schema import Graph
from support_core.graph.templates import make_environment
from support_core.graph.tools_manifest import ToolManifest
from support_core.tools.registry import ToolRegistry


class GraphPin(BaseModel):
    """The identity of one graph at one pack version."""

    model_config = ConfigDict(frozen=True)

    graph_id: str
    file: str
    source_hash: str
    """SHA-256 of the graph file text: any edit changes it."""

    state_shape_hash: str
    """SHA-256 of the declared state field names and type strings, and nothing else.

    Two versions with the same value hold the same state shape, so a conversation pinned to the
    older one can continue on the newer without a migration."""


class PackPin(BaseModel):
    """What a conversation pins when it starts (DESIGN.md section 6.7)."""

    model_config = ConfigDict(frozen=True)

    pack_id: str
    pack_version: str
    core_version: str
    graphs: dict[str, GraphPin]

    @property
    def fingerprint(self) -> str:
        """One hash over every graph, identifying this exact set of graph files."""
        payload = json.dumps(
            {
                "pack": [self.pack_id, self.pack_version],
                "graphs": {gid: pin.source_hash for gid, pin in sorted(self.graphs.items())},
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def graph(self, graph_id: str) -> GraphPin | None:
        return self.graphs.get(graph_id)

    def changed_graphs(self, other: "PackPin") -> set[str]:
        """Graph ids whose file content differs between two pins (added and removed included)."""
        ids = set(self.graphs) | set(other.graphs)
        return {
            gid
            for gid in ids
            if (self.graphs.get(gid) is None) != (other.graphs.get(gid) is None)  # added or removed
            or (
                gid in self.graphs
                and gid in other.graphs
                and self.graphs[gid].source_hash != other.graphs[gid].source_hash
            )
        }

    def state_compatible(self, other: "PackPin", graph_id: str) -> bool:
        """True when a frame's state for ``graph_id`` can move between the two pins unchanged.

        False means DESIGN.md section 6.7 applies: run the pack's migration hook, or hand off.
        """
        mine, theirs = self.graphs.get(graph_id), other.graphs.get(graph_id)
        if mine is None or theirs is None:
            return False
        return mine.state_shape_hash == theirs.state_shape_hash


def state_shape_hash(state_shape: dict[str, str]) -> str:
    """Hash the declared state fields. Order-independent; comments and layout do not count."""
    payload = json.dumps(sorted(state_shape.items()), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_pin(manifest: PackManifest, graphs: dict[str, Graph], core_version: str) -> PackPin:
    return PackPin(
        pack_id=manifest.id,
        pack_version=manifest.version,
        core_version=core_version,
        graphs={
            graph.id: GraphPin(
                graph_id=graph.id,
                file=graph.file,
                source_hash=graph.source_hash,
                state_shape_hash=state_shape_hash(graph.state_shape),
            )
            for graph in graphs.values()
        },
    )


@dataclass(slots=True)
class Pack:
    """A parsed, validated domain pack (DESIGN.md section 5)."""

    path: Path
    manifest: PackManifest
    persona: str
    policies: str
    graphs: dict[str, Graph]
    tools: ToolManifest
    """The pack's tools as *declarations*: what the validator type-checks arguments against.

    Built from the imported registry when the pack exports any (DESIGN.md section 8.3), and only
    from ``tools/tools.yaml`` when it exports none, in which case the pack cannot run a tool at
    all and the validator says so."""

    pin: PackPin
    registry: ToolRegistry = field(default_factory=ToolRegistry)
    """The pack's tools as *behaviour*: the only source of a risk tier at run time (phase-1
    deferred finding I). Empty for a pack that exports none."""

    environment: SandboxedEnvironment = field(default_factory=make_environment)
    """This pack's own sandboxed Jinja environment.

    A Jinja environment carries a template cache and a filter table. While nothing varies per
    pack the sharing is harmless, but DESIGN.md section 6.7 keeps two pack versions loaded side
    by side and a pack may one day supply a filter, so each pack gets its own from the start
    (phase-1 deferred finding P2). The engine renders every message through it."""

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def entry_graph(self) -> Graph:
        return self.graphs[self.manifest.entry_graph]

    def graph(self, graph_id: str) -> Graph | None:
        return self.graphs.get(graph_id)
