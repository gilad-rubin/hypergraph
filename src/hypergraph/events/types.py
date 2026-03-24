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
        PAUSED: Run paused at an interrupt.
        PARTIAL: Run completed with mixed item outcomes.
        STOPPED: Run stopped cooperatively.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    PARTIAL = "partial"
    STOPPED = "stopped"


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
        workflow_id: Optional workflow identifier for related persisted runs.
        item_index: Item index for mapped child runs, if applicable.
        timestamp: Unix timestamp when the event was created.
    """

    run_id: str
    span_id: str = field(default_factory=_generate_span_id)
    parent_span_id: str | None = None
    workflow_id: str | None = None
    item_index: int | None = None
    timestamp: float = field(default_factory=_now)


@dataclass(frozen=True)
class RunStartEvent(BaseEvent):
    """Emitted when a graph run begins.

    Attributes:
        graph_name: Name of the graph being executed.
        is_map: Whether this run is part of a map operation.
        map_size: Number of items in the map operation, if applicable.
        parent_workflow_id: Parent workflow id for nested runs, if any.
        forked_from: Source workflow id when this run is a fork.
        fork_superstep: Source superstep for a fork checkpoint.
        retry_of: Source workflow id when this run is a retry.
        retry_index: Retry sequence number for retry runs.
        is_resume: Whether this run resumed an existing workflow.
    """

    graph_name: str = ""
    is_map: bool = False
    map_size: int | None = None
    parent_workflow_id: str | None = None
    forked_from: str | None = None
    fork_superstep: int | None = None
    retry_of: str | None = None
    retry_index: int | None = None
    is_resume: bool = False


@dataclass(frozen=True)
class RunEndEvent(BaseEvent):
    """Emitted when a graph run completes.

    Attributes:
        graph_name: Name of the graph that was executed.
        status: Outcome of the run (completed, failed, paused, partial, stopped).
        error: Error message if status is FAILED.
        duration_ms: Wall-clock duration in milliseconds.
        batch_*: Aggregate mapped-item counts for parent map runs.
        batch_outcome: Aggregate mapped-item outcome for parent map runs.
    """

    graph_name: str = ""
    status: RunStatus = RunStatus.COMPLETED
    error: str | None = None
    duration_ms: float = 0.0
    batch_total_items: int | None = None
    batch_completed_items: int | None = None
    batch_failed_items: int | None = None
    batch_paused_items: int | None = None
    batch_stopped_items: int | None = None
    batch_outcome: str | None = None

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
        superstep: Zero-indexed superstep number, if known.
    """

    node_name: str = ""
    graph_name: str = ""
    superstep: int | None = None


@dataclass(frozen=True)
class NodeEndEvent(BaseEvent):
    """Emitted when a node completes successfully.

    Attributes:
        node_name: Name of the node.
        graph_name: Name of the graph containing the node.
        duration_ms: Wall-clock duration in milliseconds.
        superstep: Zero-indexed superstep number, if known.
    """

    node_name: str = ""
    graph_name: str = ""
    superstep: int | None = None
    duration_ms: float = 0.0
    cached: bool = False
    inner_logs: tuple = ()  # tuple[RunLog, ...] at runtime; untyped to avoid import


@dataclass(frozen=True)
class CacheHitEvent(BaseEvent):
    """Emitted when a node result is served from cache.

    Attributes:
        node_name: Name of the cached node.
        graph_name: Name of the graph containing the node.
        cache_key: The cache key that was hit.
        superstep: Zero-indexed superstep number, if known.
    """

    node_name: str = ""
    graph_name: str = ""
    cache_key: str = ""
    superstep: int | None = None


@dataclass(frozen=True)
class NodeErrorEvent(BaseEvent):
    """Emitted when a node fails with an exception.

    Attributes:
        node_name: Name of the node.
        graph_name: Name of the graph containing the node.
        error: Error message.
        error_type: Fully qualified exception type name.
        superstep: Zero-indexed superstep number, if known.
    """

    node_name: str = ""
    graph_name: str = ""
    error: str = ""
    error_type: str = ""
    superstep: int | None = None


@dataclass(frozen=True)
class RouteDecisionEvent(BaseEvent):
    """Emitted when a routing node makes a decision.

    Attributes:
        node_name: Name of the routing node.
        graph_name: Name of the graph containing the node.
        decision: The chosen target(s).
        node_span_id: Span id of the routing node span, when available.
        superstep: Zero-indexed superstep number, if known.
    """

    node_name: str = ""
    graph_name: str = ""
    decision: str | list[str] = ""
    node_span_id: str | None = None
    superstep: int | None = None


@dataclass(frozen=True)
class InterruptEvent(BaseEvent):
    """Emitted when execution is interrupted for human-in-the-loop.

    Attributes:
        node_name: Name of the node that triggered the interrupt.
        graph_name: Name of the graph containing the node.
        workflow_id: Optional workflow identifier.
        value: The interrupt payload.
        response_param: Parameter name expected for the response.
        superstep: Zero-indexed superstep number, if known.
    """

    node_name: str = ""
    graph_name: str = ""
    value: object = None
    response_param: str = ""
    superstep: int | None = None


@dataclass(frozen=True)
class SuperstepStartEvent(BaseEvent):
    """Emitted at the start of each superstep (parallel execution round).

    Attributes:
        graph_name: Name of the graph being executed.
        superstep: Zero-indexed superstep number.
    """

    graph_name: str = ""
    superstep: int = 0


@dataclass(frozen=True)
class StopRequestedEvent(BaseEvent):
    """Emitted when a stop is requested on a workflow.

    Attributes:
        workflow_id: Optional workflow identifier.
        graph_name: Name of the graph being stopped.
        info: Optional metadata from ``runner.stop(workflow_id, info=...)``.
    """

    graph_name: str = ""
    info: object = None


@dataclass(frozen=True)
class StreamingChunkEvent(BaseEvent):
    """Emitted by ``ctx.stream(chunk)`` inside a node.

    Side-channel for live UI preview.  Does not affect the node's
    return value or output type.

    Attributes:
        chunk: The streamed payload (token, JSON fragment, etc.).
        node_name: Name of the node that emitted the chunk.
    """

    chunk: object = None
    node_name: str = ""


Event = (
    RunStartEvent
    | RunEndEvent
    | NodeStartEvent
    | NodeEndEvent
    | CacheHitEvent
    | NodeErrorEvent
    | RouteDecisionEvent
    | SuperstepStartEvent
    | InterruptEvent
    | StopRequestedEvent
    | StreamingChunkEvent
)
