"""Generate self-contained HTML for graph visualization."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any


def _read_vendor_asset(name: str) -> str:
    """Read a vendor asset file (React, ReactFlow, ELK, etc.)."""
    asset_files = files("hypergraph.viz.assets.vendor")
    return (asset_files / name).read_text(encoding="utf-8")


def _read_asset(name: str) -> str:
    """Read an asset file from our assets directory."""
    asset_files = files("hypergraph.viz.assets")
    return (asset_files / name).read_text(encoding="utf-8")


def generate_html(
    graph_data: dict[str, Any],
    *,
    width: int = 800,
    height: int = 600,
) -> str:
    """Generate self-contained HTML for visualizing a graph.

    All JavaScript and CSS are bundled inline - no external dependencies.
    Works offline and in restricted environments (VSCode notebooks).

    Args:
        graph_data: Output from renderer.render_graph()
        width: Visualization width in pixels
        height: Visualization height in pixels

    Returns:
        Complete HTML document as string
    """
    # Load vendor libraries
    react_js = _read_vendor_asset("react.production.min.js")
    react_dom_js = _read_vendor_asset("react-dom.production.min.js")
    reactflow_js = _read_vendor_asset("reactflow.umd.js")
    reactflow_css = _read_vendor_asset("reactflow.css")
    elk_js = _read_vendor_asset("elk.bundled.js")
    htm_js = _read_vendor_asset("htm.min.js")
    tailwind_css = _read_vendor_asset("tailwind.min.css")

    # Load our custom JS
    state_utils_js = _read_asset("state_utils.js")
    theme_utils_js = _read_asset("theme_utils.js")

    # Serialize graph data
    graph_json = json.dumps(graph_data)

    # Node styling - inline for simplicity
    node_styles = _get_node_styles_js()

    # React components
    react_components = _get_react_components_js()

    # Main app initialization
    app_init = _get_app_init_js()

    return f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>{tailwind_css}</style>
    <style>{reactflow_css}</style>
    <style>
        html, body, #root {{
            width: 100%;
            height: 100%;
            margin: 0;
            padding: 0;
            overflow: hidden;
        }}
        .react-flow__node {{
            cursor: default;
        }}
        .react-flow__node.selected {{
            outline: none;
        }}
    </style>
</head>
<body>
    <div id="root"></div>

    <!-- Vendor Libraries -->
    <script>{react_js}</script>
    <script>{react_dom_js}</script>
    <script>{reactflow_js}</script>
    <script>{elk_js}</script>
    <script>{htm_js}</script>

    <!-- Our Utilities -->
    <script>{state_utils_js}</script>
    <script>{theme_utils_js}</script>

    <!-- Graph Data -->
    <script id="graph-data" type="application/json">{graph_json}</script>

    <!-- Node Styles -->
    <script>{node_styles}</script>

    <!-- React Components -->
    <script>{react_components}</script>

    <!-- App Initialization -->
    <script>{app_init}</script>
</body>
</html>'''


def _get_node_styles_js() -> str:
    """Generate JavaScript object with node styling definitions."""
    return '''
const NODE_STYLES = {
    FUNCTION: {
        dark: {
            border: "border-indigo-500/50",
            bg: "bg-slate-800",
            iconColor: "text-indigo-400",
            text: "text-slate-100"
        },
        light: {
            border: "border-indigo-400",
            bg: "bg-white",
            iconColor: "text-indigo-600",
            text: "text-slate-900"
        },
        icon: "fn"
    },
    PIPELINE: {
        dark: {
            border: "border-amber-500/50",
            bg: "bg-slate-800",
            iconColor: "text-amber-400",
            text: "text-slate-100"
        },
        light: {
            border: "border-amber-400",
            bg: "bg-white",
            iconColor: "text-amber-600",
            text: "text-slate-900"
        },
        icon: "{}"
    },
    ROUTE: {
        dark: {
            border: "border-purple-500/50",
            bg: "bg-slate-800",
            iconColor: "text-purple-400",
            text: "text-slate-100"
        },
        light: {
            border: "border-purple-400",
            bg: "bg-white",
            iconColor: "text-purple-600",
            text: "text-slate-900"
        },
        icon: "?"
    },
    DATA: {
        dark: {
            border: "border-emerald-500/50",
            bg: "bg-slate-800",
            iconColor: "text-emerald-400",
            text: "text-slate-100"
        },
        light: {
            border: "border-emerald-400",
            bg: "bg-white",
            iconColor: "text-emerald-600",
            text: "text-slate-900"
        },
        icon: "o"
    }
};

function getNodeStyle(nodeType, theme) {
    const styles = NODE_STYLES[nodeType] || NODE_STYLES.FUNCTION;
    return theme === "dark" ? styles.dark : styles.light;
}

function getNodeIcon(nodeType) {
    const styles = NODE_STYLES[nodeType] || NODE_STYLES.FUNCTION;
    return styles.icon;
}
'''


def _get_react_components_js() -> str:
    """Generate React components using HTM."""
    return '''
// Bind HTM to React.createElement
const html = htm.bind(React.createElement);

// Get React Flow components from the UMD global
const { ReactFlow, Handle, Position, ReactFlowProvider, useNodesState, useEdgesState } = window.ReactFlow;

// Truncate long strings
function truncate(str, maxLen) {
    if (!str) return "";
    return str.length > maxLen ? str.slice(0, maxLen - 1) + "…" : str;
}

// Custom Node Component
function CustomNode({ id, data }) {
    const { nodeType, label, outputs, inputs, isExpanded, theme, showTypes } = data;
    const style = getNodeStyle(nodeType, theme || "dark");
    const icon = getNodeIcon(nodeType);

    const hasOutputs = outputs && outputs.length > 0;

    return html`
        <div class="rounded-lg border ${style.border} ${style.bg} shadow-md" style=${{ minWidth: "120px" }}>
            <!-- Target Handle -->
            <${Handle}
                type="target"
                position=${Position.Top}
                style=${{ background: "#64748b", width: "8px", height: "8px" }}
            />

            <!-- Header -->
            <div class="px-3 py-2 flex items-center gap-2">
                <span class="${style.iconColor} text-xs font-mono">${icon}</span>
                <span class="${style.text} font-medium text-sm truncate">${truncate(label, 25)}</span>
                ${nodeType === "PIPELINE" && html`
                    <span class="ml-auto text-xs ${style.iconColor}">
                        ${isExpanded ? "−" : "+"}
                    </span>
                `}
            </div>

            <!-- Outputs Section -->
            ${hasOutputs && html`
                <div class="border-t ${style.border} px-2 py-1">
                    ${outputs.map((out, i) => html`
                        <div key=${i} class="flex items-center gap-1 text-xs">
                            <span class="text-slate-400">→</span>
                            <span class="${style.text}">${truncate(out.name, 20)}</span>
                            ${showTypes && out.type && html`
                                <span class="text-slate-500">: ${truncate(out.type, 15)}</span>
                            `}
                        </div>
                    `)}
                </div>
            `}

            <!-- Source Handle -->
            <${Handle}
                type="source"
                position=${Position.Bottom}
                style=${{ background: "#64748b", width: "8px", height: "8px" }}
            />
        </div>
    `;
}

// Register the custom node type
const nodeTypes = {
    custom: CustomNode
};

// Calculate node dimensions for ELK
function calculateNodeDimensions(node) {
    const CHAR_WIDTH = 7;
    const HEADER_HEIGHT = 36;
    const OUTPUT_ROW_HEIGHT = 20;
    const PADDING = 48;
    const MAX_WIDTH = 280;
    const MIN_WIDTH = 120;

    const labelWidth = (node.data.label?.length || 0) * CHAR_WIDTH;
    const outputWidths = (node.data.outputs || []).map(o => {
        const nameLen = (o.name?.length || 0);
        const typeLen = node.data.showTypes ? (o.type?.length || 0) + 2 : 0;
        return (nameLen + typeLen) * CHAR_WIDTH;
    });

    const maxContent = Math.max(labelWidth, ...outputWidths, 0);
    const width = Math.min(Math.max(maxContent + PADDING, MIN_WIDTH), MAX_WIDTH);

    const outputCount = node.data.outputs?.length || 0;
    const height = HEADER_HEIGHT + (outputCount > 0 ? outputCount * OUTPUT_ROW_HEIGHT + 8 : 0);

    return { width, height };
}

// ELK Layout Hook
function useLayoutedElements(initialNodes, initialEdges) {
    const [nodes, setNodes] = React.useState([]);
    const [edges, setEdges] = React.useState([]);
    const [isLayouting, setIsLayouting] = React.useState(true);

    React.useEffect(() => {
        if (!initialNodes.length) {
            setIsLayouting(false);
            return;
        }

        async function runLayout() {
            setIsLayouting(true);

            const elk = new ELK();

            // Build ELK graph
            const elkGraph = {
                id: "root",
                layoutOptions: {
                    "elk.algorithm": "layered",
                    "elk.direction": "DOWN",
                    "elk.spacing.nodeNode": "50",
                    "elk.layered.spacing.nodeNodeBetweenLayers": "50",
                    "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP"
                },
                children: initialNodes.map(node => {
                    const dims = calculateNodeDimensions(node);
                    return {
                        id: node.id,
                        width: dims.width,
                        height: dims.height
                    };
                }),
                edges: initialEdges.map(edge => ({
                    id: edge.id,
                    sources: [edge.source],
                    targets: [edge.target]
                }))
            };

            try {
                const layouted = await elk.layout(elkGraph);

                // Apply positions back to nodes
                const positionedNodes = initialNodes.map(node => {
                    const elkNode = layouted.children?.find(n => n.id === node.id);
                    return {
                        ...node,
                        position: elkNode
                            ? { x: elkNode.x || 0, y: elkNode.y || 0 }
                            : { x: 0, y: 0 }
                    };
                });

                setNodes(positionedNodes);
                setEdges(initialEdges);
            } catch (error) {
                console.error("ELK layout error:", error);
                // Fallback: simple vertical layout
                const positioned = initialNodes.map((node, i) => ({
                    ...node,
                    position: { x: 100, y: i * 100 }
                }));
                setNodes(positioned);
                setEdges(initialEdges);
            }

            setIsLayouting(false);
        }

        runLayout();
    }, [initialNodes, initialEdges]);

    return { nodes, edges, isLayouting };
}

// Main Graph Component
function GraphVisualization({ graphData }) {
    const { nodes: initialNodes, edges: initialEdges, options } = graphData;

    // Detect theme if auto
    const theme = React.useMemo(() => {
        if (options.theme !== "auto") return options.theme;
        return detectTheme().theme;
    }, [options.theme]);

    // Apply theme to nodes
    const themedNodes = React.useMemo(() => {
        return initialNodes.map(node => ({
            ...node,
            data: { ...node.data, theme, showTypes: options.showTypes }
        }));
    }, [initialNodes, theme, options.showTypes]);

    // Run ELK layout
    const { nodes, edges, isLayouting } = useLayoutedElements(themedNodes, initialEdges);

    if (isLayouting) {
        return html`<div class="flex items-center justify-center h-full text-slate-500">
            Loading...
        </div>`;
    }

    const bgColor = theme === "dark" ? "#0f172a" : "#f8fafc";

    return html`
        <${ReactFlow}
            nodes=${nodes}
            edges=${edges}
            nodeTypes=${nodeTypes}
            fitView
            fitViewOptions=${{ padding: 0.2 }}
            style=${{ background: bgColor }}
            nodesDraggable=${true}
            nodesConnectable=${false}
            elementsSelectable=${true}
            zoomOnScroll=${true}
            panOnScroll=${false}
            panOnDrag=${true}
            preventScrolling=${true}
        />
    `;
}
'''


def _get_app_init_js() -> str:
    """Generate app initialization code."""
    return '''
(function() {
    "use strict";

    document.addEventListener("DOMContentLoaded", function() {
        // Parse graph data
        const dataElement = document.getElementById("graph-data");
        if (!dataElement) {
            console.error("Graph data not found");
            return;
        }

        const graphData = JSON.parse(dataElement.textContent);

        // Render the app
        const root = ReactDOM.createRoot(document.getElementById("root"));
        root.render(
            html`
                <${ReactFlowProvider}>
                    <${GraphVisualization} graphData=${graphData} />
                </${ReactFlowProvider}>
            `
        );
    });
})();
'''
