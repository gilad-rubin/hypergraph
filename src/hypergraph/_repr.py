"""HTML rendering primitives for _repr_html_ methods.

Provides reusable components for beautiful Jupyter/notebook display.
All styles are inline (no <style> tags — some renderers strip them).
"""

from __future__ import annotations

import html as _html
from typing import Any

from hypergraph._utils import format_datetime, format_duration_ms, plural

# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------

STATUS_COLORS: dict[str, str] = {
    "completed": "#059669",
    "failed": "#dc2626",
    "partial": "#d97706",
    "cached": "#2563eb",
    "active": "#d97706",
    "paused": "#7c3aed",
}

_BADGE_BG: dict[str, str] = {
    "completed": "#ecfdf5",
    "failed": "#fef2f2",
    "partial": "#fffbeb",
    "cached": "#eff6ff",
    "active": "#fffbeb",
    "paused": "#f5f3ff",
}

# Shared inline styles
_FONT = "font-family: ui-monospace, 'SF Mono', 'Cascadia Code', Menlo, monospace"
_BORDER = "border: 1px solid #e5e7eb"
_RADIUS = "border-radius: 6px"
_CELL_PAD = "padding: 6px 10px"
_CODE_STYLE = "background:#f8fafc; padding:1px 4px; border-radius:3px"


# ---------------------------------------------------------------------------
# Primitive components
# ---------------------------------------------------------------------------


def status_badge(status: str) -> str:
    """Render a colored pill badge for a status value."""
    color = STATUS_COLORS.get(status, "#6b7280")
    bg = _BADGE_BG.get(status, "#f3f4f6")
    return f'<span style="background:{bg}; color:{color}; padding:2px 8px; border-radius:9999px; font-size:0.85em; font-weight:600">{status}</span>'


def html_table(headers: list[str], rows: list[list[str]], title: str | None = None) -> str:
    """Render a styled HTML table.

    Args:
        headers: Column header labels.
        rows: List of rows, each a list of HTML cell content.
        title: Optional title shown above the table.
    """
    parts: list[str] = []
    if title:
        parts.append(f'<div style="{_FONT}; font-weight:700; margin-bottom:8px">{title}</div>')

    parts.append(f'<table style="{_FONT}; {_BORDER}; {_RADIUS}; border-collapse: separate; border-spacing:0; font-size:0.9em; width:auto">')

    # Header row
    parts.append("<thead><tr>")
    for h in headers:
        parts.append(
            f'<th style="{_CELL_PAD}; text-align:left; background:#f9fafb; border-bottom:2px solid #e5e7eb; color:#374151; font-weight:600">{h}</th>'
        )
    parts.append("</tr></thead>")

    # Body rows
    parts.append("<tbody>")
    for row in rows:
        parts.append("<tr>")
        for cell in row:
            parts.append(f'<td style="{_CELL_PAD}; border-bottom:1px solid #f3f4f6; color:#374151">{cell}</td>')
        parts.append("</tr>")
    parts.append("</tbody></table>")

    return "".join(parts)


def html_panel(title: str, body: str) -> str:
    """Render a titled panel wrapper."""
    return (
        f'<div style="{_FONT}; {_BORDER}; {_RADIUS}; overflow:hidden; margin:4px 0">'
        f'<div style="background:#f9fafb; padding:8px 12px; '
        f'border-bottom:1px solid #e5e7eb; font-weight:700; color:#111827">{title}</div>'
        f'<div style="padding:10px 12px">{body}</div>'
        f"</div>"
    )


def html_kv(label: str, value: str) -> str:
    """Render a key-value pair."""
    return f'<span style="color:#6b7280; font-size:0.85em">{label}:</span> <span style="color:#111827">{value}</span>'


def html_detail(summary: str, content: str) -> str:
    """Render a collapsible <details> section."""
    return (
        f'<details style="margin-top:8px">'
        f'<summary style="cursor:pointer; color:#2563eb; font-size:0.9em; '
        f'{_FONT}">{summary}</summary>'
        f'<div style="margin-top:8px">{content}</div>'
        f"</details>"
    )


def _code(content: str) -> str:
    """Wrap content in a styled inline <code> tag."""
    return f'<code style="{_CODE_STYLE}">{content}</code>'


def duration_html(ms: float | None) -> str:
    """Format duration with monospace styling."""
    text = format_duration_ms(ms)
    return f'<code style="{_CODE_STYLE}; color:#374151">{text}</code>'


def datetime_html(dt) -> str:
    """Format datetime for HTML display."""
    text = format_datetime(dt)
    return f'<span style="color:#6b7280; font-size:0.85em">{text}</span>'


# ---------------------------------------------------------------------------
# Value rendering
# ---------------------------------------------------------------------------

_MAX_VALUE_LEN = 200
_MAX_ITEMS = 8


def _compact_html(value: Any) -> str:
    """Render a single Python value as compact, HTML-safe text."""
    if value is None:
        return '<span style="color:#6b7280">None</span>'

    if isinstance(value, str):
        if len(value) <= _MAX_VALUE_LEN:
            return _code(_html.escape(repr(value)))
        preview = repr(value[:_MAX_VALUE_LEN])
        return f'{_code(_html.escape(preview) + "…")} <span style="color:#6b7280">(len={len(value)})</span>'

    if isinstance(value, (int, float, bool)):
        return _code(f"{value!r}")

    # numpy-like arrays
    shape = getattr(value, "shape", None)
    if shape is not None and hasattr(value, "dtype"):
        dtype = getattr(value, "dtype", None)
        return _code(f"&lt;{type(value).__name__} shape={shape!r} dtype={dtype!r}&gt;")

    # dict preview
    if isinstance(value, dict):
        n = len(value)
        if n == 0:
            return _code("{}")
        keys = ", ".join(_html.escape(repr(k)) for k in list(value)[:4])
        suffix = f" … (+{n - 4})" if n > 4 else ""
        return f'{_code("{" + keys + suffix + "}")} <span style="color:#6b7280">({plural(n, "key")})</span>'

    # list/tuple preview
    if isinstance(value, (list, tuple)):
        n = len(value)
        bracket = "[]" if isinstance(value, list) else "()"
        if n == 0:
            return _code(bracket)
        return f'{_code(bracket[0] + "…" + bracket[1])} <span style="color:#6b7280">({plural(n, "item")})</span>'

    # fallback
    text = repr(value)
    if len(text) > _MAX_VALUE_LEN:
        text = text[:_MAX_VALUE_LEN] + "…"
    return _code(_html.escape(text))


def values_html(values: dict[str, Any], *, max_items: int = _MAX_ITEMS) -> str:
    """Render a dict as a compact key-value table.

    Used for progressive disclosure of RunResult.values, Checkpoint.values, etc.
    Shows first `max_items` entries with smart value truncation.
    """
    if not values:
        return '<span style="color:#6b7280; font-style:italic">no values</span>'
    items = list(values.items())
    rows = [[_code(_html.escape(str(k))), _compact_html(v)] for k, v in items[:max_items]]
    table = html_table(["Key", "Value"], rows)
    if len(items) > max_items:
        table += f'<div style="color:#6b7280; font-size:0.85em; margin-top:4px">… and {plural(len(items) - max_items, "more key")}</div>'
    return table


def error_html(error: BaseException | str | None) -> str:
    """Render an error as styled HTML."""
    if error is None:
        return ""
    text = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    escaped = _html.escape(text)
    return f'<div style="color:#dc2626; {_FONT}; font-size:0.85em; padding:4px 8px; background:#fef2f2; {_RADIUS}; margin-top:4px"><b>Error:</b> {escaped}</div>'
