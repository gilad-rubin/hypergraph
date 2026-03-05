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
        from hypergraph._repr import ERROR_COLOR, FONT_SANS_STYLE, MUTED_COLOR, duration_html, status_badge, theme_wrap, widget_state_key

        status = "cached" if self.cached else self.status.value
        dur = duration_html(self.duration_ms) if self.duration_ms > 0 else ""
        error = f' <span style="color:{ERROR_COLOR}; font-size:0.85em">{self.error[:80]}</span>' if self.error else ""
        return theme_wrap(
            f'<span style="{FONT_SANS_STYLE}; font-size:0.9em">'
            f"<b>[{self.index}] {self.node_name}</b> {status_badge(status)} {dur}"
            f' <span style="color:{MUTED_COLOR}">superstep {self.superstep}</span>'
            f"{error}</span>",
            state_key=widget_state_key("step-record", self.run_id, self.index, self.node_name, self.superstep),
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
        from hypergraph._repr import (
            ERROR_COLOR,
            MUTED_COLOR,
            _code,
            datetime_html,
            duration_html,
            html_kv,
            html_panel,
            status_badge,
            theme_wrap,
            widget_state_key,
        )

        kvs = [
            html_kv("Status", status_badge(self.status.value)),
            html_kv("Duration", duration_html(self.duration_ms)),
        ]
        if self.node_count:
            kvs.append(html_kv("Steps", str(self.node_count)))
        if self.error_count:
            kvs.append(html_kv("Errors", f'<span style="color:{ERROR_COLOR}; font-weight:600">{self.error_count}</span>'))
        if self.parent_run_id:
            kvs.append(html_kv("Parent", _code(self.parent_run_id)))
        if self.forked_from:
            label = self.forked_from if self.fork_superstep is None else f"{self.forked_from}@{self.fork_superstep}"
            kvs.append(html_kv("Forked From", _code(label)))
        if self.retry_of:
            label = self.retry_of if self.retry_index is None else f"{self.retry_of} (#{self.retry_index})"
            kvs.append(html_kv("Retry Of", _code(label)))
        kvs.append(html_kv("Created", datetime_html(self.created_at)))
        title = f"Run: {self.id}"
        if self.graph_name:
            title += f' <span style="color:{MUTED_COLOR}; font-weight:400">({self.graph_name})</span>'
        body = " &nbsp;|&nbsp; ".join(kvs)
        return theme_wrap(html_panel(title, body), state_key=widget_state_key("run", self.id))

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
        from hypergraph._repr import _code, html_panel, theme_wrap, widget_state_key

        keys = ", ".join(_code(k) for k in sorted(self.values.keys())[:10])
        if len(self.values) > 10:
            keys += f" ... (+{len(self.values) - 10} more)"
        body = f"<b>{len(self.values)}</b> values: {keys}<br><b>{len(self.steps)}</b> steps"
        return theme_wrap(
            html_panel("Checkpoint", body),
            state_key=widget_state_key("checkpoint", self.source_run_id or "ad-hoc", self.source_superstep),
        )


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
        from hypergraph._repr import (
            ERROR_COLOR,
            FONT_SANS_STYLE,
            MUTED_COLOR,
            _code,
            duration_html,
            html_detail,
            html_table,
            html_table_controls,
            html_table_controls_script,
            html_table_with_row_attrs,
            status_badge,
            theme_wrap,
            unique_dom_id,
            widget_state_key,
        )

        if not self:
            return theme_wrap(
                f'<div style="color:{MUTED_COLOR}; {FONT_SANS_STYLE}">RunTable: (empty)</div>',
                state_key=widget_state_key("run-table", "empty"),
            )
        headers = ["ID", "Graph", "Status", "Duration", "Steps", "Errors"]

        def _group_key(run: Run) -> str:
            if run.parent_run_id:
                return run.parent_run_id
            if "/" in run.id:
                return run.id.split("/", 1)[0]
            return run.id

        def _synth_parent(group_id: str, members: list[Run]) -> Run:
            # Synthesize a parent summary when only child rows are present.
            status = WorkflowStatus.COMPLETED
            if any(r.status == WorkflowStatus.ACTIVE for r in members):
                status = WorkflowStatus.ACTIVE
            elif any(r.status == WorkflowStatus.FAILED for r in members):
                status = WorkflowStatus.FAILED
            created = max((r.created_at for r in members), default=_utcnow())
            return Run(
                id=group_id,
                status=status,
                graph_name=members[0].graph_name if members else None,
                duration_ms=max((r.duration_ms or 0.0 for r in members), default=None),
                node_count=sum(r.node_count for r in members),
                error_count=sum(r.error_count for r in members),
                created_at=created,
            )

        by_id = {run.id: run for run in self}
        groups: dict[str, list[Run]] = {}
        for run in self:
            groups.setdefault(_group_key(run), []).append(run)

        row_items: list[tuple[Run, bool, str]] = []
        for group_id, members in groups.items():
            parent = by_id.get(group_id)
            children = sorted(
                [r for r in members if r.id != group_id],
                key=lambda r: r.created_at,
                reverse=True,
            )
            if parent is None:
                parent = _synth_parent(group_id, members)
            row_items.append((parent, False, group_id))
            for child in children:
                row_items.append((child, True, group_id))

        rows: list[list[str]] = []
        row_attrs: list[dict[str, str]] = []
        for run, is_child, _group_id in row_items:
            rows.append(
                [
                    _code(run.id),
                    run.graph_name or "—",
                    status_badge(run.status.value),
                    duration_html(run.duration_ms),
                    str(run.node_count) if run.node_count else "—",
                    f'<span style="color:{ERROR_COLOR}">{run.error_count}</span>' if run.error_count else "0",
                ]
            )
            row_attrs.append(
                {
                    "data-id": run.id,
                    "data-status": run.status.value,
                    "data-parent": "1" if is_child else "0",
                    "data-created-ts": str(run.created_at.timestamp()),
                    "data-duration-ms": str(run.duration_ms or 0.0),
                    "data-errors": str(run.error_count),
                }
            )

        detail_sections: list[str] = []
        for group_id, members in groups.items():
            parent = by_id.get(group_id)
            children = sorted(
                [r for r in members if r.id != group_id],
                key=lambda r: r.created_at,
                reverse=True,
            )
            if parent is None:
                parent = _synth_parent(group_id, members)

            summary = f"{_code(group_id)} — {status_badge(parent.status.value)}"
            if children:
                summary += f' <span style="color:{MUTED_COLOR}">({plural(len(children), "child run")})</span>'

            detail_parts = [parent._repr_html_()]
            if children:
                child_rows = [
                    [
                        _code(c.id),
                        c.graph_name or "—",
                        status_badge(c.status.value),
                        duration_html(c.duration_ms),
                        str(c.node_count) if c.node_count else "—",
                        f'<span style="color:{ERROR_COLOR}">{c.error_count}</span>' if c.error_count else "0",
                    ]
                    for c in children
                ]
                detail_parts.append(html_table(["ID", "Graph", "Status", "Duration", "Steps", "Errors"], child_rows))

            # Inline step history when available
            run_steps = getattr(self, "_steps_by_run", {}).get(group_id)
            if run_steps:
                detail_parts.append(
                    html_detail(
                        f"Steps ({plural(len(run_steps), 'step')})",
                        run_steps._repr_html_(),
                        state_key=f"run-steps-{group_id}",
                    )
                )

            detail_sections.append(html_detail(summary, "<br>".join(detail_parts), state_key=f"run-{group_id}"))

        run_ids = ",".join(run.id for run in self)
        table_id = unique_dom_id("run-table-ui", run_ids, len(row_items))
        view_id = f"{table_id}-view"
        status_id = f"{table_id}-status"
        sort_id = f"{table_id}-sort"
        show_id = f"{table_id}-show"

        controls = html_table_controls(
            view_id=view_id,
            status_id=status_id,
            sort_id=sort_id,
            show_id=show_id,
            show_options=[20, 50, 100],
            total_rows=len(row_items),
        )
        table_html = html_table_with_row_attrs(
            headers,
            rows,
            row_attrs=row_attrs,
            title=plural(len(self), "run"),
            table_id=table_id,
        )
        script = html_table_controls_script(
            table_id=table_id,
            view_id=view_id,
            status_id=status_id,
            sort_id=sort_id,
            show_id=show_id,
        )
        traces = html_detail("Run Traces", "".join(detail_sections), state_key="run-traces")
        body = controls + table_html + traces + script
        return theme_wrap(body, state_key=widget_state_key("run-table", run_ids))


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
        from hypergraph._repr import (
            FONT_SANS_STYLE,
            MUTED_COLOR,
            _code,
            datetime_html,
            duration_html,
            html_detail,
            html_kv,
            html_table,
            status_badge,
            theme_wrap,
            values_html,
            widget_state_key,
        )

        if not self:
            return theme_wrap(
                f'<div style="color:{MUTED_COLOR}; {FONT_SANS_STYLE}">StepTable: (empty)</div>',
                state_key=widget_state_key("step-table", "empty"),
            )
        headers = ["#", "Node", "Status", "Duration", "At", "Superstep"]
        rows = []
        for step in self:
            status = "cached" if step.cached else step.status.value
            rows.append(
                [
                    str(step.index),
                    _code(step.node_name),
                    status_badge(status),
                    duration_html(step.duration_ms) if step.duration_ms > 0 else "—",
                    datetime_html(step.completed_at or step.created_at),
                    str(step.superstep),
                ]
            )
        step_fingerprint = ",".join(f"{step.run_id}:{step.superstep}:{step.node_name}" for step in self)
        body = html_table(headers, rows, title=plural(len(self), "step"))
        for i, step in enumerate(self):
            status = "cached" if step.cached else step.status.value
            meta = " &nbsp;|&nbsp; ".join(
                [
                    html_kv("Node", _code(step.node_name)),
                    html_kv("Superstep", str(step.superstep)),
                    html_kv("Status", status_badge(status)),
                    html_kv("Duration", duration_html(step.duration_ms) if step.duration_ms > 0 else "—"),
                ]
            )
            content = meta
            if step.decision is not None:
                content += "<br>" + html_kv("Decision", _code(str(step.decision)))
            if step.error:
                content += "<br>" + html_kv("Error", _code(step.error))
            if step.values:
                content += "<br><br>" + html_detail("Values", values_html(step.values), state_key=f"values-{i}")
            body += html_detail(f"[{step.index}] {step.node_name}", content, state_key=f"step-{i}")
        return theme_wrap(body, state_key=widget_state_key("step-table", step_fingerprint))


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
        from hypergraph._repr import (
            _code,
            datetime_html,
            html_detail,
            html_kv,
            html_panel,
            html_table,
            status_badge,
            theme_wrap,
            widget_state_key,
        )

        if not self:
            return theme_wrap("<div>LineageView: (empty)</div>", state_key=widget_state_key("lineage", "empty"))

        headers = ["Lane", "Workflow", "Kind", "Status", "Fork Point", "Created", "Steps", "Cached", "Failed"]
        rows: list[list[str]] = []
        for row in self:
            run = row.run
            lane = _code(row.lane.rstrip() or "●")
            workflow = _code(run.id) + (" &nbsp;<b>(selected)</b>" if row.is_selected else "")
            fork_point = "root"
            if run.forked_from:
                at = f"@{run.fork_superstep}" if run.fork_superstep is not None else ""
                fork_point = _code(f"{run.forked_from}{at}")
            kind = "retry" if run.retry_of else ("fork" if run.forked_from else "root")
            n_steps = len(self.steps_by_run.get(run.id, [])) if self.steps_by_run else run.node_count
            cached = 0
            failed = 0
            if self.steps_by_run and run.id in self.steps_by_run:
                cached = sum(1 for s in self.steps_by_run[run.id] if s.cached)
                failed = sum(1 for s in self.steps_by_run[run.id] if s.status == StepStatus.FAILED)
            rows.append(
                [
                    lane,
                    workflow,
                    kind,
                    status_badge(run.status.value),
                    fork_point,
                    datetime_html(run.created_at),
                    str(n_steps) if n_steps else "0",
                    str(cached),
                    str(failed),
                ]
            )

        body = html_table(headers, rows, title=f"Lineage from root {self.root_run_id}")

        if self.steps_by_run:
            for row in self:
                run = row.run
                steps = self.steps_by_run.get(run.id)
                if steps is None:
                    continue
                cached = sum(1 for s in steps if s.cached)
                failed = sum(1 for s in steps if s.status == StepStatus.FAILED)
                kind = "retry" if run.retry_of else ("fork" if run.forked_from else "root")
                meta = " &nbsp;|&nbsp; ".join(
                    [
                        html_kv("Kind", kind),
                        html_kv("Steps", str(len(steps))),
                        html_kv("Cached", str(cached)),
                        html_kv("Failed", str(failed)),
                    ]
                )
                summary = f"{row.lane}{run.id} — {plural(len(steps), 'step')}, {cached} cached, {failed} failed"
                body += html_detail(summary, f"{meta}<br><br>{steps._repr_html_()}", state_key=f"steps-{run.id}")

        return theme_wrap(
            html_panel(f"Workflow Lineage: {self.selected_run_id}", body),
            state_key=widget_state_key("lineage", self.root_run_id, self.selected_run_id),
        )
