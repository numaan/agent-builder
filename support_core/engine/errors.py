"""Engine exceptions. Implements the failure vocabulary of DESIGN.md section 7.3.

The distinction that matters: a :class:`NodeError` is a *run-time* failure of a node and is
routed - to the node's ``on_error`` edge if it declares one, otherwise to the handoff hook
(section 7.3). Everything else is a defect in the pack or in core, is not routed, and fails the
turn loudly, because turning a programming error into a handoff would hide it.
"""


class EngineError(RuntimeError):
    """Base class for every failure raised by the execution engine."""


class NodeError(EngineError):
    """A node failed while running. Routed per DESIGN.md section 7.3.

    ``reason`` is the handoff reason to record when the failure reaches the handoff hook.
    DESIGN.md section 7.3 names one that a node can produce on its own - ``llm_unavailable`` -
    and phase 3 adds two more of the same kind (``llm_invalid_output``, ``low_confidence``),
    because "the model would not answer", "the model answered with something the graph does not
    allow" and "the model was not sure enough to be believed" are three different things for
    whoever picks the conversation up. The default stays ``node_error``.
    """

    def __init__(self, message: str, *, reason: str = "node_error") -> None:
        self.reason = reason
        super().__init__(message)


class NodeNotExecutableError(EngineError):
    """The graph reached a node type core cannot run yet, naming the phase that adds it.

    Not routed to ``on_error``: a pack that reaches one is a pack core should have refused to
    run, and ``graph.node_not_executable`` already said so at validation time.
    """


class StatePatchError(EngineError):
    """A desk ``resume`` offered a state patch the frame may not hold (review finding P1).

    Refused *before* anything is written, and reported to the caller rather than routed: a patch
    is a human's typing at an API, so the answer is a 4xx naming the field they got wrong and the
    fields the graph declares. Writing it and letting the next node entry fail is what produced
    an unrecoverable frame and a ``pack_incompatible`` handoff that blamed the pack for a typo.

    ``status_code`` distinguishes the two refusals: a patch the graph cannot hold is a malformed
    request (400), while a patch blocked by a live approval is a conflict with the conversation's
    own state (409).
    """

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        self.status_code = status_code
        super().__init__(message)


class IncompatiblePackError(EngineError):
    """A suspended run cannot continue on the pack now loaded (DESIGN.md section 6.7).

    Raised when the run's frames name a graph whose declared state shape has changed since the
    run started. The engine hands the conversation off rather than feeding a node a state model
    it was not written for.
    """
