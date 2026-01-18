/**
 * State utilities for hypergraph visualization.
 *
 * Handles client-side state transformations:
 * - Expand/collapse pipelines
 * - Show/hide nodes based on expansion state
 * - Apply visibility rules
 */

/**
 * Apply state transformations to nodes and edges.
 *
 * @param {Array} baseNodes - Initial nodes from Python
 * @param {Array} baseEdges - Initial edges from Python
 * @param {Object} options - {expansionState, separateOutputs, showTypes, theme}
 * @returns {{nodes: Array, edges: Array}}
 */
function applyState(baseNodes, baseEdges, options) {
    const { expansionState = {}, separateOutputs = false, showTypes = false, theme = "dark" } = options;

    let nodes = [...baseNodes];
    let edges = [...baseEdges];

    // 1. Apply expansion state to PIPELINE nodes
    nodes = nodes.map(node => {
        if (node.data.nodeType === "PIPELINE") {
            const isExpanded = expansionState[node.id] ?? node.data.isExpanded ?? false;
            return {
                ...node,
                data: { ...node.data, isExpanded }
            };
        }
        return node;
    });

    // 2. Apply theme and showTypes to all nodes
    nodes = nodes.map(node => ({
        ...node,
        data: { ...node.data, theme, showTypes }
    }));

    return { nodes, edges };
}

/**
 * Hide nodes inside collapsed pipelines.
 *
 * @param {Array} nodes - Nodes with expansion state applied
 * @param {Array} edges - All edges
 * @returns {{nodes: Array, edges: Array}}
 */
function applyVisibility(nodes, edges) {
    // Find all collapsed pipelines
    const collapsedPipelines = new Set(
        nodes
            .filter(n => n.data.nodeType === "PIPELINE" && !n.data.isExpanded)
            .map(n => n.id)
    );

    // Hide nodes whose parentNode is a collapsed pipeline
    const visibleNodes = nodes.filter(node => {
        if (!node.parentNode) return true;
        return !collapsedPipelines.has(node.parentNode);
    });

    const visibleNodeIds = new Set(visibleNodes.map(n => n.id));

    // Filter edges to only include those between visible nodes
    const visibleEdges = edges.filter(edge =>
        visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target)
    );

    return { nodes: visibleNodes, edges: visibleEdges };
}

/**
 * Compress edges that point to/from hidden nodes.
 * Redirects them to the parent pipeline node.
 *
 * @param {Array} nodes - All nodes (including hidden)
 * @param {Array} edges - All edges
 * @returns {Array} - Edges with redirections applied
 */
function compressEdges(nodes, edges) {
    // Build a map of node id -> parent pipeline
    const nodeToParent = {};
    nodes.forEach(node => {
        if (node.parentNode) {
            nodeToParent[node.id] = node.parentNode;
        }
    });

    // Find visible nodes
    const visibleNodes = new Set(
        nodes.filter(n => !n.hidden).map(n => n.id)
    );

    // Redirect edges
    return edges.map(edge => {
        let source = edge.source;
        let target = edge.target;

        // If source is hidden, redirect to its parent
        while (!visibleNodes.has(source) && nodeToParent[source]) {
            source = nodeToParent[source];
        }

        // If target is hidden, redirect to its parent
        while (!visibleNodes.has(target) && nodeToParent[target]) {
            target = nodeToParent[target];
        }

        if (source === target) {
            return null; // Self-loop after compression, remove
        }

        return {
            ...edge,
            source,
            target,
            id: `e_${source}_${target}_${edge.data?.valueName || ""}`
        };
    }).filter(Boolean);
}

/**
 * Group multiple input nodes into a single INPUT_GROUP node.
 *
 * @param {Array} nodes - Nodes
 * @param {Array} edges - Edges
 * @param {number} threshold - Minimum inputs to trigger grouping
 * @returns {{nodes: Array, edges: Array}}
 */
function groupInputs(nodes, edges, threshold = 3) {
    // Find input DATA nodes (no incoming edges)
    const nodeIds = new Set(nodes.map(n => n.id));
    const targetsWithIncoming = new Set(edges.map(e => e.target));

    const inputNodes = nodes.filter(n =>
        n.data.nodeType === "DATA" &&
        !targetsWithIncoming.has(n.id)
    );

    if (inputNodes.length < threshold) {
        return { nodes, edges };
    }

    // Create INPUT_GROUP node
    const groupNode = {
        id: "__input_group__",
        type: "custom",
        position: { x: 0, y: 0 },
        data: {
            nodeType: "INPUT_GROUP",
            label: "Inputs",
            inputs: inputNodes.map(n => ({
                name: n.data.label,
                type: n.data.outputs?.[0]?.type,
                isBound: n.data.isBound
            }))
        }
    };

    // Remove input nodes, add group
    const inputNodeIds = new Set(inputNodes.map(n => n.id));
    const filteredNodes = nodes.filter(n => !inputNodeIds.has(n.id));
    filteredNodes.push(groupNode);

    // Redirect edges from input nodes to group
    const redirectedEdges = edges.map(edge => {
        if (inputNodeIds.has(edge.source)) {
            return { ...edge, source: "__input_group__" };
        }
        return edge;
    });

    return { nodes: filteredNodes, edges: redirectedEdges };
}

// Export for use in other scripts
window.HypergraphStateUtils = {
    applyState,
    applyVisibility,
    compressEdges,
    groupInputs
};
