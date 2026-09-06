"""Which workflows a root graph offers. Implements DESIGN.md sections 6.5 and 6.6 step 2.

    `root.yaml` is the entry graph. Its first node is typically an `llm` node with edges to each
    top-level workflow via `subgraph` nodes ... - DESIGN.md section 6.5

    Available intents are the root graph's declared edges. - DESIGN.md section 6.6

One derivation, in the ``graph`` package, because two things need it and they are on opposite
sides of the engine: the executor asks it what a customer may switch to
(:mod:`support_core.engine.interrupts`), and the *validator* asks it which control-flow edges an
interrupt adds to the graph, so that the confirm-coverage analysis of DESIGN.md section 5.2 sees
the paths interrupts create. Two copies of "what counts as a workflow" would be two answers the
day a root graph is written unusually, and the validator's answer has to be the engine's.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from support_core.graph.nodes import LlmNode, RouterNode, SubgraphNode
from support_core.graph.schema import Graph


@dataclass(frozen=True, slots=True)
class WorkflowIntent:
    """One workflow a customer can ask for by name (DESIGN.md section 6.6 step 2)."""

    label: str
    """The root graph's edge label. The pack's own word for the thing, and what a person reading
    a deferred intent should see."""

    graph: str
    description: str | None = None

    def as_tuple(self) -> tuple[str, str, str | None]:
        return (self.label, self.graph, self.description)


def workflow_intents(graphs: Mapping[str, Graph], entry_graph: str) -> tuple[WorkflowIntent, ...]:
    """The entry graph's declared edges that lead to a workflow.

    Each edge of the entry graph's *start* node whose target is a ``subgraph`` node is one
    intent, named by the edge label and leading to that node's graph. A pack therefore declares
    its interruptible workflows by writing the root graph it was going to write anyway, and the
    two cannot get out of step.

    A root graph whose start node is not an ``llm`` or ``router`` node - one that does not
    classify - offers none, which makes every interrupt an ``unclear``. That is the safe answer
    for a root graph nobody designed for this, and it is silent rather than an error because
    DESIGN.md section 6.5 says "typically", not "must".
    """
    root = graphs.get(entry_graph)
    if root is None:
        return ()
    start = root.nodes.get(root.start)
    edges = getattr(start, "edges", None)
    if not isinstance(start, LlmNode | RouterNode) or not isinstance(edges, dict):
        return ()
    intents: list[WorkflowIntent] = []
    for label, target in edges.items():
        node = root.nodes.get(target)
        if not isinstance(node, SubgraphNode) or node.graph not in graphs:
            continue
        described = node.description or graphs[node.graph].description
        intents.append(
            WorkflowIntent(
                label=str(label),
                graph=node.graph,
                description=" ".join((described or "").split()) or None,
            )
        )
    return tuple(intents)
