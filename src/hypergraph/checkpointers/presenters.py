"""Presentation helpers for checkpointer notebook/HTML displays."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from importlib.resources import files
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hypergraph.checkpointers.types import Run, StepRecord, WorkflowStatus


def _safe_json_payload(payload: dict[str, Any]) -> str:
    """Serialize JSON safely for embedding inside a script tag."""
    return json.dumps(payload).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


_EXPLORER_ASSET_SHA256 = "9909428d38a1ec32738c16eb54d8a72f1f4bb6a38b03858709b86286c35b413d"


def _read_explorer_asset() -> str:
    """Read and validate the exact packaged offline explorer bytes."""
    source = files("hypergraph.checkpointers._assets").joinpath("explorer.js").read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    if digest != _EXPLORER_ASSET_SHA256:
        raise RuntimeError(
            "The packaged checkpointer explorer asset is corrupt.\n\n"
            "The complete explorer.js content does not match this Hypergraph build.\n\n"
            "How to fix: Reinstall hypergraph from a complete wheel."
        )
    return source.decode("utf-8")


def _aggregate_workflow_status(
    statuses: Iterable[WorkflowStatus],
) -> WorkflowStatus:
    """Return the canonical aggregate status for synthetic parent runs."""
    from hypergraph.checkpointers.types import WorkflowStatus

    distinct = frozenset(statuses)
    if WorkflowStatus.ACTIVE in distinct:
        return WorkflowStatus.ACTIVE
    if WorkflowStatus.PAUSED in distinct:
        return WorkflowStatus.PAUSED
    if WorkflowStatus.PARTIAL in distinct or (WorkflowStatus.FAILED in distinct and len(distinct) > 1):
        return WorkflowStatus.PARTIAL
    if WorkflowStatus.FAILED in distinct:
        return WorkflowStatus.FAILED
    if WorkflowStatus.STOPPED in distinct:
        return WorkflowStatus.STOPPED
    return WorkflowStatus.COMPLETED


def render_checkpointer_explorer_html(
    *,
    title: str,
    path: str,
    state_key: str,
    run_count: int | None = None,
    step_count: int | None = None,
    size_bytes: int | None = None,
    runs: list[Run] | None = None,
    steps_by_run: dict[str, list[StepRecord]] | None = None,
    run_limit: int | None = None,
) -> str:
    """Render a drill-through explorer for a checkpointer."""
    from hypergraph._repr import (
        ALIGNUI_WIDGET_THEME,
        BORDER_COLOR,
        FONT_MONO_STYLE,
        MUTED_COLOR,
        PANEL_COLOR,
        STATUS_PALETTE,
        TEXT_STRONG_COLOR,
        _code,
        html_kv,
        html_panel,
        theme_wrap,
        unique_dom_id,
    )

    runs = runs or []
    steps_by_run = steps_by_run or {}

    kvs = [html_kv("Path", _code(path))]
    if size_bytes is not None:
        size_mb = size_bytes / (1024 * 1024)
        kvs.append(html_kv("Size", f"{size_mb:.1f} MB" if size_mb >= 1 else f"{size_bytes / 1024:.0f} KB"))
    if run_count is not None:
        kvs.append(html_kv("Runs", str(run_count)))
    if step_count is not None:
        kvs.append(html_kv("Steps", str(step_count)))
    summary = " &nbsp;|&nbsp; ".join(kvs)

    payload = {
        "runs": [run.to_dict() for run in runs],
        "steps_by_run": {run_id: [step.to_dict() for step in run_steps] for run_id, run_steps in steps_by_run.items()},
        "initial_run_id": runs[0].id if runs else None,
        "run_limit": run_limit,
    }
    config = {
        "status_palette": STATUS_PALETTE,
        "default_status_colors": (
            ALIGNUI_WIDGET_THEME["text_soft"],
            ALIGNUI_WIDGET_THEME["bg_soft"],
        ),
    }

    explorer_id = unique_dom_id("checkpointer-explorer", path)
    panel_style = f"border:1px solid {BORDER_COLOR}; border-radius:10px; background:{PANEL_COLOR}; padding:12px; min-height:120px"
    mono = FONT_MONO_STYLE

    controls_note = "Select a run on the left, then drill into overview, steps, and lineage from one place."
    if run_limit is not None and run_count is not None and run_count > run_limit:
        controls_note += f" Showing the newest {run_limit} runs in the explorer."

    container = (
        f'<div id="{explorer_id}" data-hg-explorer="checkpointer" '
        f'style="display:flex; flex-direction:column; gap:12px">'
        f"{html_panel(title, summary)}"
        f'<div style="color:{MUTED_COLOR}; font-size:0.9em">{controls_note}</div>'
        f'<div style="display:grid; grid-template-columns:minmax(240px, 300px) minmax(420px, 1fr); gap:12px; align-items:start">'
        f'<section style="{panel_style}">'
        f'<div style="display:flex; justify-content:space-between; gap:8px; align-items:center; margin-bottom:8px">'
        f'<div data-hg-panel-title="Run Explorer" style="{mono}; font-weight:700; color:{TEXT_STRONG_COLOR}">Run Explorer</div>'
        f'<div style="color:{MUTED_COLOR}; font-size:0.85em">Explore: {_code(".runs()")} {_code(".steps(run_id)")} {_code(".search(query)")} {_code(".stats(run_id)")}</div>'
        f"</div>"
        '<div data-hg-explorer-runs style="display:flex; flex-direction:column; gap:8px"></div>'
        f'<div data-hg-explorer-empty style="display:none; color:{MUTED_COLOR}; {mono}">No runs available.</div>'
        f"</section>"
        f'<section style="display:flex; flex-direction:column; gap:12px">'
        f'<div data-hg-explorer-header style="{panel_style}"></div>'
        '<div data-hg-explorer-summary style="display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:8px"></div>'
        '<div data-hg-explorer-nav style="display:flex; gap:8px; flex-wrap:wrap"></div>'
        f'<div data-hg-explorer-body style="{panel_style}"></div>'
        f"</section>"
        f"</div>"
        f'<script type="application/json" data-hg-explorer-data>{_safe_json_payload(payload)}</script>'
        f'<script type="application/json" data-hg-explorer-config>{_safe_json_payload(config)}</script>'
        f"<script>{_read_explorer_asset()}</script>"
        f"</div>"
    )
    return theme_wrap(container, state_key=state_key)


def render_step_record_html(step: Any) -> str:
    """Render notebook HTML for a single StepRecord."""
    from hypergraph._repr import ERROR_COLOR, FONT_SANS_STYLE, MUTED_COLOR, duration_html, status_badge, theme_wrap, widget_state_key

    status = "cached" if step.cached else step.status.value
    dur = duration_html(step.duration_ms) if step.duration_ms > 0 else ""
    error = f' <span style="color:{ERROR_COLOR}; font-size:0.85em">{step.error[:80]}</span>' if step.error else ""
    return theme_wrap(
        f'<span style="{FONT_SANS_STYLE}; font-size:0.9em">'
        f"<b>[{step.index}] {step.node_name}</b> {status_badge(status)} {dur}"
        f' <span style="color:{MUTED_COLOR}">superstep {step.superstep}</span>'
        f"{error}</span>",
        state_key=widget_state_key("step-record", step.run_id, step.index, step.node_name, step.superstep),
    )


def render_run_html(run: Any) -> str:
    """Render notebook HTML for a single Run."""
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
        html_kv("Status", status_badge(run.status.value)),
        html_kv("Duration", duration_html(run.duration_ms)),
    ]
    if run.node_count:
        kvs.append(html_kv("Steps", str(run.node_count)))
    if run.error_count:
        kvs.append(html_kv("Errors", f'<span style="color:{ERROR_COLOR}; font-weight:600">{run.error_count}</span>'))
    if run.parent_run_id:
        kvs.append(html_kv("Parent", _code(run.parent_run_id)))
    if run.forked_from:
        label = run.forked_from if run.fork_superstep is None else f"{run.forked_from}@{run.fork_superstep}"
        kvs.append(html_kv("Forked From", _code(label)))
    if run.retry_of:
        label = run.retry_of if run.retry_index is None else f"{run.retry_of} (#{run.retry_index})"
        kvs.append(html_kv("Retry Of", _code(label)))
    kvs.append(html_kv("Created", datetime_html(run.created_at)))
    title = f"Run: {run.id}"
    if run.graph_name:
        title += f' <span style="color:{MUTED_COLOR}; font-weight:400">({run.graph_name})</span>'
    body = " &nbsp;|&nbsp; ".join(kvs)
    return theme_wrap(html_panel(title, body), state_key=widget_state_key("run", run.id))


def render_checkpoint_html(checkpoint: Any) -> str:
    """Render notebook HTML for a Checkpoint."""
    from hypergraph._repr import _code, html_panel, theme_wrap, widget_state_key

    keys = ", ".join(_code(k) for k in sorted(checkpoint.values.keys())[:10])
    if len(checkpoint.values) > 10:
        keys += f" ... (+{len(checkpoint.values) - 10} more)"
    body = f"<b>{len(checkpoint.values)}</b> values: {keys}<br><b>{len(checkpoint.steps)}</b> steps"
    return theme_wrap(
        html_panel("Checkpoint", body),
        state_key=widget_state_key("checkpoint", checkpoint.source_run_id or "ad-hoc", checkpoint.source_superstep),
    )


def render_run_table_html(table: Any) -> str:
    """Render notebook HTML for a RunTable."""
    from datetime import datetime, timezone

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

    if not table:
        return theme_wrap(
            f'<div style="color:{MUTED_COLOR}; {FONT_SANS_STYLE}">RunTable: (empty)</div>',
            state_key=widget_state_key("run-table", "empty"),
        )

    def _utcnow():
        return datetime.now(timezone.utc)

    headers = ["ID", "Graph", "Status", "Duration", "Steps", "Errors"]

    def _group_key(run: Any) -> str:
        if run.parent_run_id:
            return run.parent_run_id
        if "/" in run.id:
            return run.id.split("/", 1)[0]
        return run.id

    def _synth_parent(group_id: str, members: list[Any]) -> Any:
        from hypergraph.checkpointers.types import Run

        statuses = {r.status for r in members}
        status = _aggregate_workflow_status(statuses)
        created = max((r.created_at for r in members), default=_utcnow())
        return Run(
            id=group_id,
            status=status,
            graph_name=members[0].graph_name if members else None,
            duration_ms=max((r.duration_ms or 0.0 for r in members), default=0.0) if members else None,
            node_count=sum(r.node_count for r in members),
            error_count=sum(r.error_count for r in members),
            created_at=created,
        )

    by_id = {run.id: run for run in table}
    groups: dict[str, list[Any]] = {}
    for run in table:
        groups.setdefault(_group_key(run), []).append(run)

    row_items: list[tuple[Any, bool, str]] = []
    for group_id, members in groups.items():
        parent = by_id.get(group_id)
        children = sorted([r for r in members if r.id != group_id], key=lambda r: r.created_at, reverse=True)
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
        children = sorted([r for r in members if r.id != group_id], key=lambda r: r.created_at, reverse=True)
        if parent is None:
            parent = _synth_parent(group_id, members)

        summary = f"{_code(group_id)} — {status_badge(parent.status.value)}"
        if children:
            summary += f' <span style="color:{MUTED_COLOR}">({len(children)} child run{"s" if len(children) != 1 else ""})</span>'

        detail_parts = [render_run_html(parent)]
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

        run_steps = getattr(table, "_steps_by_run", {}).get(group_id)
        if run_steps:
            detail_parts.append(
                html_detail(
                    f"Steps ({len(run_steps)} step{'s' if len(run_steps) != 1 else ''})",
                    render_step_table_html(run_steps),
                    state_key=f"run-steps-{group_id}",
                )
            )

        detail_sections.append(html_detail(summary, "<br>".join(detail_parts), state_key=f"run-{group_id}"))

    run_ids = ",".join(run.id for run in table)
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
        headers, rows, row_attrs=row_attrs, title=f"{len(table)} run{'s' if len(table) != 1 else ''}", table_id=table_id
    )
    script = html_table_controls_script(table_id=table_id, view_id=view_id, status_id=status_id, sort_id=sort_id, show_id=show_id)
    traces = html_detail("Run Traces", "".join(detail_sections), state_key="run-traces")
    body = controls + table_html + traces + script
    return theme_wrap(body, state_key=widget_state_key("run-table", run_ids))


def render_step_table_html(table: Any) -> str:
    """Render notebook HTML for a StepTable."""
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

    if not table:
        return theme_wrap(
            f'<div style="color:{MUTED_COLOR}; {FONT_SANS_STYLE}">StepTable: (empty)</div>',
            state_key=widget_state_key("step-table", "empty"),
        )
    headers = ["#", "Node", "Status", "Duration", "At", "Superstep"]
    rows = []
    for step in table:
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
    step_fingerprint = ",".join(f"{step.run_id}:{step.superstep}:{step.node_name}" for step in table)
    body = html_table(headers, rows, title=f"{len(table)} step{'s' if len(table) != 1 else ''}")
    for i, step in enumerate(table):
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


def render_lineage_view_html(view: Any) -> str:
    """Render notebook HTML for a LineageView."""
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

    if not view:
        return theme_wrap("<div>LineageView: (empty)</div>", state_key=widget_state_key("lineage", "empty"))

    headers = ["Lane", "Workflow", "Kind", "Status", "Fork Point", "Created", "Steps", "Cached", "Failed"]
    rows: list[list[str]] = []
    for row in view:
        run = row.run
        lane = _code(row.lane.rstrip() or "●")
        workflow = _code(run.id) + (" &nbsp;<b>(selected)</b>" if row.is_selected else "")
        fork_point = "root"
        if run.forked_from:
            at = f"@{run.fork_superstep}" if run.fork_superstep is not None else ""
            fork_point = _code(f"{run.forked_from}{at}")
        kind = "retry" if run.retry_of else ("fork" if run.forked_from else "root")
        n_steps = len(view.steps_by_run.get(run.id, [])) if view.steps_by_run else run.node_count
        cached = 0
        failed = 0
        if view.steps_by_run and run.id in view.steps_by_run:
            cached = sum(1 for s in view.steps_by_run[run.id] if s.cached)
            failed = sum(1 for s in view.steps_by_run[run.id] if s.status.value == "failed")
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

    body = html_table(headers, rows, title=f"Lineage from root {view.root_run_id}")
    if view.steps_by_run:
        for row in view:
            run = row.run
            steps = view.steps_by_run.get(run.id)
            if steps is None:
                continue
            cached = sum(1 for s in steps if s.cached)
            failed = sum(1 for s in steps if s.status.value == "failed")
            kind = "retry" if run.retry_of else ("fork" if run.forked_from else "root")
            meta = " &nbsp;|&nbsp; ".join(
                [
                    html_kv("Kind", kind),
                    html_kv("Steps", str(len(steps))),
                    html_kv("Cached", str(cached)),
                    html_kv("Failed", str(failed)),
                ]
            )
            summary = f"{row.lane}{run.id} — {len(steps)} step{'s' if len(steps) != 1 else ''}, {cached} cached, {failed} failed"
            body += html_detail(summary, f"{meta}<br><br>{render_step_table_html(steps)}", state_key=f"steps-{run.id}")
    return theme_wrap(
        html_panel(f"Workflow Lineage: {view.selected_run_id}", body), state_key=widget_state_key("lineage", view.root_run_id, view.selected_run_id)
    )
