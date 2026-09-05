"""Engine exceptions. Implements the failure vocabulary of DESIGN.md section 7.3.

The distinction that matters: a :class:`NodeError` is a *run-time* failure of a node and is
routed - to the node's ``on_error`` edge if it declares one, otherwise to the handoff hook
(section 7.3). Everything else is a defect in the pack or in core, is not routed, and fails the
turn loudly, because turning a programming error into a handoff would hide it.
"""


class EngineError(RuntimeError):
    """Base class for every failure raised by the execution engine."""


class NodeError(EngineError):
    """A node failed while running. Routed per DESIGN.md section 7.3."""


class NodeNotExecutableError(EngineError):
    """The graph reached a node type core cannot run yet, naming the phase that adds it.

    Not routed to ``on_error``: a pack that reaches one is a pack core should have refused to
    run, and ``graph.node_not_executable`` already said so at validation time.
    """


class IncompatiblePackError(EngineError):
    """A suspended run cannot continue on the pack now loaded (DESIGN.md section 6.7).

    Raised when the run's frames name a graph whose declared state shape has changed since the
    run started. The engine hands the conversation off rather than feeding a node a state model
    it was not written for.
    """


class LockTimeout(EngineError):
    """The conversation lock could not be taken within the configured wait."""
