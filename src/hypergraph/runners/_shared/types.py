"""Core types for the execution runtime."""

from __future__ import annotations

import uuid
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
    parts = [
        f"{_truncate_text(_safe_repr(k), 80)}: {_compact_value(v, depth + 1, seen)}"
        for k, v in preview_items
    ]
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

    is_recursive_candidate = isinstance(value, (dict, list, tuple, set, frozenset)) or (
        is_dataclass(value) and not isinstance(value, type)
    )
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

    @property
    def completed(self) -> bool:
        """Whether execution completed successfully."""
        return self.status == RunStatus.COMPLETED

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


@dataclass
class NodeExecution:
    """Record of a single node execution.

    Used for tracking and staleness detection in cyclic graphs.

    Attributes:
        node_name: Name of the executed node
        input_versions: Version numbers of inputs at execution time
        outputs: Output values produced
        wait_for_versions: Version numbers of wait_for names at execution time
    """

    node_name: str
    input_versions: dict[str, int]
    outputs: dict[str, Any]
    wait_for_versions: dict[str, int] = field(default_factory=dict)


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
