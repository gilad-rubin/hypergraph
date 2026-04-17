"""Live, kernel-backed Jupyter widget for graph visualization.

Behavior:
- Ships a single-state payload to the browser (no 2^N precomputed states).
- Expand/collapse and separate_outputs / show_inputs toggles inside the
  iframe post `hypergraph-request-state` messages. The widget host JS
  forwards them to Python via anywidget, Python recomputes the single
  state via `render_graph_single_state`, and pushes the new payload back
  through the `graph_data` trait. The host JS then posts
  `hypergraph-apply-state` into the iframe and viz.js swaps the
  displayed nodes/edges.
- When the notebook is saved and reopened without a kernel, the iframe
  shows the last-synced `graph_data`; clicks show a "start a kernel"
  hint instead of silently doing nothing.
"""

from __future__ import annotations

import html as html_module
from typing import TYPE_CHECKING, Any

import anywidget
import traitlets as t

from hypergraph.viz.html import generate_widget_html
from hypergraph.viz.renderer import render_graph_single_state

if TYPE_CHECKING:
    import networkx as nx


_HOST_ESM = r"""
function render({ model, el }) {
  const width = model.get("width");
  const height = model.get("height");

  // Outer container holds the iframe and inherits the requested size.
  const wrap = document.createElement("div");
  wrap.style.cssText = `
    width: ${width}px; max-width: 100%; height: ${height}px;
    display: block; margin: 0 auto; border-radius: 8px; background: transparent;
  `;

  const iframe = document.createElement("iframe");
  iframe.setAttribute("sandbox", "allow-scripts allow-same-origin allow-popups allow-forms");
  iframe.setAttribute("frameborder", "0");
  iframe.style.cssText = `
    border: none; width: 100%; height: 100%;
    display: block; background: transparent; border-radius: 8px;
  `;
  iframe.setAttribute("srcdoc", model.get("graph_html"));
  wrap.appendChild(iframe);
  el.appendChild(wrap);

  function postToIframe(payload) {
    try {
      if (iframe.contentWindow) {
        iframe.contentWindow.postMessage(payload, "*");
      }
    } catch (_e) {}
  }

  function syncInitialGraphData() {
    // Called once on iframe load. If the notebook is being reopened
    // without a kernel the srcdoc already contains the last-synced
    // graph_data; we still post it to keep behavior consistent with
    // kernel-driven updates.
    const gd = model.get("graph_data");
    if (!gd) return;
    postToIframe({ type: "hypergraph-apply-state", graphData: gd });
  }

  iframe.addEventListener("load", function () {
    setTimeout(syncInitialGraphData, 50);
  });

  function onWindowMessage(ev) {
    if (!ev.data || ev.source !== iframe.contentWindow) return;
    if (ev.data.type !== "hypergraph-request-state") return;
    model.set("display_state_request", {
      requestId: ev.data.requestId || 0,
      displayState: ev.data.displayState || {},
      stamp: Date.now(),
    });
    model.save_changes();
  }

  window.addEventListener("message", onWindowMessage);

  // Python replies on `display_state_response`, carrying the same
  // requestId so viz.js can clear its pending timer and drop the
  // kernel-needed hint.
  model.on("change:display_state_response", function () {
    const resp = model.get("display_state_response");
    if (!resp || !resp.graphData) return;
    postToIframe({
      type: "hypergraph-apply-state",
      graphData: resp.graphData,
      requestId: resp.requestId,
    });
  });

  return function () {
    window.removeEventListener("message", onWindowMessage);
  };
}

export default { render };
"""


class HypergraphWidget(anywidget.AnyWidget):
    """Live widget that recomputes expansion/toggle state in Python on demand.

    Payload size scales with the current graph, not with the number of
    expandable containers — the 2^N precomputed state explosion that
    bloated saved notebooks is gone.
    """

    _esm = _HOST_ESM

    graph_html = t.Unicode("").tag(sync=True)
    graph_data = t.Dict().tag(sync=True)
    display_state_request = t.Dict(default_value={}).tag(sync=True)
    display_state_response = t.Dict(default_value={}).tag(sync=True)
    width = t.Int(900).tag(sync=True)
    height = t.Int(600).tag(sync=True)

    def __init__(
        self,
        flat_graph: nx.DiGraph,
        *,
        depth: int = 0,
        theme: str = "auto",
        show_types: bool = True,
        separate_outputs: bool = False,
        show_inputs: bool = True,
        show_bounded_inputs: bool = False,
        debug_overlays: bool = False,
        width: int = 900,
        height: int = 600,
    ):
        self._flat_graph = flat_graph
        self._render_opts = {
            "theme": theme,
            "show_types": show_types,
            "show_bounded_inputs": show_bounded_inputs,
            "debug_overlays": debug_overlays,
        }

        initial_data = render_graph_single_state(
            flat_graph,
            depth=depth,
            separate_outputs=separate_outputs,
            show_inputs=show_inputs,
            live_mode=True,
            **self._render_opts,
        )

        super().__init__(
            graph_html=generate_widget_html(initial_data),
            graph_data=initial_data,
            width=width,
            height=height,
        )

    def _repr_html_(self) -> str:
        """HTML fallback for environments that cannot render anywidget.

        Emits the same iframe-with-srcdoc form the old widget used so a
        user loading the notebook as static HTML still sees the initial
        visualization.
        """
        escaped = html_module.escape(self.graph_html, quote=True)
        return (
            f'<iframe srcdoc="{escaped}" '
            f'width="{self.width}" height="{self.height}" frameborder="0" '
            f'style="border: none; width: {self.width}px; max-width: 100%; '
            f"height: {self.height}px; display: block; background: transparent; "
            f'margin: 0 auto; border-radius: 8px;" '
            f'sandbox="allow-scripts allow-same-origin allow-popups allow-forms">'
            f"</iframe>"
        )

    @t.observe("display_state_request")
    def _on_display_state_request(self, change: Any) -> None:  # type: ignore[override]
        req = change["new"] or {}
        if not req:
            return
        display_state = req.get("displayState") or {}
        request_id = req.get("requestId") or 0

        expansion = {str(k): bool(v) for k, v in (display_state.get("expansion") or {}).items()}
        separate_outputs = bool(display_state.get("separate_outputs", self.graph_data.get("meta", {}).get("separate_outputs", False)))
        show_inputs = bool(display_state.get("show_inputs", self.graph_data.get("meta", {}).get("show_inputs", True)))

        new_data = render_graph_single_state(
            self._flat_graph,
            expansion_state=expansion,
            separate_outputs=separate_outputs,
            show_inputs=show_inputs,
            live_mode=True,
            **self._render_opts,
        )
        self.graph_data = new_data
        self.display_state_response = {"requestId": request_id, "graphData": new_data}
