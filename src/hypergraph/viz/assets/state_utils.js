/**
 * State management utilities for Hypergraph visualization
 * Handles node state transformations and visibility based on expansion/toggle states
 */
(function(root, factory) {
  var api = factory();
  if (root) root.HypergraphVizState = api;
})(typeof window !== 'undefined' ? window : this, function() {
  'use strict';

  /**
   * Apply state transformations to nodes based on current options
   * @param {Array} baseNodes - Original node array from graph data
   * @param {Array} baseEdges - Original edge array from graph data
   * @param {Object} options - State options
   * @param {Map|Object} options.expansionState - Which pipeline nodes are expanded
   * @param {boolean} options.separateOutputs - Whether to show DATA nodes separately
   * @param {boolean} options.showTypes - Whether to show type hints
   * @param {string} options.theme - Current theme ('light' or 'dark')
   * @returns {Object} { nodes, edges } with transformed state
   */
  function applyState(baseNodes, baseEdges, options) {
    var expansionState = options.expansionState;
    var separateOutputs = options.separateOutputs;
    var showTypes = options.showTypes;
    var theme = options.theme;

    var expMap = expansionState instanceof Map
      ? expansionState
      : new Map(Object.entries(expansionState || {}));

    // Identify DATA nodes (outputs) by their sourceId property
    var dataNodeIds = new Set(baseNodes.filter(function(n) { return n.data && n.data.sourceId; }).map(function(n) { return n.id; }));
    // Identify INPUT_GROUP nodes
    var inputGroupIds = new Set(baseNodes.filter(function(n) { return n.data && n.data.nodeType === 'INPUT_GROUP'; }).map(function(n) { return n.id; }));

    // Build function→output mapping for embedding outputs in function nodes
    var functionOutputs = {};
    baseNodes.forEach(function(n) {
      if (n.data && n.data.sourceId) {
        if (!functionOutputs[n.data.sourceId]) functionOutputs[n.data.sourceId] = [];
        functionOutputs[n.data.sourceId].push({ name: n.data.label, type: n.data.typeHint });
      }
    });

    var applyMeta = function(n) {
      var isPipeline = n.data && n.data.nodeType === 'PIPELINE';
      var expanded = isPipeline ? Boolean(expMap.get(n.id)) : undefined;
      return {
        ...n,
        type: isPipeline && expanded ? 'pipelineGroup' : n.type,
        style: isPipeline && !expanded ? undefined : n.style,
        data: {
          ...n.data,
          theme: theme,
          showTypes: showTypes,
          isExpanded: expanded,
        },
      };
    };

    // Helper: check if internal-only DATA node should be hidden
    // Note: INPUT nodes are NOT filtered here - they stay visible at root level
    // and are positioned inside containers via layout, not parent-child relationships
    var shouldFilterInternalData = function(n) {
      if (!n.data || n.data.nodeType !== 'DATA') return false;
      if (!n.data.internalOnly) return false;
      var parent = n.parentNode;
      if (!parent) return false;
      return !expMap.get(parent);
    };

    if (separateOutputs) {
      // Identify PIPELINE nodes (containers)
      var pipelineIds = new Set(baseNodes
        .filter(function(n) { return n.data && n.data.nodeType === 'PIPELINE'; })
        .map(function(n) { return n.id; }));

      // Show DATA nodes and INPUT_GROUP, clear embedded outputs from function nodes
      // BUT hide:
      // - Container DATA nodes when their container is expanded
      // - Internal-only DATA nodes when their parent is collapsed
      var nodes = baseNodes
        .filter(function(n) {
          // Hide internal-only DATA nodes when parent is collapsed
          if (shouldFilterInternalData(n)) return false;

          // Hide container DATA nodes when their container is expanded
          if (n.data && n.data.sourceId && pipelineIds.has(n.data.sourceId)) {
            // This is a container's DATA node - hide if container is expanded
            var isContainerExpanded = expMap.get(n.data.sourceId) || false;
            if (isContainerExpanded) return false;  // Hide
          }
          return true;  // Keep
        })
        .map(function(n) {
          var transformed = applyMeta(n);
          return {
            ...transformed,
            data: {
              ...transformed.data,
              separateOutputs: true,
              outputs: [],  // Clear embedded outputs when showing separate DATA nodes
            },
          };
        });
      return { nodes: nodes, edges: baseEdges };
    } else {
      // Hide DATA nodes (but keep INPUT_GROUP visible), embed outputs in function nodes, remap edges
      // Note: INPUT nodes are NOT filtered - they stay visible at root level
      var nodes = baseNodes
        .filter(function(n) { return !dataNodeIds.has(n.id); })  // Remove DATA nodes only
        .map(function(n) {
          var transformed = applyMeta(n);
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
      var edges = baseEdges
        .filter(function(e) { return !dataNodeIds.has(e.target); })  // Remove edges TO DATA nodes only
        .map(function(e) {
          if (dataNodeIds.has(e.source)) {
            // Edge FROM DATA node → remap to source function node
            var dataNode = baseNodes.find(function(n) { return n.id === e.source; });
            if (dataNode && dataNode.data && dataNode.data.sourceId) {
              return {
                ...e,
                id: 'e_' + dataNode.data.sourceId + '_' + e.target,
                source: dataNode.data.sourceId,
              };
            }
          }
          return e;
        });

      return { nodes: nodes, edges: edges };
    }
  }

  /**
   * Apply visibility based on expansion state
   * Nodes inside collapsed pipelines are hidden
   *
   * Additional scope-aware visibility rules:
   * - DATA nodes with internalOnly: hidden when their parent container is collapsed
   *
   * Note: INPUT/INPUT_GROUP nodes with ownerContainer are NOT hidden when collapsed.
   * Instead, they appear at root level and route to the container. When the container
   * is expanded, layout.js dynamically sets their parentNode to position them inside.
   *
   * @param {Array} nodes - Nodes with state applied
   * @param {Map|Object} expansionState - Which pipeline nodes are expanded
   * @returns {Array} Nodes with hidden flag set appropriately
   */
  function applyVisibility(nodes, expansionState) {
    var expMap = expansionState instanceof Map
      ? expansionState
      : new Map(Object.entries(expansionState || {}));

    var parentMap = new Map();
    nodes.forEach(function(n) {
      if (n.parentNode) parentMap.set(n.id, n.parentNode);
    });

    // Check if node is hidden due to collapsed ancestor
    var isHiddenByAncestor = function(nodeId) {
      var curr = nodeId;
      while (curr) {
        var parent = parentMap.get(curr);
        if (!parent) return false;
        if (expMap.get(parent) === false) return true;
        curr = parent;
      }
      return false;
    };

    // Check if DATA node should be hidden due to being internal-only
    var shouldHideInternalData = function(n) {
      if (!n.data || n.data.nodeType !== 'DATA') return false;
      if (!n.data.internalOnly) return false;
      // Internal-only DATA nodes are hidden when their parent is collapsed
      var parent = n.parentNode;
      if (!parent) return false;
      return !expMap.get(parent);
    };

    return nodes.map(function(n) {
      var hidden = isHiddenByAncestor(n.id) || shouldHideInternalData(n);
      return { ...n, hidden: hidden };
    });
  }

  // Export API
  return {
    applyState: applyState,
    applyVisibility: applyVisibility
  };
});
