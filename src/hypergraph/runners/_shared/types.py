"""Core types for the execution runtime."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RunStatus(Enum):
    """Status of a graph execution run.

    Values:
        COMPLETED: Run finished successfully
        FAILED: Run encountered an error
        PAUSED: Execution paused at an InterruptNode, waiting for user response
    """

    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


def _generate_run_id() -> str:
    """Generate a unique run ID."""
    return f"run-{uuid.uuid4().hex[:12]}"


@dataclass
class RunResult:
    """Result of a graph execution.

    Attributes:
        values: Dict of all output values produced
        status: Run status (COMPLETED or FAILED)
        run_id: Unique identifier for this run
        workflow_id: Optional workflow identifier for tracking related runs
        error: Exception if status is FAILED, else None
    """

    values: dict[str, Any]
    status: RunStatus
    run_id: str = field(default_factory=_generate_run_id)
    workflow_id: str | None = None
    error: BaseException | None = None
    pause: PauseInfo | None = None

    @property
    def paused(self) -> bool:
        """Whether execution is paused at an InterruptNode."""
        return self.status == RunStatus.PAUSED

    def __getitem__(self, key: str) -> Any:
        """Dict-like access to values."""
        return self.values[key]

    def __contains__(self, key: str) -> bool:
        """Check if key exists in values."""
        return key in self.values

    def get(self, key: str, default: Any = None) -> Any:
        """Get value with default."""
        return self.values.get(key, default)


@dataclass
class PauseInfo:
    """Information about a paused execution.

    Attributes:
        node_name: Name of the InterruptNode that paused (uses "/" for nesting)
        output_param: The output parameter name where the response goes
        value: The input value surfaced to the caller
    """

    node_name: str
    output_param: str
    value: Any

    @property
    def response_key(self) -> str:
        """Key to use in values dict when resuming.

        Top-level: returns output_param directly (e.g., 'decision').
        Nested: dot-separated path (e.g., 'review.decision').
        """
        parts = self.node_name.split("/")
        if len(parts) == 1:
            return self.output_param
        return ".".join(parts[:-1]) + "." + self.output_param


class PauseExecution(BaseException):
    """Raised by InterruptNode executor to signal a pause.

    Extends BaseException (not Exception) so it won't be caught
    by the runner's generic ``except Exception`` handler.

    When raised inside a nested graph, the parent GraphNode executor
    catches it and re-raises with a prefixed node_name (e.g.
    ``"outer/inner/interrupt_node"``), propagating the pause up
    through arbitrarily deep nesting.
    """

    def __init__(self, pause_info: PauseInfo):
        self.pause_info = pause_info
        super().__init__(f"Paused at {pause_info.node_name}")


@dataclass
class RunnerCapabilities:
    """Declares what a runner supports.

    Used for compatibility checking between graphs and runners.

    Attributes:
        supports_cycles: Can execute graphs with cycles (default: True)
        supports_async_nodes: Can execute async nodes (default: False)
        supports_streaming: Supports .iter() streaming (default: False)
        returns_coroutine: run() returns a coroutine (default: False)
    """

    supports_cycles: bool = True
    supports_async_nodes: bool = False
    supports_streaming: bool = False
    returns_coroutine: bool = False
    supports_interrupts: bool = False


@dataclass
class NodeExecution:
    """Record of a single node execution.

    Used for tracking and staleness detection in cyclic graphs.

    Attributes:
        node_name: Name of the executed node
        input_versions: Version numbers of inputs at execution time
        outputs: Output values produced
    """

    node_name: str
    input_versions: dict[str, int]
    outputs: dict[str, Any]


@dataclass
class GraphState:
    """Internal runtime state during graph execution.

    Tracks current values and their versions for staleness detection.

    Attributes:
        values: Current value for each output/input name
        versions: Version number for each value (incremented on update)
        node_executions: History of node executions (for staleness detection)
        routing_decisions: Routing decisions made by gate nodes
    """

    values: dict[str, Any] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)
    node_executions: dict[str, NodeExecution] = field(default_factory=dict)
    routing_decisions: dict[str, Any] = field(default_factory=dict)

    def update_value(self, name: str, value: Any) -> None:
        """Update a value and increment its version if value changed.

        Only increments version if:
        - Name is new (not previously set), or
        - Value is different from previous value
        """
        old_value = self.values.get(name)
        is_new = name not in self.values

        self.values[name] = value

        # Only increment version if value actually changed
        if is_new:
            self.versions[name] = self.versions.get(name, 0) + 1
        else:
            # Defensive comparison for types like numpy arrays
            try:
                changed = bool(old_value != value)
            except (ValueError, TypeError):
                # Comparison failed (e.g., numpy arrays), assume changed
                changed = old_value is not value
            if changed:
                self.versions[name] = self.versions.get(name, 0) + 1

    def get_version(self, name: str) -> int:
        """Get current version of a value (0 if not set)."""
        return self.versions.get(name, 0)

    def copy(self) -> "GraphState":
        """Create a copy of this state with independent NodeExecution instances.

        Values and versions dicts are shallow-copied (keys are strings).
        NodeExecution instances are copied to prevent shared mutation.
        """
        from dataclasses import replace

        return GraphState(
            values=dict(self.values),
            versions=dict(self.versions),
            node_executions={
                k: replace(v, input_versions=dict(v.input_versions), outputs=dict(v.outputs))
                for k, v in self.node_executions.items()
            },
            routing_decisions=dict(self.routing_decisions),
        )
