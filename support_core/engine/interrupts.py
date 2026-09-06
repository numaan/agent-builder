"""Interrupts and the frame stack. Implements DESIGN.md section 6.6 with 6.5.

    A conversation holds a stack of frames. Normal sub-graph calls push and pop. Interrupts
    handle the case where the customer changes topic while a workflow is suspended.
    - DESIGN.md section 6.6

This module holds the parts of that which are *decisions about a pack* rather than control flow:
which workflows a customer may ask for, which graphs may be interrupted, and the four sentences
core says when it parks, offers, resumes or abandons a workflow. The control flow itself is in
:mod:`support_core.engine.executor`, because it is the frame stack and nothing else.

**Where the intents come from.** DESIGN.md section 6.6 step 2: "Available intents are the root
graph's declared edges." So they are derived from the pack's own root graph - each edge of the
entry graph's start node whose target is a ``subgraph`` node is one intent, named by the edge
label and leading to that node's graph. A pack therefore declares its interruptible workflows by
writing the root graph it was going to write anyway, and cannot get the two out of step. A pack
whose root does not classify with an ``llm`` or ``router`` node offers no intents, which makes
every interrupt an ``unclear`` and is the safe answer for a root graph nobody designed for this.

**Why core writes the sentences.** DESIGN.md section 19 step 8 has the *node* say "Thanks. I'll
come back to the address change once the refund is sorted", but the two node types that suspend
into ``waiting_customer`` are ``ask`` and ``confirm``, and neither speaks when it resumes: an
``ask`` returns a state patch and a ``confirm`` returns a decision. Leaving the sentence to the
pack would mean every pack that wanted a working interrupt had to write the same four messages
into every graph. So core says them, they are short and factual, and each one is a promise the
engine actually keeps - which is what DESIGN.md section 14's forbidden-promises rule asks of any
sentence this system emits.
"""

from collections.abc import Sequence

from support_core.graph.manifest import PackManifest
from support_core.graph.pack import Pack
from support_core.graph.routing import WorkflowIntent
from support_core.graph.routing import workflow_intents as _intents_of


def workflow_intents(pack: Pack) -> tuple[WorkflowIntent, ...]:
    """The root graph's declared edges, as workflows (DESIGN.md sections 6.5, 6.6).

    A thin wrapper over :func:`support_core.graph.routing.workflow_intents`, which lives in the
    ``graph`` package because the *validator* needs the same answer: an interrupt adds
    control-flow edges, and the confirm-coverage analysis of DESIGN.md section 5.2 has to see the
    paths they create. Two derivations would be two answers.
    """
    return _intents_of(pack.graphs, pack.manifest.entry_graph)


INTERRUPT_RETURN_NODE = "__interrupt_return__"
"""The reserved node id the return offer's trace step is written under.

The offer is a core step on a *parked* frame, not a node in anybody's graph, and it still has to
have a step id: DESIGN.md section 7.1's id is ``run_id:frame_seq:node_id:attempt`` and every
checkpoint writes one. A pack cannot collide with this name, because a pack node id must match
``^[a-z][a-z0-9_]*$`` and this one begins with an underscore.
"""


def resolve_intent(intents: Sequence[WorkflowIntent], named: str | None) -> WorkflowIntent | None:
    """The workflow a check's answer names, matched on the label *or* the graph id.

    Both, because the model is shown both and a model that answers with the graph id has still
    given a usable answer. Nothing else is accepted: an intent the root graph does not declare is
    not a workflow this conversation can reach, whatever the model called it, and the caller
    treats that as ``unclear`` rather than guessing at the nearest match.
    """
    if not named:
        return None
    wanted = named.strip().lower()
    for intent in intents:
        if wanted in {intent.label.lower(), intent.graph.lower()}:
            return intent
    return None


def interrupts_allowed(manifest: PackManifest, graph_id: str) -> bool:
    """Whether a workflow suspended in ``graph_id`` may be interrupted (``pack.yaml``).

    An allow-list, not a default. DESIGN.md section 6.6 step 4 says "if the current graph allows
    interrupts (``pack.yaml: interrupts``)", and the safe reading of *allows* is *says so*: a
    graph named in neither list is treated exactly like one in ``blocked_in``, so the customer's
    request is recorded and offered back rather than either dropped or acted on. A pack that
    wants a workflow to be interruptible writes it in ``allowed_from``, which is one line and is
    a decision somebody made on purpose.
    """
    interrupts = manifest.interrupts
    if graph_id in interrupts.blocked_in:
        return False
    return graph_id in interrupts.allowed_from


def interrupts_configured(manifest: PackManifest) -> bool:
    """Whether the pack says anything about interrupts at all.

    A pack that says nothing gets no interrupt check - and therefore no model call and no
    latency - because the only answer the engine could act on would be ``continue``.
    """
    return bool(manifest.interrupts.allowed_from or manifest.interrupts.blocked_in)


# -- what core says ---------------------------------------------------------------------------
#
# Short, factual, and each one a promise the engine keeps. See the module docstring.


def deferral_notice(intent: WorkflowIntent, current: str) -> str:
    """DESIGN.md section 19 step 8: "let's finish X first", said honestly.

    It says the request has been *noted* rather than that it will be done, because what the
    engine guarantees is that the root graph is shown it again (step 15) - not that the workflow
    will run, which depends on the customer still wanting it.
    """
    return (
        f"Let me finish this first - I have made a note that you also asked about "
        f"{_words(intent)}, and I will come back to it when we are done here."
    )


def deferral_hint(intent: WorkflowIntent, current: str) -> str:
    """What the *node* is told, as opposed to what the customer is told.

    It rides on the :class:`~support_core.engine.types.ResumeEvent` and reaches an ``ask`` node's
    slot extractor, which is the reason it exists: without it, "sure - and can you change my
    address?" is a reply to "what is the six-digit code?" and a structured extractor has to
    decide what part of it is the code. With it, the extractor knows the second half is a
    different request that has already been dealt with.
    """
    return (
        f"While {_words_for(current)} was in progress the customer also asked about "
        f"{_words(intent)}. That request has been recorded and will be offered back to them "
        f"later; it is not an answer to the question this step asked, and no value in it "
        f"belongs to this step's slots."
    )


def switch_notice(intent: WorkflowIntent) -> str:
    """Said when an interrupt is taken: the old workflow is paused, not lost."""
    return (
        f"Of course - I have paused what we were doing and will come back to it. "
        f"Let me help with {_words(intent)}."
    )


def return_offer(graph_id: str, intent_label: str | None = None) -> str:
    """DESIGN.md section 6.6 step 4: "the engine asks the customer whether to return"."""
    what = _words_for(intent_label or graph_id)
    return f"That is done. Shall we go back to {what}?"


def abandoned_notice(graph_id: str, intent_label: str | None = None) -> str:
    return f"All right, I have left {_words_for(intent_label or graph_id)} as it was."


def cancelled_notice() -> str:
    """Said when the check reads ``cancel``. It claims nothing about what was undone."""
    return (
        "All right, I have stopped that. Nothing further was done. "
        "Is there something else I can help with?"
    )


def _words(intent: WorkflowIntent) -> str:
    return _words_for(intent.label)


def _words_for(name: str) -> str:
    """A graph id or edge label as something a customer can read.

    Not a translation and not a description: ``update_address`` becomes "update address". A pack
    that wants better wording gives the workflow a ``description``, which is what the model sees;
    this is only for the four core sentences, where being plain is better than being wrong.
    """
    return " ".join(name.replace("_", " ").split()) or "that"
