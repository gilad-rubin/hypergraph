/**
 * Dagre layout and React layout hook for Hypergraph visualization.
 */
(function(root) {
  'use strict';

  var R = root.HypergraphVizRuntime;
  if (!R) {
    console.error('HypergraphVizLayout: Missing HypergraphVizRuntime');
    return;
  }

  var dagre = R.dagre;
  var useState = R.useState;
  var useEffect = R.useEffect;
  var calculateDimensions = R.calculateDimensions;
  var resolveNodeType = R.resolveNodeType;
  var getOffset = R.getOffset;
  var getTopInset = R.getTopInset;
  var getVisibleTop = R.getVisibleTop;
  var LAYOUT_PADDING = R.LAYOUT_PADDING;
  var EDGE_ENDPOINT_PADDING = R.EDGE_ENDPOINT_PADDING;
  var LAYOUT_RANKSEP = R.LAYOUT_RANKSEP;
  var FEEDBACK_EDGE_GUTTER = R.FEEDBACK_EDGE_GUTTER;
  var FEEDBACK_EDGE_HEADROOM = R.FEEDBACK_EDGE_HEADROOM;
  var FEEDBACK_EDGE_STEM = R.FEEDBACK_EDGE_STEM;

  // ╔═══════════════════════════════════════════════════════════╗
  // ║  Section 3: Dagre Layout Engine                          ║
  // ╚═══════════════════════════════════════════════════════════╝

  function computeBounds(nodes, padding) {
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    nodes.forEach(function(n) {
      minX = Math.min(minX, n.x - n.width * 0.5);
      maxX = Math.max(maxX, n.x + n.width * 0.5);
      minY = Math.min(minY, n.y - n.height * 0.5);
      maxY = Math.max(maxY, n.y + n.height * 0.5);
    });
    return {
      min: { x: minX - padding, y: minY - padding },
      width: maxX - minX + 2 * padding,
      height: maxY - minY + 2 * padding,
    };
  }

  function offsetAll(nodes, edges, min) {
    nodes.forEach(function(n) { n.x -= min.x; n.y -= min.y; });
    edges.forEach(function(e) {
      if (!e.points) return;
      e.points.forEach(function(pt) { pt.x -= min.x; pt.y -= min.y; });
    });
  }

  /**
   * Run dagre layout and read native edge routing points.
   * This is THE key change: dagre computes multi-point edge paths,
   * and we use them directly instead of generating 2-point straight lines.
   */
  function layoutGraph(nodes, edges, endpointPadding, ranksep) {
    if (endpointPadding === undefined) endpointPadding = EDGE_ENDPOINT_PADDING;
    if (ranksep === undefined) ranksep = LAYOUT_RANKSEP;
    var g = new dagre.graphlib.Graph({ multigraph: true });
    g.setGraph({ rankdir: 'TB', nodesep: 42, ranksep: ranksep, marginx: 0, marginy: 0 });
    g.setDefaultEdgeLabel(function() { return {}; });

    var nodeIds = new Set();
    nodes.forEach(function(n) {
      nodeIds.add(n.id);
      g.setNode(n.id, { width: n.width, height: n.height });
    });

    edges.forEach(function(e) {
      if (nodeIds.has(e.source) && nodeIds.has(e.target)) {
        g.setEdge(e.source, e.target, {}, e.id);
      }
    });

    dagre.layout(g);

    // Read node positions
    var nodeById = {};
    nodes.forEach(function(n) { nodeById[n.id] = n; });
    g.nodes().forEach(function(id) {
      var pos = g.node(id);
      var node = nodeById[id];
      if (node && pos) { node.x = pos.x; node.y = pos.y; }
    });

    // Read edge routing points from dagre (THE FIX)
    edges.forEach(function(e) {
      if (!nodeIds.has(e.source) || !nodeIds.has(e.target)) { e.points = []; return; }
      var src = nodeById[e.source], tgt = nodeById[e.target];
      if (!src || !tgt) { e.points = []; return; }

      var srcType = resolveNodeType(src.data && src.data.nodeType, src.data && src.data.isExpanded);
      var tgtType = resolveNodeType(tgt.data && tgt.data.nodeType, tgt.data && tgt.data.isExpanded);
      var srcBottom = src.y + src.height * 0.5 - getOffset(srcType);
      var tgtTop = tgt.y - tgt.height * 0.5 + getTopInset(tgtType);

      // BRANCH/START/END nodes always use center-x for clean vertical anchors
      var srcForceCenterX = srcType === 'BRANCH' || srcType === 'START' || srcType === 'END';
      var tgtForceCenterX = tgtType === 'BRANCH' || tgtType === 'START' || tgtType === 'END';

      var dagreEdge = g.edge(e.source, e.target, e.id);
      if (dagreEdge && dagreEdge.points && dagreEdge.points.length > 0) {
        var pts = dagreEdge.points.map(function(p) { return { x: p.x, y: p.y }; });
        var srcPad = src.width * endpointPadding;
        var tgtPad = tgt.width * endpointPadding;
        var srcLeft = src.x - src.width * 0.5 + srcPad;
        var srcRight = src.x + src.width * 0.5 - srcPad;
        var tgtLeft = tgt.x - tgt.width * 0.5 + tgtPad;
        var tgtRight = tgt.x + tgt.width * 0.5 - tgtPad;
        pts[0] = {
          x: srcForceCenterX ? src.x : Math.max(srcLeft, Math.min(srcRight, pts[0].x)),
          y: srcBottom,
        };
        pts[pts.length - 1] = {
          x: tgtForceCenterX ? tgt.x : Math.max(tgtLeft, Math.min(tgtRight, pts[pts.length - 1].x)),
          y: tgtTop,
        };
        e.points = pts;
      } else {
        throw new Error('Dagre did not return routing points for edge ' + (e.id || (e.source + ' -> ' + e.target)));
      }
    });

    var size = computeBounds(nodes, LAYOUT_PADDING);
    offsetAll(nodes, edges, size.min);
    return { nodes: nodes, edges: edges, size: size };
  }

  // ── Feedback edge selection (Python-precomputed hints) ──

  function computeFeedbackEdgeKeys(nodes, edges) {
    var feedback = new Set();

    // Python-precomputed contract: this edge must be routed as feedback.
    edges.forEach(function(edge) {
      var d = edge.data || (edge._original && edge._original.data) || {};
      if (d && d.forceFeedback) feedback.add(edge.id);
    });
    return feedback;
  }

  function buildFeedbackEdgePoints(edge, nodePositions, nodeDimensions, nodeTypes) {
    var sp = nodePositions.get(edge.source), tp = nodePositions.get(edge.target);
    var sd = nodeDimensions.get(edge.source), td = nodeDimensions.get(edge.target);
    if (!sp || !tp || !sd || !td) return null;

    var srcCX = sp.x + sd.width / 2;
    var srcType = nodeTypes.get(edge.source) || 'FUNCTION';
    var srcBY = sp.y + sd.height - getOffset(srcType);
    var tgtCX = tp.x + td.width / 2;
    var tgtType = nodeTypes.get(edge.target) || 'FUNCTION';
    var tgtTY = getVisibleTop(tp, tgtType);

    var gutterX = Math.max(0, Math.min(sp.x, tp.x) - FEEDBACK_EDGE_GUTTER);
    var stemY = srcBY + FEEDBACK_EDGE_STEM;
    var loopY = Math.max(0, Math.min(sp.y, tp.y) - FEEDBACK_EDGE_HEADROOM);
    if (loopY >= stemY) loopY = Math.max(0, stemY - FEEDBACK_EDGE_HEADROOM);

    return [
      { x: srcCX, y: srcBY }, { x: srcCX, y: stemY },
      { x: gutterX, y: stemY }, { x: gutterX, y: loopY },
      { x: tgtCX, y: loopY }, { x: tgtCX, y: tgtTY },
    ];
  }

  function adjustEdgeEndpoints(edge, points, nodeById, endpointPadding) {
    if (!points || points.length < 2) return points;
    var src = nodeById.get(edge.source), tgt = nodeById.get(edge.target);
    if (!src || !tgt) return points;

    var srcType = resolveNodeType(src.data && src.data.nodeType, src.data && src.data.isExpanded);
    var tgtType = resolveNodeType(tgt.data && tgt.data.nodeType, tgt.data && tgt.data.isExpanded);
    var srcBottom = src.y + src.height * 0.5 - getOffset(srcType);
    var tgtTop = tgt.y - tgt.height * 0.5 + getTopInset(tgtType);
    var srcForceCenterX = srcType === 'BRANCH' || srcType === 'START' || srcType === 'END';
    var tgtForceCenterX = tgtType === 'BRANCH' || tgtType === 'START' || tgtType === 'END';
    var srcPad = src.width * endpointPadding;
    var tgtPad = tgt.width * endpointPadding;
    var srcLeft = src.x - src.width * 0.5 + srcPad;
    var srcRight = src.x + src.width * 0.5 - srcPad;
    var tgtLeft = tgt.x - tgt.width * 0.5 + tgtPad;
    var tgtRight = tgt.x + tgt.width * 0.5 - tgtPad;

    points[0] = {
      x: srcForceCenterX ? src.x : Math.max(srcLeft, Math.min(srcRight, points[0].x)),
      y: srcBottom,
    };
    points[points.length - 1] = {
      x: tgtForceCenterX ? tgt.x : Math.max(tgtLeft, Math.min(tgtRight, points[points.length - 1].x)),
      y: tgtTop,
    };
    return points;
  }

  // ── Compound dagre layout for visible nested graphs ──

  function performCompoundLayout(visibleNodes, edges, expansionState, endpointPadding, ranksep) {
    var g = new dagre.graphlib.Graph({ compound: true, multigraph: true });
    g.setGraph({ rankdir: 'TB', nodesep: 42, ranksep: ranksep, marginx: 0, marginy: 0 });
    g.setDefaultEdgeLabel(function() { return {}; });

    var visibleIds = new Set(visibleNodes.map(function(n) { return n.id; }));
    var visibleNodeById = new Map(visibleNodes.map(function(n) { return [n.id, n]; }));
    var displayParentById = new Map();
    var layoutNodes = [];

    visibleNodes.forEach(function(n) {
      var d = calculateDimensions(n);
      layoutNodes.push({ id: n.id, width: d.width, height: d.height, x: 0, y: 0, data: n.data, _original: n });
      g.setNode(n.id, { width: d.width, height: d.height });
    });

    visibleNodes.forEach(function(n) {
      var parentId = null;
      if (n.parentNode && visibleIds.has(n.parentNode)) {
        parentId = n.parentNode;
      } else if (n.data && (n.data.nodeType === 'INPUT' || n.data.nodeType === 'INPUT_GROUP')) {
        var owner = n.data.deepestOwnerContainer || n.data.ownerContainer;
        var ownerPrefix = owner ? owner + '/' : '';
        var outgoing = edges.filter(function(e) { return e.source === n.id && visibleIds.has(e.target); });
        var onlyFeedsOwner = outgoing.length > 0 && outgoing.every(function(e) {
          return e.target === owner || e.target.indexOf(ownerPrefix) === 0;
        });
        if (owner && visibleIds.has(owner) && expansionState.get(owner) && onlyFeedsOwner) parentId = owner;
      }

      if (parentId) {
        displayParentById.set(n.id, parentId);
        g.setParent(n.id, parentId);
      }
    });

    var feedbackEdgeKeys = computeFeedbackEdgeKeys(visibleNodes, edges);
    var layoutEdges = edges
      .filter(function(e) { return visibleIds.has(e.source) && visibleIds.has(e.target); })
      .filter(function(e) { return !feedbackEdgeKeys.has(e.id); })
      .map(function(e) { return { id: e.id, source: e.source, target: e.target, _original: e }; });

    var rootInputsWithRootFunctionTargets = new Set();
    layoutEdges.forEach(function(e) {
      if (displayParentById.get(e.source) || displayParentById.get(e.target)) return;
      var sourceNode = visibleNodeById.get(e.source);
      var sourceType = (sourceNode && sourceNode.data && sourceNode.data.nodeType) || 'FUNCTION';
      if (sourceType !== 'INPUT' && sourceType !== 'INPUT_GROUP') return;
      var targetNode = visibleNodeById.get(e.target);
      var targetType = (targetNode && targetNode.data && targetNode.data.nodeType) || 'FUNCTION';
      if (targetType !== 'PIPELINE') rootInputsWithRootFunctionTargets.add(e.source);
    });

    layoutEdges.forEach(function(e) {
      var sourceParent = displayParentById.get(e.source);
      var targetParent = displayParentById.get(e.target);
      var sourceNode = visibleNodeById.get(e.source);
      var sourceType = (sourceNode && sourceNode.data && sourceNode.data.nodeType) || 'FUNCTION';
      var edgeLabel = {};
      if (!sourceParent && targetParent && (sourceType === 'INPUT' || sourceType === 'INPUT_GROUP')) {
        edgeLabel = { minlen: rootInputsWithRootFunctionTargets.has(e.source) ? 4 : 1, weight: 10 };
      }
      g.setEdge(e.source, e.target, edgeLabel, e.id);
    });
    dagre.layout(g);

    var nodeById = new Map();
    layoutNodes.forEach(function(n) {
      var pos = g.node(n.id);
      if (pos) {
        n.x = pos.x;
        n.y = pos.y;
        n.width = pos.width || n.width;
        n.height = pos.height || n.height;
      }
      nodeById.set(n.id, n);
    });

    layoutEdges.forEach(function(e) {
      var dagreEdge = g.edge(e.source, e.target, e.id);
      if (!dagreEdge || !dagreEdge.points || !dagreEdge.points.length) {
        throw new Error('Dagre did not return routing points for edge ' + (e.id || (e.source + ' -> ' + e.target)));
      }
      e.points = adjustEdgeEndpoints(
        e,
        dagreEdge.points.map(function(p) { return { x: p.x, y: p.y }; }),
        nodeById,
        endpointPadding
      );
    });

    var size = computeBounds(layoutNodes, LAYOUT_PADDING);
    offsetAll(layoutNodes, layoutEdges, size.min);

    var nodePositions = new Map();
    var nodeDimensions = new Map();
    var nodeTypes = new Map();
    layoutNodes.forEach(function(n) {
      nodePositions.set(n.id, { x: n.x - n.width / 2, y: n.y - n.height / 2 });
      nodeDimensions.set(n.id, { width: n.width, height: n.height });
      var nt = (n.data && n.data.nodeType) || 'FUNCTION';
      if (nt === 'PIPELINE' && !(n.data && n.data.isExpanded)) nt = 'FUNCTION';
      nodeTypes.set(n.id, nt);
    });

    var allNodes = layoutNodes.map(function(n) {
      var parentId = displayParentById.get(n.id);
      var absPos = nodePositions.get(n.id);
      var pos = absPos;
      var nwp = { ...n._original };
      if (parentId) {
        var parentPos = nodePositions.get(parentId);
        if (parentPos) pos = { x: absPos.x - parentPos.x, y: absPos.y - parentPos.y };
        nwp.parentNode = parentId;
        nwp.extent = 'parent';
      }
      return {
        ...nwp, position: pos, width: n.width, height: n.height,
        style: { ...nwp.style, width: n.width, height: n.height },
        handles: [
          { type: 'target', position: 'top', x: n.width / 2, y: 0, width: 8, height: 8, id: null },
          { type: 'source', position: 'bottom', x: n.width / 2, y: n.height, width: 8, height: 8, id: null },
        ],
      };
    });

    var allEdges = layoutEdges.map(function(e) {
      return { ...e._original, data: { ...e._original.data, points: e.points } };
    });

    edges
      .filter(function(e) { return visibleIds.has(e.source) && visibleIds.has(e.target); })
      .filter(function(e) { return feedbackEdgeKeys.has(e.id); })
      .forEach(function(e) {
        var pts = buildFeedbackEdgePoints(e, nodePositions, nodeDimensions, nodeTypes);
        if (pts) allEdges.push({ ...e, data: { ...e.data, points: pts, isFeedbackEdge: true } });
      });

    return { nodes: allNodes, edges: allEdges, size: size };
  }

  // ── useLayout hook ──

  function useLayout(nodes, edges, expansionState, endpointPadding, ranksep) {
    var nodesState = useState([]), edgesState = useState([]);
    var errorState = useState(null), versionState = useState(0);
    var heightState = useState(600), widthState = useState(600), busyState = useState(false);

    useEffect(function() {
      if (!nodes.length) { busyState[1](false); return; }
      busyState[1](true);
      try {
        var visible = nodes.filter(function(n) { return !n.hidden; });

        if (expansionState && expansionState.size > 0) {
          var res = performCompoundLayout(visible, edges, expansionState, endpointPadding, ranksep);
          nodesState[1](res.nodes); edgesState[1](res.edges);
          versionState[1](function(v) { return v + 1; }); busyState[1](false); errorState[1](null);
          if (res.size) { widthState[1](res.size.width); heightState[1](res.size.height); }
          return;
        }

        // Flat layout
        var flat = visible.filter(function(n) { return !n.parentNode; });
        var flatIds = new Set(flat.map(function(n) { return n.id; }));
        var visEdges = edges.filter(function(e) { return flatIds.has(e.source) && flatIds.has(e.target); });
        var fbKeys = computeFeedbackEdgeKeys(flat, visEdges);
        var layoutNodes = flat.map(function(n) {
          var d = calculateDimensions(n);
          return { id: n.id, width: d.width, height: d.height, x: 0, y: 0, data: n.data, _original: n };
        });
        var layoutEdges = visEdges
          .filter(function(e) { return !fbKeys.has(e.id); })
          .map(function(e) { return { id: e.id, source: e.source, target: e.target, _original: e }; });

        var result = layoutGraph(layoutNodes, layoutEdges, endpointPadding, ranksep);

        var positioned = result.nodes.map(function(n) {
          return {
            ...n._original, position: { x: n.x - n.width / 2, y: n.y - n.height / 2 },
            width: n.width, height: n.height,
            style: { ...n._original.style, width: n.width, height: n.height },
            handles: [
              { type: 'target', position: 'top', x: n.width / 2, y: 0, width: 8, height: 8, id: null },
              { type: 'source', position: 'bottom', x: n.width / 2, y: n.height, width: 8, height: 8, id: null },
            ],
          };
        });
        var posEdges = result.edges.map(function(e) {
          return { ...e._original, data: { ...e._original.data, points: e.points } };
        });

        // Add feedback edges
        var nodePos = new Map(), nodeDim = new Map(), nodeTyp = new Map();
        positioned.forEach(function(n) {
          nodePos.set(n.id, n.position); nodeDim.set(n.id, { width: n.width, height: n.height });
          var nt = (n.data && n.data.nodeType) || 'FUNCTION';
          if (nt === 'PIPELINE' && !(n.data && n.data.isExpanded)) nt = 'FUNCTION';
          nodeTyp.set(n.id, nt);
        });
        visEdges.filter(function(e) { return fbKeys.has(e.id); }).forEach(function(e) {
          var pts = buildFeedbackEdgePoints(e, nodePos, nodeDim, nodeTyp);
          if (pts) posEdges.push({ ...e, data: { ...e.data, points: pts, isFeedbackEdge: true } });
        });

        nodesState[1](positioned); edgesState[1](posEdges);
        versionState[1](function(v) { return v + 1; }); busyState[1](false); errorState[1](null);
        if (result.size) { widthState[1](result.size.width); heightState[1](result.size.height); }
      } catch (err) {
        console.error('Layout error:', err);
        errorState[1](err && err.message ? err.message : 'Layout error');
        nodesState[1]([]); edgesState[1]([]);
        versionState[1](function(v) { return v + 1; }); busyState[1](false);
      }
    }, [nodes, edges, expansionState, endpointPadding, ranksep]);

    return {
      layoutedNodes: nodesState[0], layoutedEdges: edgesState[0], layoutError: errorState[0],
      graphHeight: heightState[0], graphWidth: widthState[0], layoutVersion: versionState[0], isLayouting: busyState[0],
    };
  }

  var Layout = {
    computeBounds: computeBounds,
    offsetAll: offsetAll,
    layoutGraph: layoutGraph,
    computeFeedbackEdgeKeys: computeFeedbackEdgeKeys,
    buildFeedbackEdgePoints: buildFeedbackEdgePoints,
    adjustEdgeEndpoints: adjustEdgeEndpoints,
    performCompoundLayout: performCompoundLayout,
    useLayout: useLayout,
  };

  root.HypergraphVizLayout = Layout;
  root.HypergraphViz = root.HypergraphViz || {};
  root.HypergraphViz.Layout = Layout;
})(typeof window !== 'undefined' ? window : this);
