"""Canonical runner results, logs, and status types."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from hypergraph._utils import plural

if TYPE_CHECKING:
    from hypergraph.runners._shared._inspect import MapInspection, RunInspection
    from hypergraph.runners._shared._inspect_html import InspectionDisplay

ErrorHandling = Literal["raise", "continue"]

DURATION_PRECISION = 3  # decimal places for duration_ms (microsecond precision)


@dataclass(frozen=True)
class FailureEvidence:
    """Ephemeral evidence for an exception raised by a node executor.

    ``inputs`` is a shallow snapshot of the resolved graph inputs. Contained
    values retain identity and stay referenced until this evidence is
    collected. Raw inputs are intentionally available only through explicit
    attribute access; implicit representations and serialization omit them.
    """

    node_name: str
    error: BaseException = field(repr=False)
    inputs: dict[str, Any] = field(repr=False)
    superstep: int
    duration_ms: float
    graph_name: str
    workflow_id: str | None
    item_index: int | None

    def __post_init__(self) -> None:
        """Own the input mapping without copying any contained value."""
        object.__setattr__(self, "inputs", dict(self.inputs))

    def __repr__(self) -> str:
        """Return a safe summary that never renders raw inputs or the error."""
        return (
            f"FailureEvidence({self.node_name!r} | {type(self.error).__name__} | superstep {self.superstep} | {_format_duration(self.duration_ms)})"
        )


class RunStatus(Enum):
    """Status of a graph execution run.

    Values:
        COMPLETED: Run finished successfully
        FAILED: Run encountered an error
        PAUSED: Execution paused at an InterruptNode, waiting for user response
        PARTIAL: Some map items completed, others failed (batch operations)
        STOPPED: Run was cooperatively stopped via runner.stop()
    """

    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    PARTIAL = "partial"
    STOPPED = "stopped"


def aggregate_run_status(results: Sequence[RunResult]) -> RunStatus:
    """Return the batch-level status for a sequence of run results."""
    has_failed = any(result.status == RunStatus.FAILED for result in results)
    has_completed = any(result.status == RunStatus.COMPLETED for result in results)
    if has_failed and has_completed:
        return RunStatus.PARTIAL
    if has_failed:
        return RunStatus.FAILED
    if any(result.status == RunStatus.PAUSED for result in results):
        return RunStatus.PAUSED
    if any(result.status == RunStatus.STOPPED for result in results):
        return RunStatus.STOPPED
    return RunStatus.COMPLETED


def generate_run_id() -> str:
    """Generate a unique run ID."""
    return f"run-{uuid.uuid4().hex[:12]}"


_generate_run_id = generate_run_id


@dataclass
class RunResult:
    """Result of a graph execution.

    Attributes:
        values: Dict of all output values produced
        status: Run status (COMPLETED, FAILED, PAUSED, or STOPPED)
        run_id: Unique identifier for this run
        workflow_id: Optional workflow identifier for tracking related runs
        error: Exception if status is FAILED, else None
        pause: PauseInfo if status is PAUSED, else None
        log: RunLog with execution trace (timing, status, routing), or None
        checkpoint_ok: False when background checkpoint step-saves failed
            under ``durability="async"`` (best-effort persistence). The run
            itself still completes; check this flag to detect gaps in the
            persisted history.
        checkpoint_errors: String reprs of failed background step-saves.
        restored: Whether this completed map child was skipped because its
            checkpoint was restored. Other resume/cache/missing-log paths are
            always False.
        node_failures: Attributable leaf-node failures in deterministic order
    """

    values: dict[str, Any]
    status: RunStatus
    run_id: str = field(default_factory=generate_run_id)
    workflow_id: str | None = None
    error: BaseException | None = None
    pause: PauseInfo | None = None
    log: RunLog | None = None
    checkpoint_ok: bool = True
    checkpoint_errors: tuple[str, ...] = ()
    restored: bool = False
    node_failures: tuple[FailureEvidence, ...] = ()
    _inspection: RunInspection | None = field(default=None, repr=False, compare=False)

    @property
    def stopped(self) -> bool:
        """Whether execution was stopped via runner.stop()."""
        return self.status == RunStatus.STOPPED

    @property
    def paused(self) -> bool:
        """Whether execution is paused at an InterruptNode."""
        return self.status == RunStatus.PAUSED

    @property
    def completed(self) -> bool:
        """Whether execution completed successfully."""
        return self.status == RunStatus.COMPLETED

    @property
    def failed(self) -> bool:
        """Whether execution failed."""
        return self.status == RunStatus.FAILED

    @property
    def failure(self) -> FailureEvidence | None:
        """First attributable node failure, if one exists."""
        return self.node_failures[0] if self.node_failures else None

    def inspect(self) -> InspectionDisplay:
        """Return an explicit rich inspection view for this settled result.

        Runs created with ``inspect=True`` include shallow successful-node
        input/output snapshots. Ordinary and restored results remain
        inspectable but say truthfully which values were not captured.
        """
        from hypergraph.runners._shared._inspect import degraded_run_inspection
        from hypergraph.runners._shared._inspect_html import InspectionDisplay

        artifact = self._inspection
        if artifact is None:
            artifact = degraded_run_inspection(self)
        return InspectionDisplay(artifact)

    def summary(self) -> str:
        """One-line overview: 'completed | 3 nodes | 12ms' or 'failed: ValueError'."""
        if self.restored:
            summary = "restored from checkpoint"
        elif self.log:
            summary = self.log.summary()
        elif self.error:
            summary = f"{self.status.value}: {type(self.error).__name__}: {self.error}"
        else:
            summary = self.status.value
        if not self.checkpoint_ok:
            error_count = len(self.checkpoint_errors)
            detail = f" ({plural(error_count, 'save error')})" if error_count else ""
            summary += f" | checkpoint gap{detail}"
        return summary

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict with status, run_id, and log.

        Does NOT include raw values or error objects — only metadata.
        Use result.values directly for output access.
        """
        d: dict[str, Any] = {
            "status": self.status.value,
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "checkpoint_ok": self.checkpoint_ok,
            "checkpoint_errors": list(self.checkpoint_errors),
            "restored": self.restored,
        }
        if self.log:
            d["log"] = self.log.to_dict()
        if self.error:
            d["error"] = f"{type(self.error).__name__}: {self.error}"
        if self.status == RunStatus.FAILED:
            d["node_failures"] = [
                {
                    "node_name": failure.node_name,
                    "error": f"{type(failure.error).__name__}: {failure.error}",
                    "superstep": failure.superstep,
                    "duration_ms": failure.duration_ms,
                    "graph_name": failure.graph_name,
                    "workflow_id": failure.workflow_id,
                    "item_index": failure.item_index,
                }
                for failure in self.node_failures
            ]
        return d

    def __getitem__(self, key: str) -> Any:
        """Dict-like access to values."""
        return self.values[key]

    def __contains__(self, key: str) -> bool:
        """Check if key exists in values."""
        return key in self.values

    def get(self, key: str, default: Any = None) -> Any:
        """Get value with default."""
        return self.values.get(key, default)

    def __repr__(self) -> str:
        """Compact repr to avoid extremely large notebook output."""
        from hypergraph._repr import render_run_result_repr

        return render_run_result_repr(self)

    def _repr_pretty_(self, pretty_printer: Any, cycle: bool) -> None:
        """Use the compact repr for IPython pretty display."""
        from hypergraph._repr import render_run_result_pretty

        render_run_result_pretty(self, pretty_printer, cycle)

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import plain_reprs, render_run_result_html

        if plain_reprs():
            return None
        return render_run_result_html(self)


def build_terminal_run_result(
    *,
    values: dict[str, Any],
    status: RunStatus,
    run_id: str,
    workflow_id: str | None,
    log: RunLog,
    checkpoint_errors: Sequence[str] = (),
    inspection: RunInspection | None = None,
) -> RunResult:
    """Build a completed or stopped result with durability evidence."""
    errors = tuple(checkpoint_errors)
    return RunResult(
        values=values,
        status=status,
        run_id=run_id,
        workflow_id=workflow_id,
        log=log,
        checkpoint_ok=not errors,
        checkpoint_errors=errors,
        _inspection=inspection,
    )


def build_paused_run_result(
    *,
    values: dict[str, Any],
    run_id: str,
    workflow_id: str | None,
    pause: PauseInfo,
    log: RunLog,
    checkpoint_errors: Sequence[str] = (),
    inspection: RunInspection | None = None,
) -> RunResult:
    """Build a paused result with durability evidence."""
    errors = tuple(checkpoint_errors)
    return RunResult(
        values=values,
        status=RunStatus.PAUSED,
        run_id=run_id,
        workflow_id=workflow_id,
        pause=pause,
        log=log,
        checkpoint_ok=not errors,
        checkpoint_errors=errors,
        _inspection=inspection,
    )


def build_failed_run_result(
    *,
    values: dict[str, Any],
    run_id: str,
    workflow_id: str | None,
    error: BaseException,
    log: RunLog,
    node_failures: Sequence[FailureEvidence] = (),
    checkpoint_errors: Sequence[str] = (),
    inspection: RunInspection | None = None,
) -> RunResult:
    """Build a failed result with durability evidence."""
    errors = tuple(checkpoint_errors)
    return RunResult(
        values=values,
        status=RunStatus.FAILED,
        run_id=run_id,
        workflow_id=workflow_id,
        error=error,
        node_failures=tuple(node_failures),
        log=log,
        checkpoint_ok=not errors,
        checkpoint_errors=errors,
        _inspection=inspection,
    )


def build_restored_run_result(
    *,
    values: dict[str, Any],
    graph_name: str,
    run_id: str,
) -> RunResult:
    """Build a completed map child restored from persisted state."""
    return RunResult(
        values=values,
        status=RunStatus.COMPLETED,
        run_id=run_id,
        workflow_id=run_id,
        log=build_restored_run_log(graph_name, run_id),
        restored=True,
    )


def build_pre_run_failed_result(error: BaseException) -> RunResult:
    """Build a failed map item for an error raised before execution starts."""
    return RunResult(
        values={},
        status=RunStatus.FAILED,
        run_id=generate_run_id(),
        error=error,
    )


@dataclass(frozen=True, eq=False)
class MapResult:
    """Result of a batch map() execution.

    Wraps individual RunResult items with batch-level metadata.
    Supports read-only sequence protocol: len(), iter(), indexing.
    String key access collects values across items:
        results["doubled"] → [2, 4, None, 6, 8]
        (None for failed items whose outputs are missing)
    """

    results: tuple[RunResult, ...]
    run_id: str | None  # None for empty (no-op) maps
    total_duration_ms: float
    map_over: tuple[str, ...]
    map_mode: str  # "zip" | "product"
    graph_name: str
    unstarted_item_indexes: tuple[int, ...] = ()
    _inspection: MapInspection | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Normalize and validate indexes for inputs curtailed before start."""
        indexes = tuple(self.unstarted_item_indexes)
        object.__setattr__(self, "unstarted_item_indexes", indexes)
        requested_count = len(self.results) + len(indexes)
        if any(index < 0 or index >= requested_count for index in indexes) or any(
            left >= right for left, right in zip(indexes, indexes[1:], strict=False)
        ):
            raise ValueError(
                "unstarted_item_indexes must be sorted, unique, non-negative, "
                "and within the requested map scope.\n\n"
                "How to fix: Pass each unstarted original input index once, in "
                "ascending order, within requested_count."
            )

    # --- Sequence protocol (read-only backward compat) ---

    def __len__(self) -> int:
        return len(self.results)

    def __iter__(self):
        return iter(self.results)

    def __bool__(self) -> bool:
        return len(self.results) > 0

    def __reversed__(self):
        return reversed(self.results)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self.results[key]
        if isinstance(key, slice):
            return list(self.results[key])
        if isinstance(key, str):
            return [r.get(key) for r in self.results]
        raise TypeError(f"indices must be integers, slices, or strings, not {type(key).__name__}")

    def __contains__(self, item):
        return item in self.results

    def __eq__(self, other):
        if isinstance(other, MapResult):
            return self.results == other.results
        if isinstance(other, list):
            return list(self.results) == other
        return NotImplemented

    # Equality is list-like and RunResult items are mutable, so hashing would
    # either violate the equality contract or work only for empty batches.
    __hash__ = None

    # --- Aggregate properties ---

    @property
    def requested_count(self) -> int:
        """Number of requested inputs, including those never started."""
        return len(self.results) + len(self.unstarted_item_indexes)

    @property
    def status(self) -> RunStatus:
        """Batch-level aggregate status.

        PARTIAL when some items completed and some failed — the common case
        for large batches where a few items hit transient errors.
        FAILED when at least one item failed and none completed. Empty → COMPLETED.
        STOPPED when cooperative stop leaves requested inputs unstarted.
        """
        if self.unstarted_item_indexes:
            return RunStatus.STOPPED
        return aggregate_run_status(self.results)

    @property
    def completed(self) -> bool:
        """Whether the batch aggregate completed (including an empty map)."""
        return self.status == RunStatus.COMPLETED

    @property
    def paused(self) -> bool:
        """Whether the batch aggregate paused."""
        return self.status == RunStatus.PAUSED

    @property
    def stopped(self) -> bool:
        """Whether the batch aggregate stopped."""
        return self.status == RunStatus.STOPPED

    @property
    def failed(self) -> bool:
        """True if any item failed (FAILED or PARTIAL)."""
        return any(r.status == RunStatus.FAILED for r in self.results)

    @property
    def partial(self) -> bool:
        """True if some items completed and some failed."""
        return self.status == RunStatus.PARTIAL

    @property
    def failures(self) -> list[RunResult]:
        """Only failed items."""
        return [r for r in self.results if r.status == RunStatus.FAILED]

    @property
    def restored_count(self) -> int:
        """Number of completed items restored without child execution."""
        return sum(1 for result in self.results if result.restored)

    @property
    def _timed_completed_items(self) -> tuple[RunResult, ...]:
        """Fresh completed items whose real execution logs can be averaged."""
        return tuple(
            result for result in self.results if result.status == RunStatus.COMPLETED and not result.restored and _has_timed_work(result.log)
        )

    @property
    def checkpoint_ok(self) -> bool:
        """Whether every item persisted all best-effort async checkpoints."""
        return all(result.checkpoint_ok for result in self.results)

    @property
    def checkpoint_errors(self) -> tuple[str, ...]:
        """Checkpoint-save errors flattened in stable item order."""
        return tuple(error for result in self.results for error in result.checkpoint_errors)

    @property
    def _checkpoint_gap_count(self) -> int:
        """Number of items with incomplete best-effort checkpoint persistence."""
        return sum(1 for result in self.results if not result.checkpoint_ok)

    @property
    def log(self) -> MapLog:
        """Batch-level execution trace."""
        return MapLog(
            graph_name=self.graph_name,
            total_duration_ms=self.total_duration_ms,
            items=tuple(r.log if r.log is not None else _build_map_item_placeholder_log(r, self.graph_name) for r in self.results),
            _item_restored=tuple(result.restored for result in self.results),
            _item_timed=tuple(
                result.status == RunStatus.COMPLETED and not result.restored and _has_timed_work(result.log) for result in self.results
            ),
        )

    def get(self, key: str, default: Any = None) -> list[Any]:
        """Collect values across items with a default.
        results.get("doubled", 0) → [2, 4, 0, 6, 8]"""
        return [r.get(key, default) for r in self.results]

    def inspect(self) -> InspectionDisplay:
        """Return an explicit rich inspection view for this settled batch."""
        from hypergraph.runners._shared._inspect import degraded_map_inspection
        from hypergraph.runners._shared._inspect_html import InspectionDisplay

        artifact = self._inspection
        if artifact is None:
            artifact = degraded_map_inspection(self)
        return InspectionDisplay(artifact)

    # --- Progressive disclosure ---

    def summary(self) -> str:
        """One-liner: '5 items | 4 completed, 1 failed | avg 42ms/item'"""
        n = len(self.results)
        n_completed = sum(1 for r in self.results if r.status == RunStatus.COMPLETED)
        n_failed = sum(1 for r in self.results if r.status == RunStatus.FAILED)
        n_paused = sum(1 for r in self.results if r.status == RunStatus.PAUSED)
        n_stopped = sum(1 for r in self.results if r.status == RunStatus.STOPPED)
        n_restored = self.restored_count
        if self.unstarted_item_indexes:
            parts = [
                f"{n} of {plural(self.requested_count, 'item')} settled",
                plural(len(self.unstarted_item_indexes), "unstarted item"),
            ]
        else:
            parts = [plural(n, "item")]
        status_parts = []
        if n_completed:
            status_parts.append(f"{n_completed} completed")
        if n_failed:
            status_parts.append(f"{n_failed} failed")
        if n_paused:
            status_parts.append(f"{n_paused} paused")
        if n_stopped:
            status_parts.append(f"{n_stopped} stopped")
        if n_restored:
            status_parts.append(f"{n_restored} restored")
        if status_parts:
            parts.append(", ".join(status_parts))
        checkpoint_gap_count = self._checkpoint_gap_count
        if checkpoint_gap_count:
            parts.append(f"{plural(checkpoint_gap_count, 'item')} with checkpoint gaps")
        timed_completed_items = self._timed_completed_items
        if timed_completed_items:
            completed_ms = sum(result.log.total_duration_ms for result in timed_completed_items if result.log is not None)
            avg = completed_ms / len(timed_completed_items)
            parts.append(f"avg {_format_duration(avg)}/item")
        return " | ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable batch metadata + per-item results."""
        n_completed = sum(1 for r in self.results if r.status == RunStatus.COMPLETED)
        n_failed = sum(1 for r in self.results if r.status == RunStatus.FAILED)
        return {
            "run_id": self.run_id,
            "total_duration_ms": self.total_duration_ms,
            "map_over": list(self.map_over),
            "map_mode": self.map_mode,
            "graph_name": self.graph_name,
            "checkpoint_ok": self.checkpoint_ok,
            "checkpoint_errors": list(self.checkpoint_errors),
            "item_count": len(self.results),
            "requested_count": self.requested_count,
            "unstarted_item_indexes": list(self.unstarted_item_indexes),
            "completed_count": n_completed,
            "restored_count": self.restored_count,
            "failed_count": n_failed,
            "items": [item.to_dict() for item in self.results],
        }

    def __repr__(self) -> str:
        from hypergraph._repr import render_map_result_repr

        return render_map_result_repr(self)

    def _repr_pretty_(self, pretty_printer: Any, cycle: bool) -> None:
        from hypergraph._repr import render_map_result_pretty

        render_map_result_pretty(self, pretty_printer, cycle)

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import plain_reprs, render_map_result_html

        if plain_reprs():
            return None
        return render_map_result_html(self)


Sequence.register(MapResult)


@dataclass
class PauseInfo:
    """Information about a paused execution.

    Attributes:
        node_name: Name of the InterruptNode that paused (uses "/" for nesting)
        output_param: The first output parameter name (backward compat)
        value: The first input value surfaced to the caller (backward compat)
        output_params: All output parameter names if multi-output, else None
        values: All input values as {name: value} if multi-input, else None
    """

    node_name: str
    output_param: str
    value: Any
    output_params: tuple[str, ...] | None = None
    values: dict[str, Any] | None = None

    @property
    def response_key(self) -> str:
        """Key to use in values dict when resuming (first output).

        ``output_param`` is already the resolved graph-scope response address.
        """
        return self.output_param

    @property
    def response_keys(self) -> dict[str, str]:
        """Map output names to resume keys.

        Returns a dict mapping each output parameter name to the key to use when
        providing the response value in the input dict. The names are already
        resolved graph-scope addresses after GraphNode projection, so both key
        and value are the same address (for example, ``"decision"`` in flat
        mode or ``"review.decision"`` in namespaced mode).
        """
        params = self.output_params or (self.output_param,)
        return {p: p for p in params}


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
                    "inner_log": s.log.to_dict() if s._inner_logs else None,
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
