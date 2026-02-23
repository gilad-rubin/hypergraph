"""Exceptions for hypergraph execution runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hypergraph.runners._shared.types import GraphState


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

    Carries the partial GraphState accumulated before the error,
    replacing the monkey-patched ``_partial_state`` attribute pattern.

    Attributes:
        partial_state: GraphState snapshot from before the failure
    """

    def __init__(self, cause: BaseException, partial_state: GraphState) -> None:
        self.partial_state = partial_state
        super().__init__(str(cause))
        self.__cause__ = cause
