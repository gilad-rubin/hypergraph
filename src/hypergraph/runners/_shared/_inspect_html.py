"""Safe settled HTML presentation for typed inspection artifacts."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any

from hypergraph.runners._shared._inspect import RunInspection


def _safe_repr(value: Any, *, limit: int = 20_000) -> str:
    try:
        rendered = repr(value)
    except Exception as error:
        rendered = f"<unrenderable {type(value).__name__}: {type(error).__name__}>"
    if len(rendered) > limit:
        return f"{rendered[: limit - 1]}… <truncated; original characters={len(rendered)}>"
    return rendered


def _safe_str(value: Any) -> str:
    try:
        return str(value)
    except Exception as error:
        return f"<unrenderable {type(value).__name__}: {type(error).__name__}>"


def _render_mapping(values: dict[str, Any] | None, *, captured: bool) -> str:
    if values is None:
        message = "not captured; rerun with inspect=True" if not captured else "not available"
        return f'<p class="hg-inspect-missing">{html.escape(message)}</p>'
    rows = "".join(
        f"<tr><th>{html.escape(str(name))}</th><td><code>{html.escape(_safe_repr(value))}</code></td></tr>" for name, value in values.items()
    )
    return f'<table class="hg-inspect-values"><tbody>{rows}</tbody></table>'


def render_run_inspection(artifact: RunInspection) -> str:
    """Render one run artifact without executing or trusting captured values."""
    node_sections: list[str] = []
    for node in artifact.nodes:
        status = html.escape(node.status)
        failure_html = ""
        if node.failure is not None:
            failure_html = (
                '<section class="hg-inspect-error"><h4>Error</h4>'
                f"<code>{html.escape(type(node.failure.error).__name__)}: "
                f"{html.escape(_safe_str(node.failure.error))}</code></section>"
            )
        node_sections.append(
            '<details class="hg-inspect-node" open>'
            "<summary>"
            f"<strong>{html.escape(node.qualified_name)}</strong> "
            f'<span data-status="{status}">{status}</span> '
            f"<span>{node.duration_ms:.3f} ms</span>"
            f"{' <span>cached</span>' if node.cached else ''}"
            "</summary>"
            '<div class="hg-inspect-grid">'
            "<section><h4>Inputs</h4>"
            f"{_render_mapping(node.inputs, captured=artifact.captured)}</section>"
            "<section><h4>Outputs</h4>"
            f"{_render_mapping(node.outputs, captured=artifact.captured)}</section>"
            "</div>"
            f"{failure_html}"
            "</details>"
        )

    captured_note = (
        "Captured values are shallow snapshots. Treat saved output as sensitive."
        if artifact.captured
        else "Successful-node values were not captured; rerun with inspect=True."
    )
    return (
        '<div class="hg-inspect" data-hypergraph-inspect="run">'
        "<style>"
        ".hg-inspect{font:14px/1.45 system-ui,sans-serif;color:inherit;border:1px solid #8884;border-radius:10px;overflow:hidden}"
        ".hg-inspect header,.hg-inspect footer{padding:12px 14px;background:#8881}"
        ".hg-inspect h3,.hg-inspect h4,.hg-inspect p{margin:0}"
        ".hg-inspect-node{padding:10px 14px;border-top:1px solid #8883}"
        ".hg-inspect-node summary{cursor:pointer;display:flex;gap:8px;align-items:center}"
        ".hg-inspect-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:10px}"
        ".hg-inspect-values{width:100%;border-collapse:collapse}.hg-inspect-values th,.hg-inspect-values td{text-align:left;vertical-align:top;padding:4px;border-top:1px solid #8882}"
        ".hg-inspect-values code{white-space:pre-wrap;overflow-wrap:anywhere}.hg-inspect-missing{opacity:.65}"
        "@media(max-width:600px){.hg-inspect-grid{grid-template-columns:1fr}}"
        "</style>"
        "<header>"
        f"<h3>{html.escape(artifact.graph_name or 'Hypergraph run')}</h3>"
        f"<p>{html.escape(artifact.status)} · {html.escape(artifact.run_id)} · "
        f"{artifact.total_duration_ms:.3f} ms</p>"
        "</header>"
        f"{''.join(node_sections)}"
        f"<footer><p>{html.escape(captured_note)}</p></footer>"
        "</div>"
    )


@dataclass(frozen=True, slots=True)
class InspectionDisplay:
    """Explicit rich display value returned by ``RunResult.inspect()``."""

    artifact: RunInspection

    def _repr_html_(self) -> str:
        return render_run_inspection(self.artifact)
