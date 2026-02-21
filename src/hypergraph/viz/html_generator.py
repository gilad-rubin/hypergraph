"""
HTML generator for Hypergraph visualization.

Generates a standalone HTML document with embedded React Flow visualization.
All JavaScript modules are loaded from bundled assets in viz/assets/.
"""
import json
from importlib.resources import files
from typing import Any, Dict, Optional


def _escape_json_for_html(json_str: str) -> str:
    """Escape JSON for safe embedding in HTML script tags.

    Prevents XSS by escaping </ sequences that could break out of script tags.
    """
    return json_str.replace("</", "<\\/")


def _validate_graph_data(graph_data: Dict[str, Any]) -> None:
    """Lightweight validation of the renderer payload."""
    if not isinstance(graph_data, dict):
        raise ValueError("graph_data must be a dict")

    required_keys = ("nodes", "edges", "meta")
    missing = [key for key in required_keys if key not in graph_data]
    if missing:
        raise ValueError(f"graph_data missing keys: {missing}")

    nodes = graph_data.get("nodes", [])
    edges = graph_data.get("edges", [])

    if not isinstance(nodes, list):
        raise ValueError("graph_data['nodes'] must be a list")
    if not isinstance(edges, list):
        raise ValueError("graph_data['edges'] must be a list")

    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("node entries must be dicts")
        if "id" not in node:
            raise ValueError("node missing 'id'")
        if "data" not in node:
            raise ValueError(f"node {node.get('id')} missing 'data'")

    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError("edge entries must be dicts")
        for key in ("id", "source", "target"):
            if key not in edge:
                raise ValueError(f"edge missing '{key}'")


def generate_widget_html(graph_data: Dict[str, Any]) -> str:
    """Generate an HTML document for React Flow rendering.

    All JS/CSS assets are bundled within the package (hypergraph.viz.assets).
    No external CDN dependencies are required - works fully offline.
    """
    _validate_graph_data(graph_data)
    graph_json = _escape_json_for_html(json.dumps(graph_data))

    def _read_asset(name: str, kind: str) -> Optional[str]:
        """Read an asset file from the bundled package resources.

        Assets are located in hypergraph/viz/assets/ which is included in the wheel.
        Uses importlib.resources for reliable access in installed packages.
        """
        try:
            asset_files = files("hypergraph.viz.assets")
            text = (asset_files / name).read_text(encoding="utf-8")
            if kind == "js":
                return f"<script>{text}</script>"
            if kind == "css":
                return f"<style>{text}</style>"
            return text
        except Exception:
            return None

    # Load library assets
    react_js = _read_asset("react.production.min.js", "js")
    react_dom_js = _read_asset("react-dom.production.min.js", "js")
    htm_js = _read_asset("htm.min.js", "js")
    kiwi_js = _read_asset("kiwi.bundled.js", "js")
    dagre_js = _read_asset("dagre.min.js", "js")
    constants_js = _read_asset("constants.js", "js")
    constraint_layout_js = _read_asset("layout-engine.js", "js")
    rf_js = _read_asset("reactflow.umd.js", "js")
    rf_css = _read_asset("reactflow.css", "css")
    tailwind_css = _read_asset("tailwind.min.css", "css")
    custom_css = _read_asset("custom.css", "css") or ""

    # Load our visualization modules
    theme_utils_js = _read_asset("theme_utils.js", "js")
    components_js = _read_asset("components.js", "js")
    app_js = _read_asset("app.js", "js")

    # Check that all required assets are available
    required_library_assets = [
        react_js, react_dom_js, htm_js, kiwi_js, dagre_js, constants_js,
        constraint_layout_js, rf_js, rf_css, tailwind_css
    ]
    required_app_assets = [
        theme_utils_js, components_js, app_js
    ]

    if not all(required_library_assets):
        missing = []
        asset_names = [
            "react", "react-dom", "htm", "kiwi", "dagre", "constants",
            "layout-engine", "reactflow.js", "reactflow.css", "tailwind.css"
        ]
        for asset, name in zip(required_library_assets, asset_names):
            if not asset:
                missing.append(name)
        raise RuntimeError(
            f"Missing bundled visualization library assets: {missing}. "
            "The hypergraph package may be incorrectly installed. "
            "Try reinstalling with: pip install --force-reinstall hypergraph"
        )

    if not all(required_app_assets):
        missing = []
        asset_names = [
            "theme_utils.js", "components.js", "app.js"
        ]
        for asset, name in zip(required_app_assets, asset_names):
            if not asset:
                missing.append(name)
        raise RuntimeError(
            f"Missing bundled visualization module assets: {missing}. "
            "The hypergraph package may be incorrectly installed. "
            "Try reinstalling with: pip install --force-reinstall hypergraph"
        )

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <!-- All assets are bundled - no external CDN dependencies -->
    {tailwind_css}
    {rf_css}
    {custom_css}
    <style>
        /* Reset and Base Styles */
        body {{ margin: 0; overflow: hidden; background: transparent; color: #e5e7eb; font-family: 'Inter', system-ui, -apple-system, sans-serif; }}
        .react-flow__attribution {{ display: none; }}
        #root {{ height: 100vh; width: 100vw; background: transparent; display: flex; align-items: center; justify-content: center; }}
        #fallback {{ font-size: 13px; letter-spacing: 0.4px; color: #94a3b8; }}

        /* Canvas Outline */
        .canvas-outline {{
            outline: 1px dashed rgba(148, 163, 184, 0.2);
            margin: 2px;
            height: calc(100vh - 4px);
            width: calc(100vw - 4px);
            border-radius: 8px;
            pointer-events: none;
            position: absolute;
            top: 0;
            left: 0;
            z-index: 50;
        }}

        /* Function Node Light Mode Fix */
        .node-function-light {{
            border-bottom-width: 1px !important; /* Prevent artifact */
        }}
    </style>
    <!-- Bundled JavaScript libraries -->
    {react_js}
    {react_dom_js}
    {htm_js}
    {kiwi_js}
    {dagre_js}
    {constants_js}
    {rf_js}
    <!-- Hypergraph visualization modules -->
    {theme_utils_js}
    {constraint_layout_js}
    {components_js}
    {app_js}
</head>
<body>
  <div id="root">
    <div id="fallback">Rendering interactive view…</div>
  </div>
  <div class="canvas-outline"></div>
  <script>
    window.onerror = function(message, source, lineno, colno, error) {{
      var el = document.getElementById("fallback");
      if (el) {{
        el.textContent = "Viz error: " + message + (source ? " (" + source + ":" + lineno + ")" : "");
        el.style.color = "#f87171";
        el.style.fontFamily = "monospace";
      }}
    }};
  </script>
  <script>
    // Wait for DOM to be fully loaded before executing
    document.addEventListener('DOMContentLoaded', function() {{
      // Keep-alive mechanism to prevent iframe cleanup in some environments
      setInterval(function() {{
        try {{
          document.documentElement.dataset.lastPing = Date.now();
        }} catch(e) {{}}
      }}, 5000);

      // Check all required modules are loaded
      var requiredModules = [
        'React', 'ReactDOM', 'ReactFlow', 'htm', 'ConstraintLayout',
        'HypergraphVizTheme', 'HypergraphVizLayout',
        'HypergraphVizComponents', 'HypergraphVizApp'
      ];
      var missing = requiredModules.filter(function(m) {{ return !window[m]; }});

      if (missing.length > 0) {{
        var el = document.getElementById("fallback");
        if (el) {{
          el.innerHTML = '<div style="display: flex; flex-direction: column; gap: 8px; max-width: 80%;">' +
            '<div style="color: #f87171; font-family: monospace; user-select: text; background: #2a1b1b; padding: 12px; border-radius: 4px;">Missing modules: ' + missing.join(', ') + '</div>' +
            '<button onclick="window.location.reload()" style="padding: 6px 12px; background: #2563eb; border: none; color: white; border-radius: 4px; cursor: pointer; align-self: flex-start;">Reload</button>' +
          '</div>';
        }}
        return;
      }}

      // Initialize the application
      try {{
        window.HypergraphVizApp.init();
      }} catch(err) {{
        console.error('Visualization initialization error:', err);
        var el = document.getElementById("fallback");
        if (el) {{
          el.innerHTML = '<div style="display: flex; flex-direction: column; gap: 8px; max-width: 80%;">' +
            '<div style="color: #f87171; font-family: monospace; user-select: text; background: #2a1b1b; padding: 12px; border-radius: 4px;">' + (err && err.message ? err.message : err) + '</div>' +
            '<button onclick="navigator.clipboard.writeText(this.previousElementSibling.innerText)" style="padding: 4px 8px; background: #374151; border: none; color: white; border-radius: 4px; cursor: pointer; align-self: flex-start;">Copy Error</button>' +
            '<button onclick="window.location.reload()" style="margin-top: 8px; padding: 6px 12px; background: #2563eb; border: none; color: white; border-radius: 4px; cursor: pointer; align-self: flex-start;">Reload</button>' +
          '</div>';
        }}
      }}
    }});
  </script>
  <script id="graph-data" type="application/json">{graph_json}</script>
</body>
</html>"""
