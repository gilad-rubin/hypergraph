"""RunLog types and the collector that builds them from the event stream.

The always-on execution trace lives here: a ``NodeRecord`` per node execution,
a ``RunLog`` per run, a ``MapLog`` per batch. ``RunLogCollector`` is the
``TypedEventProcessor`` that turns the events a run already emits into those
records — it listens, it never drives execution.

Thread safety: The EventDispatcher processes events sequentially (even in
async mode), so concurrent writes to internal dicts are not a concern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from hypergraph._utils import plural
from hypergraph.events.processor import TypedEventProcessor
from hypergraph.events.types import (
    InterruptEvent,
    NodeEndEvent,
    NodeErrorEvent,
    NodeStartEvent,
    RouteDecisionEvent,
    SuperstepStartEvent,
)

if TYPE_CHECKING:
    from hypergraph.runners._shared.results import RunResult

DURATION_PRECISION = 3  # decimal places for duration_ms (microsecond precision)


# ---------------------------------------------------------------------------
# RunLog types — always-on execution trace
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeRecord:
    """Record of a single node execution within a run.

    Attributes:
        node_name: Name of the executed node.
        superstep: Parallel execution round (0-indexed).
        duration_ms: Wall-clock execution time in milliseconds.
        status: "completed", "failed", "paused", or "restored".
        span_id: Correlates with OTel traces.
        error: Error message if status is "failed".
        cached: Whether this was a cache hit.
        decision: Gate routing decision, if this was a gate node.
    """

    node_name: str
    superstep: int
    duration_ms: float
    status: Literal["completed", "failed", "paused", "restored"]
    span_id: str
    error: str | None = None
    cached: bool = False
    decision: str | list[str] | None = None
    _inner_logs: tuple[RunLog, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "duration_ms", round(self.duration_ms, DURATION_PRECISION))

    def __repr__(self) -> str:
        from hypergraph._repr import render_node_record_repr

        return render_node_record_repr(self)

    @property
    def log(self) -> RunLog | MapLog | None:
        """Drill into nested execution traces.

        Returns RunLog for single inner, MapLog for multiple, None for leaf nodes.
        """
        if not self._inner_logs:
            return None
        if len(self._inner_logs) == 1:
            return self._inner_logs[0]
        return MapLog(
            graph_name=self._inner_logs[0].graph_name,
            total_duration_ms=sum(log.total_duration_ms for log in self._inner_logs),
            items=self._inner_logs,
        )


@dataclass(frozen=True)
class NodeStats:
    """Aggregate statistics for a node across executions.

    Produced by RunLog.node_stats — immutable after creation.
    """

    count: int = 0
    total_ms: float = 0.0
    errors: int = 0
    cached: int = 0

    @property
    def succeeded(self) -> int:
        """Executions that completed without cache hit."""
        return self.count - self.errors - self.cached

    @property
    def avg_ms(self) -> float:
        """Average execution time for succeeded (non-cached) runs."""
        return self.total_ms / self.succeeded if self.succeeded > 0 else 0.0

    def __repr__(self) -> str:
        from hypergraph._repr import render_node_stats_repr

        return render_node_stats_repr(self)


def _format_duration(ms: float) -> str:
    """Format milliseconds into human-readable duration."""
    from hypergraph._utils import format_duration_ms

    return format_duration_ms(ms)


def _compute_node_stats(steps: tuple[NodeRecord, ...]) -> dict[str, NodeStats]:
    """Aggregate per-node stats from step records."""
    accumulators: dict[str, dict[str, Any]] = {}
    for step in steps:
        if step.status == "restored":
            continue
        acc = accumulators.setdefault(step.node_name, {"count": 0, "total_ms": 0.0, "errors": 0, "cached": 0})
        acc["count"] += 1
        acc["total_ms"] += step.duration_ms
        if step.status == "failed":
            acc["errors"] += 1
        if step.cached:
            acc["cached"] += 1
    return {name: NodeStats(**vals) for name, vals in accumulators.items()}


@dataclass(frozen=True)
class RunLog:
    """Immutable execution trace, available on every RunResult.

    Provides progressive disclosure: summary() for one-liner,
    node_stats for aggregates, steps for full trace, to_dict()
    for JSON serialization.
    """

    graph_name: str
    run_id: str
    total_duration_ms: float
    steps: tuple[NodeRecord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "total_duration_ms", round(self.total_duration_ms, DURATION_PRECISION))

    @property
    def errors(self) -> tuple[NodeRecord, ...]:
        """Only failed steps."""
        return tuple(s for s in self.steps if s.status == "failed")

    @property
    def timing(self) -> dict[str, float]:
        """Total ms per node name."""
        result: dict[str, float] = {}
        for step in self.steps:
            result[step.node_name] = result.get(step.node_name, 0.0) + step.duration_ms
        return result

    @property
    def node_stats(self) -> dict[str, NodeStats]:
        """Aggregate statistics per node name."""
        return _compute_node_stats(self.steps)

    def summary(self) -> str:
        """One-line overview string."""
        n_errors = len(self.errors)
        n_nodes = len({s.node_name for s in self.steps})
        n_restored = sum(1 for step in self.steps if step.status == "restored")
        slowest = max(self.timing.items(), key=lambda x: x[1]) if self.timing else ("", 0)
        parts = [
            plural(n_nodes, "node"),
            f"{n_restored} restored" if n_restored else _format_duration(self.total_duration_ms),
            plural(n_errors, "error"),
        ]
        if slowest[1] > 0:
            parts.append(f"slowest: {slowest[0]} ({_format_duration(slowest[1])})")
        return " | ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict with only primitive types.

        Returns only str, int, float, bool, None, list, dict.
        This is intentionally shallow — no complex objects.
        """
        return {
            "graph_name": self.graph_name,
            "run_id": self.run_id,
            "total_duration_ms": self.total_duration_ms,
            "steps": [
                {
                    "node_name": s.node_name,
                    "superstep": s.superstep,
                    "duration_ms": s.duration_ms,
                    "status": s.status,
                    "span_id": s.span_id,
                    "error": s.error,
                    "cached": s.cached,
                    "decision": s.decision,
                    "inner_log": s.log.to_dict() if s._inner_logs else None,  # type: ignore[union-attr]
                }
                for s in self.steps
            ],
            "node_stats": {
                name: {
                    "count": stats.count,
                    "total_ms": stats.total_ms,
                    "avg_ms": stats.avg_ms,
                    "errors": stats.errors,
                    "cached": stats.cached,
                }
                for name, stats in self.node_stats.items()
            },
        }

    def __str__(self) -> str:
        """Formatted table output for terminal / print()."""
        from hypergraph._repr import render_run_log_str

        return render_run_log_str(self)

    def __repr__(self) -> str:
        """Concise repr for REPL/debugger."""
        from hypergraph._repr import render_run_log_repr

        return render_run_log_repr(self)

    def _repr_pretty_(self, pretty_printer: Any, cycle: bool) -> None:
        """Show full table in IPython/Jupyter notebooks."""
        from hypergraph._repr import render_run_log_pretty

        render_run_log_pretty(self, pretty_printer, cycle)

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import plain_reprs, render_run_log_html

        if plain_reprs():
            return None
        return render_run_log_html(self)


def build_restored_run_log(graph_name: str, run_id: str) -> RunLog:
    """Build visible, non-error evidence for a checkpoint-restored map child."""
    return RunLog(
        graph_name=graph_name,
        run_id=run_id,
        total_duration_ms=0.0,
        steps=(
            NodeRecord(
                node_name="map_item",
                superstep=0,
                duration_ms=0.0,
                status="restored",
                span_id="restored-checkpoint",
            ),
        ),
    )


def _build_map_item_placeholder_log(result: RunResult, graph_name: str) -> RunLog:
    """Create a synthetic per-item log when map item trace is unavailable."""
    # Local import: results.py owns RunStatus and imports this module for the
    # trace types, so the runtime dependency only ever points that way.
    from hypergraph.runners._shared.results import RunStatus

    if result.restored:
        return build_restored_run_log(graph_name, result.run_id)
    if result.status == RunStatus.FAILED:
        error_text = None
        if result.error is not None:
            error_text = f"{type(result.error).__name__}: {result.error}"
        steps: tuple[NodeRecord, ...] = (
            NodeRecord(
                node_name="map_item",
                superstep=0,
                duration_ms=0.0,
                status="failed",
                span_id="missing-log",
                error=error_text,
            ),
        )
    else:
        steps = ()
    return RunLog(
        graph_name=graph_name,
        run_id=result.run_id,
        total_duration_ms=0.0,
        steps=steps,
    )


def _has_timed_work(log: RunLog | None) -> bool:
    """Whether a real, non-error run log contains work that was not cached."""
    return log is not None and not log.errors and any(not step.cached for step in log.steps)


@dataclass(frozen=True)
class MapLog:
    """Batch-level execution trace for map() or map_over.

    Progressive disclosure: summary() → print() → [i] for per-item drill-down.
    """

    graph_name: str
    total_duration_ms: float
    items: tuple[RunLog, ...]
    _item_restored: tuple[bool, ...] = field(default=(), repr=False, compare=False)
    _item_timed: tuple[bool, ...] = field(default=(), repr=False, compare=False)

    @property
    def _restored_flags(self) -> tuple[bool, ...]:
        if len(self._item_restored) == len(self.items):
            return self._item_restored
        return tuple(any(step.status == "restored" for step in log.steps) for log in self.items)

    @property
    def _timed_flags(self) -> tuple[bool, ...]:
        if len(self._item_timed) == len(self.items):
            return self._item_timed
        return tuple(not restored and _has_timed_work(log) for log, restored in zip(self.items, self._restored_flags, strict=False))

    @property
    def restored_count(self) -> int:
        """Number of item logs representing checkpoint restoration."""
        return sum(self._restored_flags)

    @property
    def _timed_success_items(self) -> tuple[RunLog, ...]:
        return tuple(log for log, timed in zip(self.items, self._timed_flags, strict=False) if timed)

    @property
    def errors(self) -> tuple[NodeRecord, ...]:
        """All failed NodeRecords across all items."""
        return tuple(record for log in self.items for record in log.errors)

    @property
    def node_stats(self) -> dict[str, NodeStats]:
        """Aggregate stats across all items (cross-item bottleneck analysis)."""
        all_steps = tuple(step for log in self.items for step in log.steps)
        return _compute_node_stats(all_steps)

    def summary(self) -> str:
        """One-liner: '5 items | 5 completed, 0 errors | avg 42ms/item'."""
        n = len(self.items)
        n_succeeded = sum(1 for log in self.items if not log.errors)
        n_errors = len(self.errors)
        n_restored = self.restored_count
        parts = [plural(n, "item")]
        status_parts = []
        if n_succeeded:
            status_parts.append(f"{n_succeeded} completed")
        if n_errors:
            status_parts.append(plural(n_errors, "error"))
        if n_restored:
            status_parts.append(f"{n_restored} restored")
        if status_parts:
            parts.append(", ".join(status_parts))
        timed_success_items = self._timed_success_items
        if timed_success_items:
            avg = sum(log.total_duration_ms for log in timed_success_items) / len(timed_success_items)
            parts.append(f"avg {_format_duration(avg)}/item")
        return " | ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict."""
        return {
            "graph_name": self.graph_name,
            "total_duration_ms": self.total_duration_ms,
            "restored_count": self.restored_count,
            "items": [log.to_dict() for log in self.items],
        }

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> RunLog:
        return self.items[index]

    def __iter__(self):
        return iter(self.items)

    def __str__(self) -> str:
        """Per-item table with footer."""
        from hypergraph._repr import render_map_log_str

        return render_map_log_str(self)

    def __repr__(self) -> str:
        from hypergraph._repr import render_map_log_repr

        return render_map_log_repr(self)

    def _repr_pretty_(self, pretty_printer: Any, cycle: bool) -> None:
        """Show table in IPython/Jupyter notebooks."""
        from hypergraph._repr import render_map_log_pretty

        render_map_log_pretty(self, pretty_printer, cycle)

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import plain_reprs, render_map_log_html

        if plain_reprs():
            return None
        return render_map_log_html(self)


class RunLogCollector(TypedEventProcessor):
    """Collects execution events into NodeRecords, then builds a RunLog.

    Lifecycle:
        1. Runner prepends collector to processor list
        2. Events flow through on_* handlers during execution
        3. Runner calls build() after execution to get the frozen RunLog
    """

    def __init__(self) -> None:
        self._current_superstep: int = 0
        self._records: list[NodeRecord] = []
        self._decision_buffer: dict[str, str | list[str]] = {}
        self._node_start_times: dict[str, float] = {}  # span_id → timestamp

    def on_superstep_start(self, event: SuperstepStartEvent) -> None:
        self._current_superstep = event.superstep

    def on_node_start(self, event: NodeStartEvent) -> None:
        self._node_start_times[event.span_id] = event.timestamp

    def on_route_decision(self, event: RouteDecisionEvent) -> None:
        self._decision_buffer[event.node_name] = event.decision

    def on_node_end(self, event: NodeEndEvent) -> None:
        decision = self._decision_buffer.pop(event.node_name, None)
        self._node_start_times.pop(event.span_id, None)
        self._records.append(
            NodeRecord(
                node_name=event.node_name,
                superstep=self._current_superstep,
                duration_ms=event.duration_ms,
                status="completed",
                span_id=event.span_id,
                cached=event.cached,
                decision=decision,
                _inner_logs=getattr(event, "inner_logs", ()),
            )
        )

    def on_node_error(self, event: NodeErrorEvent) -> None:
        start_time = self._node_start_times.pop(event.span_id, None)
        duration_ms = (event.timestamp - start_time) * 1000 if start_time else 0.0
        decision = self._decision_buffer.pop(event.node_name, None)
        self._records.append(
            NodeRecord(
                node_name=event.node_name,
                superstep=self._current_superstep,
                duration_ms=duration_ms,
                status="failed",
                span_id=event.span_id,
                error=event.error,
                decision=decision,
            )
        )

    def on_interrupt(self, event: InterruptEvent) -> None:
        self._records.append(
            NodeRecord(
                node_name=event.node_name,
                superstep=self._current_superstep,
                duration_ms=0.0,
                status="paused",
                span_id=event.span_id,
            )
        )

    @property
    def step_count(self) -> int:
        """Number of collected node records."""
        return len(self._records)

    @property
    def failed_step_count(self) -> int:
        """Number of collected failed node records."""
        return sum(1 for record in self._records if record.status == "failed")

    def build(self, graph_name: str, run_id: str, total_duration_ms: float) -> RunLog:
        """Produce the frozen RunLog from collected records."""
        return RunLog(
            graph_name=graph_name,
            run_id=run_id,
            total_duration_ms=total_duration_ms,
            steps=tuple(self._records),
        )
