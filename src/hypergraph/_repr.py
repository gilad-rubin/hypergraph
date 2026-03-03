"""HTML rendering primitives for _repr_html_ methods.

Provides reusable components for beautiful Jupyter/notebook display.
All styles are inline (no <style> tags — some renderers strip them).
Uses CSS ``light-dark()`` for automatic dark mode adaptation.
"""

from __future__ import annotations

import hashlib
import html as _html
import itertools
from typing import Any

from hypergraph._utils import format_datetime, format_duration_ms, plural

# ---------------------------------------------------------------------------
# Theme — light-dark() for automatic dark/light adaptation
# ---------------------------------------------------------------------------


def _ld(light: str, dark: str) -> str:
    """CSS ``light-dark()`` value.  Resolves based on inherited color-scheme."""
    return f"light-dark({light},{dark})"


_ALIGNUI_FONT_SANS = "ui-monospace, 'SF Mono', 'Cascadia Code', Menlo, monospace"
_ALIGNUI_FONT_MONO = "ui-monospace, 'SF Mono', 'Cascadia Code', Menlo, monospace"


# -- Theme + state script (inline JS for notebook cell output) --------------
# Detects JupyterLab, VS Code, and Marimo themes, then overrides the
# container's ``color-scheme`` property.  Without this script (or if JS is
# disabled), ``color-scheme: light dark`` follows the system preference.
_WIDGET_JS = (
    "(function(){var c=document.currentScript.previousElementSibling;try{"
    "var b=document.body,r=document.documentElement,t=null;"
    "var jp=b.dataset.jpThemeLight;"
    "if(jp==='false')t='dark';else if(jp==='true')t='light';"
    "if(!t){var cn=b.className||'';"
    "if(cn.includes('jp-mod-dark'))t='dark';"
    "else if(cn.includes('jp-mod-light'))t='light';}"
    "var tk=b.getAttribute('data-vscode-theme-kind');"
    "if(tk)t=tk.includes('light')?'light':'dark';"
    "if(!t){var dt=b.dataset.theme||r.dataset.theme;"
    "var dm=b.dataset.mode||r.dataset.mode;"
    "if(dt==='dark'||dm==='dark')t='dark';"
    "else if(dt==='light'||dm==='light')t='light';}"
    "if(t)c.style.colorScheme=t;"
    "var k=c.getAttribute('data-hg-state-key')||'';"
    "if(!k){var h=c.querySelector('[data-hg-panel-title]');"
    "if(h&&h.getAttribute('data-hg-panel-title'))k='panel:'+h.getAttribute('data-hg-panel-title');}"
    "if(!k){k='widget';}"
    "var ds=c.querySelectorAll('details[data-hg-persist=\"1\"]');"
    "for(var i=0;i<ds.length;i++){var d=ds[i];"
    "var dk=d.getAttribute('data-hg-key')||('idx:'+i);"
    "var sk='hypergraph:details:'+k+':'+dk;"
    "d.setAttribute('data-hg-storage-key',sk);"
    "try{var v=localStorage.getItem(sk);"
    "if(v==='1')d.open=true;else if(v==='0')d.open=false;}catch(_e){}"
    "if(!d.__hgPersistBound){d.addEventListener('toggle',function(ev){"
    "var el=ev.currentTarget;var key=el.getAttribute('data-hg-storage-key');"
    "if(!key)return;try{localStorage.setItem(key,el.open?'1':'0');}catch(_e){}"
    "});d.__hgPersistBound=true;}"
    "}"
    "}catch(e){}})()"
)


def widget_state_key(namespace: str, *parts: Any) -> str:
    """Build a compact stable key for widget UI state persistence."""
    raw = "|".join([namespace, *(str(p) for p in parts if p is not None)])
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{namespace}:{digest}"


_DOM_ID_COUNTER = itertools.count(1)


def unique_dom_id(namespace: str, *parts: Any) -> str:
    """Build a unique DOM element id for a single render pass.

    IDs must be unique across multiple notebook outputs in one document.
    We combine a stable hash with a per-process monotonic counter.
    """
    stable = widget_state_key(namespace, *parts).replace(":", "-")
    return f"hg-{stable}-{next(_DOM_ID_COUNTER)}"


def theme_wrap(html: str, *, state_key: str | None = None) -> str:
    """Wrap widget HTML with automatic dark mode detection.

    Sets ``color-scheme: light dark`` on a container so all CSS
    ``light-dark()`` values resolve according to the system preference.
    A tiny inline script overrides for JupyterLab, VS Code, and Marimo
    notebook themes.
    """
    key_attr = f' data-hg-state-key="{_html.escape(state_key, quote=True)}"' if state_key else ""
    base = "color-scheme:light dark"
    return f'<div style="{base}"{key_attr}>{html}</div><script>{_WIDGET_JS}</script>'


# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------

# AlignUI-inspired token bridge.
# Keep this map portable so it can be extracted into a standalone helper lib.
ALIGNUI_WIDGET_THEME: dict[str, str] = {
    # Typography
    "font_sans": _ALIGNUI_FONT_SANS,
    "font_mono": _ALIGNUI_FONT_MONO,
    # Surfaces + text
    "bg_surface": _ld("#ffffff", "#111827"),
    "bg_panel": _ld("#f9fafb", "#1f2937"),
    "bg_soft": _ld("#f3f4f6", "#374151"),
    "bg_code": _ld("#f8fafc", "#1e293b"),
    "text_strong": _ld("#111827", "#f3f4f6"),
    "text_sub": _ld("#374151", "#d1d5db"),
    "text_soft": _ld("#6b7280", "#9ca3af"),
    # Borders + accents
    "border_soft": _ld("#e5e7eb", "#374151"),
    "border_sub": _ld("#f3f4f6", "#1f2937"),
    "link": _ld("#2563eb", "#60a5fa"),
    # Semantic states
    "success_text": _ld("#059669", "#34d399"),
    "success_base": _ld("#059669", "#34d399"),
    "success_bg": _ld("#ecfdf5", "#064e3b"),
    "error_text": _ld("#dc2626", "#f87171"),
    "error_base": _ld("#dc2626", "#f87171"),
    "error_bg": _ld("#fef2f2", "#450a0a"),
    "warning_text": _ld("#d97706", "#fbbf24"),
    "warning_base": _ld("#d97706", "#fbbf24"),
    "warning_bg": _ld("#fffbeb", "#451a03"),
    "info_text": _ld("#2563eb", "#60a5fa"),
    "info_base": _ld("#2563eb", "#60a5fa"),
    "info_bg": _ld("#eff6ff", "#1e3a5f"),
    "feature_text": _ld("#7c3aed", "#a78bfa"),
    "feature_bg": _ld("#f5f3ff", "#2e1065"),
    # Effects
    "shadow_xs": "none",
}


def widget_theme_tokens() -> dict[str, str]:
    """Return portable widget design tokens for external reuse/extraction."""
    return dict(ALIGNUI_WIDGET_THEME)


# Semantic color tokens (exported for _repr_html_ methods in other modules)
ERROR_COLOR = ALIGNUI_WIDGET_THEME["error_text"]
MUTED_COLOR = ALIGNUI_WIDGET_THEME["text_soft"]
BORDER_COLOR = ALIGNUI_WIDGET_THEME["border_soft"]
SURFACE_COLOR = ALIGNUI_WIDGET_THEME["bg_surface"]
PANEL_COLOR = ALIGNUI_WIDGET_THEME["bg_panel"]
TEXT_COLOR = ALIGNUI_WIDGET_THEME["text_sub"]
TEXT_STRONG_COLOR = ALIGNUI_WIDGET_THEME["text_strong"]
FONT_SANS_STYLE = f"font-family:{ALIGNUI_WIDGET_THEME['font_sans']}"
FONT_MONO_STYLE = f"font-family:{ALIGNUI_WIDGET_THEME['font_mono']}"

# Internal tokens (derived from portable bridge)
_TEXT = TEXT_COLOR
_TEXT_STRONG = TEXT_STRONG_COLOR
_BG_PANEL = PANEL_COLOR
_BG_SOFT = ALIGNUI_WIDGET_THEME["bg_soft"]
_BG_CODE = ALIGNUI_WIDGET_THEME["bg_code"]
_BORDER_COLOR = BORDER_COLOR
_BORDER_LIGHT = ALIGNUI_WIDGET_THEME["border_sub"]
_LINK = ALIGNUI_WIDGET_THEME["link"]
_ERROR_BG = ALIGNUI_WIDGET_THEME["error_bg"]

STATUS_COLORS: dict[str, str] = {
    "completed": ALIGNUI_WIDGET_THEME["success_text"],
    "failed": ALIGNUI_WIDGET_THEME["error_text"],
    "partial": ALIGNUI_WIDGET_THEME["warning_text"],
    "cached": ALIGNUI_WIDGET_THEME["info_text"],
    "active": ALIGNUI_WIDGET_THEME["warning_text"],
    "paused": ALIGNUI_WIDGET_THEME["feature_text"],
}

_BADGE_BG: dict[str, str] = {
    "completed": ALIGNUI_WIDGET_THEME["success_bg"],
    "failed": ALIGNUI_WIDGET_THEME["error_bg"],
    "partial": ALIGNUI_WIDGET_THEME["warning_bg"],
    "cached": ALIGNUI_WIDGET_THEME["info_bg"],
    "active": ALIGNUI_WIDGET_THEME["warning_bg"],
    "paused": ALIGNUI_WIDGET_THEME["feature_bg"],
}

# Shared inline styles
_FONT = FONT_MONO_STYLE
_FONT_MONO = FONT_MONO_STYLE
_BORDER = f"border: 1px solid {_BORDER_COLOR}"
_RADIUS = "border-radius: 6px"
_CELL_PAD = "padding: 6px 10px"
_CODE_STYLE = f"background:{_BG_CODE}; padding:1px 4px; border-radius:3px"

_CONTROL_TEXT_SIZE = "0.85em"
_CONTROL_LABEL_STYLE = f"{_FONT}; font-size:{_CONTROL_TEXT_SIZE}; color:{MUTED_COLOR}"
_CONTROL_SELECT_STYLE = (
    f"{_FONT}; font-size:{_CONTROL_TEXT_SIZE}; padding:2px 6px; border-radius:6px; "
    f"border:1px solid {_BORDER_COLOR}; background:{SURFACE_COLOR}; color:{_TEXT_STRONG}"
)
_CONTROL_BUTTON_STYLE = (
    f"{_FONT}; font-size:{_CONTROL_TEXT_SIZE}; padding:2px 8px; border-radius:6px; "
    f"border:1px solid {_BORDER_COLOR}; background:{SURFACE_COLOR}; color:{_TEXT_STRONG}; cursor:pointer"
)


# ---------------------------------------------------------------------------
# Primitive components
# ---------------------------------------------------------------------------


def status_badge(status: str) -> str:
    """Render a colored pill badge for a status value."""
    color = STATUS_COLORS.get(status, ALIGNUI_WIDGET_THEME["text_soft"])
    bg = _BADGE_BG.get(status, ALIGNUI_WIDGET_THEME["bg_soft"])
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
        parts.append(f'<div style="{_FONT}; font-weight:700; margin-bottom:8px; color:{_TEXT_STRONG}">{title}</div>')

    parts.append(f'<table style="{_FONT}; {_BORDER}; {_RADIUS}; border-collapse: separate; border-spacing:0; font-size:0.9em; width:auto">')

    # Header row
    parts.append("<thead><tr>")
    for h in headers:
        parts.append(
            f'<th style="{_CELL_PAD}; text-align:left; background:{_BG_PANEL}; border-bottom:2px solid {_BORDER_COLOR}; color:{_TEXT_STRONG}; font-weight:600">{h}</th>'
        )
    parts.append("</tr></thead>")

    # Body rows
    parts.append("<tbody>")
    for row in rows:
        parts.append("<tr>")
        for cell in row:
            parts.append(f'<td style="{_CELL_PAD}; border-bottom:1px solid {_BORDER_LIGHT}; color:{_TEXT}">{cell}</td>')
        parts.append("</tr>")
    parts.append("</tbody></table>")

    return "".join(parts)


def html_table_with_row_attrs(
    headers: list[str],
    rows: list[list[str]],
    *,
    row_attrs: list[dict[str, str]] | None = None,
    title: str | None = None,
    table_id: str | None = None,
) -> str:
    """Render a styled HTML table with optional attributes on each row."""
    parts: list[str] = []
    if title:
        parts.append(f'<div style="{_FONT}; font-weight:700; margin-bottom:8px; color:{_TEXT_STRONG}">{title}</div>')

    id_attr = f' id="{_html.escape(table_id, quote=True)}"' if table_id else ""
    parts.append(f'<table{id_attr} style="{_FONT}; {_BORDER}; {_RADIUS}; border-collapse: separate; border-spacing:0; font-size:0.9em; width:auto">')

    parts.append("<thead><tr>")
    for h in headers:
        parts.append(
            f'<th style="{_CELL_PAD}; text-align:left; background:{_BG_PANEL}; border-bottom:2px solid {_BORDER_COLOR}; color:{_TEXT_STRONG}; font-weight:600">{h}</th>'
        )
    parts.append("</tr></thead>")

    parts.append("<tbody>")
    for i, row in enumerate(rows):
        attrs = ""
        if row_attrs and i < len(row_attrs):
            attrs = "".join(f' {_html.escape(k, quote=True)}="{_html.escape(v, quote=True)}"' for k, v in row_attrs[i].items())
        parts.append(f"<tr{attrs}>")
        for cell in row:
            parts.append(f'<td style="{_CELL_PAD}; border-bottom:1px solid {_BORDER_LIGHT}; color:{_TEXT}">{cell}</td>')
        parts.append("</tr>")
    parts.append("</tbody></table>")

    return "".join(parts)


def html_table_controls(
    *,
    view_id: str,
    status_id: str,
    sort_id: str,
    show_id: str,
    show_options: list[int],
    total_rows: int,
) -> str:
    """Render standard controls for interactive HTML tables."""
    show_opts = []
    for n in show_options:
        selected = " selected" if n == show_options[0] else ""
        show_opts.append(f'<option value="{n}"{selected}>{n}</option>')
    show_opts.append(f'<option value="all">All ({total_rows})</option>')

    return (
        '<div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin:8px 0 10px 0">'
        f'<span style="{_CONTROL_LABEL_STYLE}">View:</span>'
        f'<select id="{_html.escape(view_id, quote=True)}" style="{_CONTROL_SELECT_STYLE}">'
        '<option value="parents" selected>Parents only</option>'
        '<option value="all">All runs</option>'
        "</select>"
        f'<span style="{_CONTROL_LABEL_STYLE}">Status:</span>'
        f'<select id="{_html.escape(status_id, quote=True)}" style="{_CONTROL_SELECT_STYLE}">'
        '<option value="all" selected>All</option>'
        '<option value="completed">Completed</option>'
        '<option value="failed">Failed</option>'
        '<option value="active">Active</option>'
        "</select>"
        f'<span style="{_CONTROL_LABEL_STYLE}">Sort:</span>'
        f'<select id="{_html.escape(sort_id, quote=True)}" style="{_CONTROL_SELECT_STYLE}">'
        '<option value="created-desc" selected>Newest</option>'
        '<option value="created-asc">Oldest</option>'
        '<option value="duration-desc">Duration</option>'
        '<option value="errors-desc">Errors</option>'
        '<option value="id-asc">ID</option>'
        "</select>"
        f'<span style="{_CONTROL_LABEL_STYLE}">Show:</span>'
        f'<select id="{_html.escape(show_id, quote=True)}" style="{_CONTROL_SELECT_STYLE}">' + "".join(show_opts) + "</select>"
        "</div>"
    )


def html_table_controls_script(
    *,
    table_id: str,
    view_id: str,
    status_id: str,
    sort_id: str,
    show_id: str,
) -> str:
    """Attach filtering/sorting/show controls to a table.

    Expected row attributes:
    - data-parent: "1" for child rows, "0" for parent rows
    - data-status: one of active/completed/failed
    - data-created-ts: numeric timestamp
    - data-duration-ms: numeric duration
    - data-errors: numeric error count
    - data-id: run ID
    """
    return (
        "<script>"
        "(function(){"
        f"var t=document.getElementById('{_html.escape(table_id, quote=True)}');"
        f"var v=document.getElementById('{_html.escape(view_id, quote=True)}');"
        f"var s=document.getElementById('{_html.escape(status_id, quote=True)}');"
        f"var o=document.getElementById('{_html.escape(sort_id, quote=True)}');"
        f"var l=document.getElementById('{_html.escape(show_id, quote=True)}');"
        "if(!t||!v||!s||!o||!l||t.__hgCtlBound)return;"
        "t.__hgCtlBound=true;"
        "var tb=t.querySelector('tbody'); if(!tb)return;"
        "var num=function(x){var n=parseFloat(x);return isNaN(n)?0:n;};"
        "var rows=function(){"
        "var out=[];"
        "var kids=tb.children;"
        "for(var i=0;i<kids.length;i++){"
        "if((kids[i].tagName||'').toLowerCase()==='tr')out.push(kids[i]);"
        "}"
        "return out;"
        "};"
        "var cmp=function(a,b,mode){"
        "if(mode==='created-asc')return num(a.getAttribute('data-created-ts'))-num(b.getAttribute('data-created-ts'));"
        "if(mode==='created-desc')return num(b.getAttribute('data-created-ts'))-num(a.getAttribute('data-created-ts'));"
        "if(mode==='duration-desc')return num(b.getAttribute('data-duration-ms'))-num(a.getAttribute('data-duration-ms'));"
        "if(mode==='errors-desc')return num(b.getAttribute('data-errors'))-num(a.getAttribute('data-errors'));"
        "if(mode==='id-asc'){var ia=(a.getAttribute('data-id')||'');var ib=(b.getAttribute('data-id')||'');return ia.localeCompare(ib);}"
        "return 0;"
        "};"
        "var apply=function(){"
        "var rv=v.value||'parents';"
        "var rs=s.value||'all';"
        "var ro=o.value||'created-desc';"
        "var rl=l.value||'20';"
        "var all=rows();"
        "for(var i=0;i<all.length;i++){"
        "var r=all[i];"
        "var isChild=(r.getAttribute('data-parent')==='1');"
        "var st=(r.getAttribute('data-status')||'');"
        "var ok=true;"
        "if(rv==='parents'&&isChild)ok=false;"
        "if(rs!=='all'&&st!==rs)ok=false;"
        "r.setAttribute('data-visible',ok?'1':'0');"
        "}"
        "var vis=[];var hid=[];"
        "for(var j=0;j<all.length;j++){if(all[j].getAttribute('data-visible')==='1')vis.push(all[j]);else hid.push(all[j]);}"
        "vis.sort(function(a,b){return cmp(a,b,ro);});"
        "for(var k=0;k<vis.length;k++){tb.appendChild(vis[k]);}"
        "for(var m=0;m<hid.length;m++){tb.appendChild(hid[m]);}"
        "var lim=(rl==='all')?-1:parseInt(rl,10);"
        "var shown=0;var ordered=rows();"
        "for(var n=0;n<ordered.length;n++){"
        "var row=ordered[n];"
        "if(row.getAttribute('data-visible')==='1'){"
        "shown+=1;"
        "row.style.display=(lim<0||shown<=lim)?'table-row':'none';"
        "}else{row.style.display='none';}"
        "}"
        "};"
        "v.addEventListener('change',apply);"
        "s.addEventListener('change',apply);"
        "o.addEventListener('change',apply);"
        "l.addEventListener('change',apply);"
        "apply();"
        "})();"
        "</script>"
    )


def html_filter_paginate_controls(
    *,
    filter_id: str,
    page_size_id: str,
    prev_id: str,
    next_id: str,
    page_info_id: str,
    counts: dict[str, int],
    page_size_options: list[int],
    default_page_size: int,
) -> str:
    """Render reusable filter + pagination controls for list-like widgets."""
    status_order = ["completed", "failed", "active", "paused", "partial", "cached"]
    status_options = []
    for status in status_order:
        n = counts.get(status, 0)
        if n > 0:
            label = status.capitalize()
            status_options.append(f'<option value="{status}">{label} ({n})</option>')

    size_opts = []
    for n in page_size_options:
        selected = " selected" if n == default_page_size else ""
        size_opts.append(f'<option value="{n}"{selected}>{n}</option>')
    size_opts.append(f'<option value="all">All ({counts.get("all", 0)})</option>')

    return (
        '<div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin:6px 0 8px 0">'
        f'<label style="{_CONTROL_LABEL_STYLE}">Filter:</label>'
        f'<select id="{_html.escape(filter_id, quote=True)}" style="{_CONTROL_SELECT_STYLE}">'
        f'<option value="all">All ({counts.get("all", 0)})</option>' + "".join(status_options) + "</select>"
        f'<label style="{_CONTROL_LABEL_STYLE}">Page size:</label>'
        f'<select id="{_html.escape(page_size_id, quote=True)}" style="{_CONTROL_SELECT_STYLE}">' + "".join(size_opts) + "</select>"
        f'<button type="button" id="{_html.escape(prev_id, quote=True)}" style="{_CONTROL_BUTTON_STYLE}">Prev</button>'
        f'<button type="button" id="{_html.escape(next_id, quote=True)}" style="{_CONTROL_BUTTON_STYLE}">Next</button>'
        f'<span id="{_html.escape(page_info_id, quote=True)}" style="{_CONTROL_LABEL_STYLE}">Page 1/1</span>'
        "</div>"
    )


def html_filter_paginate_script(
    *,
    list_id: str,
    item_selector: str,
    status_attr: str,
    filter_id: str,
    page_size_id: str,
    prev_id: str,
    next_id: str,
    page_info_id: str,
    item_display: str = "block",
) -> str:
    """Attach reusable filter + pagination behavior to list items."""
    return (
        "<script>"
        "(function(){"
        f"var box=document.getElementById('{_html.escape(list_id, quote=True)}');"
        f"var sel=document.getElementById('{_html.escape(filter_id, quote=True)}');"
        f"var sizeSel=document.getElementById('{_html.escape(page_size_id, quote=True)}');"
        f"var prev=document.getElementById('{_html.escape(prev_id, quote=True)}');"
        f"var next=document.getElementById('{_html.escape(next_id, quote=True)}');"
        f"var info=document.getElementById('{_html.escape(page_info_id, quote=True)}');"
        "if(!box||!sel||!sizeSel||!prev||!next||!info||box.__hgPagerBound)return;"
        "box.__hgPagerBound=true;"
        "var page=1;"
        f"var q='{_html.escape(item_selector, quote=True)}';"
        "var items=function(){return Array.prototype.slice.call(box.querySelectorAll(q));};"
        "var apply=function(){"
        "var want=sel.value||'all';"
        "var sizeVal=sizeSel.value||'20';"
        "var size=(sizeVal==='all')?-1:parseInt(sizeVal,10);"
        "var all=items();"
        "var filtered=[];"
        "for(var i=0;i<all.length;i++){"
        "var it=all[i];"
        f"var st=(it.getAttribute('{_html.escape(status_attr, quote=True)}')||'');"
        "if(want==='all'||st===want)filtered.push(it);"
        "}"
        "var total=filtered.length;"
        "var pages=(size<0)?1:Math.max(1,Math.ceil(total/size));"
        "if(page>pages)page=pages;if(page<1)page=1;"
        "var start=(size<0)?0:(page-1)*size;"
        "var end=(size<0)?total:(start+size);"
        "for(var j=0;j<all.length;j++){all[j].style.display='none';}"
        "for(var k=start;k<end&&k<total;k++){"
        f"filtered[k].style.display='{_html.escape(item_display, quote=True)}';"
        "}"
        "prev.disabled=(page<=1);"
        "next.disabled=(page>=pages);"
        "if(total===0){info.textContent='0 items';}"
        "else{var a=start+1;var b=Math.min(end,total);info.textContent='Page '+page+'/'+pages+' • showing '+a+'-'+b+' of '+total;}"
        "};"
        "sel.addEventListener('change',function(){page=1;apply();});"
        "sizeSel.addEventListener('change',function(){page=1;apply();});"
        "prev.addEventListener('click',function(){if(page>1){page-=1;apply();}});"
        "next.addEventListener('click',function(){page+=1;apply();});"
        "apply();"
        "})();"
        "</script>"
    )


def html_panel(title: str, body: str) -> str:
    """Render a titled panel wrapper."""
    title_attr = _html.escape(title, quote=True)
    return (
        f'<div style="{_FONT}; {_BORDER}; {_RADIUS}; overflow:hidden; margin:4px 0">'
        f'<div style="background:{_BG_PANEL}; padding:8px 12px; '
        f'border-bottom:1px solid {_BORDER_COLOR}; font-weight:700; color:{_TEXT_STRONG}" data-hg-panel-title="{title_attr}">{title}</div>'
        f'<div style="padding:10px 12px">{body}</div>'
        f"</div>"
    )


def html_kv(label: str, value: str) -> str:
    """Render a key-value pair."""
    return f'<span style="color:{MUTED_COLOR}; font-size:0.85em">{label}:</span> <span style="color:{_TEXT_STRONG}">{value}</span>'


def html_detail(summary: str, content: str, *, state_key: str | None = None, persist: bool = True) -> str:
    """Render a collapsible <details> section."""
    persist_attr = ' data-hg-persist="1"' if persist else ""
    key_attr = f' data-hg-key="{_html.escape(state_key, quote=True)}"' if state_key else ""
    return (
        f'<details style="margin-top:8px"{persist_attr}{key_attr}>'
        f'<summary style="cursor:pointer; color:{_LINK}; font-size:0.9em; '
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
    return f'<code style="{_CODE_STYLE}; color:{_TEXT}">{text}</code>'


def datetime_html(dt) -> str:
    """Format datetime for HTML display."""
    text = format_datetime(dt)
    return f'<span style="color:{MUTED_COLOR}; font-size:0.85em">{text}</span>'


# ---------------------------------------------------------------------------
# Value rendering
# ---------------------------------------------------------------------------

_MAX_VALUE_LEN = 200
_MAX_ITEMS = 8


def _compact_html(value: Any) -> str:
    """Render a single Python value as compact, HTML-safe text."""
    if value is None:
        return f'<span style="color:{MUTED_COLOR}">None</span>'

    if isinstance(value, str):
        if len(value) <= _MAX_VALUE_LEN:
            return _code(_html.escape(repr(value)))
        preview = repr(value[:_MAX_VALUE_LEN])
        return f'{_code(_html.escape(preview) + "…")} <span style="color:{MUTED_COLOR}">(len={len(value)})</span>'

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
        return f'{_code("{" + keys + suffix + "}")} <span style="color:{MUTED_COLOR}">({plural(n, "key")})</span>'

    # list/tuple preview
    if isinstance(value, (list, tuple)):
        n = len(value)
        bracket = "[]" if isinstance(value, list) else "()"
        if n == 0:
            return _code(bracket)
        return f'{_code(bracket[0] + "…" + bracket[1])} <span style="color:{MUTED_COLOR}">({plural(n, "item")})</span>'

    # fallback
    text = repr(value)
    if len(text) > _MAX_VALUE_LEN:
        text = text[:_MAX_VALUE_LEN] + "…"
    return _code(_html.escape(text))


def values_html(values: dict[str, Any], *, max_items: int = _MAX_ITEMS) -> str:
    """Render a dict as a compact key-value table.

    Used for progressive disclosure of RunResult.values, Checkpoint.values, etc.
    Shows first ``max_items`` entries with smart value truncation.
    """
    if not values:
        return f'<span style="color:{MUTED_COLOR}; font-style:italic">no values</span>'
    items = list(values.items())
    rows = [[_code(_html.escape(str(k))), _compact_html(v)] for k, v in items[:max_items]]
    table = html_table(["Key", "Value"], rows)
    if len(items) > max_items:
        table += f'<div style="color:{MUTED_COLOR}; font-size:0.85em; margin-top:4px">… and {plural(len(items) - max_items, "more key")}</div>'
    return table


def error_html(error: BaseException | str | None) -> str:
    """Render an error as styled HTML."""
    if error is None:
        return ""
    text = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    escaped = _html.escape(text)
    return f'<div style="color:{ERROR_COLOR}; {_FONT}; font-size:0.85em; padding:4px 8px; background:{_ERROR_BG}; {_RADIUS}; margin-top:4px"><b>Error:</b> {escaped}</div>'
