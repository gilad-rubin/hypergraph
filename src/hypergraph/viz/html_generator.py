import json
from importlib.resources import files
from typing import Any, Dict, Optional


def generate_widget_html(graph_data: Dict[str, Any]) -> str:
    """Generate an HTML document for React Flow rendering.

    All JS/CSS assets are bundled within the package (hypergraph.viz.assets).
    No external CDN dependencies are required - works fully offline.
    """

    graph_json = json.dumps(graph_data)

    def _read_asset(name: str, kind: str) -> Optional[str]:
        """Read an asset file from the bundled package resources.
        
        Assets are located in hypergraph/viz/assets/ which is included in the wheel.
        Uses importlib.resources for reliable access in installed packages.
        """
        try:
            # Access assets from the installed package using importlib.resources
            asset_files = files("hypergraph.viz.assets")
            text = (asset_files / name).read_text(encoding="utf-8")
            if kind == "js":
                return f"<script>{text}</script>"
            if kind == "css":
                return f"<style>{text}</style>"
            return text
        except Exception:
            return None

    # Load all bundled assets
    react_js = _read_asset("react.production.min.js", "js")
    react_dom_js = _read_asset("react-dom.production.min.js", "js")
    htm_js = _read_asset("htm.min.js", "js")
    kiwi_js = _read_asset("kiwi.bundled.js", "js")
    constraint_layout_js = _read_asset("constraint-layout.js", "js")
    rf_js = _read_asset("reactflow.umd.js", "js")
    rf_css = _read_asset("reactflow.css", "css")
    tailwind_css = _read_asset("tailwind.min.css", "css")

    # If local assets are missing, keep a minimal external fallback.
    # Check that all required assets are available
    required_assets = [react_js, react_dom_js, htm_js, kiwi_js, constraint_layout_js, rf_js, rf_css, tailwind_css]
    if not all(required_assets):
        missing = []
        asset_names = ["react", "react-dom", "htm", "kiwi", "constraint-layout", "reactflow.js", "reactflow.css", "tailwind.css"]
        for asset, name in zip(required_assets, asset_names):
            if not asset:
                missing.append(name)
        raise RuntimeError(
            f"Missing bundled visualization assets: {missing}. "
            "The hypergraph package may be incorrectly installed. "
            "Try reinstalling with: pip install --force-reinstall hypergraph"
        )

    # Build HTML header with Python string interpolation
    html_head = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <!-- All assets are bundled - no external CDN dependencies -->
    {tailwind_css}
    {rf_css}
    {_read_asset("custom.css", "css") or ""}
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
    {constraint_layout_js}
    {rf_js}
</head>"""

    # JavaScript body
    html_body = r"""<body>
  <div id="root">
    <div id="fallback">Rendering interactive view…</div>
  </div>
  <div class="canvas-outline"></div>
  <script>
    window.onerror = function(message, source, lineno, colno, error) {
      const el = document.getElementById("fallback");
      if (el) {
        el.textContent = "Viz error: " + message + (source ? " (" + source + ":" + lineno + ")" : "");
        el.style.color = "#f87171";
        el.style.fontFamily = "monospace";
      }
    };
  </script>
  <script>
    // Wait for DOM to be fully loaded before executing
    // This replaces type="module" which auto-defers, for better VSCode notebook iframe compatibility
    document.addEventListener('DOMContentLoaded', function() {
    (function() {
    'use strict';
    const fallback = document.getElementById("fallback");
    const fail = (msg) => {
      if (fallback) {
        fallback.innerHTML = `
            <div style="display: flex; flex-direction: column; gap: 8px; max-width: 80%;">
                <div style="color: #f87171; font-family: monospace; user-select: text; background: #2a1b1b; padding: 12px; rounded: 4px;">${msg}</div>
                <button onclick="navigator.clipboard.writeText(this.previousElementSibling.innerText)" style="padding: 4px 8px; background: #374151; border: none; color: white; border-radius: 4px; cursor: pointer; align-self: flex-start;">Copy Error</button>
                <button onclick="window.location.reload()" style="margin-top: 8px; padding: 6px 12px; background: #2563eb; border: none; color: white; border-radius: 4px; cursor: pointer; align-self: flex-start;">Retry Visualization</button>
            </div>
        `;
      }
    };

    // Keep-alive mechanism to prevent iframe cleanup in some environments
    setInterval(() => {
      try {
        // Minimal DOM interaction to keep the context alive
        document.documentElement.dataset.lastPing = Date.now();
      } catch(e) {}
    }, 5000);

    try {
      const React = window.React;
      const ReactDOM = window.ReactDOM;
      const RF = window.ReactFlow;
      const htm = window.htm;
      const ConstraintLayout = window.ConstraintLayout;

      if (!React || !ReactDOM || !RF || !htm || !ConstraintLayout) {
        throw new Error("Missing globals: " + JSON.stringify({
          React: !!React, ReactDOM: !!ReactDOM, ReactFlow: !!RF, htm: !!htm, ConstraintLayout: !!ConstraintLayout
        }));
      }

      const { ReactFlow, Background, Controls, MiniMap, Handle, Position, ReactFlowProvider, useEdgesState, useNodesState, MarkerType, BaseEdge, getBezierPath, getSmoothStepPath, EdgeLabelRenderer, useReactFlow, Panel, useUpdateNodeInternals } = RF;
      const { useState, useEffect, useMemo, useCallback, useRef } = React;

      const html = htm.bind(React.createElement);
      
      // === LAYOUT CONSTANTS ===
      // Truncation limit for type hints (both display and width calculation)
      const TYPE_HINT_MAX_CHARS = 25;
      // Truncation limit for node labels (function names, pipeline names)
      const NODE_LABEL_MAX_CHARS = 25;
      // Character width estimate for monospace font (text-xs ~12px)
      const CHAR_WIDTH_PX = 7;
      // Base padding for nodes: px-3 (12px) * 2 + icon (12px) + gaps (16px) = 52px
      const NODE_BASE_PADDING = 52;
      // Base padding for function nodes (now simpler without FUNCTION label)
      const FUNCTION_NODE_BASE_PADDING = 48;
      // Maximum node width to ensure uniform appearance
      const MAX_NODE_WIDTH = 280;
      
      // Helper to truncate type hints consistently
      const truncateTypeHint = (type) => type && type.length > TYPE_HINT_MAX_CHARS 
        ? type.substring(0, TYPE_HINT_MAX_CHARS) + '...' 
        : type;
      
      // Helper to truncate node labels consistently
      const truncateLabel = (label) => label && label.length > NODE_LABEL_MAX_CHARS 
        ? label.substring(0, NODE_LABEL_MAX_CHARS) + '...' 
        : label;
      
      const fallbackApplyState = (baseNodes, baseEdges, options) => {
        const { expansionState, separateOutputs, showTypes, theme } = options;
        const expMap = expansionState instanceof Map ? expansionState : new Map(Object.entries(expansionState || {}));

        // Identify DATA nodes (outputs) by their sourceId property
        const dataNodeIds = new Set(baseNodes.filter(n => n.data?.sourceId).map(n => n.id));
        // Identify INPUT_GROUP nodes
        const inputGroupIds = new Set(baseNodes.filter(n => n.data?.nodeType === 'INPUT_GROUP').map(n => n.id));

        // Build function→output mapping for embedding outputs in function nodes
        const functionOutputs = {};
        baseNodes.forEach(n => {
          if (n.data?.sourceId) {
            if (!functionOutputs[n.data.sourceId]) functionOutputs[n.data.sourceId] = [];
            functionOutputs[n.data.sourceId].push({ name: n.data.label, type: n.data.typeHint });
          }
        });

        const applyMeta = (n) => {
          const isPipeline = n.data?.nodeType === 'PIPELINE';
          const expanded = isPipeline ? Boolean(expMap.get(n.id)) : undefined;
          return {
            ...n,
            type: isPipeline && expanded ? 'pipelineGroup' : n.type,
            style: isPipeline && !expanded ? undefined : n.style,
            data: {
              ...n.data,
              theme,
              showTypes,
              isExpanded: expanded,
            },
          };
        };

        if (separateOutputs) {
          // Show DATA nodes and INPUT_GROUP, clear embedded outputs from function nodes
          const nodes = baseNodes.map(n => {
            const transformed = applyMeta(n);
            return {
              ...transformed,
              data: {
                ...transformed.data,
                separateOutputs: true,
                outputs: [],  // Clear embedded outputs when showing separate DATA nodes
              },
            };
          });
          return { nodes, edges: baseEdges };
        } else {
          // Hide DATA nodes (but keep INPUT_GROUP visible), embed outputs in function nodes, remap edges
          const nodes = baseNodes
            .filter(n => !dataNodeIds.has(n.id))  // Remove DATA nodes only, keep INPUT_GROUP
            .map(n => {
              const transformed = applyMeta(n);
              return {
                ...transformed,
                data: {
                  ...transformed.data,
                  separateOutputs: false,
                  outputs: functionOutputs[n.id] || [],  // Embed outputs in function nodes
                },
              };
            });

          // Remap edges to skip DATA nodes (but keep INPUT_GROUP edges)
          const edges = baseEdges
            .filter(e => !dataNodeIds.has(e.target))  // Remove edges TO DATA nodes only
            .map(e => {
              if (dataNodeIds.has(e.source)) {
                // Edge FROM DATA node → remap to source function node
                const dataNode = baseNodes.find(n => n.id === e.source);
                if (dataNode?.data?.sourceId) {
                  return {
                    ...e,
                    id: `e_${dataNode.data.sourceId}_${e.target}`,
                    source: dataNode.data.sourceId,
                  };
                }
              }
              return e;
            });

          return { nodes, edges };
        }
      };

      const fallbackApplyVisibility = (nodes, expansionState) => {
        const expMap = expansionState instanceof Map ? expansionState : new Map(Object.entries(expansionState || {}));
        const parentMap = new Map();
        nodes.forEach((n) => {
          if (n.parentNode) parentMap.set(n.id, n.parentNode);
        });

        const isHidden = (nodeId) => {
          let curr = nodeId;
          while (curr) {
            const parent = parentMap.get(curr);
            if (!parent) return false;
            if (expMap.get(parent) === false) return true;
            curr = parent;
          }
          return false;
        };

        return nodes.map((n) => ({ ...n, hidden: isHidden(n.id) }));
      };

      // --- Icons ---
      const Icons = {
        Moon: () => html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>`,
        Sun: () => html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line></svg>`,
        ZoomIn: () => html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="8" y1="11" x2="14" y2="11"/></svg>`,
        ZoomOut: () => html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="8" y1="11" x2="14" y2="11"/></svg>`,
        Center: () => html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>`,
        Function: () => html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><rect x="2" y="2" width="20" height="20" rx="2.18" ry="2.18"></rect><line x1="7" y1="2" x2="7" y2="22"></line><line x1="17" y1="2" x2="17" y2="22"></line><line x1="2" y1="12" x2="22" y2="12"></line></svg>`,
        Pipeline: () => html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><polygon points="12 2 2 7 12 12 22 7 12 2"></polygon><polyline points="2 17 12 22 22 17"></polyline><polyline points="2 12 12 17 22 12"></polyline></svg>`,
        Dual: () => html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><path d="M12 2a10 10 0 1 0 10 10H12V2z"></path><path d="M12 12L2 12"></path><path d="M12 12L12 22"></path></svg>`,
        Branch: () => html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><path d="M6 3v12"></path><circle cx="18" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><path d="M18 9a9 9 0 0 1-9 9"></path></svg>`,
        Input: () => html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="16"></line><line x1="8" y1="12" x2="16" y2="12"></line></svg>`,
        Data: () => html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-3 h-3"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><line x1="10" y1="9" x2="8" y2="9"></line></svg>`,
        Map: () => html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6"></polygon><line x1="8" y1="2" x2="8" y2="18"></line><line x1="16" y1="6" x2="16" y2="22"></line></svg>`,
        Bug: () => html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><path d="m8 2 1.88 1.88"></path><path d="M14.12 3.88 16 2"></path><path d="M9 7.13v-1a3.003 3.003 0 1 1 6 0v1"></path><path d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6"></path><path d="M12 20v-9"></path><path d="M6.53 9C4.6 8.8 3 7.1 3 5"></path><path d="M6 13H2"></path><path d="M3 21c0-2.1 1.7-3.9 3.8-4"></path><path d="M20.97 5c0 2.1-1.6 3.8-3.5 4"></path><path d="M22 13h-4"></path><path d="M17.2 17c2.1.1 3.8 1.9 3.8 4"></path></svg>`,
        // Split outputs icon (arrows diverging)
        SplitOutputs: () => html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><path d="M16 3h5v5"></path><path d="M8 3H3v5"></path><path d="M12 22v-8.3a4 4 0 0 0-1.172-2.872L3 3"></path><path d="m15 9 6-6"></path></svg>`,
        // Merge outputs icon (arrows converging)
        MergeOutputs: () => html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><path d="M8 3H3v5"></path><path d="m3 3 5.586 5.586a2 2 0 0 1 .586 1.414V22"></path><path d="M16 3h5v5"></path><path d="m21 3-5.586 5.586a2 2 0 0 0-.586 1.414V22"></path></svg>`,
        // Type icon (T with bracket)
        Type: () => html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><polyline points="4 7 4 4 20 4 20 7"></polyline><line x1="9" y1="20" x2="15" y2="20"></line><line x1="12" y1="4" x2="12" y2="20"></line></svg>`
      };

      // --- Tooltip Button Component ---
      const TooltipButton = ({ onClick, tooltip, isActive, theme, children }) => {
        const [showTooltip, setShowTooltip] = useState(false);
        const isLight = theme === 'light';
        
        const btnClass = `p-2 rounded-lg shadow-lg border transition-all duration-200 ${
            isLight 
            ? 'bg-white border-slate-200 text-slate-600 hover:bg-slate-50 hover:text-slate-900' 
            : 'bg-slate-900 border-slate-700 text-slate-400 hover:bg-slate-800 hover:text-slate-100'
        }`;
        const activeClass = isLight ? 'bg-slate-100 text-indigo-600' : 'bg-slate-800 text-indigo-400';
        const tooltipClass = isLight 
            ? 'bg-slate-800 text-white' 
            : 'bg-white text-slate-800';
        
        return html`
            <div className="relative" onMouseEnter=${() => setShowTooltip(true)} onMouseLeave=${() => setShowTooltip(false)}>
                <button className=${`${btnClass} ${isActive ? activeClass : ''}`} onClick=${onClick}>
                    ${children}
                </button>
                ${showTooltip && html`
                    <div className=${`absolute right-full mr-2 top-1/2 -translate-y-1/2 px-2 py-1 text-xs font-medium rounded shadow-lg whitespace-nowrap pointer-events-none z-50 ${tooltipClass}`}>
                        ${tooltip}
                        <div className=${`absolute left-full top-1/2 -translate-y-1/2 border-4 border-transparent ${isLight ? 'border-l-slate-800' : 'border-l-white'}`}></div>
                    </div>
                `}
            </div>
        `;
      };

      // --- Custom Controls ---
      const CustomControls = ({ theme, onToggleTheme, separateOutputs, onToggleSeparate, showTypes, onToggleTypes, onFitView }) => {
        const { zoomIn, zoomOut } = useReactFlow();

        return html`
            <${Panel} position="bottom-right" className="flex flex-col gap-2 pb-4 mr-6">
                <${TooltipButton} onClick=${() => zoomIn()} tooltip="Zoom In" theme=${theme}>
                    <${Icons.ZoomIn} />
                <//>
                <${TooltipButton} onClick=${() => zoomOut()} tooltip="Zoom Out" theme=${theme}>
                    <${Icons.ZoomOut} />
                <//>
                <${TooltipButton} onClick=${onFitView} tooltip="Fit View" theme=${theme}>
                    <${Icons.Center} />
                <//>
                <div className=${`h-px my-1 ${theme === 'light' ? 'bg-slate-200' : 'bg-slate-700'}`}></div>
                <${TooltipButton} onClick=${onToggleSeparate} tooltip=${separateOutputs ? "Merge Outputs" : "Separate Outputs"} isActive=${separateOutputs} theme=${theme}>
                    ${separateOutputs ? html`<${Icons.MergeOutputs} />` : html`<${Icons.SplitOutputs} />`}
                <//>
                <${TooltipButton} onClick=${onToggleTypes} tooltip=${showTypes ? "Hide Types" : "Show Types"} isActive=${showTypes} theme=${theme}>
                    <${Icons.Type} />
                <//>
                <div className=${`h-px my-1 ${theme === 'light' ? 'bg-slate-200' : 'bg-slate-700'}`}></div>
                <${TooltipButton} onClick=${onToggleTheme} tooltip=${theme === 'dark' ? "Switch to Light Theme" : "Switch to Dark Theme"} theme=${theme}>
                    ${theme === 'dark' ? html`<${Icons.Sun} />` : html`<${Icons.Moon} />`}
                <//>
            <//>
        `;
      };

      // --- Outputs Section (combined outputs display in function nodes) ---
      const OutputsSection = ({ outputs, showTypes, isLight }) => {
        if (!outputs || outputs.length === 0) return null;
        const bgClass = isLight ? "bg-slate-50/80" : "bg-slate-900/50";
        const textClass = isLight ? "text-slate-600" : "text-slate-400";
        const arrowClass = isLight ? "text-emerald-500" : "text-emerald-400";
        const typeClass = isLight ? "text-slate-400" : "text-slate-500";
        const borderClass = isLight ? "border-slate-100" : "border-slate-800/50";
        
        return html`
            <div className=${`px-2 py-2 border-t transition-all duration-300 overflow-hidden ${bgClass} ${borderClass}`}>
                <div className="flex flex-col items-center gap-1.5">
                    ${outputs.map(out => html`
                        <div key=${out.name} className=${`flex items-center gap-1.5 text-xs max-w-full ${textClass}`}>
                            <span className=${`shrink-0 ${arrowClass}`}>→</span>
                            <span className="font-mono font-medium shrink-0">${out.name}</span>
                            ${showTypes && out.type ? html`<span className=${`font-mono truncate ${typeClass}`} title=${out.type}>: ${truncateTypeHint(out.type)}</span>` : null}
                        </div>
                    `)}
                </div>
            </div>
        `;
      };

      // --- Debug Overlay Component ---
      // Shows node bounding boxes, dimensions, and edge connection points for debugging layout issues
      const DebugOverlay = ({ nodes, edges, enabled, theme }) => {
        const [showPanel, setShowPanel] = useState(true);
        const [activeTab, setActiveTab] = useState('bounds'); // 'bounds', 'widths', or 'texts'
        
        if (!enabled) return null;
        
        const visibleNodes = nodes.filter(n => !n.hidden);
        const isLight = theme === 'light';
        
        // Calculate node boundaries and widths
        const nodeBounds = visibleNodes.map(n => {
          const elkWidth = Math.round(n.style?.width || 200);
          const label = n.data?.label || '';
          const typeHint = n.data?.typeHint || '';
          const showTypes = n.data?.showTypes;
          const params = n.data?.params || [];
          const paramTypes = n.data?.paramTypes || [];
          const outputs = n.data?.outputs || [];
          
          // Calculate what width SHOULD be based on content
          let expectedWidth = 0;
          let contentDesc = '';
          let longestText = '';
          let longestTextLen = 0;
          let allTexts = [];
          
          if (n.data?.nodeType === 'DATA' || n.data?.nodeType === 'INPUT') {
            // Formula: (labelLen + typeLen) * CHAR_WIDTH_PX + NODE_BASE_PADDING (both truncated)
            const truncLabelLen = Math.min(label.length, NODE_LABEL_MAX_CHARS);
            const typeLen = (showTypes && typeHint) ? Math.min(typeHint.length, TYPE_HINT_MAX_CHARS) + 2 : 0;
            expectedWidth = Math.min(MAX_NODE_WIDTH, (truncLabelLen + typeLen) * CHAR_WIDTH_PX + NODE_BASE_PADDING);
            contentDesc = showTypes && typeHint ? `${label}: ${typeHint}` : label;
            allTexts = [{ text: label, len: label.length, kind: 'label', truncated: label.length > NODE_LABEL_MAX_CHARS }];
            if (typeHint) allTexts.push({ text: typeHint, len: typeHint.length, kind: 'type', truncated: typeHint.length > TYPE_HINT_MAX_CHARS });
            longestText = typeHint && typeHint.length > label.length ? typeHint : label;
            longestTextLen = Math.max(label.length, typeHint ? typeHint.length : 0);
          } else if (n.data?.nodeType === 'INPUT_GROUP') {
            // Find longest param + type (both truncated)
            let maxLen = 0;
            params.forEach((p, i) => {
              const pLen = p ? p.length : 0;
              const truncPLen = Math.min(pLen, NODE_LABEL_MAX_CHARS);
              const t = paramTypes[i] || '';
              allTexts.push({ text: p, len: pLen, kind: 'param', truncated: pLen > NODE_LABEL_MAX_CHARS });
              if (t) allTexts.push({ text: t, len: t.length, kind: 'type', truncated: t.length > TYPE_HINT_MAX_CHARS });
              let len = truncPLen;
              if (showTypes && t) {
                len += 2 + Math.min(t.length, TYPE_HINT_MAX_CHARS);
              }
              if (len > maxLen) {
                maxLen = len;
                longestText = showTypes && t ? `${p}: ${t}` : p;
              }
            });
            longestTextLen = maxLen;
            expectedWidth = Math.min(MAX_NODE_WIDTH, maxLen * CHAR_WIDTH_PX + NODE_BASE_PADDING);
            contentDesc = params.join(', ');
          } else if (n.data?.nodeType === 'FUNCTION' || n.data?.nodeType === 'PIPELINE') {
            // Function/Pipeline nodes with outputs (label and output types truncated)
            allTexts = [{ text: label, len: label.length, kind: 'label', truncated: label.length > NODE_LABEL_MAX_CHARS }];
            longestText = label;
            longestTextLen = label.length;
            outputs.forEach(out => {
              const outName = out.name || '';
              const outType = out.type || '';
              allTexts.push({ text: outName, len: outName.length, kind: 'output', truncated: outName.length > NODE_LABEL_MAX_CHARS });
              if (outType) allTexts.push({ text: outType, len: outType.length, kind: 'type', truncated: outType.length > TYPE_HINT_MAX_CHARS });
              const combined = showTypes && outType ? `${outName}: ${outType}` : outName;
              if (combined.length > longestTextLen) {
                longestText = combined;
                longestTextLen = combined.length;
              }
            });
          }
          
          return {
            id: n.id,
            shortId: n.id.length > 20 ? n.id.slice(-18) + '..' : n.id,
            y: Math.round(n.position?.y || 0),
            height: Math.round(n.style?.height || 68),
            bottom: Math.round((n.position?.y || 0) + (n.style?.height || 68)),
            nodeType: n.data?.nodeType,
            width: elkWidth,
            expectedWidth,
            widthDiff: elkWidth - expectedWidth,
            contentDesc: contentDesc.length > 25 ? contentDesc.slice(0, 22) + '...' : contentDesc,
            label,
            typeHint: typeHint || '',
            longestText,
            longestTextLen,
            allTexts,
            isTruncated: longestTextLen > TYPE_HINT_MAX_CHARS,
          };
        });
        
        // Filter to only show DATA/INPUT/INPUT_GROUP for width debugging
        const inputNodes = nodeBounds.filter(n => ['DATA', 'INPUT', 'INPUT_GROUP'].includes(n.nodeType));
        
        const handleCopy = () => {
            const info = {
                nodes: nodeBounds,
                edges: edges.map(e => ({ id: e.id, source: e.source, target: e.target }))
            };
            navigator.clipboard.writeText(JSON.stringify(info, null, 2))
                .then(() => alert('Debug info copied!'))
                .catch(e => console.error('Copy failed', e));
        };

        return html`
          <${React.Fragment}>
            <${Panel} position="top-center" className="pointer-events-none">
              <div className=${`text-[10px] font-mono px-2 py-1 rounded ${isLight ? 'bg-red-100 text-red-800' : 'bg-red-900/80 text-red-200'}`}>
                DEBUG: Green=source, Blue=target | Yellow highlight = width mismatch
              </div>
            <//>
            <${Panel} position="top-right" className="mt-16 mr-4 pointer-events-auto flex flex-col gap-2">
              <div className=${`rounded-lg border shadow-xl max-h-[60vh] overflow-hidden flex flex-col ${isLight ? 'bg-white border-slate-200' : 'bg-slate-900 border-slate-700'}`}>
                <div className="flex items-center border-b border-slate-500/20">
                    <button 
                      onClick=${() => setActiveTab('bounds')}
                      className=${`flex-1 px-3 py-1.5 text-[10px] font-bold tracking-wide ${activeTab === 'bounds' ? (isLight ? 'bg-red-100 text-red-800' : 'bg-red-900/50 text-red-300') : (isLight ? 'bg-slate-100 text-slate-600 hover:bg-slate-200' : 'bg-slate-800 text-slate-400 hover:bg-slate-700')}`}
                    >
                      BOUNDS
                    </button>
                    <button 
                      onClick=${() => setActiveTab('widths')}
                      className=${`flex-1 px-3 py-1.5 text-[10px] font-bold tracking-wide border-l border-slate-500/20 ${activeTab === 'widths' ? (isLight ? 'bg-amber-100 text-amber-800' : 'bg-amber-900/50 text-amber-300') : (isLight ? 'bg-slate-100 text-slate-600 hover:bg-slate-200' : 'bg-slate-800 text-slate-400 hover:bg-slate-700')}`}
                    >
                      WIDTHS
                    </button>
                    <button 
                      onClick=${() => setActiveTab('texts')}
                      className=${`flex-1 px-3 py-1.5 text-[10px] font-bold tracking-wide border-l border-slate-500/20 ${activeTab === 'texts' ? (isLight ? 'bg-cyan-100 text-cyan-800' : 'bg-cyan-900/50 text-cyan-300') : (isLight ? 'bg-slate-100 text-slate-600 hover:bg-slate-200' : 'bg-slate-800 text-slate-400 hover:bg-slate-700')}`}
                    >
                      TEXTS
                    </button>
                    <button 
                      onClick=${() => setShowPanel(p => !p)}
                      className=${`px-3 py-1.5 text-[10px] font-bold tracking-wide border-l border-slate-500/20 ${isLight ? 'bg-slate-100 text-slate-600 hover:bg-slate-200' : 'bg-slate-800 text-slate-400 hover:bg-slate-700'}`}
                    >
                      ${showPanel ? '▼' : '▶'}
                    </button>
                    <button 
                      onClick=${handleCopy}
                      className=${`px-3 py-1.5 text-[10px] font-bold tracking-wide border-l border-slate-500/20 ${isLight ? 'bg-blue-100 text-blue-800 hover:bg-blue-200' : 'bg-blue-900/50 text-blue-300 hover:bg-blue-900/70'}`}
                      title="Copy debug info to clipboard"
                    >
                      COPY
                    </button>
                </div>
                ${showPanel && activeTab === 'bounds' ? html`
                  <div className="overflow-y-auto max-h-[50vh]">
                    <table className=${`text-[9px] font-mono w-full ${isLight ? 'text-slate-700' : 'text-slate-300'}`}>
                      <thead className=${`sticky top-0 ${isLight ? 'bg-slate-100' : 'bg-slate-800'}`}>
                        <tr>
                          <th className="px-2 py-1 text-left">Node</th>
                          <th className="px-2 py-1 text-right">Y</th>
                          <th className="px-2 py-1 text-right">H</th>
                          <th className="px-2 py-1 text-right font-bold text-amber-500">Bottom</th>
                        </tr>
                      </thead>
                      <tbody>
                        ${nodeBounds.map((n, i) => html`
                          <tr key=${n.id} className=${`${i % 2 === 0 ? (isLight ? 'bg-white' : 'bg-slate-900') : (isLight ? 'bg-slate-50' : 'bg-slate-800/50')} ${n.nodeType === 'PIPELINE' ? (isLight ? '!bg-amber-50' : '!bg-amber-900/20') : ''}`}>
                            <td className="px-2 py-0.5 truncate max-w-[120px]" title=${n.id}>${n.shortId}</td>
                            <td className="px-2 py-0.5 text-right">${n.y}</td>
                            <td className="px-2 py-0.5 text-right">${n.height}</td>
                            <td className="px-2 py-0.5 text-right font-bold text-amber-500">${n.bottom}</td>
                          </tr>
                        `)}
                      </tbody>
                    </table>
                  </div>
                ` : null}
                ${showPanel && activeTab === 'widths' ? html`
                  <div className="overflow-y-auto max-h-[50vh]">
                    <div className="px-2 py-1 text-[8px] font-mono ${isLight ? 'bg-amber-50 text-amber-800' : 'bg-amber-900/30 text-amber-300'}">
                      Formula: (labelLen + typeLen) * 7 + 52 | Green = OK, Yellow = too wide, Red = too narrow
                    </div>
                    <table className=${`text-[9px] font-mono w-full ${isLight ? 'text-slate-700' : 'text-slate-300'}`}>
                      <thead className=${`sticky top-0 ${isLight ? 'bg-slate-100' : 'bg-slate-800'}`}>
                        <tr>
                          <th className="px-2 py-1 text-left">Node</th>
                          <th className="px-2 py-1 text-left">Content</th>
                          <th className="px-2 py-1 text-right">ELK W</th>
                          <th className="px-2 py-1 text-right">Expect</th>
                          <th className="px-2 py-1 text-right">Diff</th>
                        </tr>
                      </thead>
                      <tbody>
                        ${inputNodes.map((n, i) => {
                          const diffClass = n.widthDiff > 20 ? 'text-amber-500' : n.widthDiff < -5 ? 'text-red-500' : 'text-green-500';
                          return html`
                            <tr key=${n.id} className=${`${i % 2 === 0 ? (isLight ? 'bg-white' : 'bg-slate-900') : (isLight ? 'bg-slate-50' : 'bg-slate-800/50')}`}>
                              <td className="px-2 py-0.5 truncate max-w-[80px]" title=${n.id}>${n.shortId}</td>
                              <td className="px-2 py-0.5 truncate max-w-[100px]" title=${n.contentDesc}>${n.contentDesc}</td>
                              <td className="px-2 py-0.5 text-right">${n.width}</td>
                              <td className="px-2 py-0.5 text-right">${n.expectedWidth}</td>
                              <td className=${`px-2 py-0.5 text-right font-bold ${diffClass}`}>${n.widthDiff > 0 ? '+' : ''}${n.widthDiff}</td>
                            </tr>
                          `;
                        })}
                      </tbody>
                    </table>
                  </div>
                ` : null}
                ${showPanel && activeTab === 'texts' ? html`
                  <div className="overflow-y-auto max-h-[50vh]">
                    <div className="px-2 py-1 text-[8px] font-mono ${isLight ? 'bg-cyan-50 text-cyan-800' : 'bg-cyan-900/30 text-cyan-300'}">
                      Type hints truncated at K=${TYPE_HINT_MAX_CHARS} chars | Red = truncated
                    </div>
                    <table className=${`text-[9px] font-mono w-full ${isLight ? 'text-slate-700' : 'text-slate-300'}`}>
                      <thead className=${`sticky top-0 ${isLight ? 'bg-slate-100' : 'bg-slate-800'}`}>
                        <tr>
                          <th className="px-2 py-1 text-left">Node</th>
                          <th className="px-2 py-1 text-left">Type</th>
                          <th className="px-2 py-1 text-left">Label</th>
                          <th className="px-2 py-1 text-left">TypeHint (full)</th>
                          <th className="px-2 py-1 text-right">Longest</th>
                          <th className="px-2 py-1 text-right">W</th>
                        </tr>
                      </thead>
                      <tbody>
                        ${nodeBounds.map((n, i) => {
                          const truncClass = n.isTruncated ? 'text-red-500' : 'text-green-500';
                          return html`
                            <tr key=${n.id} className=${`${i % 2 === 0 ? (isLight ? 'bg-white' : 'bg-slate-900') : (isLight ? 'bg-slate-50' : 'bg-slate-800/50')}`}>
                              <td className="px-2 py-0.5 truncate max-w-[80px]" title=${n.id}>${n.shortId}</td>
                              <td className="px-2 py-0.5">${n.nodeType || '-'}</td>
                              <td className="px-2 py-0.5 truncate max-w-[80px]" title=${n.label}>${n.label || '-'}</td>
                              <td className=${`px-2 py-0.5 truncate max-w-[120px] ${n.typeHint && n.typeHint.length > TYPE_HINT_MAX_CHARS ? 'text-red-500 font-bold' : ''}`} title=${n.typeHint}>${n.typeHint || '-'}</td>
                              <td className=${`px-2 py-0.5 text-right ${truncClass}`}>${n.longestTextLen}</td>
                              <td className="px-2 py-0.5 text-right">${n.width}</td>
                            </tr>
                          `;
                        })}
                      </tbody>
                    </table>
                  </div>
                ` : null}
              </div>
            <//>
          <//>
        `;
      };

      // --- Edge Component ---
      // Renders edges using polyline paths from constraint layout (via edge.data.points)
      // Falls back to bezier paths if no points are provided
      const CustomEdge = ({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, style = {}, markerEnd, label, data, source, target }) => {
        // Debug: Log edge coordinates to help diagnose layout issues
        React.useEffect(() => {
            if (window.__hypergraph_debug_edges) {
                console.log(`[Edge ${id}] source=${source} target=${target} points=${data?.points?.length || 0}`);
            }
        }, [id, sourceX, sourceY, targetX, targetY, source, target, data?.points]);

        const showDebug = data?.debugMode || window.__hypergraph_debug_overlays;

        // Use polyline path from constraint layout if available, otherwise fall back to bezier
        let edgePath, labelX, labelY;

        if (data?.points && data.points.length > 0) {
          // Build SVG path using B-spline (curveBasis) - same algorithm as kedro-viz
          // Override endpoints with React Flow's handle positions for perfect alignment
          const points = [...data.points];
          points[0] = { x: sourceX, y: sourceY };
          points[points.length - 1] = { x: targetX, y: targetY };

          // Simplify "mostly vertical" edges - avoid kinked appearance
          // If the horizontal distance is small relative to vertical, use a simple S-curve
          const dx = Math.abs(targetX - sourceX);
          const dy = Math.abs(targetY - sourceY);
          const isNearlyVertical = dx < 30 && dy > dx * 2;

          // curveBasis: B-spline interpolation (ported from d3-shape)
          // Creates smooth flowing curves through control points
          // Duplicate first/last points to clamp the spline to exact endpoints
          const curveBasis = (pts) => {
            if (pts.length < 2) return `M ${pts[0].x} ${pts[0].y}`;
            if (pts.length === 2) return `M ${pts[0].x} ${pts[0].y} L ${pts[1].x} ${pts[1].y}`;

            // Clamp B-spline by duplicating endpoints - ensures curve passes through them
            const clamped = [pts[0], ...pts, pts[pts.length - 1]];

            let path = `M ${clamped[0].x} ${clamped[0].y}`;

            let x0 = clamped[0].x, y0 = clamped[0].y;
            let x1 = clamped[1].x, y1 = clamped[1].y;

            // First segment: line toward second point
            path += ` L ${(5 * x0 + x1) / 6} ${(5 * y0 + y1) / 6}`;

            // Middle segments: B-spline cubic beziers
            for (let i = 2; i < clamped.length; i++) {
              const x = clamped[i].x, y = clamped[i].y;
              path += ` C ${(2 * x0 + x1) / 3} ${(2 * y0 + y1) / 3} ${(x0 + 2 * x1) / 3} ${(y0 + 2 * y1) / 3} ${(x0 + 4 * x1 + x) / 6} ${(y0 + 4 * y1 + y) / 6}`;
              x0 = x1; y0 = y1;
              x1 = x; y1 = y;
            }

            // Final segment: bezier to exact last point
            path += ` C ${(2 * x0 + x1) / 3} ${(2 * y0 + y1) / 3} ${(x0 + 2 * x1) / 3} ${(y0 + 2 * y1) / 3} ${x1} ${y1}`;

            return path;
          };

          // For nearly vertical edges, use a simple smooth S-curve instead of B-spline
          // This avoids the "kinked" appearance when source and target have small X offset
          if (isNearlyVertical) {
            // Simple S-curve: straight down with gentle horizontal transition
            const midY = (sourceY + targetY) / 2;
            edgePath = `M ${sourceX} ${sourceY} C ${sourceX} ${midY} ${targetX} ${midY} ${targetX} ${targetY}`;
          } else {
            edgePath = curveBasis(points);
          }

          // Calculate label position at midpoint of edge
          const midIdx = Math.floor(points.length / 2);
          if (points.length > 1) {
            labelX = (points[midIdx - 1].x + points[midIdx].x) / 2;
            labelY = (points[midIdx - 1].y + points[midIdx].y) / 2;
          } else {
            labelX = points[0].x;
            labelY = points[0].y;
          }
        } else {
          // Fallback to bezier path
          [edgePath, labelX, labelY] = getBezierPath({
            sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition
          });
        }

        return html`
          <${React.Fragment}>
            <${BaseEdge} path=${edgePath} markerEnd=${markerEnd} style=${style} />
            ${showDebug ? html`
              <!-- Debug: Source connection point (green) -->
              <circle cx=${sourceX} cy=${sourceY} r="5" fill="#22c55e" stroke="#15803d" strokeWidth="1" />
              <!-- Debug: Target connection point (blue) -->
              <circle cx=${targetX} cy=${targetY} r="5" fill="#3b82f6" stroke="#1d4ed8" strokeWidth="1" />
              <!-- Debug: Edge coordinates label -->
              <${EdgeLabelRenderer}>
                <div
                  style=${{
                    position: 'absolute',
                    transform: `translate(-50%, -50%) translate(${labelX}px,${labelY}px)`,
                    pointerEvents: 'none',
                  }}
                  className="px-1.5 py-0.5 rounded bg-slate-900/95 border border-slate-600 text-[8px] text-slate-300 font-mono whitespace-nowrap"
                >
                  S:(${Math.round(sourceX)},${Math.round(sourceY)}) T:(${Math.round(targetX)},${Math.round(targetY)})
                </div>
              <//>
            ` : null}
            ${(label || data?.label) && !showDebug ? html`
              <${EdgeLabelRenderer}>
                <div
                  style=${{
                    position: 'absolute',
                    transform: `translate(-50%, -50%) translate(${labelX}px,${labelY}px)`,
                    pointerEvents: 'all',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '4px',
                    padding: '3px 10px',
                    borderRadius: '10px',
                    fontSize: '10px',
                    fontFamily: 'ui-monospace, monospace',
                    fontWeight: '600',
                    letterSpacing: '0.02em',
                    ...((label || data?.label) === 'True'
                      ? {
                          background: 'rgba(16, 185, 129, 0.9)',
                          border: '1px solid #34d399',
                          color: '#ffffff',
                          boxShadow: '0 2px 6px rgba(16, 185, 129, 0.3)',
                        }
                      : (label || data?.label) === 'False'
                        ? {
                            background: 'rgba(239, 68, 68, 0.9)',
                            border: '1px solid #f87171',
                            color: '#ffffff',
                            boxShadow: '0 2px 6px rgba(239, 68, 68, 0.3)',
                          }
                        : {
                            background: 'rgba(15,23,42,0.9)',
                            border: '1px solid #334155',
                            color: '#cbd5e1',
                            boxShadow: '0 2px 4px rgba(0,0,0,0.2)',
                          }
                    ),
                  }}
                >
                  ${(label || data?.label) === 'True' ? html`
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                      <polyline points="20 6 9 17 4 12"></polyline>
                    </svg>
                  ` : (label || data?.label) === 'False' ? html`
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                      <line x1="18" y1="6" x2="6" y2="18"></line>
                      <line x1="6" y1="6" x2="18" y2="18"></line>
                    </svg>
                  ` : null}
                  ${label || data?.label}
                </div>
              <//>
            ` : null}
          <//>
        `;
      };

      // --- Node Component ---
      const CustomNode = ({ data, id }) => {
        const isExpanded = data.isExpanded;
        // Get theme from node data (updated via setNodes when theme changes)
        const theme = data.theme || 'dark';
        const updateNodeInternals = useUpdateNodeInternals();
        const showDebug = data.debugMode || window.__hypergraph_debug_overlays;
        
        // Debug wrapper to show node bounding box
        const DebugWrapper = ({ children }) => {
          if (!showDebug) return children;
          return html`
            <div className="relative">
              <div className="absolute -inset-0.5 border-2 border-dashed border-red-500 rounded pointer-events-none z-50">
                <span className="absolute -top-4 left-0 text-[8px] bg-red-500 text-white px-1 rounded font-mono whitespace-nowrap">
                  ${id}
                </span>
              </div>
              ${children}
            </div>
          `;
        };
        
        // Style Configuration
        let colors = { bg: "slate", border: "slate", text: "slate", icon: "slate" };
        let Icon = Icons.Function;
        let labelType = "NODE";
        
        if (data.nodeType === 'PIPELINE') {
          colors = { bg: "amber", border: "amber", text: "amber", icon: "amber" };
          Icon = Icons.Pipeline;
          labelType = "PIPELINE";
        } else if (data.nodeType === 'DUAL') {
          colors = { bg: "fuchsia", border: "fuchsia", text: "fuchsia", icon: "fuchsia" };
          Icon = Icons.Dual;
          labelType = "DUAL NODE";
        } else if (data.nodeType === 'BRANCH') {
          colors = { bg: "yellow", border: "yellow", text: "yellow", icon: "yellow" };
          Icon = Icons.Branch;
          labelType = "BRANCH";
        } else if (data.nodeType === 'INPUT') {
          colors = { bg: "cyan", border: "cyan", text: "cyan", icon: "cyan" };
          Icon = Icons.Input;
          labelType = "INPUT";
        } else if (data.nodeType === 'DATA') {
          colors = { bg: "slate", border: "slate", text: "slate", icon: "slate" };
          Icon = Icons.Data;
          labelType = "DATA";
        } else if (data.nodeType === 'INPUT_GROUP') {
          colors = { bg: "cyan", border: "cyan", text: "cyan", icon: "cyan" };
          Icon = Icons.Input;
          labelType = "INPUT GROUP";
        } else {
          colors = { bg: "indigo", border: "indigo", text: "indigo", icon: "indigo" };
          Icon = Icons.Function;
          labelType = "FUNCTION";
        }

        useEffect(() => {
          updateNodeInternals(id);
        }, [
          id,
          data.separateOutputs,
          data.showTypes,
          data.outputs ? data.outputs.length : 0,
          data.inputs ? data.inputs.length : 0,
          isExpanded,
          theme,
        ]);
        
        // --- Render Data Node (Compact) ---
        if (data.nodeType === 'DATA') {
            const isLight = theme === 'light';
            const isOutput = data.sourceId != null;
            const showAsOutput = data.separateOutputs && isOutput;
            const showTypes = data.showTypes;
            const typeClass = isLight ? 'text-slate-400' : 'text-slate-500';
            const hasTypeHint = showTypes && data.typeHint;
            // Truncate type hint display using global constant
            const displayTypeHint = truncateTypeHint(data.typeHint);
            return html`
                <div className=${`px-3 py-1.5 w-full relative rounded-full border shadow-sm flex items-center justify-center gap-2 transition-colors transition-shadow duration-200 hover:shadow-lg overflow-hidden
                    ${showAsOutput ? 'ring-2 ring-emerald-500/30' : ''}
                    ${isLight 
                        ? 'bg-white border-slate-200 text-slate-700 shadow-slate-200 hover:border-slate-300' 
                        : 'bg-slate-900 border-slate-700 text-slate-300 shadow-black/50 hover:border-slate-600'}
                `}>
                     <span className=${`shrink-0 ${isLight ? 'text-slate-400' : 'text-slate-500'}`}><${Icon} /></span>
                     <span className="text-xs font-mono font-medium shrink-0">${data.label}</span>
                     ${hasTypeHint ? html`<span className=${`text-[10px] font-mono truncate min-w-0 ${typeClass}`} title=${data.typeHint}>: ${displayTypeHint}</span>` : null}
                     <${Handle} type="target" position=${Position.Top} className="!w-2 !h-2 !opacity-0" style=${{ top: '-2px' }} />
                     <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" style=${{ bottom: '-2px' }} />
                </div>
            `;
        }

        // --- Render Input Node (Compact - styled as DATA) ---
        if (data.nodeType === 'INPUT') {
             const isLight = theme === 'light';
             const isBound = Boolean(data.isBound);
             const showTypes = data.showTypes;
             const typeHint = data.typeHint;
             const hasType = showTypes && typeHint;
             const typeClass = isLight ? 'text-slate-400' : 'text-slate-500';
             // Truncate type hint using global constant
             const displayType = truncateTypeHint(typeHint);
             // Reuse DATA node styling but preserve dashed border for bound inputs
             return html`
                <div className=${`px-3 py-1.5 w-full relative rounded-full border shadow-sm flex items-center justify-center gap-2 transition-colors transition-shadow duration-200 hover:shadow-lg overflow-hidden
                    ${isBound ? 'border-dashed' : ''}
                    ${isLight 
                        ? 'bg-white border-slate-200 text-slate-700 shadow-slate-200 hover:border-slate-300' 
                        : 'bg-slate-900 border-slate-700 text-slate-300 shadow-black/50 hover:border-slate-600'}
                `}>
                    <span className=${`shrink-0 ${isLight ? 'text-slate-400' : 'text-slate-500'}`}><${Icons.Data} /></span>
                    <span className="text-xs font-mono font-medium shrink-0">${data.label}</span>
                    ${hasType ? html`<span className=${`text-[10px] font-mono truncate min-w-0 ${typeClass}`} title=${typeHint}>: ${displayType}</span>` : null}
                    <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" style=${{ bottom: '-2px' }} />
                </div>
             `;
        }

        // --- Render Input Group Node ---
        if (data.nodeType === 'INPUT_GROUP') {
             const isLight = theme === 'light';
             const params = data.params || [];
             const paramTypes = data.paramTypes || [];
             const isBound = data.isBound;
             const showTypes = data.showTypes;
             const typeClass = isLight ? 'text-slate-400' : 'text-slate-500';
             
             return html`
                <div className=${`px-3 py-2 w-full relative rounded-xl border shadow-sm flex flex-col gap-1 min-w-[120px] transition-colors transition-shadow duration-200 hover:shadow-lg
                    ${isBound ? 'border-dashed' : ''}
                    ${isLight
                        ? 'bg-white border-slate-200 text-slate-700 shadow-slate-200 hover:border-slate-300'
                        : 'bg-slate-900 border-slate-700 text-slate-300 shadow-black/50 hover:border-slate-600'}
                `}>
                    ${params.map((p, i) => html`
                        <div className="flex items-center gap-2 whitespace-nowrap">
                            <span className=${isLight ? 'text-slate-400' : 'text-slate-500'}><${Icons.Data} className="w-3 h-3" /></span>
                            <div className="text-xs font-mono leading-tight">${p}</div>
                            ${showTypes && paramTypes[i] ? html`<span className=${`text-[10px] font-mono ${typeClass}`} title=${paramTypes[i]}>: ${truncateTypeHint(paramTypes[i])}</span>` : null}
                        </div>
                    `)}
                    <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" style=${{ bottom: '-2px' }} />
                </div>
             `;
        }

        // --- Render Branch Node (Diamond Shape) ---
        if (data.nodeType === 'BRANCH') {
          const isLight = theme === 'light';
          const [isHovered, setIsHovered] = useState(false);
          
          // Use inline styles for colors since Tailwind CSS bundle may not include all classes
          // CYAN color scheme to differentiate from amber pipeline nodes
          const diamondBgColor = isLight ? '#ecfeff' : '#083344';  // cyan-50 / cyan-950
          const diamondBorderColor = isLight ? '#22d3ee' : 'rgba(6,182,212,0.6)';  // cyan-400 / cyan-500/60
          const diamondHoverBorderColor = isLight ? '#06b6d4' : 'rgba(34,211,238,0.8)';  // cyan-500 / cyan-400/80
          
          // Glow effect colors
          const glowColor = 'rgba(6,182,212,0.4)';  // cyan-500 with opacity
          
          const labelColor = isLight ? '#0e7490' : '#a5f3fc';  // cyan-700 / cyan-200
          
          // Diamond geometry: 95px square rotated 45deg = ~134px diagonal
          // Container is 140px, so diamond tips are ~3px from container edges
          const diamondTipOffset = '3px';
          
          return html`
            <${DebugWrapper}>
              <div className="relative flex items-center justify-center cursor-pointer"
                   style=${{ width: '140px', height: '140px' }}
                   onMouseEnter=${() => setIsHovered(true)}
                   onMouseLeave=${() => setIsHovered(false)}
                   onTransitionEnd=${(e) => { if (e.target === e.currentTarget) updateNodeInternals(id); }}>
                
                <!-- Wrapper for Drop Shadow (Not Rotated) -->
                <div className="transition-all duration-200 ease-out"
                     style=${{ filter: 'drop-shadow(0 10px 8px rgb(0 0 0 / 0.04)) drop-shadow(0 4px 3px rgb(0 0 0 / 0.1))' }}>
                    <!-- Diamond shape using rotated square -->
                    <div className="transition-all duration-200 ease-out border"
                         style=${{
                            width: '95px',
                            height: '95px',
                            transform: 'rotate(45deg)',
                            borderRadius: '10px',
                            backgroundColor: diamondBgColor,
                            borderColor: isHovered ? diamondHoverBorderColor : diamondBorderColor,
                            boxShadow: isHovered ? `0 0 15px ${glowColor}` : '0 0 0 rgba(6,182,212,0)',
                         }}>
                    </div>
                </div>

                <!-- Label overlay (not rotated) - centered text only -->
                <div style=${{
                  position: 'absolute',
                  inset: '0',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  pointerEvents: 'none',
                  padding: '0 10px',
                }}>
                  <span className="text-sm font-semibold text-center"
                        style=${{
                          color: labelColor,
                          maxWidth: '100%',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }} title=${data.label}>${data.label}</span>
                </div>
                
                <!-- Handles: Target at top, Source at bottom -->
                <${Handle} type="target" position=${Position.Top} className="!w-2 !h-2 !opacity-0" style=${{ top: diamondTipOffset }} />
                <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" style=${{ bottom: diamondTipOffset }} id="branch-source" />
              </div>
            <//>
          `;
        }

        // --- Render Expanded Pipeline Group ---
        if (data.nodeType === 'PIPELINE' && isExpanded) {
          const isLight = theme === 'light';
          const handleCollapseClick = (e) => {
            e.stopPropagation();
            e.preventDefault();
            if (data.onToggleExpand) data.onToggleExpand();
          };
          
          return html`
            <div className=${`relative w-full h-full rounded-2xl border-2 border-dashed p-6 transition-colors duration-200
                ${isLight 
                    ? 'border-amber-300 bg-amber-50/30' 
                    : 'border-amber-500/30 bg-amber-500/5'}
            `}>
              <button 
                   type="button"
                   className=${`absolute -top-3 left-4 px-3 py-0.5 rounded-full text-xs font-bold uppercase tracking-wider flex items-center gap-2 cursor-pointer transition-colors z-10 whitespace-nowrap
                        ${isLight
                            ? 'bg-amber-100 text-amber-700 hover:bg-amber-200 border border-amber-200'
                            : 'bg-slate-950 text-amber-400 hover:text-amber-300 border border-amber-500/50'}
                   `}
                   onClick=${handleCollapseClick}
                   title=${data.label}>
                <${Icon} />
                ${truncateLabel(data.label)}
                <span className="text-[9px] opacity-60 normal-case font-normal ml-1">Click to collapse</span>
              </button>
              <${Handle} type="target" position=${Position.Top} className="!w-2 !h-2 !opacity-0" />
              <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" />
            </div>
          `;
        }

        // --- Render Standard Node ---
        const isLight = theme === 'light';
        const boundInputs = data.inputs ? data.inputs.filter(i => i.is_bound).length : 0;
        const outputs = data.outputs || [];
        const showCombined = !data.separateOutputs && outputs.length > 0;
        const showTypes = data.showTypes;

        return html`
          <div className=${`group relative w-full rounded-lg border shadow-lg backdrop-blur-sm transition-colors transition-shadow duration-200 cursor-pointer node-function-${theme} overflow-hidden
               ${isLight 
                 ? `bg-white/90 border-${colors.border}-300 shadow-slate-200 hover:border-${colors.border}-400 hover:shadow-${colors.border}-200 hover:shadow-lg`
                 : `bg-slate-950/90 border-${colors.border}-500/40 shadow-black/50 hover:border-${colors.border}-500/70 hover:shadow-${colors.border}-500/20 hover:shadow-lg`}
               `}
               onClick=${data.nodeType === 'PIPELINE' ? (e) => { e.stopPropagation(); if(data.onToggleExpand) data.onToggleExpand(); } : undefined}>
            
            <!-- Header -->
            <div className=${`px-3 py-2.5 flex flex-col items-center justify-center
                 ${showCombined ? (isLight ? 'border-b border-slate-100' : 'border-b border-slate-800/50') : ''}`}>
              <div className=${`text-sm font-semibold truncate max-w-full text-center
                   ${isLight ? 'text-slate-800' : 'text-slate-100'}`} title=${data.label}>${truncateLabel(data.label)}</div>

              <!-- Bound Input Badge -->
              ${boundInputs > 0 ? html`
                  <div className=${`absolute top-2 right-2 w-2 h-2 rounded-full ring-2 ring-offset-1
                      ${isLight 
                          ? 'bg-indigo-400 ring-indigo-100 ring-offset-white' 
                          : 'bg-indigo-500 ring-indigo-500/30 ring-offset-slate-950'}`}
                       title="${boundInputs} bound inputs">
                </div>
              ` : null}
            </div>
            
            <!-- Combined Outputs Section -->
            ${showCombined ? html`<${OutputsSection} outputs=${outputs} showTypes=${showTypes} isLight=${isLight} />` : null}

            <!-- Handles (invisible) -->
            <${Handle} type="target" position=${Position.Top} className="!w-2 !h-2 !opacity-0" />
            <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" />
            
            ${data.nodeType === 'PIPELINE' ? html`
               <div className="absolute -bottom-5 left-1/2 -translate-x-1/2 text-[9px] text-slate-400 opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap">
                 Click to expand
               </div>
            ` : null}
          </div>
        `;
      };

      const nodeTypes = { custom: CustomNode, pipelineGroup: CustomNode };
      const edgeTypes = { custom: CustomEdge };

      // Layout hook using constraint-based layout algorithm (Cassowary/kiwi.js)
      const useLayout = (nodes, edges) => {
        const [layoutedNodes, setLayoutedNodes] = useState([]);
        const [layoutedEdges, setLayoutedEdges] = useState([]);
        const [layoutError, setLayoutError] = useState(null);
        const [graphHeight, setGraphHeight] = useState(600);
        const [graphWidth, setGraphWidth] = useState(600);
        const [layoutVersion, setLayoutVersion] = useState(0);
        const [isLayouting, setIsLayouting] = useState(false);

        useEffect(() => {
          const debugMode = window.__hypergraph_debug_viz || false;
          if (debugMode) console.log('[useLayout] nodes:', nodes.length, 'edges:', edges.length);
          if (!nodes.length) {
            if (debugMode) console.log('[useLayout] No nodes, returning early');
            setIsLayouting(false);
            return;
          }

          setIsLayouting(true);

          try {
            // Filter visible nodes (flat graphs only for now)
            const visibleNodes = nodes.filter(n => !n.hidden && !n.parentNode);
            const visibleNodeIds = new Set(visibleNodes.map(n => n.id));
            const visibleEdges = edges.filter(e =>
              visibleNodeIds.has(e.source) && visibleNodeIds.has(e.target)
            );
            if (debugMode) console.log('[useLayout] visible:', visibleNodes.length, 'edges:', visibleEdges.length);

            // Calculate node dimensions for layout
            const calculateDimensions = (n) => {
              let width = 80;
              let height = 90;

              if (n.data?.nodeType === 'DATA') {
                height = 36;
                const labelLen = Math.min(n.data.label?.length || 0, NODE_LABEL_MAX_CHARS);
                const typeLen = (n.data.showTypes && n.data.typeHint) ? Math.min(n.data.typeHint.length, TYPE_HINT_MAX_CHARS) + 2 : 0;
                width = Math.min(MAX_NODE_WIDTH, (labelLen + typeLen) * CHAR_WIDTH_PX + NODE_BASE_PADDING);
              } else if (n.data?.nodeType === 'INPUT_GROUP') {
                // INPUT_GROUP shows params as rows
                const params = n.data?.params || [];
                const paramTypes = n.data?.paramTypes || [];
                let maxContentLen = 8; // "Inputs" label minimum
                params.forEach((p, i) => {
                  const paramLen = Math.min(p.length, NODE_LABEL_MAX_CHARS);
                  const typeLen = (n.data?.showTypes && paramTypes[i]) ? Math.min(paramTypes[i].length, TYPE_HINT_MAX_CHARS) + 2 : 0;
                  const totalLen = paramLen + typeLen + 4;
                  if (totalLen > maxContentLen) maxContentLen = totalLen;
                });
                width = Math.min(MAX_NODE_WIDTH, maxContentLen * CHAR_WIDTH_PX + NODE_BASE_PADDING);
                // Height matches CSS: py-2 (16px padding) + rows (20px each) + gaps (4px between)
                const numParams = Math.max(1, params.length);
                height = 16 + (numParams * 20) + ((numParams - 1) * 4);
              } else if (n.data?.nodeType === 'BRANCH') {
                width = 140;
                height = 140;
              } else {
                // Function/Pipeline node
                const labelLen = Math.min(n.data?.label?.length || 0, NODE_LABEL_MAX_CHARS);
                let maxContentLen = labelLen;
                const outputs = n.data?.outputs || [];
                if (!n.data?.separateOutputs && outputs.length > 0) {
                  outputs.forEach(o => {
                    const outName = o.name || o.label || '';
                    const outType = o.type || o.typeHint || '';
                    const outLabelLen = Math.min(outName.length, NODE_LABEL_MAX_CHARS);
                    const outTypeLen = (n.data?.showTypes && outType) ? Math.min(outType.length, TYPE_HINT_MAX_CHARS) + 2 : 0;
                    const totalLen = outLabelLen + outTypeLen + 4;
                    if (totalLen > maxContentLen) maxContentLen = totalLen;
                  });
                }
                width = Math.min(MAX_NODE_WIDTH, maxContentLen * CHAR_WIDTH_PX + FUNCTION_NODE_BASE_PADDING);
                height = 52;
                if (!n.data?.separateOutputs && outputs.length > 0) {
                  // Base height + outputs section with proper spacing
                  height = 48 + 42 + ((outputs.length - 1) * 28);
                }
              }

              if (n.style?.width) width = n.style.width;
              if (n.style?.height) height = n.style.height;

              return { width, height };
            };

            // Prepare nodes for constraint layout (expects center coordinates, width, height)
            const layoutNodes = visibleNodes.map(n => {
              const { width, height } = calculateDimensions(n);
              return {
                id: n.id,
                width,
                height,
                x: 0,  // Will be set by layout
                y: 0,  // Will be set by layout
                _original: n,  // Keep reference to original
              };
            });

            // Prepare edges for constraint layout
            const layoutEdges = visibleEdges.map(e => ({
              id: e.id,
              source: e.source,
              target: e.target,
              _original: e,
            }));

            // Detect if we're in separate outputs mode (tighter spacing)
            const isSeparateOutputs = layoutNodes.some(n => n._original?.data?.separateOutputs);

            // Use tighter spacing for separate outputs mode
            const layoutOptions = isSeparateOutputs
              ? {
                  ...ConstraintLayout.defaultOptions,
                  layout: {
                    ...ConstraintLayout.defaultOptions.layout,
                    spaceY: 100,     // Vertical spacing for separate outputs
                    layerSpaceY: 90, // Layer spacing for separate outputs
                  }
                }
              : ConstraintLayout.defaultOptions;

            // Run constraint layout (synchronous)
            const result = ConstraintLayout.graph(
              layoutNodes,
              layoutEdges,
              null,  // layers (not used)
              'vertical',  // orientation
              layoutOptions
            );

            if (debugMode) console.log('[useLayout] layout result:', result);

            // Convert back to React Flow format
            // Note: Constraint layout returns center coordinates; React Flow expects top-left
            const positionedNodes = result.nodes.map(n => {
              const w = n.width;
              const h = n.height;
              // Convert from center to top-left corner
              const x = n.x - w / 2;
              const y = n.y - h / 2;

              return {
                ...n._original,
                position: { x, y },
                width: w,
                height: h,
                style: { ...n._original.style, width: w, height: h },
                handles: [
                  { type: 'target', position: 'top', x: w / 2, y: 0, width: 8, height: 8, id: null },
                  { type: 'source', position: 'bottom', x: w / 2, y: h, width: 8, height: 8, id: null },
                ],
              };
            });

            // Convert edges with points from constraint layout
            const positionedEdges = result.edges.map(e => ({
              ...e._original,
              data: {
                ...e._original.data,
                points: e.points,  // Polyline points from constraint layout
              },
            }));

            setLayoutedNodes(positionedNodes);
            setLayoutedEdges(positionedEdges);
            setLayoutVersion(v => v + 1);
            setIsLayouting(false);
            setLayoutError(null);

            if (result.size) {
              setGraphWidth(result.size.width);
              setGraphHeight(result.size.height);
            }
          } catch (err) {
            console.error('Constraint layout error:', err);
            setLayoutError(err?.message || 'Layout error');

            // Fallback layout (grid)
            const fallbackNodes = nodes.map((n, idx) => {
              const w = n.style?.width || 200;
              const h = n.style?.height || 68;
              return {
                ...n,
                position: { x: 80 * (idx % 4), y: 120 * Math.floor(idx / 4) },
                width: w,
                height: h,
                handles: [
                  { type: 'target', position: 'top', x: w / 2, y: 0, width: 8, height: 8, id: null },
                  { type: 'source', position: 'bottom', x: w / 2, y: h, width: 8, height: 8, id: null },
                ],
              };
            });
            setLayoutedNodes(fallbackNodes);
            setLayoutedEdges(edges);
            setLayoutVersion(v => v + 1);
            setIsLayouting(false);
          }
        }, [nodes, edges]);

        return { layoutedNodes, layoutedEdges, layoutError, graphHeight, graphWidth, layoutVersion, isLayouting };
      };

      const initialData = JSON.parse(document.getElementById('graph-data').textContent || '{"nodes":[],"edges":[]}');
      const normalizeThemePref = (pref) => {
        const lower = (pref || '').toLowerCase();
        return ['light', 'dark', 'auto'].includes(lower) ? lower : 'auto';
      };
      const themePreference = normalizeThemePref(initialData.meta?.theme_preference || 'auto');
      const showThemeDebug = Boolean(initialData.meta?.theme_debug);
      const panOnScroll = Boolean(initialData.meta?.pan_on_scroll);
      const initialSeparateOutputs = Boolean(initialData.meta?.separate_outputs ?? false);
      const initialShowTypes = Boolean(initialData.meta?.show_types ?? true);
      
      // Parse URL parameters for debug mode: ?debug=overlays or ?debug=true
      const urlParams = new URLSearchParams(window.location.search);
      const debugParam = urlParams.get('debug');
      const initialDebugOverlays = debugParam === 'overlays' || debugParam === 'true' || debugParam === '1';
      // Initialize global debug flag for edge component access
      if (initialDebugOverlays) window.__hypergraph_debug_overlays = true;

      const parseColorString = (value) => {
        if (!value) return null;
        const scratch = document.createElement('div');
        scratch.style.color = value;
        scratch.style.backgroundColor = value;
        scratch.style.display = 'none';
        document.body.appendChild(scratch);
        const resolved = getComputedStyle(scratch).color || '';
        scratch.remove();
        const nums = resolved.match(/[\d\.]+/g);
        if (nums && nums.length >= 3) {
            const [r, g, b] = nums.slice(0, 3).map(Number);
            if (nums.length >= 4) {
                const alpha = Number(nums[3]);
                if (alpha < 0.1) return null;
            }
            const luminance = 0.299 * r + 0.587 * g + 0.114 * b;
            return { r, g, b, luminance, resolved, raw: value };
        }
        return null;
      };
      const detectHostTheme = () => {
        const attempts = [];
        const pushCandidate = (value, source) => {
            if (value && value !== 'transparent' && value !== 'rgba(0, 0, 0, 0)') {
                attempts.push({ value: value.trim(), source });
            }
        };

        // Detect host environment first
        let hostEnv = 'unknown';
        try {
            const parentDoc = window.parent?.document;
            if (parentDoc) {
                // Check for VS Code
                if (parentDoc.body.getAttribute('data-vscode-theme-kind') || 
                    (parentDoc.body.className && parentDoc.body.className.includes('vscode'))) {
                    hostEnv = 'vscode';
                }
                // Check for JupyterLab
                else if (parentDoc.body.dataset.jpThemeLight !== undefined || 
                         parentDoc.querySelector('.jp-Notebook')) {
                    hostEnv = 'jupyterlab';
                }
                // Check for Marimo
                else if (parentDoc.body.dataset.theme || parentDoc.body.dataset.mode ||
                         (parentDoc.body.className && parentDoc.body.className.includes('marimo'))) {
                    hostEnv = 'marimo';
                }
            }
        } catch (e) {}

        try {
            const parentDoc = window.parent?.document;
            if (parentDoc) {
                const rootStyle = getComputedStyle(parentDoc.documentElement);
                const bodyStyle = getComputedStyle(parentDoc.body);
                
                if (hostEnv === 'vscode') {
                    // VS Code: use CSS variable
                    pushCandidate(rootStyle.getPropertyValue('--vscode-editor-background'), '--vscode-editor-background');
                } else if (hostEnv === 'jupyterlab') {
                    // JupyterLab: .jp-Notebook has the actual visible background
                    const jpNotebook = parentDoc.querySelector('.jp-Notebook');
                    if (jpNotebook) {
                        const jpNotebookBg = getComputedStyle(jpNotebook).backgroundColor;
                        pushCandidate(jpNotebookBg, '.jp-Notebook background');
                    }
                    // JupyterLab CSS variables (fallback)
                    pushCandidate(rootStyle.getPropertyValue('--jp-layout-color0'), '--jp-layout-color0');
                    pushCandidate(rootStyle.getPropertyValue('--jp-layout-color1'), '--jp-layout-color1');
                } else {
                    // Unknown/Marimo: try common sources
                    pushCandidate(rootStyle.getPropertyValue('--vscode-editor-background'), '--vscode-editor-background');
                    pushCandidate(rootStyle.getPropertyValue('--jp-layout-color0'), '--jp-layout-color0');
                }
                
                // Fallback to computed backgrounds
                pushCandidate(bodyStyle.backgroundColor, 'parent body background');
                pushCandidate(rootStyle.backgroundColor, 'parent root background');
            }
        } catch (e) {}

        pushCandidate(getComputedStyle(document.body).backgroundColor, 'iframe body');

        let chosen = attempts.find(c => parseColorString(c.value));
        if (!chosen) chosen = { value: 'transparent', source: 'default' };
        const parsed = parseColorString(chosen.value);
        const luminance = parsed ? parsed.luminance : null;

        let autoTheme = luminance !== null ? (luminance > 150 ? 'light' : 'dark') : null;
        let source = luminance !== null ? `${chosen.source} luminance` : chosen.source;

        // JupyterLab detection (check before VS Code)
        try {
            const parentDoc = window.parent?.document;
            if (parentDoc) {
                // JupyterLab uses data-jp-theme-light attribute ("true" or "false")
                const jpThemeLight = parentDoc.body.dataset.jpThemeLight;
                if (jpThemeLight === 'true') {
                    autoTheme = 'light';
                    source = 'jupyterlab data-jp-theme-light';
                } else if (jpThemeLight === 'false') {
                    autoTheme = 'dark';
                    source = 'jupyterlab data-jp-theme-light';
                }
                // JupyterLab body classes
                const bodyClass = parentDoc.body.className || '';
                if (!autoTheme && bodyClass.includes('jp-mod-dark')) {
                    autoTheme = 'dark';
                    source = 'jupyterlab jp-mod-dark';
                } else if (!autoTheme && bodyClass.includes('jp-mod-light')) {
                    autoTheme = 'light';
                    source = 'jupyterlab jp-mod-light';
                }
            }
        } catch (e) {}

        // VS Code detection
        try {
            const parentDoc = window.parent?.document;
            if (parentDoc) {
                const themeKind = parentDoc.body.getAttribute('data-vscode-theme-kind');
                if (themeKind) {
                    autoTheme = themeKind.includes('light') ? 'light' : 'dark';
                    source = 'vscode-theme-kind';
                } else if (parentDoc.body.className && parentDoc.body.className.includes('vscode-light')) {
                    autoTheme = 'light';
                    source = 'vscode body class';
                } else if (parentDoc.body.className && parentDoc.body.className.includes('vscode-dark')) {
                    autoTheme = 'dark';
                    source = 'vscode body class';
                }
            }
        } catch (e) {}

        // Marimo detection
        try {
            const parentDoc = window.parent?.document;
            if (parentDoc && !autoTheme) {
                // Marimo uses data-theme or data-mode attributes
                const dataTheme = parentDoc.body.dataset.theme || parentDoc.documentElement.dataset.theme;
                const dataMode = parentDoc.body.dataset.mode || parentDoc.documentElement.dataset.mode;
                if (dataTheme === 'dark' || dataMode === 'dark') {
                    autoTheme = 'dark';
                    source = 'marimo data-theme/mode';
                } else if (dataTheme === 'light' || dataMode === 'light') {
                    autoTheme = 'light';
                    source = 'marimo data-theme/mode';
                }
                // Marimo body classes
                const bodyClass = parentDoc.body.className || '';
                if (!autoTheme && (bodyClass.includes('dark-mode') || bodyClass.includes('dark'))) {
                    autoTheme = 'dark';
                    source = 'marimo dark-mode class';
                }
                // Check color-scheme CSS property
                if (!autoTheme) {
                    const colorScheme = getComputedStyle(parentDoc.documentElement).getPropertyValue('color-scheme').trim();
                    if (colorScheme.includes('dark')) {
                        autoTheme = 'dark';
                        source = 'color-scheme property';
                    } else if (colorScheme.includes('light')) {
                        autoTheme = 'light';
                        source = 'color-scheme property';
                    }
                }
            }
        } catch (e) {}

        // Fallback to prefers-color-scheme
        if (!autoTheme && window.matchMedia) {
            if (window.matchMedia('(prefers-color-scheme: light)').matches) {
                autoTheme = 'light';
                source = 'prefers-color-scheme';
            } else if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
                autoTheme = 'dark';
                source = 'prefers-color-scheme';
            }
        }

        return {
            theme: autoTheme || 'dark',
            background: parsed ? (parsed.resolved || parsed.raw || chosen.value) : chosen.value,
            luminance,
            source,
        };
      };

      const App = () => {
        const [separateOutputs, setSeparateOutputs] = useState(initialSeparateOutputs);
        const [showTypes, setShowTypes] = useState(initialShowTypes);
        const [debugOverlays, setDebugOverlays] = useState(initialDebugOverlays);
        const [themeDebug, setThemeDebug] = useState({ source: 'init', luminance: null, background: 'transparent', appliedTheme: themePreference });
        
        // Sync debug overlay state with global flag for edge component access
        useEffect(() => {
          window.__hypergraph_debug_overlays = debugOverlays;
        }, [debugOverlays]);
        const [detectedTheme, setDetectedTheme] = useState(() => detectHostTheme());
        const [manualTheme, setManualTheme] = useState(null);
        const [bgColor, setBgColor] = useState(detectedTheme.background || 'transparent');
        
        // Track expansion state separately to preserve it across theme/toggle changes
        const [expansionState, setExpansionState] = useState(() => {
            const map = new Map();
            initialData.nodes.forEach(n => {
                if (n.data.nodeType === 'PIPELINE') {
                    map.set(n.id, n.data.isExpanded || false);
                }
            });
            return map;
        });
        
        // Use React Flow's state management
        const [rfNodes, setNodes, onNodesChange] = useNodesState([]);
        const [rfEdges, setEdges, onEdgesChange] = useEdgesState([]);
        
        const nodesRef = useRef(initialData.nodes);

        const resolvedDetected = detectedTheme || { theme: themePreference === 'auto' ? 'dark' : themePreference, background: 'transparent', luminance: null, source: 'init' };
        
        // manualTheme: null = Auto (use detected), 'light' = forced light, 'dark' = forced dark
        const activeTheme = useMemo(() => {
            if (manualTheme) return manualTheme;
            const base = themePreference === 'auto' ? (resolvedDetected.theme || 'dark') : themePreference;
            return base;
        }, [manualTheme, resolvedDetected.theme, themePreference]);
        
        // Background: Auto mode uses dynamic detected background, manual modes use predefined colors
        const activeBackground = useMemo(() => {
            // Manual override: use predefined colors
            if (manualTheme) return manualTheme === 'light' ? '#f8fafc' : '#020617';
            
            // Auto mode: use detected background from notebook environment
            const bg = resolvedDetected.background;
            if (!bg || bg === 'transparent' || bg === 'rgba(0, 0, 0, 0)') {
                return activeTheme === 'light' ? '#f8fafc' : '#020617';
            }
            return bg;
        }, [manualTheme, resolvedDetected.background, activeTheme]);
        const theme = activeTheme;

        // Expansion logic
        const onToggleExpand = useCallback((nodeId) => {
          setExpansionState(prev => {
            const newMap = new Map(prev);
            const isCurrentlyExpanded = newMap.get(nodeId) || false;
            const willExpand = !isCurrentlyExpanded;
            newMap.set(nodeId, willExpand);

            if (!willExpand) {
                const currentNodes = nodesRef.current || [];
                const childrenMap = new Map();
                currentNodes.forEach(n => {
                    if (n.parentNode) {
                        if (!childrenMap.has(n.parentNode)) childrenMap.set(n.parentNode, []);
                        childrenMap.get(n.parentNode).push(n.id);
                    }
                });

                const getDescendants = (id) => {
                    const children = childrenMap.get(id) || [];
                    let res = [...children];
                    children.forEach(childId => {
                        res = res.concat(getDescendants(childId));
                    });
                    return res;
                };

                getDescendants(nodeId).forEach(descId => {
                    if (newMap.has(descId)) newMap.set(descId, false);
                });
            }

            // Expose expansion state for debugging
            window.__hypergraphVizExpansionState = newMap;

            return newMap;
          });
        }, []);

        const applyStateFn = fallbackApplyState;
        const applyVisibilityFn = fallbackApplyVisibility;

        const stateResult = useMemo(() => {
            return applyStateFn(initialData.nodes, initialData.edges, {
                expansionState,
                separateOutputs,
                showTypes,
                theme: activeTheme,
            });
        }, [applyStateFn, initialData, expansionState, separateOutputs, showTypes, activeTheme]);

        // Add callbacks and visibility in a single path so hidden flags persist through toggles
        const nodesWithCallbacks = useMemo(() => stateResult.nodes.map(n => ({
            ...n,
            data: { ...n.data, onToggleExpand: n.data.nodeType === 'PIPELINE' ? () => onToggleExpand(n.id) : n.data.onToggleExpand },
        })), [stateResult.nodes, onToggleExpand]);

        const nodesWithVisibility = useMemo(() => {
            const nextNodes = applyVisibilityFn(nodesWithCallbacks, expansionState);
            nodesRef.current = nextNodes;
            return nextNodes;
        }, [nodesWithCallbacks, expansionState, applyVisibilityFn]);

        useEffect(() => {
          setNodes(nodesWithVisibility);
          setEdges(stateResult.edges);
        }, [nodesWithVisibility, stateResult.edges, setNodes, setEdges]);



        // Theme detection listener (updates detected theme only)
        useEffect(() => {
          const applyThemeDetection = () => {
             const detected = detectHostTheme();
             setDetectedTheme(detected);
          };

          applyThemeDetection();

          const observers = [];
          try { 
            const parentDoc = window.parent?.document;
            if (parentDoc) {
                const config = { attributes: true, attributeFilter: ['class', 'data-vscode-theme-kind', 'style'] };
                const observer = new MutationObserver(applyThemeDetection);
                observer.observe(parentDoc.body, config);
                observer.observe(parentDoc.documentElement, config);
                observers.push(observer);
            }
          } catch(e) {}

          const mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
          const mqHandler = () => applyThemeDetection();
          if (mq && mq.addEventListener) mq.addEventListener('change', mqHandler);
          else if (mq && mq.addListener) mq.addListener(mqHandler);

          return () => {
            observers.forEach(o => o.disconnect());
            if (mq && mq.removeEventListener) mq.removeEventListener('change', mqHandler);
            else if (mq && mq.removeListener) mq.removeListener(mqHandler);
          };
        }, [detectHostTheme]);

        // Apply effective theme + background
        useEffect(() => {
            setBgColor(activeBackground);
            document.body.classList.toggle('light-mode', activeTheme === 'light');
            
            // Poll for node styles for debugging
            const checkNodeStyles = () => {
                const node = document.querySelector('.react-flow__node');
                if (node) {
                    const computed = window.getComputedStyle(node.querySelector('div') || node); // Target inner div if possible
                    const nodeBg = computed.backgroundColor;
                    const nodeClass = (node.querySelector('div') || node).className;
                    setThemeDebug(prev => ({
                        ...prev,
                        nodeBg,
                        nodeClass: nodeClass.split(' ').find(c => c.startsWith('node-function-')) || 'unknown'
                    }));
                }
            };
            
            checkNodeStyles();
            const interval = setInterval(checkNodeStyles, 1000);

            if (showThemeDebug || debugOverlays) {
                setThemeDebug(prev => ({
                    ...prev,
                    source: manualTheme ? 'manual toggle' : resolvedDetected.source,
                    luminance: resolvedDetected.luminance,
                    background: activeBackground,
                    appliedTheme: activeTheme,
                }));
            }
            return () => clearInterval(interval);
        }, [activeTheme, activeBackground, resolvedDetected, showThemeDebug, themePreference, manualTheme, debugOverlays]);

        // Simple 2-state theme toggle:
        // - When in detected theme (manualTheme=null): show opposite theme's icon, click switches to opposite with predefined bg
        // - When in manual theme: click returns to detected theme with notebook's bg
        const toggleTheme = useCallback(() => {
            if (manualTheme === null) {
                // Currently in auto/detected mode → switch to opposite theme (predefined bg)
                const detected = resolvedDetected.theme || 'dark';
                setManualTheme(detected === 'dark' ? 'light' : 'dark');
            } else {
                // Currently in manual mode → return to detected theme (notebook bg)
                setManualTheme(null);
            }
        }, [manualTheme, resolvedDetected.theme]);

        // For flat graphs, edges don't need compression
        const compressedEdges = useMemo(() => {
            return stateResult.edges;
        }, [stateResult.edges]);

        // For flat graphs, no input grouping needed
        const { nodes: groupedNodes, edges: groupedEdges } = useMemo(() => {
            return { nodes: nodesWithVisibility, edges: compressedEdges };
        }, [nodesWithVisibility, compressedEdges]);

        const { layoutedNodes: rawLayoutedNodes, layoutedEdges, layoutError, graphHeight, graphWidth, layoutVersion, isLayouting } = useLayout(groupedNodes, groupedEdges);
        const { fitView, fitBounds, getViewport } = useReactFlow();
        const updateNodeInternals = useUpdateNodeInternals();

        // Custom fit function with FIXED pixel padding (not percentage)
        // This ensures consistent padding regardless of graph size
        const PADDING_TOP = 10;
        const PADDING_BOTTOM = 10;
        const PADDING_LEFT = 20;
        const PADDING_RIGHT = 70;  // Extra space for control buttons

        const fitWithFixedPadding = useCallback(() => {
            if (rawLayoutedNodes.length === 0) return;

            // Calculate bounds from nodes
            let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
            for (const node of rawLayoutedNodes) {
                const x = node.position?.x ?? 0;
                const y = node.position?.y ?? 0;
                const w = node.width ?? node.style?.width ?? 200;
                const h = node.height ?? node.style?.height ?? 50;
                minX = Math.min(minX, x);
                minY = Math.min(minY, y);
                maxX = Math.max(maxX, x + w);
                maxY = Math.max(maxY, y + h);
            }

            // Add fixed padding to bounds
            const bounds = {
                x: minX - PADDING_LEFT,
                y: minY - PADDING_TOP,
                width: (maxX - minX) + PADDING_LEFT + PADDING_RIGHT,
                height: (maxY - minY) + PADDING_TOP + PADDING_BOTTOM,
            };

            fitBounds(bounds, { duration: 0, minZoom: 0.3, maxZoom: 1.5 });
        }, [rawLayoutedNodes, fitBounds]);
        
        // ========================================================================
        // FIX: Force edge recalculation after node size changes (collapse/expand)
        // ========================================================================
        // PROBLEM: When a pipeline collapses, its DOM element shrinks, but React Flow
        // caches edge paths based on old handle positions. This causes "hanging arrows".
        // 
        // SOLUTION: Wait for layout to complete, then regenerate edges with new IDs.
        // The new edge components will calculate paths using updated node positions.
        // ========================================================================
        
        // ========================================================================
        // FIX: Force edge recalculation after collapse/expand
        // ========================================================================
        // PROBLEM: When a pipeline collapses, React Flow caches edge paths based on
        // old node dimensions, causing "hanging arrows".
        // 
        // SOLUTION: After expansion state changes, rapidly toggle the theme. This
        // triggers a full re-render of all nodes and edges, which recalculates
        // edge paths using current node dimensions. The toggle is imperceptible.
        // ========================================================================
        
        // ========================================================================
        // FIX: Force edge recalculation after collapse/expand
        // ========================================================================
        // PROBLEM: When a pipeline collapses, React Flow caches edge paths based on
        // old node dimensions/handle positions. The onTransitionEnd handler only
        // updates the collapsed node itself, but edges FROM this node still use
        // stale path calculations.
        // 
        // SOLUTION: After expansion state changes AND layout completes, call
        // updateNodeInternals for all visible nodes. This forces React Flow to
        // recalculate edge paths using current handle positions.
        // ========================================================================
        const prevExpansionRef = useRef(null);
        const expansionKey = useMemo(() => {
            return Array.from(expansionState.entries())
                .filter(([_, v]) => !v)  // Get collapsed pipelines
                .map(([k]) => k)
                .sort()
                .join(',');
        }, [expansionState]);
        
        useEffect(() => {
            // Skip on initial render
            if (prevExpansionRef.current === null) {
                prevExpansionRef.current = expansionKey;
                return;
            }
            
            // Only act if expansion state actually changed
            if (prevExpansionRef.current === expansionKey) {
                return;
            }
            prevExpansionRef.current = expansionKey;
            
            // Wait for layout AND DOM render to settle, then update all visible nodes
            // Use requestAnimationFrame to ensure we're after the paint
            const timer = setTimeout(() => {
                requestAnimationFrame(() => {
                    requestAnimationFrame(() => {
                        // Double RAF ensures we're after React render + paint
                        const visibleNodeIds = rawLayoutedNodes
                            .filter(n => !n.hidden)
                            .map(n => n.id);
                        
                        if (visibleNodeIds.length > 0 && window.__hypergraph_debug_edges) {
                            console.log('[expansion] Updating node internals for:', visibleNodeIds);
                        }
                        
                        if (visibleNodeIds.length > 0) {
                            // Batch update all visible nodes
                            visibleNodeIds.forEach(id => updateNodeInternals(id));
                        }
                    });
                });
            }, 500); // Increased delay to ensure DOM is fully settled
            
            return () => clearTimeout(timer);
        }, [expansionKey, rawLayoutedNodes, updateNodeInternals]);
        
        // Add debug mode to layouted nodes
        const layoutedNodes = useMemo(() => {
          return rawLayoutedNodes.map(n => ({
            ...n,
            data: { ...n.data, debugMode: debugOverlays }
          }));
        }, [rawLayoutedNodes, debugOverlays]);
        
        // Expose debug layout info to console API
        // Calculate absolute positions by accumulating parent offsets
        useEffect(() => {
          const nodeMap = new Map(layoutedNodes.map(n => [n.id, n]));
          
          // Helper to calculate absolute position
          const getAbsolutePosition = (node) => {
            let absX = node.position?.x || 0;
            let absY = node.position?.y || 0;
            let current = node;
            
            // Walk up parent chain to accumulate offsets
            while (current.parentNode) {
              const parent = nodeMap.get(current.parentNode);
              if (!parent) break;
              absX += parent.position?.x || 0;
              absY += parent.position?.y || 0;
              current = parent;
            }
            return { x: absX, y: absY };
          };
          
          window.__hypergraphVizLayout = {
            nodes: layoutedNodes.map(n => {
              const absPos = getAbsolutePosition(n);
              return {
                id: n.id,
                x: absPos.x,  // Absolute X position
                y: absPos.y,  // Absolute Y position
                width: n.style?.width,
                height: n.style?.height,
                hidden: n.hidden,
                nodeType: n.data?.nodeType,
                isExpanded: n.data?.isExpanded,
                parentNode: n.parentNode || null,
              };
            }),
            edges: layoutedEdges.map(e => ({
              id: e.id,
              source: e.source,
              target: e.target,
            })),
            version: layoutVersion,
          };
        }, [layoutedNodes, layoutedEdges, layoutVersion]);

        // --- Iframe Resize Logic (Task 2) ---
        useEffect(() => {
            if (graphHeight && graphWidth) {
                const desiredHeight = Math.max(400, graphHeight + 50);
                // Add extra width (100px) for the control buttons on the right side
                const desiredWidth = Math.max(400, graphWidth + 150);
                try {
                    // Try to resize the hosting iframe to avoid internal scrollbars and excess padding
                    if (window.frameElement) {
                        window.frameElement.style.height = desiredHeight + 'px';
                        window.frameElement.style.width = desiredWidth + 'px';
                    }
                } catch (e) {
                    // Ignore cross-origin errors or missing frameElement
                }
                
                // Notify parent window of size changes (for ScrollablePipelineWidget)
                try {
                    window.parent.postMessage({
                        type: 'hypergraph-viz-resize',
                        height: desiredHeight,
                        width: desiredWidth
                    }, '*');
                } catch (e) {
                    // Ignore if parent communication fails
                }
            }
        }, [graphHeight, graphWidth]);
        // --- Resize Handling (Task 2) ---
        useEffect(() => {
            const handleResize = () => {
                fitWithFixedPadding();
            };
            window.addEventListener('resize', handleResize);
            return () => window.removeEventListener('resize', handleResize);
        }, [fitWithFixedPadding]);

        // Re-fit when layout changes - Instant fit without animation
        useEffect(() => {
            if (layoutedNodes.length > 0) {
                // Immediate fit with no animation
                window.requestAnimationFrame(() => fitWithFixedPadding());
            }
        }, [layoutedNodes, fitWithFixedPadding]);

        const edgeOptions = {
            type: 'custom',
            sourcePosition: Position.Bottom,
            targetPosition: Position.Top,
            style: { stroke: theme === 'light' ? '#94a3b8' : '#64748b', strokeWidth: 2 },
            markerEnd: { type: MarkerType.ArrowClosed, color: theme === 'light' ? '#94a3b8' : '#64748b' },
        };

        // Style edges with theme-appropriate colors
        // Include expansionKey in edge IDs to force edge re-creation when pipelines collapse/expand
        const styledEdges = useMemo(() => {
            if (isLayouting) return []; // Hide edges during layout
            
            return layoutedEdges.map(e => {
                const isDataLink = e.data && e.data.isDataLink;
                return { 
                    ...e,
                    // Add version suffix to force React to re-mount edge component
                    id: expansionKey ? `${e.id}_exp_${expansionKey.replace(/,/g, '_')}` : e.id,
                    ...edgeOptions,
                    style: { 
                        ...edgeOptions.style, 
                        strokeWidth: isDataLink ? 1.5 : 2,
                    },
                    markerEnd: edgeOptions.markerEnd,
                    data: { ...e.data, debugMode: debugOverlays }
                };
            });
        }, [layoutedEdges, theme, isLayouting, debugOverlays, expansionKey]);

        // Notify parent to re-enable scroll overlay after any click interaction
        const notifyParentClick = useCallback(() => {
            try {
                window.parent.postMessage({ type: 'hypergraph-viz-click' }, '*');
            } catch (e) {
                // Ignore if parent communication fails
            }
        }, []);

        return html`
          <div 
            className=${`w-full relative overflow-hidden transition-colors duration-300`}
            style=${{ backgroundColor: bgColor, height: '100vh', width: '100vw' }}
            onClick=${notifyParentClick}
          >
            <!-- Background Grid -->
            <div className="absolute inset-0 bg-[url('https://grainy-gradients.vercel.app/noise.svg')] opacity-20 pointer-events-none mix-blend-overlay"></div>
            
            <${ReactFlow}
              key=${`rf_${expansionKey}`}
              nodes=${layoutedNodes}
              edges=${styledEdges}
              nodeTypes=${nodeTypes}
              edgeTypes=${edgeTypes}
              onNodesChange=${onNodesChange}
              onEdgesChange=${onEdgesChange}
              onNodeClick=${(e, node) => {
                // Handle click on collapsed pipeline nodes to expand
                if (node.data.nodeType === 'PIPELINE' && !node.data.isExpanded && node.data.onToggleExpand) {
                  e.stopPropagation();
                  node.data.onToggleExpand();
                }
              }}
              minZoom=${0.1}
              maxZoom=${2}
              className=${'bg-transparent'}
              panOnScroll=${panOnScroll}
              zoomOnScroll=${false}
              panOnDrag=${true}
              zoomOnPinch=${true} 
              preventScrolling=${false}
              style=${{ width: '100%', height: '100%' }}
            >
              <${Background} color=${theme === 'light' ? '#94a3b8' : '#334155'} gap=${24} size=${1} variant="dots" />
              <${CustomControls}
                theme=${theme}
                onToggleTheme=${toggleTheme}
                separateOutputs=${separateOutputs}
                onToggleSeparate=${() => setSeparateOutputs(s => !s)}
                showTypes=${showTypes}
                onToggleTypes=${() => setShowTypes(t => !t)}
                onFitView=${fitWithFixedPadding}
              />
              ${(showThemeDebug || debugOverlays) ? html`
              <${Panel} position="bottom-left" className=${`backdrop-blur-sm rounded-lg shadow-lg border text-xs px-3 py-2 mb-3 ml-3 max-w-xs pointer-events-auto
                    ${theme === 'light' ? 'bg-white/95 border-slate-200 text-slate-700' : 'bg-slate-900/90 border-slate-700 text-slate-200'}`}>
                  <div className="text-[10px] font-semibold tracking-wide uppercase opacity-70 mb-1">Theme Debug</div>
                  <div className="grid grid-cols-[60px_1fr] gap-x-2 gap-y-0.5">
                      <div className="opacity-70">Active:</div>
                      <div className="font-semibold">${theme}</div>
                      
                      <div className="opacity-70">Source:</div>
                      <div className="truncate" title=${themeDebug.source}>${themeDebug.source || 'n/a'}</div>
                      
                      <div className="opacity-70">BG Color:</div>
                      <div className="font-mono text-[10px] truncate" title=${bgColor}>${bgColor}</div>
                      
                      <div className="opacity-70">Node BG:</div>
                      <div className="font-mono text-[10px] truncate" title=${themeDebug.nodeBg}>${themeDebug.nodeBg || '...'}</div>
                      
                      <div className="opacity-70">Node Cls:</div>
                      <div className="font-mono text-[10px] truncate" title=${themeDebug.nodeClass}>${themeDebug.nodeClass || '...'}</div>
                  </div>
              <//>
              ` : null}
              ${debugOverlays ? html`<${DebugOverlay} nodes=${layoutedNodes} edges=${styledEdges} enabled=${debugOverlays} theme=${theme} />` : null}
            <//>
            ${(!isLayouting && (layoutError || (!layoutedNodes.length && rfNodes.length) || (!rfNodes.length))) ? html`
                <div className="absolute inset-0 pointer-events-none flex items-center justify-center">
                  <div className="px-4 py-2 rounded-lg border text-xs font-mono bg-slate-900/80 text-amber-200 border-amber-500/40 shadow-lg pointer-events-auto">
                    ${layoutError ? `Layout error: ${layoutError}` : (!rfNodes.length ? 'No graph data' : 'Layout produced no nodes. Showing fallback.')}
                    <button className="ml-4 underline text-amber-400 hover:text-amber-100" onClick=${() => window.location.reload()}>Reload</button>
                  </div>
                </div>
            ` : null}
          </div>
        `;
      };

      const root = ReactDOM.createRoot(document.getElementById('root'));
    root.render(html`
      <${ReactFlowProvider}>
        <${App} />
      <//>
    `);
      if (fallback) {
        fallback.remove();
      }
    } catch (err) {
      console.error(err);
      fail("Viz error: " + (err && err.message ? err.message : err));
    }
    })(); // End IIFE
    }); // End DOMContentLoaded
  </script>
  <script id="graph-data" type="application/json">__GRAPH_JSON__</script>
</body>
</html>"""

    html_template = html_head + html_body
    return html_template.replace("__GRAPH_JSON__", graph_json)
