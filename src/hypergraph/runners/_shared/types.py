"""Core types for the execution runtime."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any, Literal

ErrorHandling = Literal["raise", "continue"]

_MAX_STRING_PREVIEW = 120
_MAX_SEQUENCE_PREVIEW = 6
_MAX_MAPPING_PREVIEW = 6
_MAX_VALUE_REPR = 240
_MAX_RUN_RESULT_REPR = 4_000


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
        status: Run status (COMPLETED, FAILED, or PAUSED)
        run_id: Unique identifier for this run
        workflow_id: Optional workflow identifier for tracking related runs
        error: Exception if status is FAILED, else None
        pause: PauseInfo if status is PAUSED, else None
        log: RunLog with execution trace (timing, status, routing), or None
    """

    values: dict[str, Any]
    status: RunStatus
    run_id: str = field(default_factory=_generate_run_id)
    workflow_id: str | None = None
    error: BaseException | None = None
    pause: PauseInfo | None = None
    log: RunLog | None = None

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
        if self.log:
            return self.log.summary()
        if self.error:
            return f"{self.status.value}: {type(self.error).__name__}: {self.error}"
        return self.status.value

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict with status, run_id, and log.

        Does NOT include raw values or error objects — only metadata.
        Use result.values directly for output access.
        """
        d: dict[str, Any] = {
            "status": self.status.value,
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
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
        text = (
            "RunResult("
            f"status={self.status.value}, "
            f"values={_compact_value(self.values)}, "
            f"run_id={self.run_id!r}, "
            f"workflow_id={self.workflow_id!r}, "
            f"error={_compact_value(self.error)}, "
            f"pause={_compact_value(self.pause)}"
            ")"
        )
        return _truncate_text(text, _MAX_RUN_RESULT_REPR)

    def _repr_pretty_(self, pretty_printer: Any, cycle: bool) -> None:
        """Use the compact repr for IPython pretty display."""
        if cycle:
            pretty_printer.text("RunResult(...)")
            return
        pretty_printer.text(repr(self))


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
        """Precedence: FAILED > PAUSED > COMPLETED.
        Empty → COMPLETED (vacuous truth, same as empty batch)."""
        if any(r.status == RunStatus.FAILED for r in self.results):
            return RunStatus.FAILED
        if any(r.status == RunStatus.PAUSED for r in self.results):
            return RunStatus.PAUSED
        return RunStatus.COMPLETED

    @property
    def completed(self) -> bool:
        """True if all items completed (or empty)."""
        return self.status == RunStatus.COMPLETED

    @property
    def paused(self) -> bool:
        """True if any item is paused (and none failed)."""
        return self.status == RunStatus.PAUSED

    @property
    def failed(self) -> bool:
        """True if any item failed."""
        return self.status == RunStatus.FAILED

    @property
    def failures(self) -> list[RunResult]:
        """Only failed items."""
        return [r for r in self.results if r.status == RunStatus.FAILED]

    def get(self, key: str, default: Any = None) -> list[Any]:
        """Collect values across items with a default.
        results.get("doubled", 0) → [2, 4, 0, 6, 8]"""
        return [r.get(key, default) for r in self.results]

    # --- Progressive disclosure ---

    def summary(self) -> str:
        """One-liner: '5 items | 4 completed, 1 failed | 12ms'"""
        n = len(self.results)
        n_completed = sum(1 for r in self.results if r.status == RunStatus.COMPLETED)
        n_failed = sum(1 for r in self.results if r.status == RunStatus.FAILED)
        n_paused = sum(1 for r in self.results if r.status == RunStatus.PAUSED)
        parts = [f"{n} items"]
        status_parts = []
        if n_completed:
            status_parts.append(f"{n_completed} completed")
        if n_failed:
            status_parts.append(f"{n_failed} failed")
        if n_paused:
            status_parts.append(f"{n_paused} paused")
        if status_parts:
            parts.append(", ".join(status_parts))
        parts.append(_format_duration(self.total_duration_ms))
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
            "item_count": len(self.results),
            "completed_count": n_completed,
            "failed_count": n_failed,
            "items": [item.to_dict() for item in self.results],
        }

    def __repr__(self) -> str:
        n = len(self.results)
        n_completed = sum(1 for r in self.results if r.status == RunStatus.COMPLETED)
        n_failed = sum(1 for r in self.results if r.status == RunStatus.FAILED)
        parts = []
        if n_completed:
            parts.append(f"{n_completed} completed")
        if n_failed:
            parts.append(f"{n_failed} failed")
        n_paused = n - n_completed - n_failed
        if n_paused:
            parts.append(f"{n_paused} paused")
        status = ", ".join(parts) if parts else "empty"
        return f"MapResult({n} items: {status}, {_format_duration(self.total_duration_ms)}, map_over={self.map_over!r})"

    def _repr_pretty_(self, pretty_printer: Any, cycle: bool) -> None:
        if cycle:
            pretty_printer.text("MapResult(...)")
            return
        pretty_printer.text(repr(self))


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

        Top-level: returns output_param directly (e.g., 'decision').
        Nested: dot-separated path (e.g., 'review.decision').
        """
        parts = self.node_name.split("/")
        if len(parts) == 1:
            return self.output_param
        return ".".join(parts[:-1]) + "." + self.output_param

    @property
    def response_keys(self) -> dict[str, str]:
        """Map output names to resume keys.

        Returns a dict mapping each output parameter name to the key to use
        when providing the response value in the input dict.
        """
        params = self.output_params or (self.output_param,)
        parts = self.node_name.split("/")
        prefix = ".".join(parts[:-1]) + "." if len(parts) > 1 else ""
        return {p: prefix + p for p in params}


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
    supports_checkpointing: bool = False


@dataclass
class NodeExecution:
    """Record of a single node execution.

    Used for tracking and staleness detection in cyclic graphs.

    Attributes:
        node_name: Name of the executed node
        input_versions: Version numbers of inputs at execution time
        outputs: Output values produced
        wait_for_versions: Version numbers of wait_for names at execution time
        duration_ms: Wall-clock execution time in milliseconds
        cached: Whether this execution was a cache hit
    """

    node_name: str
    input_versions: dict[str, int]
    outputs: dict[str, Any]
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
                    outputs=dict(v.outputs),
                    wait_for_versions=dict(v.wait_for_versions),
                )
                for k, v in self.node_executions.items()
            },
            routing_decisions=dict(self.routing_decisions),
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
        status: "completed" or "failed".
        span_id: Correlates with OTel traces.
        error: Error message if status is "failed".
        cached: Whether this was a cache hit.
        decision: Gate routing decision, if this was a gate node.
    """

    node_name: str
    superstep: int
    duration_ms: float
    status: Literal["completed", "failed"]
    span_id: str
    error: str | None = None
    cached: bool = False
    decision: str | list[str] | None = None


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
    def avg_ms(self) -> float:
        """Average execution time in milliseconds."""
        return self.total_ms / self.count if self.count > 0 else 0.0


def _format_duration(ms: float) -> str:
    """Format milliseconds into human-readable duration."""
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    minutes = int(ms // 60_000)
    seconds = (ms % 60_000) / 1000
    return f"{minutes}m{seconds:04.1f}s"


def _compute_node_stats(steps: tuple[NodeRecord, ...]) -> dict[str, NodeStats]:
    """Aggregate per-node stats from step records."""
    accumulators: dict[str, dict[str, Any]] = {}
    for step in steps:
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
        slowest = max(self.timing.items(), key=lambda x: x[1]) if self.timing else ("", 0)
        parts = [
            f"{n_nodes} nodes",
            _format_duration(self.total_duration_ms),
            f"{n_errors} errors" if n_errors else "0 errors",
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
        header = (
            f"RunLog: {self.graph_name} | "
            f"{_format_duration(self.total_duration_ms)} | "
            f"{len({s.node_name for s in self.steps})} nodes | "
            f"{n_errors} error{'s' if n_errors != 1 else ''}"
        )

        has_decisions = any(s.decision is not None for s in self.steps)
        lines = [header, ""]

        # Column headers
        cols = ["  Step", "Node", "Duration", "Status"]
        if has_decisions:
            cols.append("Decision")
        lines.append("  ".join(f"{c:<16}" for c in cols).rstrip())
        lines.append("  ".join("─" * 16 for _ in cols))

        for i, step in enumerate(self.steps):
            dur = _format_duration(step.duration_ms) if step.status != "failed" or step.duration_ms > 0 else "—"
            status = "completed" if step.status == "completed" else f"FAILED: {step.error or 'unknown'}"
            if step.cached:
                status = "cached"
            row = [f"  {i:>4}", f"{step.node_name:<16}", f"{dur:<16}", status]
            if has_decisions:
                decision_str = ""
                if step.decision is not None:
                    decision_str = "→ " + ", ".join(step.decision) if isinstance(step.decision, list) else f"→ {step.decision}"
                row.append(decision_str)
            lines.append("  ".join(f"{c:<16}" for c in row).rstrip())

        return "\n".join(lines)

    def __repr__(self) -> str:
        """Concise repr for REPL/debugger."""
        return f"RunLog(graph={self.graph_name!r}, steps={len(self.steps)}, duration={_format_duration(self.total_duration_ms)})"
