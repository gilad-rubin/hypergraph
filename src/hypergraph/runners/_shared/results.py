"""Canonical runner result and status types.

The execution-trace types they carry live beside their collector in
``run_log.py`` and are re-exported here, the import path callers already use.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hypergraph._utils import plural

# One status enum, not two. ``RunStatus`` is defined in ``events.types`` — the
# lower-level, already-public surface that events are built from — and re-exported
# here so ``hypergraph.RunStatus``, ``hypergraph.runners.RunStatus`` and
# ``hypergraph.events.RunStatus`` are the same object: comparing a
# ``RunResult.status`` with a ``RunEndEvent.status`` can no longer be silently False.
from hypergraph.events.types import RunStatus as RunStatus

# One error-handling alias, not two. Defined in ``nodes.graph_node`` (which cannot
# import the runners package at runtime) and re-exported here, so adding a member
# cannot make the two definitions disagree.
from hypergraph.nodes.graph_node import ErrorHandling as ErrorHandling

# results.py stays the import path these trace types have always had —
# including for pickles that name it — so the unused ones are re-exported.
from hypergraph.runners._shared.run_log import (
    DURATION_PRECISION as DURATION_PRECISION,
)
from hypergraph.runners._shared.run_log import (
    MapLog,
    RunLog,
    _build_map_item_placeholder_log,
    _format_duration,
    _has_timed_work,
    build_restored_run_log,
)
from hypergraph.runners._shared.run_log import (
    NodeRecord as NodeRecord,
)
from hypergraph.runners._shared.run_log import (
    NodeStats as NodeStats,
)
from hypergraph.runners._shared.run_log import (
    _compute_node_stats as _compute_node_stats,
)
from hypergraph.runners.inspection import InspectionDisplay

if TYPE_CHECKING:
    from hypergraph.diagnostics import Diagnostic
    from hypergraph.runners._shared._inspect import MapInspection, RunInspection


@dataclass(frozen=True)
class FailureEvidence:
    """Ephemeral evidence for an exception raised by a node executor.

    ``inputs`` is a shallow snapshot of the resolved graph inputs. Contained
    values retain identity and stay referenced until this evidence is
    collected. Raw inputs are intentionally available only through explicit
    attribute access; implicit representations and serialization omit them.

    ``error`` is the exact exception object (a local surface); ``diagnostic``
    is its typed privacy-safe companion projection for durable consumers.
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

    @property
    def diagnostic(self) -> Diagnostic:
        """Stable, privacy-safe diagnostic for this failure.

        Derived from the exact exception object plus this evidence's node
        identity — codes, type names, counts, and static help only.
        """
        from hypergraph.diagnostics import derive_diagnostic

        return derive_diagnostic(
            self.error,
            node_name=self.node_name,
            graph_name=self.graph_name or None,
            superstep=self.superstep,
            item_index=self.item_index,
            workflow_id=self.workflow_id,
        )

    def __repr__(self) -> str:
        """Return a safe summary that never renders raw inputs or the error."""
        return (
            f"FailureEvidence({self.node_name!r} | {type(self.error).__name__} | superstep {self.superstep} | {_format_duration(self.duration_ms)})"
        )


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
        """True when ``status == RunStatus.STOPPED`` (stopped via runner.stop())."""
        return self.status == RunStatus.STOPPED

    @property
    def paused(self) -> bool:
        """True when ``status == RunStatus.PAUSED`` (paused at an InterruptNode)."""
        return self.status == RunStatus.PAUSED

    @property
    def completed(self) -> bool:
        """True when ``status == RunStatus.COMPLETED``."""
        return self.status == RunStatus.COMPLETED

    @property
    def failed(self) -> bool:
        """True when ``status == RunStatus.FAILED``."""
        return self.status == RunStatus.FAILED

    @property
    def failure(self) -> FailureEvidence | None:
        """First attributable node failure, if one exists."""
        return self.node_failures[0] if self.node_failures else None

    def inspect(self) -> InspectionDisplay[Any]:
        """Return an explicit rich inspection view for this settled result.

        The method returns one display value and emits no hidden notebook output.
        Runs created with ``inspect=True`` include shallow successful-node
        input/output snapshots. Ordinary and restored results remain
        inspectable but say truthfully which values were not captured.
        Unsupported values use a bounded ``repr`` fallback. ``repr`` is Python user code
        and may have side effects; raised exceptions become typed
        placeholders without changing the run status.
        """
        from hypergraph.runners._shared._inspect import degraded_run_inspection

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
        Use result.values directly for output access. Error entries are
        privacy-safe projections (type name, stable code, static wording),
        never ``str(exception)``; the exact object stays on ``self.error``.
        """
        from hypergraph.diagnostics import safe_error_text

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
            failure_node = self.node_failures[0].node_name if self.node_failures else None
            d["error"] = safe_error_text(self.error, node_name=failure_node)
        if self.status == RunStatus.FAILED:
            d["node_failures"] = [
                {
                    "node_name": failure.node_name,
                    "error": safe_error_text(failure.error, node_name=failure.node_name),
                    "diagnostic": failure.diagnostic.to_wire(),
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
        from hypergraph._runner_repr import render_run_result_repr

        return render_run_result_repr(self)

    def _repr_pretty_(self, pretty_printer: Any, cycle: bool) -> None:
        """Use the compact repr for IPython pretty display."""
        from hypergraph._runner_repr import render_run_result_pretty

        render_run_result_pretty(self, pretty_printer, cycle)

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import plain_reprs
        from hypergraph._runner_repr import render_run_result_html

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
    __hash__ = None  # type: ignore[assignment]

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
        """True when ``status == RunStatus.COMPLETED`` (including an empty map)."""
        return self.status == RunStatus.COMPLETED

    @property
    def paused(self) -> bool:
        """True when ``status == RunStatus.PAUSED``."""
        return self.status == RunStatus.PAUSED

    @property
    def stopped(self) -> bool:
        """True when ``status == RunStatus.STOPPED``."""
        return self.status == RunStatus.STOPPED

    @property
    def failed(self) -> bool:
        """True when ``status == RunStatus.FAILED``.

        Mirrors the aggregate exactly (parity with ``RunResult.failed``): at
        least one item failed and none completed. Use ``any_failed`` for
        "did anything fail" regardless of the aggregate status.
        """
        return self.status == RunStatus.FAILED

    @property
    def any_failed(self) -> bool:
        """True when ``bool(self.failures)`` — at least one item failed.

        Independent of the aggregate status: also True for PARTIAL batches
        and for STOPPED batches that carry real attempted-item failures.
        """
        return bool(self.failures)

    @property
    def partial(self) -> bool:
        """True when ``status == RunStatus.PARTIAL`` — some items completed and some failed."""
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

    def inspect(self) -> InspectionDisplay[Any]:
        """Return an explicit rich inspection view for this settled batch.

        The method returns one display value and emits no hidden notebook output.
        Unsupported values use a bounded ``repr`` fallback. ``repr`` is Python user code
        and may have side effects; raised exceptions become typed
        placeholders without changing the batch status.
        """
        from hypergraph.runners._shared._inspect import degraded_map_inspection

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
        from hypergraph._runner_repr import render_map_result_repr

        return render_map_result_repr(self)

    def _repr_pretty_(self, pretty_printer: Any, cycle: bool) -> None:
        from hypergraph._runner_repr import render_map_result_pretty

        render_map_result_pretty(self, pretty_printer, cycle)

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import plain_reprs
        from hypergraph._runner_repr import render_map_result_html

        if plain_reprs():
            return None
        return render_map_result_html(self)


Sequence.register(MapResult)


@dataclass
class PauseInfo:
    """Information about a paused execution.

    Attributes:
        node_name: Name of the InterruptNode that paused (uses "/" for nesting)
        value: The ask payload returned by the interrupt handler
        response_key: Graph-scope answer port to provide when resuming
    """

    node_name: str
    value: Any
    response_key: str
