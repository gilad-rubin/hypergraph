"""Presentation helpers for runner result and log types."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import TYPE_CHECKING, Any

from hypergraph._repr import (
    BORDER_COLOR,
    ERROR_COLOR,
    MUTED_COLOR,
    SURFACE_COLOR,
    _code,
    duration_html,
    error_html,
    html_detail,
    html_filter_paginate_controls,
    html_filter_paginate_script,
    html_kv,
    html_panel,
    html_table,
    html_table_with_row_attrs,
    status_badge,
    theme_wrap,
    unique_dom_id,
    values_html,
    widget_state_key,
)
from hypergraph._utils import format_duration_ms, plural

if TYPE_CHECKING:
    from hypergraph.runners._shared.results import (
        MapLog,
        MapResult,
        NodeRecord,
        NodeStats,
        RunLog,
        RunResult,
    )

_MAX_STRING_PREVIEW = 120
_MAX_SEQUENCE_PREVIEW = 6
_MAX_MAPPING_PREVIEW = 6
_MAX_VALUE_REPR = 240
_MAX_RUN_RESULT_REPR = 4_000
_MAX_MAP_LOG_ROWS = 20


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


def render_run_result_repr(result: RunResult) -> str:
    """Render a compact RunResult representation."""
    checkpoint = ""
    if not result.checkpoint_ok:
        checkpoint = f", checkpoint_ok=False, checkpoint_errors={_compact_value(result.checkpoint_errors)}"
    text = (
        "RunResult("
        f"status={result.status.value}, "
        f"values={_compact_value(result.values)}, "
        f"run_id={result.run_id!r}, "
        f"workflow_id={result.workflow_id!r}, "
        f"restored={result.restored}, "
        f"error={_compact_value(result.error)}, "
        f"pause={_compact_value(result.pause)}"
        f"{checkpoint}"
        ")"
    )
    return _truncate_text(text, _MAX_RUN_RESULT_REPR)


def render_run_result_pretty(result: RunResult, pretty_printer: Any, cycle: bool) -> None:
    """Render a RunResult through IPython's pretty protocol."""
    if cycle:
        pretty_printer.text("RunResult(...)")
        return
    pretty_printer.text(repr(result))


def render_run_result_html(result: RunResult) -> str:
    """Render a RunResult as rich notebook HTML."""
    kvs = [html_kv("Status", status_badge(result.status.value))]
    if result.restored:
        kvs.append(html_kv("Restored", status_badge("restored")))
    if result.log and not result.restored:
        kvs.append(html_kv("Duration", duration_html(result.log.total_duration_ms)))
        kvs.append(html_kv("Nodes", str(len({s.node_name for s in result.log.steps}))))
        n_errors = len(result.log.errors)
        if n_errors:
            kvs.append(html_kv("Errors", f'<span style="color:{ERROR_COLOR}">{n_errors}</span>'))
    if not result.checkpoint_ok:
        error_count = len(result.checkpoint_errors)
        detail = f" ({plural(error_count, 'save error')})" if error_count else ""
        kvs.append(html_kv("Checkpoint", f'<span style="color:{ERROR_COLOR}">gap{detail}</span>'))
    kvs.append(html_kv("Values", plural(len(result.values), "key")))
    body = " &nbsp;|&nbsp; ".join(kvs)
    if result.error:
        body += error_html(result.error)
    if result.values:
        body += html_detail(
            f"Values ({plural(len(result.values), 'key')})",
            values_html(result.values),
            state_key="values",
        )
    if result.checkpoint_errors:
        body += html_detail(
            f"Checkpoint errors ({plural(len(result.checkpoint_errors), 'save error')})",
            values_html({str(index): error for index, error in enumerate(result.checkpoint_errors)}),
            state_key="checkpoint-errors",
        )
    if result.log:
        body += html_detail("Run Log", result.log._repr_html_(), state_key="run-log")  # type: ignore[arg-type]
    return theme_wrap(
        html_panel(f"RunResult: {result.run_id}", body),
        state_key=widget_state_key("run-result", result.workflow_id or "", result.run_id),
    )


def render_map_result_repr(result: MapResult) -> str:
    """Render a concise MapResult representation."""
    from hypergraph.runners._shared.results import RunStatus

    n = len(result.results)
    n_completed = sum(1 for item in result.results if item.status == RunStatus.COMPLETED)
    n_failed = sum(1 for item in result.results if item.status == RunStatus.FAILED)
    n_paused = sum(1 for item in result.results if item.status == RunStatus.PAUSED)
    n_stopped = sum(1 for item in result.results if item.status == RunStatus.STOPPED)
    n_restored = result.restored_count
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
    checkpoint_gap_count = result._checkpoint_gap_count
    checkpoint_part = f", {plural(checkpoint_gap_count, 'item')} with checkpoint gaps" if checkpoint_gap_count else ""
    avg_part = ""
    timed_completed_items = result._timed_completed_items
    if timed_completed_items:
        completed_ms = sum(item.log.total_duration_ms for item in timed_completed_items if item.log is not None)
        avg = completed_ms / len(timed_completed_items)
        avg_part = f", avg {format_duration_ms(avg)}/item"
    if result.unstarted_item_indexes:
        scope = f"{n} of {plural(result.requested_count, 'item')} settled, {plural(len(result.unstarted_item_indexes), 'unstarted item')}"
    else:
        scope = plural(n, "item")
    return f"MapResult({scope}: {status}{checkpoint_part}{avg_part}, map_over={result.map_over!r})"


def render_map_result_pretty(result: MapResult, pretty_printer: Any, cycle: bool) -> None:
    """Render a MapResult through IPython's pretty protocol."""
    if cycle:
        pretty_printer.text("MapResult(...)")
        return
    pretty_printer.text(repr(result))


def render_map_result_html(result: MapResult) -> str:
    """Render a MapResult as rich notebook HTML."""
    from hypergraph.runners._shared.results import RunStatus

    n = len(result.results)
    n_completed = sum(1 for item in result.results if item.status == RunStatus.COMPLETED)
    n_failed = sum(1 for item in result.results if item.status == RunStatus.FAILED)
    n_restored = result.restored_count
    if result.unstarted_item_indexes:
        kvs = [
            html_kv("Settled", str(n)),
            html_kv("Requested", str(result.requested_count)),
            html_kv("Unstarted", str(len(result.unstarted_item_indexes))),
            html_kv("Status", status_badge(result.status.value)),
        ]
        scope = f"{n} of {plural(result.requested_count, 'item')} settled, {plural(len(result.unstarted_item_indexes), 'unstarted item')}"
    else:
        kvs = [
            html_kv("Items", str(n)),
            html_kv("Status", status_badge(result.status.value)),
        ]
        scope = plural(n, "item")
    if n_completed:
        kvs.append(html_kv("Completed", str(n_completed)))
    if n_failed:
        kvs.append(html_kv("Failed", f'<span style="color:{ERROR_COLOR}">{n_failed}</span>'))
    if n_restored:
        kvs.append(html_kv("Restored", str(n_restored)))
    checkpoint_gap_count = result._checkpoint_gap_count
    if checkpoint_gap_count:
        kvs.append(
            html_kv(
                "Checkpoint gaps",
                f'<span style="color:{ERROR_COLOR}">{plural(checkpoint_gap_count, "item")}</span>',
            )
        )
    timed_completed_items = result._timed_completed_items
    if timed_completed_items:
        completed_ms = sum(item.log.total_duration_ms for item in timed_completed_items if item.log is not None)
        avg = completed_ms / len(timed_completed_items)
        kvs.append(html_kv("Avg/item", duration_html(avg)))
    body = " &nbsp;|&nbsp; ".join(kvs)
    unstarted_indexes = set(result.unstarted_item_indexes)
    item_indexes = tuple(index for index in range(result.requested_count) if index not in unstarted_indexes)
    items_html = _map_items_drilldown(
        result.results,
        scope_key=widget_state_key("map-result-items", result.run_id or "", result.graph_name, n),
        item_indexes=item_indexes,
    )
    body += html_detail(f"Per-item breakdown ({plural(n, 'item')})", items_html, state_key="per-item-breakdown")
    return theme_wrap(
        html_panel(f"MapResult: {result.graph_name} ({scope})", body),
        state_key=widget_state_key("map-result", result.run_id or "", result.graph_name, n),
    )


def _map_items_drilldown(
    results: tuple[RunResult, ...],
    *,
    scope_key: str = "map-items",
    item_indexes: tuple[int, ...] | None = None,
) -> str:
    """Render nested drill-down for MapResult items."""
    total = len(results)
    status_counts: dict[str, int] = {"all": total}
    for result in results:
        status = result.status.value
        status_counts[status] = status_counts.get(status, 0) + 1
        if result.restored:
            status_counts["restored"] = status_counts.get("restored", 0) + 1

    default_page_size = 100 if total > 200 else 50
    dom_scope = unique_dom_id("map-items", scope_key, total)
    filter_id = f"{dom_scope}-filter"
    page_size_id = f"{dom_scope}-page-size"
    prev_id = f"{dom_scope}-prev"
    next_id = f"{dom_scope}-next"
    page_info_id = f"{dom_scope}-page-info"
    list_id = f"{dom_scope}-items"

    parts: list[str] = []
    for i, result in enumerate(results):
        item_index = item_indexes[i] if item_indexes is not None else i
        display_status = "restored" if result.restored else result.status.value
        filter_status = f"{result.status.value} restored" if result.restored else result.status.value
        duration = duration_html(result.log.total_duration_ms) if result.log and not result.restored else "—"
        error_label = f' — <span style="color:{ERROR_COLOR}">{type(result.error).__name__}</span>' if result.error else ""
        summary = f"Item {item_index}: {status_badge(display_status)} {duration}{error_label}"
        item_html = html_detail(summary, result._repr_html_(), state_key=f"item-{item_index}")  # type: ignore[arg-type]
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


def render_node_record_repr(record: NodeRecord) -> str:
    """Render a NodeRecord representation."""
    status = "cached" if record.cached else record.status
    duration = "—" if record.status == "restored" else format_duration_ms(record.duration_ms)
    parts = [f"NodeRecord: {record.node_name}", status, duration, f"superstep {record.superstep}"]
    if record.error:
        parts.append(f"error: {record.error[:60]}")
    if record.decision is not None:
        decision = ", ".join(record.decision) if isinstance(record.decision, list) else record.decision
        parts.append(f"-> {decision}")
    return " | ".join(parts)


def render_node_stats_repr(stats: NodeStats) -> str:
    """Render a NodeStats representation."""
    parts = []
    if stats.succeeded:
        parts.append(f"{stats.succeeded} succeeded")
    if stats.errors:
        parts.append(plural(stats.errors, "error"))
    if stats.cached:
        parts.append(f"{stats.cached} cached")
    if stats.succeeded:
        parts.append(f"avg {format_duration_ms(stats.avg_ms)}")
    return f"NodeStats: {', '.join(parts)}" if parts else "NodeStats: empty"


def render_run_log_str(log: RunLog) -> str:
    """Render a RunLog table for terminal output."""
    n_errors = len(log.errors)
    n_restored = sum(1 for step in log.steps if step.status == "restored")
    duration = f"{n_restored} restored" if n_restored else format_duration_ms(log.total_duration_ms)
    header = f"RunLog: {log.graph_name} | {duration} | {plural(len({step.node_name for step in log.steps}), 'node')} | {plural(n_errors, 'error')}"

    has_decisions = any(step.decision is not None for step in log.steps)
    lines = [header, ""]
    cols = ["  Step", "Node", "Duration", "Status"]
    if has_decisions:
        cols.append("Decision")
    lines.append("  ".join(f"{column:<16}" for column in cols).rstrip())
    lines.append("  ".join("─" * 16 for _ in cols))

    for i, step in enumerate(log.steps):
        duration = "—" if step.status == "restored" or (step.status == "failed" and step.duration_ms == 0) else format_duration_ms(step.duration_ms)
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
        row = [f"  {i:>4}", f"{step.node_name:<16}", f"{duration:<16}", status]
        if has_decisions:
            decision = ""
            if step.decision is not None:
                decision = "→ " + ", ".join(step.decision) if isinstance(step.decision, list) else f"→ {step.decision}"
            row.append(decision)
        lines.append("  ".join(f"{column:<16}" for column in row).rstrip())

    nested = [i for i, step in enumerate(log.steps) if step._inner_logs]
    if nested:
        lines.append("")
        if len(nested) == 1:
            lines.append(f"  → .steps[{nested[0]}].log for inner trace")
        else:
            lines.append(f"  → .steps[i].log for inner traces (i={nested})")

    return "\n".join(lines)


def render_run_log_repr(log: RunLog) -> str:
    """Render a concise RunLog representation."""
    n_restored = sum(1 for step in log.steps if step.status == "restored")
    if n_restored:
        return f"RunLog(graph={log.graph_name!r}, steps={len(log.steps)}, restored={n_restored})"
    return f"RunLog(graph={log.graph_name!r}, steps={len(log.steps)}, duration={format_duration_ms(log.total_duration_ms)})"


def render_run_log_pretty(log: RunLog, pretty_printer: Any, cycle: bool) -> None:
    """Render a RunLog through IPython's pretty protocol."""
    if cycle:
        pretty_printer.text("RunLog(...)")
        return
    pretty_printer.text(str(log))


def render_run_log_html(log: RunLog) -> str:
    """Render a RunLog as rich notebook HTML."""
    headers = ["Step", "Node", "Status", "Duration"]
    has_decisions = any(step.decision is not None for step in log.steps)
    if has_decisions:
        headers.append("Decision")
    rows = []
    for i, step in enumerate(log.steps):
        status = "cached" if step.cached else step.status
        duration = duration_html(None if step.status == "restored" else step.duration_ms)
        row = [str(i), _code(step.node_name), status_badge(status), duration]
        if has_decisions:
            if step.decision is not None:
                decision = ", ".join(step.decision) if isinstance(step.decision, list) else step.decision
                row.append(f"&rarr; {decision}")
            else:
                row.append("")
        rows.append(row)
    n_errors = len(log.errors)
    n_restored = sum(1 for step in log.steps if step.status == "restored")
    duration = f"{n_restored} restored" if n_restored else duration_html(log.total_duration_ms)
    title = (
        f"RunLog: {log.graph_name} &nbsp; "
        f"{duration} &nbsp; "
        f"{plural(len({step.node_name for step in log.steps}), 'node')} &nbsp; "
        f"{plural(n_errors, 'error')}"
    )
    body = html_table(headers, rows)
    return theme_wrap(
        html_panel(title, body),
        state_key=widget_state_key("run-log", log.run_id, log.graph_name),
    )


def render_map_log_str(log: MapLog) -> str:
    """Render a MapLog table for terminal output."""
    n_errors = len(log.errors)
    n_succeeded = sum(1 for item in log.items if not item.errors)
    n_restored = log.restored_count
    avg_part = ""
    timed_success_items = log._timed_success_items
    if timed_success_items:
        avg = sum(item.total_duration_ms for item in timed_success_items) / len(timed_success_items)
        avg_part = f" | avg {format_duration_ms(avg)}/item"
    restored_part = f", {n_restored} restored" if n_restored else ""
    header = (
        f"MapLog: {log.graph_name} | {plural(len(log.items), 'item')} "
        f"({n_succeeded} succeeded{restored_part}) | {plural(n_errors, 'error')}{avg_part}"
    )
    lines = [header, ""]

    cols = ["  Item", "Duration", "Status", "Nodes"]
    lines.append("  ".join(f"{column:<16}" for column in cols).rstrip())
    lines.append("  ".join("─" * 16 for _ in cols))

    display_items = log.items[:_MAX_MAP_LOG_ROWS]
    for i, item in enumerate(display_items):
        restored = log._restored_flags[i]
        duration = "—" if restored else format_duration_ms(item.total_duration_ms)
        status = "restored" if restored else ("FAILED" if item.errors else "completed")
        n_nodes = len({step.node_name for step in item.steps})
        row = [f"  {i:>4}", f"{duration:<16}", f"{status:<16}", str(n_nodes)]
        lines.append("  ".join(f"{column:<16}" for column in row).rstrip())

    remaining = len(log.items) - len(display_items)
    if remaining > 0:
        lines.append(f"  ... and {plural(remaining, 'more item')}")

    lines.append("")
    lines.append("  → .log[i] for per-item trace")
    return "\n".join(lines)


def render_map_log_repr(log: MapLog) -> str:
    """Render a concise MapLog representation."""
    return (
        f"MapLog(graph={log.graph_name!r}, items={len(log.items)}, restored={log.restored_count}, "
        f"duration={format_duration_ms(log.total_duration_ms)})"
    )


def render_map_log_pretty(log: MapLog, pretty_printer: Any, cycle: bool) -> None:
    """Render a MapLog through IPython's pretty protocol."""
    if cycle:
        pretty_printer.text("MapLog(...)")
        return
    pretty_printer.text(str(log))


def render_map_log_html(log: MapLog) -> str:
    """Render a MapLog as rich notebook HTML."""
    headers = ["Item", "Duration", "Status", "Nodes"]
    rows = []
    row_attrs = []
    trace_items = []
    n_items = len(log.items)
    status_counts: dict[str, int] = {"all": n_items}
    for i, item in enumerate(log.items):
        restored = log._restored_flags[i]
        status = "restored" if restored else ("failed" if item.errors else "completed")
        filter_status = "completed restored" if restored else status
        if not item.errors:
            status_counts["completed"] = status_counts.get("completed", 0) + 1
        else:
            status_counts["failed"] = status_counts.get("failed", 0) + 1
        if restored:
            status_counts["restored"] = status_counts.get("restored", 0) + 1
        n_nodes = len({step.node_name for step in item.steps})
        duration = duration_html(None if restored else item.total_duration_ms)
        rows.append([str(i), duration, status_badge(status), str(n_nodes)])
        row_attrs.append({"data-hg-map-log-item": "1", "data-status": filter_status})

        summary = f'Item {i}: {status_badge(status)} {duration} <span style="color:{MUTED_COLOR}">({plural(n_nodes, "node")})</span>'
        trace_detail = html_detail(summary, item._repr_html_(), state_key=f"map-log-item-{i}")  # type: ignore[arg-type]
        trace_items.append(
            '<div data-hg-map-log-item="1" '
            f'data-status="{filter_status}" '
            'style="display:block; margin:0 0 8px 0; padding:6px 8px; '
            f"border:1px solid {BORDER_COLOR}; border-radius:10px; "
            f'background:{SURFACE_COLOR}">'
            f"{trace_detail}</div>"
        )

    avg_part = ""
    timed_success_items = log._timed_success_items
    if timed_success_items:
        avg = sum(item.total_duration_ms for item in timed_success_items) / len(timed_success_items)
        avg_part = f" &nbsp; avg {duration_html(avg)}/item"

    title = f"MapLog: {log.graph_name} ({plural(n_items, 'item')}){avg_part}"
    dom_scope = unique_dom_id("map-log-controls", log.graph_name, n_items, log.total_duration_ms)
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
        state_key=widget_state_key("map-log", log.graph_name, len(log.items)),
    )
