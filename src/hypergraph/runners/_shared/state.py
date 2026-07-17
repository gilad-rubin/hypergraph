"""Canonical runner execution-state types."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.results import PauseInfo, RunLog

if TYPE_CHECKING:
    from hypergraph.checkpointers.base import Checkpointer
    from hypergraph.events.processor import EventProcessor

CheckpointErrorSink = Callable[[str], None]


class PauseExecution(BaseException):
    """Raised by InterruptNode executor to signal a pause.

    Extends BaseException (not Exception) so it won't be caught
    by the runner's generic ``except Exception`` handler.

    When raised inside a nested graph, the parent GraphNode executor
    catches it and re-raises with a prefixed node_name (e.g.
    ``"outer/inner/interrupt_node"``), propagating the pause up
    through arbitrarily deep nesting.

    Attributes:
        pause_info: Details about the interrupt that paused the run.
        partial_state: GraphState accumulated before the pause. Attached by
            the runner as the pause propagates; None until then.
        stopped: Whether a cooperative stop was also requested when the
            pause propagated.
        span_id: Span of the interrupt node, set by the superstep.
    """

    def __init__(
        self,
        pause_info: PauseInfo,
        partial_state: GraphState | None = None,
        stopped: bool = False,
    ):
        self.pause_info = pause_info
        self.partial_state = partial_state
        self.stopped = stopped
        self.span_id: str | None = None
        super().__init__(f"Paused at {pause_info.node_name}")


@dataclass
class RunnerCapabilities:
    """Declares what a runner supports.

    Used for compatibility checking between graphs and runners.

    Attributes:
        supports_cycles: Can execute graphs with cycles (default: True)
        supports_gates: Can execute graphs with gate nodes (default: True)
        supports_async_nodes: Can execute async nodes (default: False)
        supports_streaming: Streams results incrementally — per-item
            yielding via map_iter() and StreamingChunkEvent emission from
            ctx.stream() (default: False). SyncRunner and AsyncRunner set
            this to True.
        supports_events: Supports event processors (default: True)
        supports_distributed: Can distribute across workers (default: False)
        returns_coroutine: run() returns a coroutine (default: False)
    """

    supports_cycles: bool = True
    supports_gates: bool = True
    supports_async_nodes: bool = False
    supports_streaming: bool = False
    supports_events: bool = True
    supports_distributed: bool = False
    returns_coroutine: bool = False
    supports_interrupts: bool = False
    supports_checkpointing: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Per-node execution environment passed to executors.

    Created once per run as a base, then specialized per node via
    ``dataclasses.replace()`` with the active parent span and nested-log sink.

    Note:
        ``provided_values`` intentionally remains a shared mutable dict so
        interrupt resume payloads can be consumed across supersteps.

    Attempt-ledger fields (#230): ``checkpointer`` is the active persistence
    for THIS run — set only when a checkpointer and workflow_id are both
    present, so the retry coordinator can tell ledger-backed budgets from
    process-local ones. ``superstep_offset`` (per-run resume offset) plus
    ``superstep`` (current index, set per superstep) give attempt reservations
    the same superstep numbering StepRecords use.
    """

    event_processors: list[EventProcessor] | None = None
    show_progress: bool | None = None
    parent_span_id: str | None = None
    workflow_id: str | None = None
    item_index: int | None = None
    run_id: str = ""
    graph_name: str = ""
    provided_values: dict[str, Any] = field(default_factory=dict)
    is_resuming: bool = False
    on_inner_log: Callable[[RunLog], None] | None = None
    checkpoint_error_sink: CheckpointErrorSink | None = None
    emit_fn: Callable[[Any], None] | None = None
    checkpointer: Checkpointer | None = None
    superstep_offset: int = 0
    superstep: int = 0


@dataclass
class NodeExecution:
    """Record of a single node execution.

    Used for tracking and staleness detection in cyclic graphs.

    Attributes:
        node_name: Name of the executed node
        input_versions: Version numbers of inputs at execution time
        output_versions: Version numbers of outputs right after execution
        outputs: Output values produced
        wait_for_versions: Version numbers of wait_for names at execution time
        duration_ms: Wall-clock execution time in milliseconds
        cached: Whether this execution was a cache hit
        sequence: Durable execution order, or -1 for legacy unsequenced records
    """

    node_name: str
    input_versions: dict[str, int]
    outputs: dict[str, Any]
    output_versions: dict[str, int] = field(default_factory=dict)
    wait_for_versions: dict[str, int] = field(default_factory=dict)
    duration_ms: float = 0.0
    cached: bool = False
    sequence: int = -1


@dataclass
class GraphState:
    """Internal runtime state during graph execution.

    Tracks current values and their versions for staleness detection.

    Attributes:
        values: Current value for each output/input name
        versions: Version number for each value (incremented on update)
        node_executions: History of node executions (for staleness detection)
        routing_decisions: Routing decisions made by gate nodes
        stopped: Whether a cooperative stop was requested during this run.
            Runtime-only: checkpoint restores start fresh (False).
        stop_info: Optional metadata passed to ``runner.stop(info=...)``.
            Runtime-only: checkpoint restores start fresh (None).
    """

    values: dict[str, Any] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)
    node_executions: dict[str, NodeExecution] = field(default_factory=dict)
    routing_decisions: dict[str, Any] = field(default_factory=dict)
    resume_values: frozenset[str] = frozenset()
    stopped: bool = False
    stop_info: Any = None
    # Actual child workflow id used by each GraphNode's latest execution,
    # recorded by the executors so StepRecord receipts report the id that
    # really ran (crash-window restore and retention-pruned re-executions can
    # diverge from the loop's precomputed candidate). Runtime-only: checkpoint
    # restores start fresh.
    graphnode_child_run_ids: dict[str, str] = field(default_factory=dict)

    def update_value(self, name: str, value: Any) -> None:
        """Update a value and increment its version if value changed.

        Only increments version if:
        - Name is new (not previously set), or
        - Value is different from previous value
        - Value is the emit sentinel (emit signals always advance freshness)
        """
        from hypergraph.nodes.base import _EMIT_SENTINEL

        old_value = self.values.get(name)
        is_new = name not in self.values

        self.values[name] = value

        # Emit signals are event-like: every write should advance version even
        # though the sentinel object instance is stable across emissions.
        if value is _EMIT_SENTINEL:
            self.versions[name] = self.versions.get(name, 0) + 1
            return

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

    def copy(self) -> GraphState:
        """Create a copy of this state with independent NodeExecution instances.

        Values and versions dicts are shallow-copied (keys are strings).
        NodeExecution instances are copied to prevent shared mutation.
        """
        from dataclasses import replace

        return GraphState(
            values=dict(self.values),
            versions=dict(self.versions),
            node_executions={
                k: replace(
                    v,
                    input_versions=dict(v.input_versions),
                    output_versions=dict(v.output_versions),
                    outputs=dict(v.outputs),
                    wait_for_versions=dict(v.wait_for_versions),
                )
                for k, v in self.node_executions.items()
            },
            routing_decisions=dict(self.routing_decisions),
            resume_values=frozenset(self.resume_values),
            stopped=self.stopped,
            stop_info=self.stop_info,
            graphnode_child_run_ids=dict(self.graphnode_child_run_ids),
        )
