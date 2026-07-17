"""Exceptions for hypergraph execution runtime."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from hypergraph.runners._shared.results import FailureEvidence
    from hypergraph.runners._shared.state import GraphState


_CURRENT_FAILURE_EVIDENCE_INVOCATION: ContextVar[object | None] = ContextVar(
    "_CURRENT_FAILURE_EVIDENCE_INVOCATION",
    default=None,
)
_ANY_FAILURE_EVIDENCE_INVOCATION = object()


class MissingInputError(Exception):
    """Required input not provided to runner.

    Raised when attempting to execute a graph without providing all required
    input values (those not satisfied by edges, bindings, or defaults).

    Attributes:
        missing: List of missing input names
        provided: List of provided input names
        message: Human-readable error message
    """

    def __init__(
        self,
        missing: list[str],
        provided: list[str] | None = None,
        message: str | None = None,
    ) -> None:
        self.missing = missing
        self.provided = provided or []
        self.message = message or self._default_message()
        super().__init__(self.message)

    def _default_message(self) -> str:
        missing_str = ", ".join(f"'{m}'" for m in self.missing)
        msg = f"Missing required inputs: {missing_str}"
        if self.provided:
            provided_str = ", ".join(f"'{p}'" for p in self.provided)
            msg += f"\nProvided: {provided_str}"
        return msg


class InfiniteLoopError(Exception):
    """Exceeded maximum iterations in cyclic graph execution.

    Raised when a graph with cycles exceeds the max_iterations limit
    without reaching a stable state.

    Attributes:
        max_iterations: The limit that was exceeded
        message: Human-readable error message
    """

    def __init__(
        self,
        max_iterations: int,
        message: str | None = None,
    ) -> None:
        self.max_iterations = max_iterations
        self.message = message or self._default_message()
        super().__init__(self.message)

    def _default_message(self) -> str:
        return (
            f"Graph execution exceeded {self.max_iterations} iterations. "
            f"The graph may have an infinite loop, or you may need to increase "
            f"max_iterations."
        )


class IncompatibleRunnerError(Exception):
    """Runner doesn't support required graph features.

    Raised when attempting to execute a graph with a runner that lacks
    necessary capabilities (e.g., SyncRunner with async nodes).

    Attributes:
        message: Human-readable error message describing the incompatibility
        node_name: Optional name of the incompatible node
        capability: Optional name of the missing capability
    """

    def __init__(
        self,
        message: str,
        *,
        node_name: str | None = None,
        capability: str | None = None,
    ) -> None:
        self.message = message
        self.node_name = node_name
        self.capability = capability
        super().__init__(message)


class ExecutionError(Exception):
    """Wraps an exception that occurred during graph execution.

    Carries the partial GraphState accumulated before the error, plus
    per-superstep failure attribution as real constructor parameters.

    Attributes:
        partial_state: GraphState snapshot from before the failure
        attempted_node_names: Names of nodes attempted in the failing
            superstep. Empty when the failure cannot be attributed to
            specific nodes (e.g. a scheduler-level error).
        node_errors: Mapping of node name to the exception that node raised.
        node_failures: Attributable leaf-node failures in deterministic order.
    """

    def __init__(
        self,
        cause: BaseException,
        partial_state: GraphState,
        attempted_node_names: tuple[str, ...] = (),
        node_errors: Mapping[str, BaseException] | None = None,
        node_failures: tuple[FailureEvidence, ...] = (),
    ) -> None:
        self.partial_state = partial_state
        self.attempted_node_names = tuple(attempted_node_names)
        self.node_errors: dict[str, BaseException] = dict(node_errors) if node_errors else {}
        self.node_failures = tuple(node_failures)
        super().__init__(str(cause))
        self.__cause__ = cause

    @property
    def failure(self) -> FailureEvidence | None:
        """First attributable node failure, if one exists."""
        return self.node_failures[0] if self.node_failures else None


class _NodeExecutionError(ExecutionError):
    """Runner-owned wrapper proving failure arose in the current invocation."""

    def __init__(
        self,
        cause: BaseException,
        partial_state: GraphState,
        attempted_node_names: tuple[str, ...] = (),
        node_errors: Mapping[str, BaseException] | None = None,
        node_failures: tuple[FailureEvidence, ...] = (),
        *,
        invocation_token: object | None,
    ) -> None:
        self._invocation_token = invocation_token
        super().__init__(
            cause,
            partial_state,
            attempted_node_names,
            node_errors,
            node_failures,
        )


class _FailureEvidenceCarrier(Exception):
    """Typed suppressed context used when map re-raises a child error."""

    def __init__(
        self,
        error: BaseException,
        node_failures: tuple[FailureEvidence, ...],
        invocation_token: object | None,
    ) -> None:
        self.error = error
        self.node_failures = tuple(node_failures)
        self._invocation_token = invocation_token
        super().__init__()


@contextmanager
def _failure_evidence_context(
    error: BaseException,
    node_failures: tuple[FailureEvidence, ...],
) -> Iterator[None]:
    """Install typed evidence as the standard context for a caller re-raise."""
    try:
        raise _FailureEvidenceCarrier(
            error,
            node_failures,
            _get_failure_evidence_invocation(),
        )
    except _FailureEvidenceCarrier:
        yield


def _get_failure_evidence_invocation() -> object | None:
    """Return the nearest GraphNode invocation token in this execution context."""
    return _CURRENT_FAILURE_EVIDENCE_INVOCATION.get()


@contextmanager
def _bind_failure_evidence_invocation(invocation_token: object | None) -> Iterator[None]:
    """Bind a GraphNode invocation token for its delegated executor call."""
    if invocation_token is None:
        yield
        return

    reset_token = _CURRENT_FAILURE_EVIDENCE_INVOCATION.set(invocation_token)
    try:
        yield
    finally:
        _CURRENT_FAILURE_EVIDENCE_INVOCATION.reset(reset_token)


def _get_failure_evidence_from_context(
    error: BaseException | None,
    *,
    invocation_token: object = _ANY_FAILURE_EVIDENCE_INVOCATION,
) -> tuple[FailureEvidence, ...] | None:
    """Return evidence from trusted context, or None when none is present."""
    if error is None:
        return None

    seen = {id(error)}
    current = BaseException.__getattribute__(error, "__context__")
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current) is _NodeExecutionError:
            if current.__cause__ is not error:
                return ()
            if invocation_token is not _ANY_FAILURE_EVIDENCE_INVOCATION and current._invocation_token is not invocation_token:
                return ()
            return current.node_failures
        if isinstance(current, _NodeExecutionError):
            return ()
        if isinstance(current, ExecutionError):
            return ()
        if type(current) is _FailureEvidenceCarrier:
            if current.error is not error:
                return ()
            if invocation_token is not _ANY_FAILURE_EVIDENCE_INVOCATION and current._invocation_token is not invocation_token:
                return ()
            return current.node_failures
        if isinstance(current, _FailureEvidenceCarrier):
            return ()
        current = BaseException.__getattribute__(current, "__context__")
    return None


def get_failure_evidence(error: BaseException | None) -> tuple[FailureEvidence, ...]:
    """Return node-failure evidence carried by a runner exception chain."""
    contextual = _get_failure_evidence_from_context(error)
    if contextual is not None:
        return contextual
    if error is None:
        return ()
    return error.node_failures if isinstance(error, ExecutionError) else ()


class WorkflowAlreadyCompletedError(Exception):
    """Raised when attempting to resume a workflow that is already completed."""

    def __init__(self, workflow_id: str) -> None:
        self.workflow_id = workflow_id
        super().__init__(f"Workflow '{workflow_id}' is already completed. Fork to create a new lineage.")


class CompactedRetentionError(Exception):
    """Raised when compacted history makes nested recovery ambiguous."""

    def __init__(self, node_name: str) -> None:
        self.node_name = node_name
        super().__init__(
            f"Cannot safely recover nested graph '{node_name}': windowed/compacted retention "
            "may have pruned the parent step history, so Hypergraph cannot distinguish a "
            "crash-window restore from a legitimate re-execution.\n\n"
            "How to fix:\n"
            "  Use retention='full' or retention='latest' for workflows that combine nested "
            "graphs with resume/crash recovery, or fork the workflow.\n\n"
            "Windowed nested recovery support is tracked in #277."
        )


class WorkflowStoppedError(Exception):
    """Raised when a stopped workflow is rerun without an explicit signal."""

    def __init__(self, workflow_id: str) -> None:
        self.workflow_id = workflow_id
        super().__init__(
            f"Workflow '{workflow_id}' is stopped and cannot resume without an explicit signal.\n\n"
            "The stopped workflow keeps its partial checkpoint state and lineage.\n\n"
            "How to fix:\n"
            "  Pass a non-empty runtime value mapping to resume the same workflow_id, "
            "or pass override_workflow=True to fork a new lineage."
        )


class GraphChangedError(Exception):
    """Raised when graph structure changed for an existing workflow lineage."""

    def __init__(self, workflow_id: str) -> None:
        self.workflow_id = workflow_id
        super().__init__(f"Graph structure changed for workflow '{workflow_id}'. Fork instead of resuming in place.")


class WorkflowForkError(Exception):
    """Raised when fork arguments are invalid for the requested workflow."""


class InputOverrideRequiresForkError(Exception):
    """Raised when input values are provided while resuming an existing workflow."""

    def __init__(self, workflow_id: str) -> None:
        self.workflow_id = workflow_id
        super().__init__(f"Cannot pass input values when resuming workflow '{workflow_id}'. Use checkpoint + new workflow_id to fork.")


class WorkflowAlreadyRunningError(Exception):
    """Raised when an execution starts for a workflow_id that is already active.

    At most one active execution per ``workflow_id``. If you need independently
    controlled work, use different workflow IDs.
    """

    def __init__(self, workflow_id: str) -> None:
        self.workflow_id = workflow_id
        super().__init__(
            f"Workflow '{workflow_id}' already has an active execution. "
            f"Only one active execution per workflow_id at a time.\n\n"
            f"How to fix:\n"
            f"  Wait for the current execution to complete, or use a different workflow_id."
        )
