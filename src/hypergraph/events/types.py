"""Event types emitted during graph execution."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal

from hypergraph.diagnostics import Diagnostic


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
    batch_restored_items: int | None = None

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
class NodeAttemptStartEvent(BaseEvent):
    """Emitted when one callable invocation (attempt) begins.

    Attempt events exist only for attempt-managed nodes (a declared retry
    policy and/or timeout); a cache hit emits zero attempt events. They hang
    off the single logical node span via ``parent_span_id``.

    Attributes:
        node_name: Name of the node.
        graph_name: Name of the graph containing the node.
        superstep: Zero-indexed superstep number, if known.
        attempt_series_id: Stable id of the attempt series (durable when a
            checkpointer backs the run, ephemeral otherwise).
        attempt_number: One-based attempt number within the series.
        max_attempts: The series budget (counts the initial invocation).
        timeout_seconds: Configured per-attempt timeout, if any.
        attempt_deadline_at: Absolute cooperative deadline for this attempt,
            if a per-attempt timeout is active.
        series_deadline_at: Immutable absolute retry-window deadline, if any.
    """

    node_name: str = ""
    graph_name: str = ""
    superstep: int | None = None
    attempt_series_id: str = ""
    attempt_number: int = 0
    max_attempts: int = 0
    timeout_seconds: float | None = None
    attempt_deadline_at: datetime | None = None
    series_deadline_at: datetime | None = None


@dataclass(frozen=True)
class NodeAttemptEndEvent(BaseEvent):
    """Emitted when one callable invocation settles.

    An intermediate failed attempt emits this event and nothing else: it never
    emits ``NodeErrorEvent``, never bumps logical error counts, and never
    closes the logical node span. No end event is fabricated after process
    death — the durable attempt record alone transitions to OUTCOME_UNKNOWN.

    ``deadline_elapsed`` and ``cancellation_requested`` are independent
    witnessed facts; together with the settled ``outcome`` they never claim
    that arbitrary user work or external side effects stopped.

    Attributes:
        node_name: Name of the node.
        graph_name: Name of the graph containing the node.
        superstep: Zero-indexed superstep number, if known.
        attempt_series_id: Stable id of the attempt series.
        attempt_number: One-based attempt number within the series.
        outcome: How the attempt settled.
        settlement: How control returned from the callable.
        deadline_scope: Which deadline elapsed, if one did.
        deadline_elapsed: True when a cooperative deadline elapsed.
        cancellation_requested: True when cancellation was requested.
        duration_ms: Wall-clock duration of this attempt in milliseconds.
        error_type: Qualified exception type name for failed attempts.
        retry_scheduled: True when another attempt was granted.
        retry_not_before: Absolute persisted wake time for a granted retry.
    """

    node_name: str = ""
    graph_name: str = ""
    superstep: int | None = None
    attempt_series_id: str = ""
    attempt_number: int = 0
    outcome: Literal["succeeded", "failed", "timed_out", "cancelled"] = "succeeded"
    settlement: Literal["returned", "raised", "cancelled"] = "returned"
    deadline_scope: Literal["attempt", "series"] | None = None
    deadline_elapsed: bool = False
    cancellation_requested: bool = False
    duration_ms: float = 0.0
    error_type: str | None = None
    retry_scheduled: bool = False
    retry_not_before: datetime | None = None


@dataclass(frozen=True)
class NodeErrorEvent(BaseEvent):
    """Emitted when a node fails with an exception.

    Emitted once per logical failure — never for intermediate retry attempts.
    ``error`` carries the privacy-safe projection (type name, stable code,
    static problem wording), never raw exception message text; the exact
    exception object stays on local surfaces (``RunResult.error``,
    ``get_failure_evidence``).

    Attributes:
        node_name: Name of the node.
        graph_name: Name of the graph containing the node.
        error: Privacy-safe error projection text.
        error_type: Fully qualified exception type name.
        superstep: Zero-indexed superstep number, if known.
        diagnostic: Typed privacy-safe diagnostic, when derivable.
    """

    node_name: str = ""
    graph_name: str = ""
    error: str = ""
    error_type: str = ""
    superstep: int | None = None
    diagnostic: Diagnostic | None = None


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
        workflow_id: Inherited from BaseEvent — workflow identifier, if any.
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
        workflow_id: Inherited from BaseEvent — workflow identifier, if any.
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
        graph_name: Name of the graph containing the node.
        parent_span_id: Inherited from BaseEvent — span of the emitting node.
        workflow_id: Inherited from BaseEvent — workflow identifier, if any.
        item_index: Inherited from BaseEvent — mapped item index, if any.
    """

    chunk: object = None
    node_name: str = ""
    graph_name: str = ""


@dataclass(frozen=True)
class InnerCacheEvent(BaseEvent):
    """Emitted when a hypercache-decorated call happens inside a running node.

    One event per cached call, reflecting the cache decision made by
    CacheService. Zero user boilerplate: emitted automatically when
    hypercache is installed and the node_cache_observer is active.

    Attributes:
        node_name: Name of the graph node that triggered the call.
        graph_name: Name of the graph containing the node.
        instance: Qualified instance name (matches cache key identity).
        operation: Method name of the cached call.
        hit: True if the value was served from cache; False if computed.
        stale: True if the cached value was past its stale window.
        refreshing: True if a background refresh was triggered.
        wrote: True if a new value was written to the cache store.
        mode: Cache mode in effect: "normal" | "bypass" | "refresh_forced".
    """

    node_name: str = ""
    graph_name: str = ""
    instance: str = ""
    operation: str = ""
    hit: bool = False
    stale: bool = False
    refreshing: bool = False
    wrote: bool = False
    mode: str = ""


Event = (
    RunStartEvent
    | RunEndEvent
    | NodeStartEvent
    | NodeAttemptStartEvent
    | NodeAttemptEndEvent
    | NodeEndEvent
    | CacheHitEvent
    | NodeErrorEvent
    | RouteDecisionEvent
    | SuperstepStartEvent
    | InterruptEvent
    | StopRequestedEvent
    | StreamingChunkEvent
    | InnerCacheEvent
)
