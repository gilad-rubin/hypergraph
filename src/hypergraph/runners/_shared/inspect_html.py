"""HTML + payload helpers for the rich inspect widget."""

from __future__ import annotations

import html
import json
import re
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from hypergraph._repr import status_badge, theme_wrap, unique_dom_id, widget_state_key

if TYPE_CHECKING:
    from hypergraph.runners._shared.inspect import FailureCase, RunView


_IMAGE_URL_RE = re.compile(r"\.(?:jpe?g|png|gif|webp|svg)(?:\?.*)?$", re.IGNORECASE)
_DATA_URI_RE = re.compile(r"^data:image/", re.IGNORECASE)
_MARKDOWN_RE = re.compile(r"(\*\*|__|#{1,6}\s|```|\n[-*+]\s|\n\d+\.\s|!\[|\[[^\]]+\]\()")

_MAX_TEXT_CAPTURE = 20_000
_MAX_PREVIEW_TEXT = 180
_MAX_SEQUENCE_ITEMS = 200
_MAX_MAPPING_ITEMS = 100
_MAX_TABLE_ROWS = 200
_MAX_TABLE_COLUMNS = 20
_MAX_DEPTH = 6


def _escape_json_for_html(text: str) -> str:
    return text.replace("</", "<\\/")


def _safe_json_payload(payload: dict[str, Any]) -> str:
    return _escape_json_for_html(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def _read_asset(name: str) -> str:
    return (files("hypergraph.runners._shared.assets") / name).read_text(encoding="utf-8")


def _looks_like_markdown(value: str) -> bool:
    return bool(_MARKDOWN_RE.search(value))


def _looks_like_image(value: str) -> bool:
    return bool(_IMAGE_URL_RE.search(value) or _DATA_URI_RE.search(value))


def _render_markdown_html(text: str) -> str:
    try:
        from markdown_it import MarkdownIt
    except Exception:
        return f"<pre>{html.escape(text)}</pre>"
    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": False})
    return md.render(text)


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _is_uniform_object_array(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    if not all(isinstance(item, dict) for item in value):
        return False
    first_keys = list(value[0].keys())
    return all(list(item.keys()) == first_keys for item in value)


def _is_pydantic_model(value: Any) -> bool:
    return hasattr(value, "model_dump") and callable(value.model_dump)


def _is_pandas_dataframe(value: Any) -> bool:
    return hasattr(value, "to_dict") and hasattr(value, "columns") and hasattr(value, "shape") and type(value).__name__ == "DataFrame"


def _inline_preview(value: dict[str, Any]) -> dict[str, Any]:
    kind = value["kind"]
    if kind in {"null", "boolean", "number"}:
        return value
    if kind == "image":
        return {"kind": "image", "preview": "image"}
    if kind in {"text", "markdown"}:
        return {
            "kind": kind,
            "preview": value.get("preview") or _truncate_text(value.get("text", ""), 120),
        }
    if kind == "table":
        return {
            "kind": "table",
            "preview": f"{value.get('row_count', 0)} rows",
        }
    if kind in {"array", "mapping", "dataclass", "pydantic"}:
        return {
            "kind": kind,
            "preview": value.get("summary") or value.get("type_name") or kind,
        }
    return {"kind": kind, "preview": value.get("summary") or value.get("type_name") or kind}


def _serialize_table_from_rows(rows: list[dict[str, Any]], *, type_name: str) -> dict[str, Any]:
    columns = list(rows[0].keys())[:_MAX_TABLE_COLUMNS]
    trimmed_rows = rows[:_MAX_TABLE_ROWS]
    serialized_rows = []
    for row in trimmed_rows:
        serialized_rows.append({column: _inline_preview(serialize_inspect_value(row.get(column), _depth=1)) for column in columns})
    return {
        "kind": "table",
        "type_name": type_name,
        "columns": columns,
        "rows": serialized_rows,
        "row_count": len(rows),
        "truncated": len(rows) > len(trimmed_rows) or len(rows[0]) > len(columns),
        "summary": f"{len(rows)} rows",
    }


def serialize_inspect_value(
    value: Any,
    *,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> dict[str, Any]:
    """Serialize a Python value into a widget-friendly payload."""

    if _seen is None:
        _seen = set()

    if _depth > _MAX_DEPTH:
        return {
            "kind": "text",
            "type_name": type(value).__name__,
            "text": _truncate_text(repr(value), _MAX_TEXT_CAPTURE),
            "preview": _truncate_text(repr(value), _MAX_PREVIEW_TEXT),
            "length": len(repr(value)),
            "truncated": len(repr(value)) > _MAX_TEXT_CAPTURE,
        }

    if value is None:
        return {"kind": "null", "type_name": "NoneType", "value": None}
    if isinstance(value, bool):
        return {"kind": "boolean", "type_name": "bool", "value": value}
    if isinstance(value, (int, float)):
        return {"kind": "number", "type_name": type(value).__name__, "value": value}
    if isinstance(value, (datetime, date, time)):
        rendered = value.isoformat()
        return {
            "kind": "text",
            "type_name": type(value).__name__,
            "text": rendered,
            "preview": rendered,
            "length": len(rendered),
            "truncated": False,
        }
    if isinstance(value, str):
        text = value[:_MAX_TEXT_CAPTURE]
        preview = _truncate_text(value.replace("\n", " "), _MAX_PREVIEW_TEXT)
        truncated = len(value) > len(text)
        if _looks_like_image(value):
            return {
                "kind": "image",
                "type_name": "image",
                "src": value,
                "preview": preview,
            }
        if _looks_like_markdown(value):
            return {
                "kind": "markdown",
                "type_name": "markdown",
                "text": text,
                "preview": preview,
                "html": _render_markdown_html(text),
                "length": len(value),
                "truncated": truncated,
            }
        return {
            "kind": "text",
            "type_name": "str",
            "text": text,
            "preview": preview,
            "length": len(value),
            "truncated": truncated,
        }

    object_id = id(value)
    recursive = isinstance(value, (dict, list, tuple, set, frozenset)) or is_dataclass(value)
    if recursive:
        if object_id in _seen:
            return {
                "kind": "text",
                "type_name": type(value).__name__,
                "text": f"<recursive {type(value).__name__}>",
                "preview": f"<recursive {type(value).__name__}>",
                "length": len(type(value).__name__) + 11,
                "truncated": False,
            }
        _seen.add(object_id)

    try:
        if _is_pandas_dataframe(value):
            frame_rows = value.to_dict(orient="records")
            return _serialize_table_from_rows(frame_rows, type_name="DataFrame")

        if _is_pydantic_model(value):
            model_data = value.model_dump(mode="python")
            return {
                "kind": "pydantic",
                "type_name": type(value).__name__,
                "entries": [
                    {"key": key, "value": serialize_inspect_value(model_data[key], _depth=_depth + 1, _seen=_seen)}
                    for key in list(model_data.keys())[:_MAX_MAPPING_ITEMS]
                ],
                "length": len(model_data),
                "truncated": len(model_data) > _MAX_MAPPING_ITEMS,
                "summary": f"{len(model_data)} fields",
            }

        if is_dataclass(value) and not isinstance(value, type):
            field_map = {field.name: getattr(value, field.name) for field in fields(value)}
            return {
                "kind": "dataclass",
                "type_name": type(value).__name__,
                "entries": [
                    {"key": key, "value": serialize_inspect_value(field_map[key], _depth=_depth + 1, _seen=_seen)}
                    for key in list(field_map.keys())[:_MAX_MAPPING_ITEMS]
                ],
                "length": len(field_map),
                "truncated": len(field_map) > _MAX_MAPPING_ITEMS,
                "summary": f"{len(field_map)} fields",
            }

        if isinstance(value, dict):
            keys = list(value.keys())[:_MAX_MAPPING_ITEMS]
            return {
                "kind": "mapping",
                "type_name": type(value).__name__,
                "entries": [{"key": str(key), "value": serialize_inspect_value(value[key], _depth=_depth + 1, _seen=_seen)} for key in keys],
                "length": len(value),
                "truncated": len(value) > len(keys),
                "summary": f"{len(value)} fields",
            }

        if isinstance(value, list):
            if _is_uniform_object_array(value):
                return _serialize_table_from_rows(value, type_name="list")
            items = value[:_MAX_SEQUENCE_ITEMS]
            return {
                "kind": "array",
                "type_name": "list",
                "items": [serialize_inspect_value(item, _depth=_depth + 1, _seen=_seen) for item in items],
                "length": len(value),
                "truncated": len(value) > len(items),
                "summary": f"{len(value)} items",
                "preview": f"{len(value)} items",
            }

        if isinstance(value, tuple):
            items = list(value)[:_MAX_SEQUENCE_ITEMS]
            return {
                "kind": "array",
                "type_name": "tuple",
                "items": [serialize_inspect_value(item, _depth=_depth + 1, _seen=_seen) for item in items],
                "length": len(value),
                "truncated": len(value) > len(items),
                "summary": f"{len(value)} items",
                "preview": f"{len(value)} items",
            }

        if isinstance(value, (set, frozenset)):
            items = list(value)[:_MAX_SEQUENCE_ITEMS]
            return {
                "kind": "array",
                "type_name": type(value).__name__,
                "items": [serialize_inspect_value(item, _depth=_depth + 1, _seen=_seen) for item in items],
                "length": len(value),
                "truncated": len(value) > len(items),
                "summary": f"{len(value)} items",
                "preview": f"{len(value)} items",
            }

        text = repr(value)
        return {
            "kind": "text",
            "type_name": type(value).__name__,
            "text": _truncate_text(text, _MAX_TEXT_CAPTURE),
            "preview": _truncate_text(text, _MAX_PREVIEW_TEXT),
            "length": len(text),
            "truncated": len(text) > _MAX_TEXT_CAPTURE,
        }
    finally:
        if recursive:
            _seen.discard(object_id)


def _serialize_failure_case(failure: FailureCase | None) -> dict[str, Any] | None:
    if failure is None:
        return None
    return {
        "node_name": failure.node_name,
        "error_type": type(failure.error).__name__,
        "message": str(failure.error),
        "inputs": serialize_inspect_value(failure.inputs),
        "superstep": failure.superstep,
        "duration_ms": failure.duration_ms,
        "started_at_ms": getattr(failure, "started_at_ms", None),
        "ended_at_ms": getattr(failure, "ended_at_ms", None),
        "item_index": failure.item_index,
    }


def _effective_node_window(node: Any, fallback_start_ms: float) -> tuple[float, float]:
    raw_start = node.started_at_ms
    duration_ms = max(float(node.duration_ms or 0.0), 0.0)
    if raw_start is None:
        raw_start = fallback_start_ms
    raw_end = node.ended_at_ms
    raw_end = raw_start + duration_ms if raw_end is None else max(float(raw_end), raw_start + duration_ms)
    return float(raw_start), float(raw_end)


def _timeline_layout(nodes: tuple[Any, ...]) -> tuple[list[tuple[float, float]], float]:
    if not nodes:
        return [], 0.0

    windows: list[tuple[float, float]] = []
    last_raw_end = 0.0
    collapsed_gap_ms = 0.0

    for node in nodes:
        raw_start, raw_end = _effective_node_window(node, last_raw_end)
        if raw_start > last_raw_end:
            collapsed_gap_ms += raw_start - last_raw_end
        display_start = raw_start - collapsed_gap_ms
        display_end = raw_end - collapsed_gap_ms
        windows.append((max(display_start, 0.0), max(display_end, 0.0)))
        last_raw_end = max(last_raw_end, raw_end)

    total_ms = max((end for _, end in windows), default=0.0)
    return windows, total_ms


def build_run_view_payload(view: RunView) -> dict[str, Any]:
    """Convert a RunView into the payload consumed by the JS widget."""

    status = view.status.value if hasattr(view.status, "value") else str(view.status)
    windows, timeline_total_duration_ms = _timeline_layout(view.nodes)
    return {
        "run_id": view.run_id,
        "status": status,
        "total_duration_ms": view.total_duration_ms,
        "timeline_total_duration_ms": round(timeline_total_duration_ms, 3),
        "failure": _serialize_failure_case(view.failure),
        "nodes": [
            {
                "node_name": node.node_name,
                "status": node.status,
                "superstep": node.superstep,
                "duration_ms": node.duration_ms,
                "started_at_ms": node.started_at_ms,
                "ended_at_ms": node.ended_at_ms,
                "timeline_started_at_ms": round(window[0], 3),
                "timeline_ended_at_ms": round(window[1], 3),
                "cached": node.cached,
                "error": node.error,
                "inputs": serialize_inspect_value(node.inputs),
                "outputs": serialize_inspect_value(node.outputs),
            }
            for node, window in zip(view.nodes, windows, strict=False)
        ],
    }


def generate_inspect_document(*, payload: dict[str, Any], widget_id: str, graph_html: str | None = None) -> str:
    """Generate the standalone HTML document rendered inside the inspect iframe."""

    inspect_js = _read_asset("inspect.js")
    payload_json = _safe_json_payload(payload)
    graph_json = _escape_json_for_html(json.dumps(graph_html, ensure_ascii=False))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Hypergraph Inspect</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {{
      --hg-font-sans: "Inter", "Segoe UI Variable", "Segoe UI", sans-serif;
      --hg-font-mono: "JetBrains Mono", "SF Mono", "Cascadia Code", Menlo, monospace;
      --hg-bg: #050607;
      --hg-bg-alt: #090b0e;
      --hg-panel: #0b0d10;
      --hg-panel-strong: #101318;
      --hg-muted-panel: #12161b;
      --hg-popover: #0f1318;
      --hg-ink: #f3f6fa;
      --hg-subtle: #b6bfcb;
      --hg-muted: #8891a0;
      --hg-dim: #626b78;
      --hg-border: rgba(255,255,255,0.08);
      --hg-border-soft: rgba(255,255,255,0.05);
      --hg-track: rgba(255,255,255,0.06);
      --hg-track-strong: rgba(255,255,255,0.10);
      --hg-primary: #60a5fa;
      --hg-primary-soft: rgba(59,130,246,0.16);
      --hg-accent: #8b5cf6;
      --hg-success: #34d399;
      --hg-success-soft: rgba(16,185,129,0.18);
      --hg-error: #f87171;
      --hg-error-soft: rgba(239,68,68,0.18);
      --hg-running: #60a5fa;
      --hg-running-soft: rgba(59,130,246,0.18);
      --hg-stopped: #fbbf24;
      --hg-stopped-soft: rgba(245,158,11,0.18);
      --hg-shadow: 0 24px 80px rgba(0,0,0,0.45);
      color-scheme: light dark;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(59,130,246,0.10), transparent 34%),
        radial-gradient(circle at top right, rgba(139,92,246,0.10), transparent 24%),
        linear-gradient(180deg, var(--hg-bg), var(--hg-bg-alt));
      color: var(--hg-ink);
      font-family: var(--hg-font-sans);
    }}
    body {{ overflow: hidden; }}
    button, input, select {{ font: inherit; }}
    button {{
      appearance: none;
      border: 0;
      background: none;
      color: inherit;
    }}
    .hg-shell {{
      display: flex;
      flex-direction: column;
      background: linear-gradient(180deg, rgba(255,255,255,0.01), rgba(255,255,255,0));
    }}
    .hg-header {{
      height: 48px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 0 16px;
      background: var(--hg-panel);
      border-bottom: 1px solid var(--hg-border);
      box-shadow: 0 1px 0 rgba(255,255,255,0.02);
      flex-shrink: 0;
    }}
    .hg-header-copy {{
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }}
    .hg-header-side {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-shrink: 0;
    }}
    .hg-kicker {{
      font: 600 11px/1 var(--hg-font-mono);
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--hg-muted);
    }}
    .hg-header-sep {{
      color: rgba(255,255,255,0.18);
      font-size: 14px;
    }}
    .hg-header-run {{
      font: 600 13px/1 var(--hg-font-sans);
      color: var(--hg-ink);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .hg-header-sub {{
      font: 12px/1 var(--hg-font-mono);
      color: var(--hg-dim);
    }}
    .hg-banner {{
      margin: 12px 16px 0;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid var(--hg-border);
      background: var(--hg-panel-strong);
      box-shadow: var(--hg-shadow);
    }}
    .hg-banner-failure {{
      border-color: rgba(248,113,113,0.25);
      background: linear-gradient(180deg, rgba(239,68,68,0.12), rgba(15,19,24,0.95));
    }}
    .hg-banner-label {{
      font: 600 10px/1 var(--hg-font-mono);
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--hg-muted);
    }}
    .hg-banner-title {{
      margin-top: 6px;
      font: 600 13px/1.4 var(--hg-font-sans);
      color: var(--hg-ink);
    }}
    .hg-banner-body {{
      margin-top: 4px;
      font: 12px/1.5 var(--hg-font-mono);
      color: var(--hg-error);
      white-space: pre-wrap;
    }}
    .hg-summary-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      padding: 8px 16px;
      background: var(--hg-panel);
      border-bottom: 1px solid var(--hg-border-soft);
      flex-shrink: 0;
    }}
    .hg-summary-cell {{
      min-height: 44px;
      padding: 8px 10px;
      border: 1px solid var(--hg-border);
      border-radius: 10px;
      background: var(--hg-muted-panel);
    }}
    .hg-summary-label {{
      font: 600 10px/1 var(--hg-font-mono);
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--hg-muted);
      margin-bottom: 8px;
    }}
    .hg-summary-value {{
      font: 600 12px/1.2 var(--hg-font-sans);
      color: var(--hg-ink);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .hg-tabs {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 16px;
      background: var(--hg-panel);
      border-bottom: 1px solid var(--hg-border-soft);
      flex-shrink: 0;
    }}
    .hg-tab {{
      min-height: 28px;
      padding: 0 12px;
      border-radius: 999px;
      border: 1px solid var(--hg-border);
      background: var(--hg-muted-panel);
      color: var(--hg-muted);
      font: 500 11px/1 var(--hg-font-mono);
      cursor: pointer;
      transition: background 160ms ease, color 160ms ease, border-color 160ms ease;
    }}
    .hg-tab:hover {{
      color: var(--hg-ink);
      border-color: rgba(255,255,255,0.14);
    }}
    .hg-tab.is-active {{
      color: #ffffff;
      background: rgba(59,130,246,0.18);
      border-color: rgba(59,130,246,0.35);
      box-shadow: inset 0 0 0 1px rgba(59,130,246,0.14);
    }}
    .hg-main {{
      flex: 1;
      display: grid;
      grid-template-columns: minmax(0, 1fr) 340px;
      min-height: 0;
      overflow: hidden;
    }}
    .hg-panel {{
      background: var(--hg-panel);
      overflow: hidden;
    }}
    .hg-timeline-panel {{
      border-right: 1px solid var(--hg-border);
    }}
    .hg-panel-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 8px 12px;
      background: rgba(255,255,255,0.03);
      border-bottom: 1px solid rgba(255,255,255,0.05);
      color: var(--hg-muted);
      font: 600 11px/1 var(--hg-font-mono);
      text-transform: uppercase;
      letter-spacing: 0.12em;
    }}
    .hg-rows {{
      display: flex;
      flex-direction: column;
      min-height: 280px;
      overflow: auto;
      padding: 6px 0;
    }}
    .hg-row {{
      display: grid;
      grid-template-columns: minmax(140px, 160px) 64px auto minmax(180px, 1fr);
      align-items: center;
      gap: 12px;
      min-height: 32px;
      width: 100%;
      padding: 0 12px;
      background: transparent;
      color: inherit;
      text-align: left;
      cursor: pointer;
      transition: background 140ms ease;
      border-top: 1px solid transparent;
      border-bottom: 1px solid transparent;
    }}
    .hg-row:hover {{
      background: rgba(255,255,255,0.04);
    }}
    .hg-row.is-selected {{
      background: rgba(255,255,255,0.08);
      border-top-color: rgba(96,165,250,0.16);
      border-bottom-color: rgba(96,165,250,0.16);
    }}
    .hg-row-name {{
      min-width: 0;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      font: 500 11px/1 var(--hg-font-mono);
      color: #d7dce4;
    }}
    .hg-row-meta {{
      text-align: right;
      font: 500 11px/1 var(--hg-font-mono);
      color: var(--hg-muted);
      white-space: nowrap;
    }}
    .hg-row-status {{
      display: inline-flex;
      justify-content: flex-start;
    }}
    .hg-row-track {{
      position: relative;
      height: 24px;
      border-radius: 4px;
      background: rgba(255,255,255,0.04);
      overflow: hidden;
    }}
    .hg-row-bar {{
      position: absolute;
      top: 50%;
      transform: translateY(-50%);
      height: 16px;
      border-radius: 999px;
      transition: left 220ms ease, width 220ms ease;
      box-shadow: 0 0 0 1px rgba(255,255,255,0.04) inset;
    }}
    .hg-row-bar-completed, .hg-row-bar-cached {{
      background: linear-gradient(90deg, rgba(16,185,129,0.60), rgba(16,185,129,0.95));
    }}
    .hg-row-bar-failed {{
      background: linear-gradient(90deg, rgba(239,68,68,0.55), rgba(239,68,68,0.95));
    }}
    .hg-row-bar-running {{
      background: linear-gradient(90deg, rgba(59,130,246,0.45), rgba(96,165,250,0.95));
      box-shadow: 0 0 20px rgba(96,165,250,0.18);
    }}
    .hg-row-bar-stopped, .hg-row-bar-paused, .hg-row-bar-partial {{
      background: linear-gradient(90deg, rgba(245,158,11,0.45), rgba(251,191,36,0.92));
    }}
    .hg-status {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      min-height: 22px;
      padding: 0 8px;
      border-radius: 4px;
      font: 600 10px/1 var(--hg-font-mono);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      border: 1px solid transparent;
      white-space: nowrap;
    }}
    .hg-status::before {{
      content: "";
      width: 5px;
      height: 5px;
      border-radius: 999px;
      background: currentColor;
      flex-shrink: 0;
    }}
    .hg-status-completed, .hg-status-cached {{
      background: rgba(16,185,129,0.18);
      border-color: rgba(16,185,129,0.22);
      color: var(--hg-success);
    }}
    .hg-status-failed {{
      background: rgba(239,68,68,0.16);
      border-color: rgba(239,68,68,0.22);
      color: var(--hg-error);
    }}
    .hg-status-running {{
      background: rgba(59,130,246,0.16);
      border-color: rgba(59,130,246,0.22);
      color: var(--hg-running);
    }}
    .hg-status-stopped, .hg-status-paused, .hg-status-partial {{
      background: rgba(245,158,11,0.16);
      border-color: rgba(245,158,11,0.22);
      color: var(--hg-stopped);
    }}
    .hg-status-neutral {{
      background: rgba(255,255,255,0.06);
      border-color: rgba(255,255,255,0.08);
      color: var(--hg-muted);
    }}
    .hg-detail-panel {{
      display: flex;
      flex-direction: column;
      min-width: 0;
      background: var(--hg-panel);
    }}
    .hg-detail-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      padding: 10px 12px;
      border-bottom: 1px solid rgba(255,255,255,0.05);
      background: rgba(255,255,255,0.03);
    }}
    .hg-detail-title {{
      font: 600 18px/1.1 var(--hg-font-sans);
      letter-spacing: -0.03em;
      color: var(--hg-ink);
    }}
    .hg-detail-sub {{
      margin-top: 8px;
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      color: var(--hg-muted);
      font: 12px/1.4 var(--hg-font-mono);
    }}
    .hg-detail-times {{
      color: var(--hg-muted);
      font: 600 10px/1 var(--hg-font-mono);
      text-transform: uppercase;
      letter-spacing: 0.12em;
      white-space: nowrap;
      padding-top: 3px;
    }}
    .hg-detail-section {{
      padding: 12px;
      border-top: 1px solid rgba(255,255,255,0.04);
      overflow: auto;
    }}
    .hg-detail-kicker {{
      font: 600 10px/1 var(--hg-font-mono);
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--hg-muted);
      margin-bottom: 8px;
    }}
    .hg-error-block {{
      padding: 10px 12px;
      border-radius: 8px;
      background: rgba(239,68,68,0.12);
      border: 1px solid rgba(239,68,68,0.16);
      color: var(--hg-error);
      font: 12px/1.5 var(--hg-font-mono);
      white-space: pre-wrap;
    }}
    .hg-details {{
      border: 1px solid var(--hg-border);
      border-radius: 8px;
      background: var(--hg-muted-panel);
      padding: 8px 10px;
    }}
    .hg-details > summary {{
      cursor: pointer;
      list-style: none;
      font: 600 11px/1.5 var(--hg-font-mono);
      color: var(--hg-subtle);
    }}
    .hg-details > summary::-webkit-details-marker {{ display: none; }}
    .hg-tree {{
      display: flex;
      flex-direction: column;
      gap: 10px;
      margin-top: 10px;
    }}
    .hg-tree-row {{
      display: grid;
      grid-template-columns: minmax(76px, 110px) minmax(0, 1fr);
      gap: 12px;
      align-items: start;
    }}
    .hg-tree-key {{
      font: 500 10px/1.4 var(--hg-font-mono);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--hg-muted);
      padding-top: 3px;
    }}
    .hg-tree-value {{ min-width: 0; }}
    .hg-value-meta {{
      margin-top: 8px;
      color: var(--hg-dim);
      font: 11px/1.4 var(--hg-font-mono);
    }}
    .hg-text-block {{
      margin: 10px 0 0;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      padding: 10px 12px;
      border-radius: 8px;
      background: #0a0d11;
      border: 1px solid rgba(255,255,255,0.06);
      font: 12px/1.55 var(--hg-font-mono);
      color: var(--hg-subtle);
    }}
    .hg-markdown {{
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 8px;
      background: #0a0d11;
      border: 1px solid rgba(255,255,255,0.06);
      font-size: 13px;
      line-height: 1.6;
      color: var(--hg-subtle);
    }}
    .hg-markdown :first-child {{ margin-top: 0; }}
    .hg-markdown :last-child {{ margin-bottom: 0; }}
    .hg-markdown pre {{
      overflow: auto;
      padding: 10px 12px;
      border-radius: 6px;
      background: rgba(0,0,0,0.45);
      color: #f3f6fa;
      font: 12px/1.5 var(--hg-font-mono);
    }}
    .hg-markdown code {{ font-family: var(--hg-font-mono); }}
    .hg-image-wrap {{
      padding: 6px;
      border: 1px solid var(--hg-border);
      border-radius: 8px;
      background: #0a0d11;
    }}
    .hg-image {{
      display: block;
      width: 100%;
      max-height: 320px;
      object-fit: contain;
      border-radius: 6px;
    }}
    .hg-data-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }}
    .hg-data-table th,
    .hg-data-table td {{
      padding: 8px 10px;
      border-bottom: 1px solid rgba(255,255,255,0.05);
      text-align: left;
      vertical-align: top;
    }}
    .hg-data-table th {{
      font: 600 10px/1 var(--hg-font-mono);
      text-transform: uppercase;
      letter-spacing: 0.10em;
      color: var(--hg-muted);
      background: rgba(255,255,255,0.02);
    }}
    .hg-table-footer {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-top: 8px;
      color: var(--hg-muted);
      font: 11px/1.4 var(--hg-font-mono);
    }}
    .hg-table-controls {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }}
    .hg-mini-btn {{
      min-height: 24px;
      padding: 0 10px;
      border-radius: 999px;
      border: 1px solid var(--hg-border);
      background: var(--hg-muted-panel);
      color: var(--hg-subtle);
      font: 500 11px/1 var(--hg-font-mono);
      cursor: pointer;
    }}
    .hg-mini-btn:disabled {{
      opacity: 0.35;
      cursor: not-allowed;
    }}
    .hg-inline-text {{
      color: var(--hg-subtle);
      font: 12px/1.45 var(--hg-font-sans);
    }}
    .hg-inline-chip,
    .hg-scalar {{
      display: inline-flex;
      align-items: center;
      min-height: 18px;
      padding: 0 6px;
      border-radius: 999px;
      border: 1px solid var(--hg-border);
      background: rgba(255,255,255,0.04);
      font: 500 10px/1 var(--hg-font-mono);
      color: var(--hg-subtle);
    }}
    .hg-null {{ color: var(--hg-muted); }}
    .hg-num {{ color: var(--hg-primary); }}
    .hg-bool {{ color: var(--hg-success); }}
    .hg-value-grid {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      padding: 8px;
    }}
    .hg-value-card {{
      border: 1px solid var(--hg-border);
      border-radius: 8px;
      background: var(--hg-muted-panel);
      padding: 10px 12px;
      text-align: left;
      cursor: pointer;
      color: inherit;
      transition: background 150ms ease, border-color 150ms ease;
    }}
    .hg-value-card:hover {{
      background: rgba(255,255,255,0.05);
      border-color: rgba(255,255,255,0.12);
    }}
    .hg-value-card.is-selected {{
      background: rgba(59,130,246,0.12);
      border-color: rgba(59,130,246,0.28);
    }}
    .hg-value-card-head {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      font: 500 11px/1.4 var(--hg-font-mono);
      color: #d7dce4;
    }}
    .hg-value-card-body {{
      margin-top: 10px;
      display: grid;
      gap: 6px;
      color: var(--hg-subtle);
      font: 12px/1.45 var(--hg-font-sans);
    }}
    .hg-muted {{
      color: var(--hg-muted);
      margin-right: 6px;
      text-transform: uppercase;
      font: 600 10px/1 var(--hg-font-mono);
      letter-spacing: 0.08em;
    }}
    .hg-graph-panel {{
      min-height: 100%;
      background: var(--hg-panel);
    }}
    .hg-graph-frame {{
      width: 100%;
      height: calc(100vh - 156px);
      min-height: 520px;
      border: 0;
      background: transparent;
    }}
    .hg-empty-state {{
      display: grid;
      place-items: center;
      min-height: 280px;
      padding: 24px;
      color: var(--hg-muted);
      text-align: center;
      font: 12px/1.6 var(--hg-font-mono);
    }}
    .app {{
      display: flex;
      flex-direction: column;
    }}
    .header {{
      height: 48px;
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 0 16px;
      background: var(--hg-panel);
      border-bottom: 1px solid var(--hg-border);
      flex-shrink: 0;
    }}
    .header h1 {{
      margin: 0;
      font-size: 13px;
      font-weight: 600;
      color: var(--hg-ink);
    }}
    .header .sep {{
      color: var(--hg-muted-foreground, var(--hg-muted));
      opacity: 0.3;
    }}
    .header .sub {{
      font-size: 13px;
      color: var(--hg-muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .controls {{
      display: flex;
      align-items: center;
      gap: 16px;
      padding: 8px 16px;
      background: var(--hg-panel);
      border-bottom: 1px solid var(--hg-border);
      flex-shrink: 0;
      font-size: 14px;
      z-index: 10;
      box-shadow: 0 1px 2px rgba(0,0,0,0.2);
      overflow-x: auto;
    }}
    .divider {{
      width: 1px;
      height: 24px;
      background: var(--hg-border);
      flex-shrink: 0;
    }}
    .speed-group {{
      display: flex;
      background: var(--hg-muted-panel);
      border-radius: 9999px;
      padding: 2px;
      gap: 2px;
      flex-shrink: 0;
    }}
    .speed-btn {{
      padding: 2px 8px;
      border-radius: 9999px;
      border: none;
      font: 11px/1 var(--hg-font-mono);
      color: var(--hg-muted);
      background: transparent;
      cursor: pointer;
      transition: all 0.15s;
    }}
    .speed-btn:hover {{
      color: var(--hg-ink);
      background: rgba(255,255,255,0.06);
    }}
    .speed-btn.active {{
      color: white;
      background: var(--hg-primary);
    }}
    .main {{
      display: flex;
      flex-direction: column;
      flex: 1;
      overflow: auto;
      min-height: 0;
    }}
    .gantt-panel {{
      flex: 1;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      background: var(--hg-panel);
      min-width: 0;
    }}
    .gantt-bar-top {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 4px 12px;
      background: rgba(255,255,255,0.03);
      border-bottom: 1px solid rgba(255,255,255,0.05);
      flex-shrink: 0;
    }}
    .gantt-bar-top span {{
      font: 11px/1 var(--hg-font-mono);
      color: var(--hg-muted);
      white-space: nowrap;
    }}
    .gantt-scroll {{
      flex: 1;
      overflow: auto;
      min-height: 0;
    }}
    .main--timeline .gantt-panel,
    .main--values .gantt-panel {{
      flex: 0 0 auto;
    }}
    .main--timeline .gantt-scroll,
    .main--values .gantt-scroll {{
      flex: 0 0 auto;
    }}
    .main--timeline .gantt-flex,
    .main--values .gantt-flex {{
      min-height: 0;
    }}
    .main--graph .gantt-panel {{
      flex: 1 1 auto;
      min-height: 520px;
    }}
    .main--graph .gantt-scroll {{
      flex: 1 1 auto;
    }}
    .gantt-flex {{
      display: flex;
      min-width: 360px;
      min-height: 100%;
    }}
    .label-col {{
      width: 160px;
      flex-shrink: 0;
      border-right: 1px solid rgba(255,255,255,0.05);
      background: var(--hg-panel);
    }}
    .label-row {{
      display: flex;
      align-items: center;
      gap: 6px;
      cursor: pointer;
      transition: background 0.15s;
      padding: 0 6px;
    }}
    .label-row:hover {{
      background: rgba(255,255,255,0.05);
    }}
    .label-row.selected {{
      background: rgba(255,255,255,0.10);
    }}
    .label-row .dot {{
      width: 4px;
      height: 4px;
      border-radius: 50%;
      background: #374151;
      display: inline-block;
    }}
    .label-row .name {{
      font: 500 11px/1 var(--hg-font-mono);
      color: #d1d5db;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .label-row .name.failed {{
      color: #ef4444;
    }}
    .si {{
      width: 14px;
      height: 14px;
      flex-shrink: 0;
      margin-right: 8px;
      border-radius: 999px;
      display: inline-block;
      position: relative;
    }}
    .si-completed {{
      background: rgba(34,197,94,0.18);
      border: 1px solid rgba(34,197,94,0.35);
    }}
    .si-cached {{
      background: rgba(16,185,129,0.18);
      border: 1px solid rgba(16,185,129,0.35);
    }}
    .si-failed {{
      background: rgba(239,68,68,0.18);
      border: 1px solid rgba(239,68,68,0.35);
    }}
    .si-running {{
      background: rgba(59,130,246,0.18);
      border: 1px solid rgba(96,165,250,0.55);
      animation: spin 0.8s linear infinite;
    }}
    .si-paused, .si-stopped, .si-partial {{
      background: rgba(245,158,11,0.18);
      border: 1px solid rgba(251,191,36,0.35);
    }}
    .si-neutral {{
      background: rgba(75,85,99,0.15);
      border: 1px solid rgba(107,114,128,0.35);
    }}
    .bar-col {{
      flex: 1;
      position: relative;
      overflow: hidden;
      min-width: 0;
    }}
    .detail {{
      width: 100%;
      display: none;
      background: var(--hg-panel);
      border-top: 1px solid var(--hg-border);
      flex-shrink: 0;
    }}
    .detail.open {{
      display: block;
    }}
    .detail-inner {{
      width: 100%;
    }}
    .detail-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 8px 12px;
      border-bottom: 1px solid rgba(255,255,255,0.05);
      background: rgba(255,255,255,0.03);
    }}
    .detail-head h3 {{
      margin: 0;
      font: 600 12px/1 var(--hg-font-sans);
      color: var(--hg-ink);
    }}
    .detail-close {{
      width: 20px;
      height: 20px;
      border: none;
      background: none;
      color: #6b7280;
      cursor: pointer;
      border-radius: 4px;
      font-size: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .detail-close:hover {{
      background: rgba(255,255,255,0.08);
      color: #d1d5db;
    }}
    .detail-body {{
      padding: 12px 14px 14px;
    }}
    .detail-sect {{
      margin-bottom: 14px;
    }}
    .detail-sect:last-child {{
      margin-bottom: 0;
    }}
    .detail-sect-title {{
      font: 600 10px/1 var(--hg-font-sans);
      color: var(--hg-muted);
      text-transform: uppercase;
      letter-spacing: .05em;
      margin-bottom: 6px;
    }}
    .detail-kv {{
      display: flex;
      justify-content: space-between;
      margin-bottom: 3px;
      font-size: 11px;
      gap: 12px;
    }}
    .detail-kv .k {{
      color: var(--hg-muted);
    }}
    .detail-kv .v {{
      font: 500 11px/1 var(--hg-font-mono);
      color: var(--hg-subtle);
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 3px;
      font: 500 10px/1 var(--hg-font-mono);
      padding: 1px 5px;
      border-radius: 3px;
      white-space: nowrap;
    }}
    .badge .dot {{
      width: 5px;
      height: 5px;
      border-radius: 50%;
    }}
    .badge-completed {{
      background: rgba(34,197,94,0.18);
      color: #4ade80;
    }}
    .badge-completed .dot {{
      background: #4ade80;
    }}
    .badge-cached {{
      background: rgba(16,185,129,0.18);
      color: #34d399;
    }}
    .badge-cached .dot {{
      background: #34d399;
    }}
    .badge-failed {{
      background: rgba(239,68,68,0.18);
      color: #f87171;
    }}
    .badge-failed .dot {{
      background: #f87171;
    }}
    .badge-running {{
      background: rgba(59,130,246,0.18);
      color: #60a5fa;
    }}
    .badge-running .dot {{
      background: #60a5fa;
      animation: pulse 1.5s infinite;
    }}
    .badge-paused, .badge-stopped, .badge-partial {{
      background: rgba(245,158,11,0.18);
      color: #fbbf24;
    }}
    .badge-paused .dot, .badge-stopped .dot, .badge-partial .dot {{
      background: #fbbf24;
    }}
    .badge-neutral {{
      background: rgba(75,85,99,0.15);
      color: #9ca3af;
    }}
    .badge-neutral .dot {{
      background: #9ca3af;
    }}
    [data-hg-theme="light"] html,
    [data-hg-theme="light"] body {{
      --hg-bg: #eef3fb;
      --hg-bg-alt: #f7f9fc;
      --hg-panel: rgba(255,255,255,0.96);
      --hg-panel-strong: #ffffff;
      --hg-muted-panel: #f8fafc;
      --hg-popover: #ffffff;
      --hg-ink: #0f172a;
      --hg-subtle: #1e293b;
      --hg-muted: #334155;
      --hg-dim: #64748b;
      --hg-border: rgba(15,23,42,0.10);
      --hg-border-soft: rgba(15,23,42,0.06);
      --hg-track: rgba(148,163,184,0.14);
      --hg-track-strong: rgba(148,163,184,0.22);
      background:
        radial-gradient(circle at top left, rgba(59,130,246,0.12), transparent 34%),
        radial-gradient(circle at top right, rgba(168,85,247,0.08), transparent 24%),
        linear-gradient(180deg, #eef3fb, #f7f9fc);
      color: #0f172a;
    }}
    [data-hg-theme="light"] .hg-shell,
    [data-hg-theme="light"] .app {{
      background: linear-gradient(180deg, rgba(255,255,255,0.88), rgba(255,255,255,0.94));
    }}
    [data-hg-theme="light"] .hg-header,
    [data-hg-theme="light"] .hg-summary-grid,
    [data-hg-theme="light"] .hg-tabs,
    [data-hg-theme="light"] .header,
    [data-hg-theme="light"] .controls,
    [data-hg-theme="light"] .gantt-panel,
    [data-hg-theme="light"] .detail,
    [data-hg-theme="light"] .detail-head,
    [data-hg-theme="light"] .gantt-bar-top,
    [data-hg-theme="light"] .hg-panel,
    [data-hg-theme="light"] .hg-detail-panel,
    [data-hg-theme="light"] .label-col {{
      background: rgba(255,255,255,0.92);
      color: #0f172a;
    }}
    [data-hg-theme="light"] .hg-header,
    [data-hg-theme="light"] .hg-banner,
    [data-hg-theme="light"] .hg-summary-cell,
    [data-hg-theme="light"] .hg-tab,
    [data-hg-theme="light"] .hg-value-card,
    [data-hg-theme="light"] .hg-details,
    [data-hg-theme="light"] .hg-image-wrap,
    [data-hg-theme="light"] .hg-mini-btn,
    [data-hg-theme="light"] .speed-group,
    [data-hg-theme="light"] .speed-btn,
    [data-hg-theme="light"] .header,
    [data-hg-theme="light"] .controls,
    [data-hg-theme="light"] .detail,
    [data-hg-theme="light"] .detail-head,
    [data-hg-theme="light"] .gantt-bar-top,
    [data-hg-theme="light"] .detail-close {{
      border-color: rgba(15,23,42,0.10);
    }}
    [data-hg-theme="light"] .hg-header,
    [data-hg-theme="light"] .hg-banner,
    [data-hg-theme="light"] .hg-summary-cell,
    [data-hg-theme="light"] .hg-tab,
    [data-hg-theme="light"] .hg-value-card,
    [data-hg-theme="light"] .hg-details,
    [data-hg-theme="light"] .hg-image-wrap,
    [data-hg-theme="light"] .hg-mini-btn,
    [data-hg-theme="light"] .header,
    [data-hg-theme="light"] .controls,
    [data-hg-theme="light"] .detail,
    [data-hg-theme="light"] .detail-head,
    [data-hg-theme="light"] .gantt-bar-top {{
      box-shadow: 0 18px 48px rgba(15,23,42,0.06);
    }}
    [data-hg-theme="light"] .hg-header,
    [data-hg-theme="light"] .hg-summary-grid,
    [data-hg-theme="light"] .hg-tabs,
    [data-hg-theme="light"] .header,
    [data-hg-theme="light"] .controls,
    [data-hg-theme="light"] .detail,
    [data-hg-theme="light"] .detail-head,
    [data-hg-theme="light"] .gantt-bar-top,
    [data-hg-theme="light"] .label-col,
    [data-hg-theme="light"] .hg-data-table th,
    [data-hg-theme="light"] .hg-data-table td {{
      border-color: rgba(15,23,42,0.08);
    }}
    [data-hg-theme="light"] .hg-summary-cell,
    [data-hg-theme="light"] .hg-tab,
    [data-hg-theme="light"] .hg-value-card,
    [data-hg-theme="light"] .hg-details,
    [data-hg-theme="light"] .hg-mini-btn,
    [data-hg-theme="light"] .speed-group {{
      background: #f4f7fb;
    }}
    [data-hg-theme="light"] .hg-kicker,
    [data-hg-theme="light"] .hg-summary-label,
    [data-hg-theme="light"] .hg-panel-head,
    [data-hg-theme="light"] .hg-detail-times,
    [data-hg-theme="light"] .hg-detail-kicker,
    [data-hg-theme="light"] .detail-sect-title,
    [data-hg-theme="light"] .hg-tree-key,
    [data-hg-theme="light"] .hg-muted,
    [data-hg-theme="light"] .gantt-bar-top span,
    [data-hg-theme="light"] .header .sub,
    [data-hg-theme="light"] .sub,
    [data-hg-theme="light"] .detail-kv .k {{
      color: #475569;
    }}
    [data-hg-theme="light"] .hg-header-run,
    [data-hg-theme="light"] .hg-summary-value,
    [data-hg-theme="light"] .hg-detail-title,
    [data-hg-theme="light"] .hg-inline-text,
    [data-hg-theme="light"] .hg-value-card-body,
    [data-hg-theme="light"] .detail-kv .v,
    [data-hg-theme="light"] .header h1,
    [data-hg-theme="light"] .label-row .name,
    [data-hg-theme="light"] .hg-row-name,
    [data-hg-theme="light"] .hg-row-meta,
    [data-hg-theme="light"] .hg-value-meta,
    [data-hg-theme="light"] .hg-details > summary,
    [data-hg-theme="light"] .hg-value-card-head {{
      color: #1e293b;
    }}
    [data-hg-theme="light"] .hg-header-sep,
    [data-hg-theme="light"] .header .sep {{
      color: rgba(15,23,42,0.18);
    }}
    [data-hg-theme="light"] .hg-row:hover,
    [data-hg-theme="light"] .label-row:hover,
    [data-hg-theme="light"] .hg-value-card:hover,
    [data-hg-theme="light"] .speed-btn:hover,
    [data-hg-theme="light"] .detail-close:hover {{
      background: rgba(148,163,184,0.12);
    }}
    [data-hg-theme="light"] .hg-row.is-selected,
    [data-hg-theme="light"] .label-row.selected {{
      background: rgba(96,165,250,0.12);
    }}
    [data-hg-theme="light"] .hg-row.is-selected {{
      border-top-color: rgba(59,130,246,0.22);
      border-bottom-color: rgba(59,130,246,0.22);
    }}
    [data-hg-theme="light"] .hg-row-track,
    [data-hg-theme="light"] .hg-inline-chip,
    [data-hg-theme="light"] .hg-scalar {{
      background: rgba(148,163,184,0.14);
      color: #334155;
      border-color: rgba(148,163,184,0.26);
    }}
    [data-hg-theme="light"] .hg-null {{
      color: #64748b;
    }}
    [data-hg-theme="light"] .hg-num {{
      color: #2563eb;
    }}
    [data-hg-theme="light"] .hg-bool {{
      color: #047857;
    }}
    [data-hg-theme="light"] .hg-banner-failure {{
      border-color: rgba(239,68,68,0.20);
      background: linear-gradient(180deg, rgba(254,226,226,0.98), rgba(255,255,255,0.94));
    }}
    [data-hg-theme="light"] .hg-banner-body,
    [data-hg-theme="light"] .hg-error-block,
    [data-hg-theme="light"] .label-row .name.failed {{
      color: #dc2626;
    }}
    [data-hg-theme="light"] .hg-error-block {{
      background: rgba(254,226,226,0.86);
      border-color: rgba(248,113,113,0.24);
    }}
    [data-hg-theme="light"] .hg-text-block,
    [data-hg-theme="light"] .hg-markdown,
    [data-hg-theme="light"] .hg-image-wrap {{
      background: #f8fafc;
      border-color: rgba(148,163,184,0.22);
      color: #1e293b;
    }}
    [data-hg-theme="light"] .hg-markdown pre {{
      background: rgba(15,23,42,0.08);
      color: #0f172a;
    }}
    [data-hg-theme="light"] .hg-data-table th {{
      background: rgba(148,163,184,0.08);
      color: #475569;
    }}
    [data-hg-theme="light"] .hg-data-table td,
    [data-hg-theme="light"] .hg-inline-text,
    [data-hg-theme="light"] .hg-markdown,
    [data-hg-theme="light"] .hg-text-block,
    [data-hg-theme="light"] .detail-body {{
      color: #1e293b;
    }}
    [data-hg-theme="light"] .detail-head h3,
    [data-hg-theme="light"] .detail-body,
    [data-hg-theme="light"] .detail-sect-title,
    [data-hg-theme="light"] .detail-kv .k,
    [data-hg-theme="light"] .detail-kv .v,
    [data-hg-theme="light"] .gantt-bar-top span {{
      color: #334155;
    }}
    [data-hg-theme="light"] .detail-kv .v,
    [data-hg-theme="light"] .detail-head h3 {{
      color: #0f172a;
    }}
    [data-hg-theme="light"] .label-row .dot {{
      background: #94a3b8;
    }}
    [data-hg-theme="light"] .speed-btn.active,
    [data-hg-theme="light"] .hg-tab.is-active {{
      background: #2563eb;
      color: #ffffff;
      border-color: #2563eb;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.14);
    }}
    [data-hg-theme="light"] .badge-neutral {{
      background: rgba(148,163,184,0.18);
      color: #64748b;
    }}
    [data-hg-theme="light"] .badge-neutral .dot {{
      background: #64748b;
    }}
    [data-hg-theme="light"] .detail-close {{
      color: #64748b;
    }}
    [data-hg-theme="light"] .detail-close:hover {{
      color: #0f172a;
    }}
    [data-hg-theme="light"] .bar-col svg line {{
      stroke: rgba(148,163,184,0.20);
    }}
    [data-hg-theme="light"] .bar-col svg text {{
      fill: #475569;
    }}
    @keyframes pulse {{
      0%,100% {{ opacity: 1; }}
      50% {{ opacity: .3; }}
    }}
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
    @media (max-width: 980px) {{
      .detail, .detail.open {{
        width: 100%;
      }}
      .detail-inner {{
        width: 100%;
      }}
      .hg-graph-frame {{
        height: 520px;
      }}
    }}
  </style>
</head>
<body>
  <div id="inspect-root"></div>
  <script>{inspect_js}</script>
  <script>
    window.HypergraphInspect.init({{
      rootId: "inspect-root",
      widgetId: {json.dumps(widget_id)},
      graphHtml: {graph_json},
      payload: {payload_json}
    }});
  </script>
</body>
</html>"""


def render_inspect_widget(view: RunView, *, widget_id: str | None = None) -> str:
    """Render the outer iframe wrapper for a RunView."""

    actual_widget_id = widget_id or unique_dom_id("hypergraph-inspect-frame", view.run_id)
    srcdoc = html.escape(
        generate_inspect_document(
            payload=build_run_view_payload(view),
            widget_id=actual_widget_id,
            graph_html=view.graph_html,
        ),
        quote=True,
    )
    css_fix = """<style>
.cell-output-ipywidget-background { background-color: transparent !important; }
.jp-OutputArea-output { background-color: transparent; }
</style>"""
    return (
        f"{css_fix}"
        f'<iframe id="{actual_widget_id}" '
        f'class="hypergraph-inspect-frame" '
        f'srcdoc="{srcdoc}" '
        f'width="100%" height="360" frameborder="0" '
        f'style="border:none; width:100%; max-width:100%; min-height:260px; height:360px; display:block; background:transparent; border-radius:18px;" '
        f'sandbox="allow-scripts allow-same-origin"></iframe>'
    )


def render_map_inspect_widget(
    *,
    result: Any | None = None,
    graph_name: str | None = None,
    error: BaseException | None = None,
) -> str:
    """Render a lightweight batch-level inspect widget for start_map()."""

    title = graph_name or "Batch"
    if result is None and error is None:
        body = (
            '<div style="display:grid; gap:10px;">'
            '<div style="font-size:0.95em; color:light-dark(#334155,#cbd5e1)">'
            "Batch execution is running. Child inspect data is being captured and the widget will settle when the batch finishes."
            "</div>"
            '<div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">'
            f"{status_badge('active')}"
            '<span style="font-size:0.9em; color:light-dark(#475569,#94a3b8)">Waiting for mapped items to complete…</span>'
            "</div>"
            "</div>"
        )
        return theme_wrap(
            (
                '<div style="border:1px solid light-dark(#dbe4f0,#1f2937); border-radius:14px; padding:14px 16px; '
                'background:light-dark(rgba(255,255,255,0.9),rgba(10,14,20,0.92)); box-shadow:0 18px 42px rgba(15,23,42,0.08);">'
                f'<div style="display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:10px;">'
                f'<div style="font:600 1rem/1.2 ui-sans-serif,system-ui,sans-serif; color:light-dark(#0f172a,#f8fafc)">Inspect Batch</div>'
                f'<div style="font:500 0.9rem/1.2 ui-monospace,SFMono-Regular,monospace; color:light-dark(#64748b,#94a3b8)">{html.escape(title)}</div>'
                f"</div>{body}</div>"
            ),
            state_key=widget_state_key("map-inspect-running", graph_name or ""),
        )

    if error is not None:
        error_text = html.escape(f"{type(error).__name__}: {error}")
        body = (
            '<div style="display:grid; gap:10px;">'
            f"{status_badge('failed')}"
            f'<div style="padding:10px 12px; border-radius:10px; background:light-dark(#fef2f2,#3a1518); '
            f"border:1px solid light-dark(#fecaca,#7f1d1d); color:light-dark(#b91c1c,#fca5a5); "
            f'font:500 0.9rem/1.5 ui-monospace,SFMono-Regular,monospace;">{error_text}</div>'
            "</div>"
        )
        return theme_wrap(
            (
                '<div style="border:1px solid light-dark(#f5c2c7,#7f1d1d); border-radius:14px; padding:14px 16px; '
                'background:light-dark(rgba(255,255,255,0.96),rgba(10,14,20,0.92));">'
                f'<div style="font:600 1rem/1.2 ui-sans-serif,system-ui,sans-serif; color:light-dark(#0f172a,#f8fafc); margin-bottom:10px;">Inspect Batch</div>'
                f"{body}</div>"
            ),
            state_key=widget_state_key("map-inspect-error", graph_name or ""),
        )

    failed_results = [item for item in result.results if item.status.value == "failed"]
    completed = sum(1 for item in result.results if item.status.value == "completed")
    selected = failed_results[0] if failed_results else (result.results[0] if result.results else None)
    child_html = ""
    if selected is not None:
        child_html = '<div style="margin-top:14px;">' + selected.inspect()._repr_html_() + "</div>"
    failure_cases = [item.failure for item in failed_results if item.failure is not None]
    failure_lines = "".join(
        f'<li style="margin:0 0 6px 0;"><span style="font-weight:600;">item {case.item_index}</span>'
        f' &middot; {html.escape(case.node_name)} &middot; <span style="color:light-dark(#b91c1c,#fca5a5)">{html.escape(str(case.error))}</span></li>'
        for case in failure_cases[:5]
    )
    summary = (
        '<div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:10px;">'
        f"{status_badge(result.status.value)}"
        f'<span style="font-size:0.9em; color:light-dark(#475569,#94a3b8)">{len(result.results)} items</span>'
        f'<span style="font-size:0.9em; color:light-dark(#475569,#94a3b8)">{completed} completed</span>'
        f'<span style="font-size:0.9em; color:light-dark(#475569,#94a3b8)">{len(failure_cases)} failed</span>'
        "</div>"
    )
    failure_block = (
        '<div style="padding:10px 12px; border-radius:10px; border:1px solid light-dark(#fecaca,#7f1d1d); '
        'background:light-dark(#fff7f7,#271316); margin-bottom:10px;">'
        '<div style="font:600 0.85rem/1 ui-monospace,SFMono-Regular,monospace; text-transform:uppercase; letter-spacing:0.06em; '
        'color:light-dark(#991b1b,#fca5a5); margin-bottom:8px;">Failed items</div>'
        f'<ul style="margin:0; padding-left:18px; color:light-dark(#334155,#cbd5e1); font-size:0.92rem;">{failure_lines or "<li>None</li>"}</ul>'
        "</div>"
    )
    return theme_wrap(
        (
            '<div style="border:1px solid light-dark(#dbe4f0,#1f2937); border-radius:14px; padding:14px 16px; '
            'background:light-dark(rgba(255,255,255,0.9),rgba(10,14,20,0.92)); box-shadow:0 18px 42px rgba(15,23,42,0.08);">'
            f'<div style="display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:10px;">'
            f'<div style="font:600 1rem/1.2 ui-sans-serif,system-ui,sans-serif; color:light-dark(#0f172a,#f8fafc)">Inspect Batch</div>'
            f'<div style="font:500 0.9rem/1.2 ui-monospace,SFMono-Regular,monospace; color:light-dark(#64748b,#94a3b8)">{html.escape(result.run_id or title)}</div>'
            f"</div>{summary}{failure_block}{child_html}</div>"
        ),
        state_key=widget_state_key("map-inspect-result", result.run_id or graph_name or ""),
    )


def build_inspect_update_script(widget_id: str, view: RunView) -> str:
    """Build the JS snippet that pushes live state into the existing iframe."""

    payload_json = _safe_json_payload(build_run_view_payload(view))
    return f"""
(function() {{
  var frame = document.getElementById({json.dumps(widget_id)});
  if (!frame) return;
  var message = {{ type: "hypergraph-inspect-update", widgetId: {json.dumps(widget_id)}, payload: {payload_json} }};
  var send = function() {{
    try {{
      if (frame.contentWindow) {{
        frame.contentWindow.postMessage(message, "*");
      }}
    }} catch (_err) {{}}
  }};
  send();
  setTimeout(send, 30);
  setTimeout(send, 180);
  setTimeout(send, 450);
}})();
"""


def generate_inspect_graph_html(graph: Any) -> str | None:
    """Build the static graph tab HTML once for inspect-enabled runs."""

    try:
        from hypergraph.viz.html import generate_widget_html
        from hypergraph.viz.renderer import render_graph
    except Exception:
        return None

    try:
        flat_graph = graph.to_flat_graph()
        graph_data = render_graph(
            flat_graph,
            depth=0,
            theme="auto",
            show_types=True,
            separate_outputs=False,
            show_inputs=True,
            show_bounded_inputs=False,
            debug_overlays=False,
        )
        return generate_widget_html(graph_data)
    except Exception:
        return None
