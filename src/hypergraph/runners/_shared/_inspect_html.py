"""Safe settled HTML presentation for typed inspection artifacts."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any

from hypergraph.runners._shared._inspect import MapInspection, RunInspection


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


def _render_mapping(
    values: dict[str, Any] | None,
    *,
    values_captured: bool,
    restored: bool,
) -> str:
    if values is None:
        if restored and not values_captured:
            message = "restored values not captured"
        else:
            message = "not captured; rerun with inspect=True" if not values_captured else "not available"
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
            f"{_render_mapping(node.inputs, values_captured=node.values_captured, restored=node.status == 'restored')}</section>"
            "<section><h4>Outputs</h4>"
            f"{_render_mapping(node.outputs, values_captured=node.values_captured, restored=node.status == 'restored')}</section>"
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


def render_map_inspection(artifact: MapInspection) -> str:
    """Render one batch artifact with original-index child drill-down."""
    item_sections: list[str] = []
    for item in artifact.items:
        requested = _render_mapping(
            item.requested_inputs,
            values_captured=artifact.captured,
            restored=False,
        )
        run = render_run_inspection(item.run) if item.run is not None else '<p class="hg-inspect-missing">run has not published yet</p>'
        item_sections.append(
            '<details class="hg-inspect-map-item">'
            f"<summary><strong>Item {item.item_index}</strong> "
            f'<span data-status="{html.escape(item.status)}">'
            f"{html.escape(item.status)}</span></summary>"
            "<section><h4>Requested map inputs</h4>"
            f"{requested}</section>{run}</details>"
        )

    unstarted = ""
    if artifact.unstarted_item_indexes:
        indexes = ", ".join(str(index) for index in artifact.unstarted_item_indexes)
        unstarted = f'<p class="hg-inspect-unstarted">Unstarted item indexes: {html.escape(indexes)}</p>'
    return (
        '<div class="hg-inspect hg-inspect-map" data-hypergraph-inspect="map">'
        "<header>"
        f"<h3>{html.escape(artifact.graph_name or 'Hypergraph map')}</h3>"
        f"<p>{html.escape(artifact.status)} · "
        f"{html.escape(artifact.run_id or 'no run id')} · "
        f"requested {artifact.requested_count} · completed {artifact.completed_count} · "
        f"failed {artifact.failed_count} · restored {artifact.restored_count} · "
        f"unstarted {artifact.unstarted_count}</p>"
        f"{unstarted}</header>{''.join(item_sections)}"
        "</div>"
    )


@dataclass(frozen=True, slots=True)
class InspectionDisplay:
    """Explicit rich display returned by run and map result inspection."""

    artifact: RunInspection | MapInspection

    def _repr_html_(self) -> str:
        if isinstance(self.artifact, MapInspection):
            return render_map_inspection(self.artifact)
        return render_run_inspection(self.artifact)
