"""Event types emitted during graph execution."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class RunStatus(Enum):
    """Status of a graph run in event context.

    Values:
        COMPLETED: Run finished successfully.
        FAILED: Run encountered an error.
    """

    COMPLETED = "completed"
    FAILED = "failed"


def _generate_span_id() -> str:
    """Generate a unique span ID."""
    return uuid.uuid4().hex[:16]


def _now() -> float:
    """Current timestamp."""
    return time.time()


@dataclass(frozen=True)
class BaseEvent:
    """Base class for all execution events.

    Attributes:
        run_id: Unique identifier for the run that produced this event.
        span_id: Unique identifier for this event's scope.
        parent_span_id: Span ID of the parent scope, or None for root runs.
        timestamp: Unix timestamp when the event was created.
    """

    run_id: str
    span_id: str = field(default_factory=_generate_span_id)
    parent_span_id: str | None = None
    timestamp: float = field(default_factory=_now)


@dataclass(frozen=True)
class RunStartEvent(BaseEvent):
    """Emitted when a graph run begins.

    Attributes:
        graph_name: Name of the graph being executed.
        workflow_id: Optional workflow identifier for tracking related runs.
        is_map: Whether this run is part of a map operation.
        map_size: Number of items in the map operation, if applicable.
    """

    graph_name: str = ""
    workflow_id: str | None = None
    is_map: bool = False
    map_size: int | None = None


@dataclass(frozen=True)
class RunEndEvent(BaseEvent):
    """Emitted when a graph run completes.

    Attributes:
        graph_name: Name of the graph that was executed.
        status: Outcome of the run (RunStatus.COMPLETED or RunStatus.FAILED).
        error: Error message if status is FAILED.
        duration_ms: Wall-clock duration in milliseconds.
    """

    graph_name: str = ""
    status: RunStatus = RunStatus.COMPLETED
    error: str | None = None
    duration_ms: float = 0.0

    def __post_init__(self) -> None:
        # Coerce string status values to RunStatus enum
        if isinstance(self.status, str):
            object.__setattr__(self, "status", RunStatus(self.status))


@dataclass(frozen=True)
class NodeStartEvent(BaseEvent):
    """Emitted when a node begins execution.

    Attributes:
        node_name: Name of the node.
        graph_name: Name of the graph containing the node.
    """

    node_name: str = ""
    graph_name: str = ""


@dataclass(frozen=True)
class NodeEndEvent(BaseEvent):
    """Emitted when a node completes successfully.

    Attributes:
        node_name: Name of the node.
        graph_name: Name of the graph containing the node.
        duration_ms: Wall-clock duration in milliseconds.
    """

    node_name: str = ""
    graph_name: str = ""
    duration_ms: float = 0.0
    cached: bool = False


@dataclass(frozen=True)
class CacheHitEvent(BaseEvent):
    """Emitted when a node result is served from cache.

    Attributes:
        node_name: Name of the cached node.
        graph_name: Name of the graph containing the node.
        cache_key: The cache key that was hit.
    """

    node_name: str = ""
    graph_name: str = ""
    cache_key: str = ""


@dataclass(frozen=True)
class NodeErrorEvent(BaseEvent):
    """Emitted when a node fails with an exception.

    Attributes:
        node_name: Name of the node.
        graph_name: Name of the graph containing the node.
        error: Error message.
        error_type: Fully qualified exception type name.
    """

    node_name: str = ""
    graph_name: str = ""
    error: str = ""
    error_type: str = ""


@dataclass(frozen=True)
class RouteDecisionEvent(BaseEvent):
    """Emitted when a routing node makes a decision.

    Attributes:
        node_name: Name of the routing node.
        graph_name: Name of the graph containing the node.
        decision: The chosen target(s).
    """

    node_name: str = ""
    graph_name: str = ""
    decision: str | list[str] = ""


@dataclass(frozen=True)
class InterruptEvent(BaseEvent):
    """Emitted when execution is interrupted for human-in-the-loop.

    Attributes:
        node_name: Name of the node that triggered the interrupt.
        graph_name: Name of the graph containing the node.
        workflow_id: Optional workflow identifier.
        value: The interrupt payload.
        response_param: Parameter name expected for the response.
    """

    node_name: str = ""
    graph_name: str = ""
    workflow_id: str | None = None
    value: object = None
    response_param: str = ""


@dataclass(frozen=True)
class StopRequestedEvent(BaseEvent):
    """Emitted when a stop is requested on a workflow.

    Attributes:
        workflow_id: Optional workflow identifier.
    """

    workflow_id: str | None = None


Event = (
    RunStartEvent
    | RunEndEvent
    | NodeStartEvent
    | NodeEndEvent
    | CacheHitEvent
    | NodeErrorEvent
    | RouteDecisionEvent
    | InterruptEvent
    | StopRequestedEvent
)
