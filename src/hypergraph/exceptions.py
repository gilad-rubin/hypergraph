"""Exceptions for hypergraph execution runtime."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from hypergraph.runners._shared.results import FailureEvidence
    from hypergraph.runners._shared.state import GraphState


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
    """Runner-owned wrapper proving failure arose at the executor boundary."""


class _FailureEvidenceCarrier(Exception):
    """Typed suppressed context used when map re-raises a child error."""

    def __init__(self, error: BaseException, node_failures: tuple[FailureEvidence, ...]) -> None:
        self.error = error
        self.node_failures = tuple(node_failures)
        super().__init__()


@contextmanager
def _failure_evidence_context(
    error: BaseException,
    node_failures: tuple[FailureEvidence, ...],
) -> Iterator[None]:
    """Install typed evidence as the standard context for a caller re-raise."""
    try:
        raise _FailureEvidenceCarrier(error, node_failures)
    except _FailureEvidenceCarrier:
        yield


def _get_failure_evidence_from_context(
    error: BaseException | None,
) -> tuple[FailureEvidence, ...] | None:
    """Return evidence from trusted context, or None when none is present."""
    if error is None:
        return None

    seen = {id(error)}
    current = BaseException.__getattribute__(error, "__context__")
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _NodeExecutionError):
            return current.node_failures if current.__cause__ is error else ()
        if isinstance(current, ExecutionError):
            return ()
        if isinstance(current, _FailureEvidenceCarrier):
            return current.node_failures if current.error is error else ()
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
    """Raised when a second run() starts for a workflow_id that already has an active run.

    At most one active ``run()`` per ``workflow_id``.  If you need concurrent
    runs, use different workflow IDs.
    """

    def __init__(self, workflow_id: str) -> None:
        self.workflow_id = workflow_id
        super().__init__(
            f"Workflow '{workflow_id}' already has an active run. "
            f"Only one run() per workflow_id at a time.\n\n"
            f"How to fix:\n"
            f"  Wait for the current run to complete, or use a different workflow_id."
        )
