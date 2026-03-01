"""Checkpointer types for run persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from hypergraph._utils import format_duration_ms, plural


def _utcnow() -> datetime:
    """UTC-aware datetime (avoids deprecated utcnow)."""
    return datetime.now(timezone.utc)


class StepStatus(Enum):
    """Status of a single step execution."""

    COMPLETED = "completed"
    FAILED = "failed"


class WorkflowStatus(Enum):
    """Status of a run (kept as WorkflowStatus to avoid collision with runners.RunStatus)."""

    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class StepRecord:
    """Single atomic record of a node execution.

    Contains both metadata and output values. The checkpointer
    saves each StepRecord atomically — either all data is saved
    or nothing.
    """

    run_id: str
    superstep: int
    node_name: str
    index: int
    status: StepStatus
    input_versions: dict[str, int]
    values: dict[str, Any] | None = None
    duration_ms: float = 0.0
    cached: bool = False
    decision: str | list[str] | None = None
    error: str | None = None
    node_type: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    completed_at: datetime | None = None
    child_run_id: str | None = None

    def __repr__(self) -> str:
        status = "cached" if self.cached else self.status.value
        parts = [f"Step [{self.index}] {self.node_name}", status]
        if self.duration_ms > 0:
            parts.append(format_duration_ms(self.duration_ms))
        parts.append(f"superstep {self.superstep}")
        if self.error:
            parts.append(f"error: {self.error[:60]}")
        return " | ".join(parts)

    def _repr_html_(self) -> str:
        from hypergraph._repr import duration_html, status_badge

        status = "cached" if self.cached else self.status.value
        dur = duration_html(self.duration_ms) if self.duration_ms > 0 else ""
        error = f' <span style="color:#dc2626; font-size:0.85em">{self.error[:80]}</span>' if self.error else ""
        return (
            f'<span style="font-family:ui-monospace,monospace; font-size:0.9em">'
            f"<b>[{self.index}] {self.node_name}</b> {status_badge(status)} {dur}"
            f' <span style="color:#6b7280">superstep {self.superstep}</span>'
            f"{error}</span>"
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict with only primitive types."""
        return {
            "run_id": self.run_id,
            "superstep": self.superstep,
            "node_name": self.node_name,
            "index": self.index,
            "status": self.status.value,
            "input_versions": self.input_versions,
            "values": self.values,
            "duration_ms": self.duration_ms,
            "cached": self.cached,
            "decision": self.decision,
            "error": self.error,
            "node_type": self.node_type,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "child_run_id": self.child_run_id,
        }


@dataclass
class Run:
    """Run metadata record."""

    id: str
    status: WorkflowStatus
    graph_name: str | None = None
    duration_ms: float | None = None
    node_count: int = 0
    error_count: int = 0
    parent_run_id: str | None = None
    config: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=_utcnow)
    completed_at: datetime | None = None

    def __repr__(self) -> str:
        parts = [f"Run: {self.id}"]
        if self.graph_name:
            parts[0] += f" ({self.graph_name})"
        parts.append(self.status.value)
        if self.duration_ms is not None:
            parts.append(format_duration_ms(self.duration_ms))
        items = []
        if self.node_count:
            items.append(plural(self.node_count, "step"))
        if self.error_count:
            items.append(plural(self.error_count, "error"))
        if items:
            parts.append(", ".join(items))
        return " | ".join(parts)

    def _repr_html_(self) -> str:
        from hypergraph._repr import _code, datetime_html, duration_html, html_kv, html_panel, status_badge

        kvs = [
            html_kv("Status", status_badge(self.status.value)),
            html_kv("Duration", duration_html(self.duration_ms)),
        ]
        if self.node_count:
            kvs.append(html_kv("Steps", str(self.node_count)))
        if self.error_count:
            kvs.append(html_kv("Errors", f'<span style="color:#dc2626; font-weight:600">{self.error_count}</span>'))
        if self.parent_run_id:
            kvs.append(html_kv("Parent", _code(self.parent_run_id)))
        kvs.append(html_kv("Created", datetime_html(self.created_at)))
        title = f"Run: {self.id}"
        if self.graph_name:
            title += f' <span style="color:#6b7280; font-weight:400">({self.graph_name})</span>'
        body = " &nbsp;|&nbsp; ".join(kvs)
        return html_panel(title, body)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict."""
        return {
            "id": self.id,
            "status": self.status.value,
            "graph_name": self.graph_name,
            "duration_ms": self.duration_ms,
            "node_count": self.node_count,
            "error_count": self.error_count,
            "parent_run_id": self.parent_run_id,
            "config": self.config,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


@dataclass
class Checkpoint:
    """Point-in-time snapshot for forking runs.

    Combines accumulated state and step history at a given superstep.
    """

    values: dict[str, Any]
    steps: list[StepRecord]

    def __repr__(self) -> str:
        return f"Checkpoint: {plural(len(self.values), 'value')}, {plural(len(self.steps), 'step')}"

    def _repr_html_(self) -> str:
        from hypergraph._repr import _code, html_panel

        keys = ", ".join(_code(k) for k in sorted(self.values.keys())[:10])
        if len(self.values) > 10:
            keys += f" ... (+{len(self.values) - 10} more)"
        body = f"<b>{len(self.values)}</b> values: {keys}<br><b>{len(self.steps)}</b> steps"
        return html_panel("Checkpoint", body)


class RunTable(list):
    """List[Run] with table display in notebooks and REPL.

    Extends list for full backward compatibility — all list operations
    (len, iter, indexing, slicing) work as expected.
    """

    def __repr__(self) -> str:
        if not self:
            return "RunTable: (empty)"
        lines = [f"RunTable: {plural(len(self), 'run')}", ""]
        for run in self:
            lines.append(f"  {run!r}")
        return "\n".join(lines)

    def _repr_html_(self) -> str:
        from hypergraph._repr import _code, duration_html, html_table, status_badge

        if not self:
            return '<div style="color:#6b7280; font-family:ui-monospace,monospace">RunTable: (empty)</div>'
        headers = ["ID", "Graph", "Status", "Duration", "Steps", "Errors"]
        rows = []
        for run in self:
            rows.append(
                [
                    _code(run.id),
                    run.graph_name or "—",
                    status_badge(run.status.value),
                    duration_html(run.duration_ms),
                    str(run.node_count) if run.node_count else "—",
                    f'<span style="color:#dc2626">{run.error_count}</span>' if run.error_count else "0",
                ]
            )
        return html_table(headers, rows, title=plural(len(self), "run"))


class StepTable(list):
    """List[StepRecord] with table display in notebooks and REPL.

    Extends list for full backward compatibility.
    """

    def __repr__(self) -> str:
        if not self:
            return "StepTable: (empty)"
        lines = [f"StepTable: {plural(len(self), 'step')}", ""]
        for step in self:
            lines.append(f"  {step!r}")
        return "\n".join(lines)

    def _repr_html_(self) -> str:
        from hypergraph._repr import _code, duration_html, html_table, status_badge

        if not self:
            return '<div style="color:#6b7280; font-family:ui-monospace,monospace">StepTable: (empty)</div>'
        headers = ["#", "Node", "Status", "Duration", "Superstep"]
        rows = []
        for step in self:
            status = "cached" if step.cached else step.status.value
            rows.append(
                [
                    str(step.index),
                    _code(step.node_name),
                    status_badge(status),
                    duration_html(step.duration_ms) if step.duration_ms > 0 else "—",
                    str(step.superstep),
                ]
            )
        return html_table(headers, rows, title=plural(len(self), "step"))
