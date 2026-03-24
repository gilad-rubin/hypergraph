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
    PAUSED = "paused"


class WorkflowStatus(Enum):
    """Status of a run (kept as WorkflowStatus to avoid collision with runners.RunStatus)."""

    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"
    PARTIAL = "partial"
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
    partial: bool = False

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
        from hypergraph.checkpointers.presenters import render_step_record_html

        return render_step_record_html(self)

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
            "partial": self.partial,
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
    forked_from: str | None = None
    fork_superstep: int | None = None
    retry_of: str | None = None
    retry_index: int | None = None
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
        if self.retry_of:
            retry_num = f"#{self.retry_index}" if self.retry_index is not None else ""
            items.append(f"retry{retry_num} of {self.retry_of}")
        elif self.forked_from:
            at = f"@{self.fork_superstep}" if self.fork_superstep is not None else ""
            items.append(f"fork of {self.forked_from}{at}")
        if items:
            parts.append(", ".join(items))
        return " | ".join(parts)

    def _repr_html_(self) -> str:
        from hypergraph.checkpointers.presenters import render_run_html

        return render_run_html(self)

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
            "forked_from": self.forked_from,
            "fork_superstep": self.fork_superstep,
            "retry_of": self.retry_of,
            "retry_index": self.retry_index,
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
    source_run_id: str | None = None
    source_superstep: int | None = None
    retry_of: str | None = None
    retry_index: int | None = None

    def __repr__(self) -> str:
        origin = ""
        if self.source_run_id:
            at = f"@{self.source_superstep}" if self.source_superstep is not None else ""
            origin = f" from {self.source_run_id}{at}"
        return f"Checkpoint{origin}: {plural(len(self.values), 'value')}, {plural(len(self.steps), 'step')}"

    def _repr_html_(self) -> str:
        from hypergraph.checkpointers.presenters import render_checkpoint_html

        return render_checkpoint_html(self)


class RunTable(list):
    """List[Run] with table display in notebooks and REPL.

    Extends list for full backward compatibility — all list operations
    (len, iter, indexing, slicing) work as expected.

    When created by the checkpointer widget, ``_steps_by_run`` maps
    ``run_id → StepTable`` so each run trace can inline its steps.
    """

    _steps_by_run: dict[str, StepTable]

    def __init__(self, items: Any = (), *, steps_by_run: dict[str, Any] | None = None):
        super().__init__(items)
        self._steps_by_run = steps_by_run or {}

    def __repr__(self) -> str:
        if not self:
            return "RunTable: (empty)"
        lines = [f"RunTable: {plural(len(self), 'run')}", ""]
        for run in self:
            lines.append(f"  {run!r}")
        return "\n".join(lines)

    def _repr_html_(self) -> str:
        from hypergraph.checkpointers.presenters import render_run_table_html

        return render_run_table_html(self)


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
        from hypergraph.checkpointers.presenters import render_step_table_html

        return render_step_table_html(self)


@dataclass(frozen=True)
class LineageRow:
    """One row in a fork lineage tree."""

    lane: str
    run: Run
    depth: int
    is_selected: bool = False


class LineageView(list):
    """Git-like lineage visualization for workflow forks.

    A lineage view is anchored at the root ancestor of a workflow and contains
    all fork descendants in tree order. Each row has a lane prefix similar to
    ``git log --graph`` output.
    """

    def __init__(
        self,
        rows: list[LineageRow],
        *,
        selected_run_id: str,
        root_run_id: str,
        steps_by_run: dict[str, StepTable] | None = None,
    ):
        super().__init__(rows)
        self.selected_run_id = selected_run_id
        self.root_run_id = root_run_id
        self.steps_by_run = steps_by_run or {}

    def __repr__(self) -> str:
        if not self:
            return "LineageView: (empty)"

        lines = [f"LineageView: {self.selected_run_id} (root={self.root_run_id})", ""]
        for row in self:
            run = row.run
            marker = " <selected>" if row.is_selected else ""
            status = run.status.value
            kind = "retry" if run.retry_of else ("fork" if run.forked_from else "root")
            summary = f"{row.lane}{run.id} [{status}] ({kind})"
            if run.forked_from:
                at = f"@{run.fork_superstep}" if run.fork_superstep is not None else ""
                summary += f" <- {run.forked_from}{at}"
            if self.steps_by_run and run.id in self.steps_by_run:
                steps = self.steps_by_run[run.id]
                cached = sum(1 for s in steps if s.cached)
                failed = sum(1 for s in steps if s.status == StepStatus.FAILED)
                summary += f" | steps={len(steps)} cached={cached} failed={failed}"
            lines.append(summary + marker)
        return "\n".join(lines)

    def _repr_html_(self) -> str:
        from hypergraph.checkpointers.presenters import render_lineage_view_html

        return render_lineage_view_html(self)
