"""Core types for the execution runtime."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from hypergraph._utils import plural

if TYPE_CHECKING:
    from hypergraph.events.processor import EventProcessor

ErrorHandling = Literal["raise", "continue"]
CheckpointErrorSink = Callable[[str], None]

_MAX_STRING_PREVIEW = 120
_MAX_SEQUENCE_PREVIEW = 6
_MAX_MAPPING_PREVIEW = 6
_MAX_VALUE_REPR = 240
_MAX_RUN_RESULT_REPR = 4_000
DURATION_PRECISION = 3  # decimal places for duration_ms (microsecond precision)


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


def _generate_run_id() -> str:
    """Generate a unique run ID."""
    return f"run-{uuid.uuid4().hex[:12]}"


def _truncate_text(text: str, max_length: int) -> str:
    """Truncate text to max_length and append an ellipsis when needed."""
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


def _safe_repr(value: Any) -> str:
    """Return repr(value), falling back to a safe placeholder."""
    try:
        return repr(value)
    except Exception as exc:  # pragma: no cover - defensive fallback
        return f"<unreprable {type(value).__name__}: {exc}>"


def _compact_string(text: str) -> str:
    """Compact long strings while preserving quote style."""
    if len(text) <= _MAX_STRING_PREVIEW:
        return repr(text)
    preview = _truncate_text(text, _MAX_STRING_PREVIEW)
    return f"{preview!r} (len={len(text)})"


def _compact_mapping(mapping: dict[Any, Any], depth: int, seen: set[int]) -> str:
    """Return a compact representation for dict-like values."""
    items = list(mapping.items())
    preview_items = items[:_MAX_MAPPING_PREVIEW]
    parts = [f"{_truncate_text(_safe_repr(k), 80)}: {_compact_value(v, depth + 1, seen)}" for k, v in preview_items]
    remaining = len(items) - len(preview_items)
    if remaining > 0:
        parts.append(f"... (+{remaining} more)")
    return "{" + ", ".join(parts) + "}"


def _compact_sequence(values: list[Any], sequence_type: str, depth: int, seen: set[int]) -> str:
    """Return a compact representation for long sequence-like values."""
    if len(values) <= _MAX_SEQUENCE_PREVIEW:
        compact_items = [_compact_value(v, depth + 1, seen) for v in values]
        if sequence_type == "tuple":
            if len(compact_items) == 1:
                return f"({compact_items[0]},)"
            return "(" + ", ".join(compact_items) + ")"
        if sequence_type == "set":
            if not compact_items:
                return "set()"
            return "{" + ", ".join(compact_items) + "}"
        if sequence_type == "frozenset":
            if not compact_items:
                return "frozenset()"
            return "frozenset({" + ", ".join(compact_items) + "})"
        return "[" + ", ".join(compact_items) + "]"
    preview = ", ".join(_compact_value(v, depth + 1, seen) for v in values[:_MAX_SEQUENCE_PREVIEW])
    return f"<{sequence_type} len={len(values)} preview=[{preview}, ...]>"


def _compact_value(value: Any, depth: int = 0, seen: set[int] | None = None) -> str:
    """Build a compact, recursion-safe representation for nested values."""
    if seen is None:
        seen = set()

    if isinstance(value, str):
        return _compact_string(value)

    if isinstance(value, (int, float, bool, type(None))):
        return repr(value)

    if isinstance(value, bytes):
        return _truncate_text(repr(value), _MAX_VALUE_REPR)

    if depth >= 2:
        return _truncate_text(_safe_repr(value), _MAX_VALUE_REPR)

    is_recursive_candidate = isinstance(value, (dict, list, tuple, set, frozenset)) or (is_dataclass(value) and not isinstance(value, type))
    if is_recursive_candidate:
        object_id = id(value)
        if object_id in seen:
            return f"<recursive {type(value).__name__}>"
        seen.add(object_id)

    try:
        if is_dataclass(value) and not isinstance(value, type):
            field_map = {f.name: getattr(value, f.name) for f in fields(value)}
            return f"<{type(value).__name__} {_compact_mapping(field_map, depth, seen)}>"

        if isinstance(value, dict):
            return _compact_mapping(value, depth, seen)

        if isinstance(value, list):
            return _compact_sequence(value, "list", depth, seen)

        if isinstance(value, tuple):
            return _compact_sequence(list(value), "tuple", depth, seen)

        if isinstance(value, (set, frozenset)):
            preview_list = list(value)
            return _compact_sequence(preview_list, type(value).__name__, depth, seen)

        shape = getattr(value, "shape", None)
        if shape is not None and hasattr(value, "dtype"):
            dtype = getattr(value, "dtype", None)
            return f"<{type(value).__name__} shape={shape!r} dtype={dtype!r}>"

        return _truncate_text(_safe_repr(value), _MAX_VALUE_REPR)
    finally:
        if is_recursive_candidate:
            seen.discard(id(value))


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
    """

    values: dict[str, Any]
    status: RunStatus
    run_id: str = field(default_factory=_generate_run_id)
    workflow_id: str | None = None
    error: BaseException | None = None
    pause: PauseInfo | None = None
    log: RunLog | None = None
    checkpoint_ok: bool = True
    checkpoint_errors: tuple[str, ...] = ()
    restored: bool = False

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
        checkpoint = ""
        if not self.checkpoint_ok:
            checkpoint = f", checkpoint_ok=False, checkpoint_errors={_compact_value(self.checkpoint_errors)}"
        text = (
            "RunResult("
            f"status={self.status.value}, "
            f"values={_compact_value(self.values)}, "
            f"run_id={self.run_id!r}, "
            f"workflow_id={self.workflow_id!r}, "
            f"restored={self.restored}, "
            f"error={_compact_value(self.error)}, "
            f"pause={_compact_value(self.pause)}"
            f"{checkpoint}"
            ")"
        )
        return _truncate_text(text, _MAX_RUN_RESULT_REPR)

    def _repr_pretty_(self, pretty_printer: Any, cycle: bool) -> None:
        """Use the compact repr for IPython pretty display."""
        if cycle:
            pretty_printer.text("RunResult(...)")
            return
        pretty_printer.text(repr(self))

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import (
            ERROR_COLOR,
            duration_html,
            error_html,
            html_detail,
            html_kv,
            html_panel,
            plain_reprs,
            status_badge,
            theme_wrap,
            values_html,
            widget_state_key,
        )

        if plain_reprs():
            return None

        kvs = [html_kv("Status", status_badge(self.status.value))]
        if self.restored:
            kvs.append(html_kv("Restored", status_badge("restored")))
        if self.log and not self.restored:
            kvs.append(html_kv("Duration", duration_html(self.log.total_duration_ms)))
            kvs.append(html_kv("Nodes", str(len({s.node_name for s in self.log.steps}))))
            n_errors = len(self.log.errors)
            if n_errors:
                kvs.append(html_kv("Errors", f'<span style="color:{ERROR_COLOR}">{n_errors}</span>'))
        if not self.checkpoint_ok:
            error_count = len(self.checkpoint_errors)
            detail = f" ({plural(error_count, 'save error')})" if error_count else ""
            kvs.append(html_kv("Checkpoint", f'<span style="color:{ERROR_COLOR}">gap{detail}</span>'))
        kvs.append(html_kv("Values", plural(len(self.values), "key")))
        body = " &nbsp;|&nbsp; ".join(kvs)
        if self.error:
            body += error_html(self.error)
        if self.values:
            body += html_detail(
                f"Values ({plural(len(self.values), 'key')})",
                values_html(self.values),
                state_key="values",
            )
        if self.checkpoint_errors:
            body += html_detail(
                f"Checkpoint errors ({plural(len(self.checkpoint_errors), 'save error')})",
                values_html({str(index): error for index, error in enumerate(self.checkpoint_errors)}),
                state_key="checkpoint-errors",
            )
        if self.log:
            body += html_detail("Run Log", self.log._repr_html_(), state_key="run-log")
        return theme_wrap(
            html_panel(f"RunResult: {self.run_id}", body),
            state_key=widget_state_key("run-result", self.workflow_id or "", self.run_id),
        )


@dataclass(frozen=True)
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

    # --- Aggregate properties ---

    @property
    def status(self) -> RunStatus:
        """Batch-level aggregate status.

        PARTIAL when some items completed and some failed — the common case
        for large batches where a few items hit transient errors.
        FAILED when at least one item failed and none completed. Empty → COMPLETED.
        """
        return aggregate_run_status(self.results)

    @property
    def completed(self) -> bool:
        """True if all items completed (or empty)."""
        return self.status == RunStatus.COMPLETED

    @property
    def paused(self) -> bool:
        """True if any item is paused (and none failed)."""
        return self.status == RunStatus.PAUSED

    @property
    def stopped(self) -> bool:
        """True if any item stopped (and none failed or paused)."""
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

    # --- Progressive disclosure ---

    def summary(self) -> str:
        """One-liner: '5 items | 4 completed, 1 failed | avg 42ms/item'"""
        n = len(self.results)
        n_completed = sum(1 for r in self.results if r.status == RunStatus.COMPLETED)
        n_failed = sum(1 for r in self.results if r.status == RunStatus.FAILED)
        n_paused = sum(1 for r in self.results if r.status == RunStatus.PAUSED)
        n_stopped = sum(1 for r in self.results if r.status == RunStatus.STOPPED)
        n_restored = self.restored_count
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
            "completed_count": n_completed,
            "restored_count": self.restored_count,
            "failed_count": n_failed,
            "items": [item.to_dict() for item in self.results],
        }

    def __repr__(self) -> str:
        n = len(self.results)
        n_completed = sum(1 for r in self.results if r.status == RunStatus.COMPLETED)
        n_failed = sum(1 for r in self.results if r.status == RunStatus.FAILED)
        n_paused = sum(1 for r in self.results if r.status == RunStatus.PAUSED)
        n_stopped = sum(1 for r in self.results if r.status == RunStatus.STOPPED)
        n_restored = self.restored_count
        parts = []
        if n_completed:
            parts.append(f"{n_completed} completed")
        if n_failed:
            parts.append(f"{n_failed} failed")
        if n_paused:
            parts.append(f"{n_paused} paused")
        if n_stopped:
            parts.append(f"{n_stopped} stopped")
        if n_restored:
            parts.append(f"{n_restored} restored")
        status = ", ".join(parts) if parts else "empty"
        checkpoint_gap_count = self._checkpoint_gap_count
        checkpoint_part = f", {plural(checkpoint_gap_count, 'item')} with checkpoint gaps" if checkpoint_gap_count else ""
        avg_part = ""
        timed_completed_items = self._timed_completed_items
        if timed_completed_items:
            completed_ms = sum(result.log.total_duration_ms for result in timed_completed_items if result.log is not None)
            avg = completed_ms / len(timed_completed_items)
            avg_part = f", avg {_format_duration(avg)}/item"
        return f"MapResult({plural(n, 'item')}: {status}{checkpoint_part}{avg_part}, map_over={self.map_over!r})"

    def _repr_pretty_(self, pretty_printer: Any, cycle: bool) -> None:
        if cycle:
            pretty_printer.text("MapResult(...)")
            return
        pretty_printer.text(repr(self))

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import (
            ERROR_COLOR,
            duration_html,
            html_detail,
            html_kv,
            html_panel,
            plain_reprs,
            status_badge,
            theme_wrap,
            widget_state_key,
        )

        if plain_reprs():
            return None

        n = len(self.results)
        n_completed = sum(1 for r in self.results if r.status == RunStatus.COMPLETED)
        n_failed = sum(1 for r in self.results if r.status == RunStatus.FAILED)
        n_restored = self.restored_count
        kvs = [
            html_kv("Items", str(n)),
            html_kv("Status", status_badge(self.status.value)),
        ]
        if n_completed:
            kvs.append(html_kv("Completed", str(n_completed)))
        if n_failed:
            kvs.append(html_kv("Failed", f'<span style="color:{ERROR_COLOR}">{n_failed}</span>'))
        if n_restored:
            kvs.append(html_kv("Restored", str(n_restored)))
        checkpoint_gap_count = self._checkpoint_gap_count
        if checkpoint_gap_count:
            kvs.append(
                html_kv(
                    "Checkpoint gaps",
                    f'<span style="color:{ERROR_COLOR}">{plural(checkpoint_gap_count, "item")}</span>',
                )
            )
        timed_completed_items = self._timed_completed_items
        if timed_completed_items:
            completed_ms = sum(result.log.total_duration_ms for result in timed_completed_items if result.log is not None)
            avg = completed_ms / len(timed_completed_items)
            kvs.append(html_kv("Avg/item", duration_html(avg)))
        body = " &nbsp;|&nbsp; ".join(kvs)

        # Nested drill-down: each item is expandable to its full RunResult
        items_html = _map_items_drilldown(
            self.results,
            scope_key=widget_state_key("map-result-items", self.run_id or "", self.graph_name, n),
        )
        body += html_detail(f"Per-item breakdown ({plural(n, 'item')})", items_html, state_key="per-item-breakdown")

        return theme_wrap(
            html_panel(f"MapResult: {self.graph_name} ({plural(n, 'item')})", body),
            state_key=widget_state_key("map-result", self.run_id or "", self.graph_name, n),
        )


def _map_items_drilldown(
    results: tuple[RunResult, ...],
    *,
    scope_key: str = "map-items",
) -> str:
    """Render nested drill-down for MapResult items.

    Each item is a collapsible <details> showing a one-line summary.
    Expanding reveals the full RunResult HTML with values, execution log,
    and nested graph traces — clickable all the way down.
    """
    from hypergraph._repr import (
        ERROR_COLOR,
        duration_html,
        html_detail,
        html_filter_paginate_controls,
        html_filter_paginate_script,
        status_badge,
        unique_dom_id,
    )

    total = len(results)
    status_counts: dict[str, int] = {"all": total}
    for result in results:
        status = result.status.value
        status_counts[status] = status_counts.get(status, 0) + 1
        if result.restored:
            status_counts["restored"] = status_counts.get("restored", 0) + 1

    # Intelligent default: moderate batches open at 50, larger ones at 100 to
    # reduce unnecessary paging while keeping the view readable.
    default_page_size = 100 if total > 200 else 50
    dom_scope = unique_dom_id("map-items", scope_key, total)
    filter_id = f"{dom_scope}-filter"
    page_size_id = f"{dom_scope}-page-size"
    prev_id = f"{dom_scope}-prev"
    next_id = f"{dom_scope}-next"
    page_info_id = f"{dom_scope}-page-info"
    list_id = f"{dom_scope}-items"

    parts: list[str] = []
    for i, r in enumerate(results):
        display_status = "restored" if r.restored else r.status.value
        filter_status = f"{r.status.value} restored" if r.restored else r.status.value
        dur = duration_html(r.log.total_duration_ms) if r.log and not r.restored else "—"
        err_label = f' — <span style="color:{ERROR_COLOR}">{type(r.error).__name__}</span>' if r.error else ""
        summary = f"Item {i}: {status_badge(display_status)} {dur}{err_label}"
        # Render the full RunResult HTML inside each item's expandable section.
        item_html = html_detail(summary, r._repr_html_(), state_key=f"item-{i}")
        parts.append(f'<div data-hg-map-item="1" data-status="{filter_status}" style="display:block">{item_html}</div>')

    controls = html_filter_paginate_controls(
        filter_id=filter_id,
        page_size_id=page_size_id,
        prev_id=prev_id,
        next_id=next_id,
        page_info_id=page_info_id,
        counts=status_counts,
        page_size_options=[20, 50, 100],
        default_page_size=default_page_size,
    )
    items_block = f'<div id="{list_id}">{"".join(parts)}</div>'
    script = html_filter_paginate_script(
        list_id=list_id,
        item_selector='[data-hg-map-item="1"]',
        status_attr="data-status",
        filter_id=filter_id,
        page_size_id=page_size_id,
        prev_id=prev_id,
        next_id=next_id,
        page_info_id=page_info_id,
        item_display="block",
    )
    return controls + items_block + script


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
    """

    node_name: str
    input_versions: dict[str, int]
    outputs: dict[str, Any]
    output_versions: dict[str, int] = field(default_factory=dict)
    wait_for_versions: dict[str, int] = field(default_factory=dict)
    duration_ms: float = 0.0
    cached: bool = False


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
        )


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
        status = "cached" if self.cached else self.status
        duration = "—" if self.status == "restored" else _format_duration(self.duration_ms)
        parts = [f"NodeRecord: {self.node_name}", status, duration, f"superstep {self.superstep}"]
        if self.error:
            parts.append(f"error: {self.error[:60]}")
        if self.decision is not None:
            d = ", ".join(self.decision) if isinstance(self.decision, list) else self.decision
            parts.append(f"-> {d}")
        return " | ".join(parts)

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
        parts = []
        if self.succeeded:
            parts.append(f"{self.succeeded} succeeded")
        if self.errors:
            parts.append(plural(self.errors, "error"))
        if self.cached:
            parts.append(f"{self.cached} cached")
        if self.succeeded:
            parts.append(f"avg {_format_duration(self.avg_ms)}")
        return f"NodeStats: {', '.join(parts)}" if parts else "NodeStats: empty"


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
        n_errors = len(self.errors)
        n_restored = sum(1 for step in self.steps if step.status == "restored")
        duration = f"{n_restored} restored" if n_restored else _format_duration(self.total_duration_ms)
        header = f"RunLog: {self.graph_name} | {duration} | {plural(len({s.node_name for s in self.steps}), 'node')} | {plural(n_errors, 'error')}"

        has_decisions = any(s.decision is not None for s in self.steps)
        lines = [header, ""]

        # Column headers
        cols = ["  Step", "Node", "Duration", "Status"]
        if has_decisions:
            cols.append("Decision")
        lines.append("  ".join(f"{c:<16}" for c in cols).rstrip())
        lines.append("  ".join("─" * 16 for _ in cols))

        for i, step in enumerate(self.steps):
            dur = "—" if step.status == "restored" or (step.status == "failed" and step.duration_ms == 0) else _format_duration(step.duration_ms)
            if step.status == "completed":
                status = "completed"
            elif step.status == "paused":
                status = "paused"
            elif step.status == "restored":
                status = "restored"
            else:
                status = f"FAILED: {step.error or 'unknown'}"
            if step.cached:
                status = "cached"
            if step._inner_logs:
                status += f" ({len(step._inner_logs)} inner)"
            row = [f"  {i:>4}", f"{step.node_name:<16}", f"{dur:<16}", status]
            if has_decisions:
                decision_str = ""
                if step.decision is not None:
                    decision_str = "→ " + ", ".join(step.decision) if isinstance(step.decision, list) else f"→ {step.decision}"
                row.append(decision_str)
            lines.append("  ".join(f"{c:<16}" for c in row).rstrip())

        nested = [i for i, s in enumerate(self.steps) if s._inner_logs]
        if nested:
            lines.append("")
            if len(nested) == 1:
                lines.append(f"  → .steps[{nested[0]}].log for inner trace")
            else:
                lines.append(f"  → .steps[i].log for inner traces (i={nested})")

        return "\n".join(lines)

    def __repr__(self) -> str:
        """Concise repr for REPL/debugger."""
        n_restored = sum(1 for step in self.steps if step.status == "restored")
        if n_restored:
            return f"RunLog(graph={self.graph_name!r}, steps={len(self.steps)}, restored={n_restored})"
        return f"RunLog(graph={self.graph_name!r}, steps={len(self.steps)}, duration={_format_duration(self.total_duration_ms)})"

    def _repr_pretty_(self, pretty_printer: Any, cycle: bool) -> None:
        """Show full table in IPython/Jupyter notebooks."""
        if cycle:
            pretty_printer.text("RunLog(...)")
            return
        pretty_printer.text(str(self))

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import (
            _code,
            duration_html,
            html_panel,
            html_table,
            plain_reprs,
            status_badge,
            theme_wrap,
            widget_state_key,
        )

        if plain_reprs():
            return None

        headers = ["Step", "Node", "Status", "Duration"]
        has_decisions = any(s.decision is not None for s in self.steps)
        if has_decisions:
            headers.append("Decision")
        rows = []
        for i, step in enumerate(self.steps):
            status = "cached" if step.cached else step.status
            dur = duration_html(None if step.status == "restored" else step.duration_ms)
            row = [str(i), _code(step.node_name), status_badge(status), dur]
            if has_decisions:
                if step.decision is not None:
                    d = ", ".join(step.decision) if isinstance(step.decision, list) else step.decision
                    row.append(f"&rarr; {d}")
                else:
                    row.append("")
            rows.append(row)
        n_errors = len(self.errors)
        n_restored = sum(1 for step in self.steps if step.status == "restored")
        duration = f"{n_restored} restored" if n_restored else duration_html(self.total_duration_ms)
        title = (
            f"RunLog: {self.graph_name} &nbsp; "
            f"{duration} &nbsp; "
            f"{plural(len({s.node_name for s in self.steps}), 'node')} &nbsp; "
            f"{plural(n_errors, 'error')}"
        )
        body = html_table(headers, rows)
        return theme_wrap(
            html_panel(title, body),
            state_key=widget_state_key("run-log", self.run_id, self.graph_name),
        )


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


# ---------------------------------------------------------------------------
# MapLog — batch-level execution trace
# ---------------------------------------------------------------------------

_MAX_MAP_LOG_ROWS = 20


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
        n_errors = len(self.errors)
        n_succeeded = sum(1 for log in self.items if not log.errors)
        n_restored = self.restored_count
        avg_part = ""
        timed_success_items = self._timed_success_items
        if timed_success_items:
            avg = sum(log.total_duration_ms for log in timed_success_items) / len(timed_success_items)
            avg_part = f" | avg {_format_duration(avg)}/item"
        restored_part = f", {n_restored} restored" if n_restored else ""
        header = (
            f"MapLog: {self.graph_name} | {plural(len(self.items), 'item')} "
            f"({n_succeeded} succeeded{restored_part}) | {plural(n_errors, 'error')}{avg_part}"
        )
        lines = [header, ""]

        cols = ["  Item", "Duration", "Status", "Nodes"]
        lines.append("  ".join(f"{c:<16}" for c in cols).rstrip())
        lines.append("  ".join("─" * 16 for _ in cols))

        display_items = self.items[:_MAX_MAP_LOG_ROWS]
        for i, log in enumerate(display_items):
            restored = self._restored_flags[i]
            dur = "—" if restored else _format_duration(log.total_duration_ms)
            status = "restored" if restored else ("FAILED" if log.errors else "completed")
            n_nodes = len({s.node_name for s in log.steps})
            row = [f"  {i:>4}", f"{dur:<16}", f"{status:<16}", str(n_nodes)]
            lines.append("  ".join(f"{c:<16}" for c in row).rstrip())

        remaining = len(self.items) - len(display_items)
        if remaining > 0:
            lines.append(f"  ... and {plural(remaining, 'more item')}")

        lines.append("")
        lines.append("  → .log[i] for per-item trace")

        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"MapLog(graph={self.graph_name!r}, items={len(self.items)}, restored={self.restored_count}, "
            f"duration={_format_duration(self.total_duration_ms)})"
        )

    def _repr_pretty_(self, pretty_printer: Any, cycle: bool) -> None:
        """Show table in IPython/Jupyter notebooks."""
        if cycle:
            pretty_printer.text("MapLog(...)")
            return
        pretty_printer.text(str(self))

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import (
            BORDER_COLOR,
            MUTED_COLOR,
            SURFACE_COLOR,
            duration_html,
            html_detail,
            html_filter_paginate_controls,
            html_filter_paginate_script,
            html_panel,
            html_table_with_row_attrs,
            plain_reprs,
            status_badge,
            theme_wrap,
            unique_dom_id,
            widget_state_key,
        )

        if plain_reprs():
            return None

        headers = ["Item", "Duration", "Status", "Nodes"]
        rows = []
        row_attrs = []
        trace_items = []
        n_items = len(self.items)
        status_counts: dict[str, int] = {"all": n_items}
        for i, log in enumerate(self.items):
            restored = self._restored_flags[i]
            status = "restored" if restored else ("failed" if log.errors else "completed")
            filter_status = "completed restored" if restored else status
            if not log.errors:
                status_counts["completed"] = status_counts.get("completed", 0) + 1
            else:
                status_counts["failed"] = status_counts.get("failed", 0) + 1
            if restored:
                status_counts["restored"] = status_counts.get("restored", 0) + 1
            n_nodes = len({s.node_name for s in log.steps})
            duration = duration_html(None if restored else log.total_duration_ms)
            rows.append([str(i), duration, status_badge(status), str(n_nodes)])
            row_attrs.append({"data-hg-map-log-item": "1", "data-status": filter_status})

            summary = f'Item {i}: {status_badge(status)} {duration} <span style="color:{MUTED_COLOR}">({plural(n_nodes, "node")})</span>'
            trace_detail = html_detail(summary, log._repr_html_(), state_key=f"map-log-item-{i}")
            trace_items.append(
                '<div data-hg-map-log-item="1" '
                f'data-status="{filter_status}" '
                'style="display:block; margin:0 0 8px 0; padding:6px 8px; '
                f"border:1px solid {BORDER_COLOR}; border-radius:10px; "
                f'background:{SURFACE_COLOR}">'
                f"{trace_detail}</div>"
            )

        avg_part = ""
        timed_success_items = self._timed_success_items
        if timed_success_items:
            avg = sum(log.total_duration_ms for log in timed_success_items) / len(timed_success_items)
            avg_part = f" &nbsp; avg {duration_html(avg)}/item"

        title = f"MapLog: {self.graph_name} ({plural(n_items, 'item')}){avg_part}"
        dom_scope = unique_dom_id("map-log-controls", self.graph_name, n_items, self.total_duration_ms)
        table_id = f"{dom_scope}-table"
        traces_id = f"{dom_scope}-traces"
        filter_id = f"{dom_scope}-filter"
        page_size_id = f"{dom_scope}-page-size"
        prev_id = f"{dom_scope}-prev"
        next_id = f"{dom_scope}-next"
        page_info_id = f"{dom_scope}-page-info"
        default_page_size = 100 if n_items > 200 else 50

        controls = html_filter_paginate_controls(
            filter_id=filter_id,
            page_size_id=page_size_id,
            prev_id=prev_id,
            next_id=next_id,
            page_info_id=page_info_id,
            counts=status_counts,
            page_size_options=[25, 50, 100],
            default_page_size=default_page_size,
        )
        table_html = html_table_with_row_attrs(headers, rows, table_id=table_id, row_attrs=row_attrs)
        traces_block = f'<div id="{traces_id}">{"".join(trace_items)}</div>'
        traces = html_detail("Item Traces", traces_block, state_key="map-log-item-traces")
        table_script = html_filter_paginate_script(
            list_id=table_id,
            item_selector='tbody tr[data-hg-map-log-item="1"]',
            status_attr="data-status",
            filter_id=filter_id,
            page_size_id=page_size_id,
            prev_id=prev_id,
            next_id=next_id,
            page_info_id=page_info_id,
            item_display="table-row",
        )
        traces_script = html_filter_paginate_script(
            list_id=traces_id,
            item_selector='[data-hg-map-log-item="1"]',
            status_attr="data-status",
            filter_id=filter_id,
            page_size_id=page_size_id,
            prev_id=prev_id,
            next_id=next_id,
            page_info_id=page_info_id,
            item_display="block",
        )
        body = controls + table_html + traces + table_script + traces_script

        return theme_wrap(
            html_panel(title, body),
            state_key=widget_state_key("map-log", self.graph_name, len(self.items)),
        )
