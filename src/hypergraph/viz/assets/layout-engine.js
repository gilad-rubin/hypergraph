/**
 * Dagre-based layout engine for hypergraph visualization.
 *
 * Thin wrapper around dagre that matches the ConstraintLayout API
 * expected by layout.js. Each container gets its own flat dagre graph
 * (no compound graphs — dagre has known bugs with external edges to
 * child nodes).
 *
 * Exports: window.ConstraintLayout = { graph, defaultOptions }
 */
(function() {
  'use strict';

  // === Constants ===
  var VizConstants = window.HypergraphVizConstants || {};
  var NODE_TYPE_OFFSETS = VizConstants.NODE_TYPE_OFFSETS || {
    'PIPELINE': 26, 'GRAPH': 26, 'FUNCTION': 14,
    'DATA': 6, 'INPUT': 6, 'INPUT_GROUP': 6, 'BRANCH': 10, 'END': 6,
  };
  var NODE_TYPE_TOP_INSETS = VizConstants.NODE_TYPE_TOP_INSETS || {
    'PIPELINE': 0, 'GRAPH': 0, 'FUNCTION': 0,
    'DATA': 0, 'INPUT': 0, 'INPUT_GROUP': 0, 'BRANCH': 3, 'END': 0,
  };
  var DEFAULT_OFFSET = VizConstants.DEFAULT_OFFSET ?? 10;
  var DEFAULT_TOP_INSET = VizConstants.DEFAULT_TOP_INSET ?? 0;

  // === Visible bounds helpers ===
  function resolveNodeType(node) {
    var nodeType = (node.data && node.data.nodeType) || 'FUNCTION';
    if (nodeType === 'PIPELINE' && !(node.data && node.data.isExpanded)) {
      nodeType = 'FUNCTION';
    }
    return nodeType;
  }

  function nodeVisibleBottom(node) {
    var offset = NODE_TYPE_OFFSETS[resolveNodeType(node)] ?? DEFAULT_OFFSET;
    return node.y + node.height * 0.5 - offset;
  }

  function nodeVisibleTop(node) {
    var inset = NODE_TYPE_TOP_INSETS[resolveNodeType(node)] ?? DEFAULT_TOP_INSET;
    return node.y - node.height * 0.5 + inset;
  }

  // === Dagre graph builder ===
  function runDagreLayout(nodes, edges, options) {
    var g = new dagre.graphlib.Graph();
    g.setGraph({
      rankdir: 'TB',
      nodesep: options.layout.spaceX,
      ranksep: options.layout.spaceY,
      marginx: 0,
      marginy: 0,
    });
    g.setDefaultEdgeLabel(function() { return {}; });

    // Build node ID set for filtering edges
    var nodeIds = new Set();
    nodes.forEach(function(n) {
      nodeIds.add(n.id);
      g.setNode(n.id, { width: n.width, height: n.height });
    });

    edges.forEach(function(e) {
      if (nodeIds.has(e.source) && nodeIds.has(e.target)) {
        g.setEdge(e.source, e.target);
      }
    });

    dagre.layout(g);

    // Read positions back into nodes
    var nodeById = {};
    nodes.forEach(function(n) { nodeById[n.id] = n; });

    g.nodes().forEach(function(id) {
      var pos = g.node(id);
      var node = nodeById[id];
      if (node && pos) {
        node.x = pos.x;
        node.y = pos.y;
      }
    });
  }

  // === Edge point generation ===
  function generateEdgePoints(edges, nodeById) {
    edges.forEach(function(e) {
      var src = nodeById[e.source];
      var tgt = nodeById[e.target];
      if (!src || !tgt) {
        e.points = [];
        return;
      }
      // 2-point straight path: srcVisibleBottom → tgtVisibleTop
      e.points = [
        { x: src.x, y: nodeVisibleBottom(src) },
        { x: tgt.x, y: nodeVisibleTop(tgt) },
      ];
    });
  }

  // === Bounds + offset ===
  function computeBounds(nodes, padding) {
    var size = {
      min: { x: Infinity, y: Infinity },
      max: { x: -Infinity, y: -Infinity },
    };

    nodes.forEach(function(n) {
      var left = n.x - n.width * 0.5;
      var right = n.x + n.width * 0.5;
      var top = n.y - n.height * 0.5;
      var bottom = n.y + n.height * 0.5;
      if (left < size.min.x) size.min.x = left;
      if (right > size.max.x) size.max.x = right;
      if (top < size.min.y) size.min.y = top;
      if (bottom > size.max.y) size.max.y = bottom;
    });

    size.width = size.max.x - size.min.x + 2 * padding;
    size.height = size.max.y - size.min.y + 2 * padding;
    size.min.x -= padding;
    size.min.y -= padding;

    return size;
  }

  function offsetNodes(nodes, min) {
    nodes.forEach(function(n) {
      n.x -= min.x;
      n.y -= min.y;
    });
  }

  function offsetEdges(edges, min) {
    edges.forEach(function(e) {
      if (!e.points) return;
      e.points.forEach(function(pt) {
        pt.x -= min.x;
        pt.y -= min.y;
      });
    });
  }

  // === Default options (matches old API shape) ===
  var defaultOptions = {
    layout: {
      spaceX: 42,
      spaceY: 140,
      padding: 70,
    },
    routing: {
      stemMinTarget: 15,
    },
  };

  // === Entry point ===
  function graph(nodes, edges, layers, orientation, options) {
    options = options || defaultOptions;

    // Run dagre layout
    runDagreLayout(nodes, edges, options);

    // Build lookup for edge point generation
    var nodeById = {};
    nodes.forEach(function(n) { nodeById[n.id] = n; });

    // Generate edge points
    generateEdgePoints(edges, nodeById);

    // Compute bounds and normalize to 0,0 origin
    var size = computeBounds(nodes, options.layout.padding);
    offsetNodes(nodes, size.min);
    offsetEdges(edges, size.min);

    return {
      nodes: nodes,
      edges: edges,
      layers: layers,
      size: size,
    };
  }

  // Export to global scope (same API as constraint-layout.js)
  window.ConstraintLayout = {
    graph: graph,
    defaultOptions: defaultOptions,
  };

})();
