/**
 * Layout hook for Hypergraph visualization
 * Uses constraint-based layout algorithm for positioning nodes
 * Supports recursive layout for nested graphs
 */
(function(root, factory) {
  var api = factory(root);
  if (root) root.HypergraphVizLayout = api;
})(typeof window !== 'undefined' ? window : this, function(root) {
  'use strict';

  // Get dependencies from globals
  var React = root.React;
  var ConstraintLayout = root.ConstraintLayout;

  if (!React || !ConstraintLayout) {
    console.error('HypergraphVizLayout: Missing required globals (React, ConstraintLayout)');
    return {};
  }

  var useState = React.useState;
  var useEffect = React.useEffect;

  // === LAYOUT CONSTANTS ===
  var VizConstants = root.HypergraphVizConstants || {};
  var TYPE_HINT_MAX_CHARS = VizConstants.TYPE_HINT_MAX_CHARS || 25;
  var NODE_LABEL_MAX_CHARS = VizConstants.NODE_LABEL_MAX_CHARS || 25;
  var CHAR_WIDTH_PX = VizConstants.CHAR_WIDTH_PX || 7;
  var NODE_BASE_PADDING = VizConstants.NODE_BASE_PADDING || 52;
  var FUNCTION_NODE_BASE_PADDING = VizConstants.FUNCTION_NODE_BASE_PADDING || 48;
  var MAX_NODE_WIDTH = VizConstants.MAX_NODE_WIDTH || 280;

  // Constants for nested graph layout
  var GRAPH_PADDING = VizConstants.GRAPH_PADDING || 24;
  var HEADER_HEIGHT = VizConstants.HEADER_HEIGHT || 32;
  var VERTICAL_GAP = VizConstants.VERTICAL_GAP || 60;
  var EDGE_CONVERGENCE_OFFSET = VizConstants.EDGE_CONVERGENCE_OFFSET || 20;
  var FEEDBACK_EDGE_GUTTER = VizConstants.FEEDBACK_EDGE_GUTTER || 40;
  var FEEDBACK_EDGE_HEADROOM = VizConstants.FEEDBACK_EDGE_HEADROOM || 30;
  var FEEDBACK_EDGE_STEM = VizConstants.FEEDBACK_EDGE_STEM || 10;


  /**
   * Calculate dimensions for a node based on its type and content
   * @param {Object} n - Node object with data
   * @returns {Object} { width, height }
   */
  function calculateDimensions(n) {
    var width = 80;
    var height = 90;

    if (n.data && (n.data.nodeType === 'DATA' || n.data.nodeType === 'INPUT')) {
      // DATA and INPUT nodes use the same compact pill styling
      height = 36;
      var labelLen = Math.min((n.data.label && n.data.label.length) || 0, NODE_LABEL_MAX_CHARS);
      var typeLen = (n.data.showTypes && n.data.typeHint) ? Math.min(n.data.typeHint.length, TYPE_HINT_MAX_CHARS) + 2 : 0;
      width = Math.min(MAX_NODE_WIDTH, (labelLen + typeLen) * CHAR_WIDTH_PX + NODE_BASE_PADDING);
    } else if (n.data && n.data.nodeType === 'INPUT_GROUP') {
      // INPUT_GROUP shows params as rows
      var params = n.data.params || [];
      var paramTypes = n.data.paramTypes || [];
      var maxContentLen = 0;
      params.forEach(function(p, i) {
        var paramLen = Math.min(p.length, NODE_LABEL_MAX_CHARS);
        var tLen = (n.data.showTypes && paramTypes[i]) ? Math.min(paramTypes[i].length, TYPE_HINT_MAX_CHARS) + 2 : 0;
        var totalLen = 3 + paramLen + tLen;
        if (totalLen > maxContentLen) maxContentLen = totalLen;
      });
      maxContentLen = Math.max(maxContentLen, 6);
      width = Math.min(MAX_NODE_WIDTH, maxContentLen * CHAR_WIDTH_PX + 32);
      var numParams = Math.max(1, params.length);
      // Keep wrapper height aligned with INPUT_GROUP visual rows plus bottom handle offset.
      var inputGroupBottomOffset = (VizConstants.NODE_TYPE_OFFSETS && VizConstants.NODE_TYPE_OFFSETS.INPUT_GROUP) || 6;
      height = 16 + (numParams * 16) + ((numParams - 1) * 4) + inputGroupBottomOffset;
    } else if (n.data && n.data.nodeType === 'BRANCH') {
      width = 140;
      height = 140;
    } else {
      // Function/Pipeline node
      var labelLen = Math.min((n.data && n.data.label && n.data.label.length) || 0, NODE_LABEL_MAX_CHARS);
      var maxContentLen = labelLen;
      var outputs = (n.data && n.data.outputs) || [];
      if (n.data && !n.data.separateOutputs && outputs.length > 0) {
        outputs.forEach(function(o) {
          var outName = o.name || o.label || '';
          var outType = o.type || o.typeHint || '';
          var outLabelLen = Math.min(outName.length, NODE_LABEL_MAX_CHARS);
          var outTypeLen = (n.data.showTypes && outType) ? Math.min(outType.length, TYPE_HINT_MAX_CHARS) + 2 : 0;
          var totalLen = outLabelLen + outTypeLen + 4;
          if (totalLen > maxContentLen) maxContentLen = totalLen;
        });
      }
      width = Math.min(MAX_NODE_WIDTH, maxContentLen * CHAR_WIDTH_PX + FUNCTION_NODE_BASE_PADDING);
      height = 56;
      if (n.data && !n.data.separateOutputs && outputs.length > 0) {
        // Keep dimension model aligned with OutputsSection rendering.
        var rowCount = outputs.length;
        var outputSectionHeight = 16 + (rowCount * 16) + (Math.max(0, rowCount - 1) * 6) + 6;
        height = 56 + outputSectionHeight;
      }
    }

    if (n.style && n.style.width) width = n.style.width;
    if (n.style && n.style.height) height = n.style.height;

    return { width: width, height: height };
  }

  function buildEdgeKey(source, target) {
    return source + '->' + target;
  }

  function computeFeedbackEdgeKeys(nodes, edges) {
    var nodeIds = new Set(nodes.map(function(n) { return n.id; }));
    var adjacency = new Map();
    nodes.forEach(function(n) { adjacency.set(n.id, []); });

    edges.forEach(function(e) {
      if (!nodeIds.has(e.source) || !nodeIds.has(e.target)) return;
      adjacency.get(e.source).push(e);
    });

    var state = new Map();
    nodes.forEach(function(n) { state.set(n.id, 0); });
    var feedbackEdges = new Set();

    var dfs = function(nodeId) {
      state.set(nodeId, 1);
      var outgoing = adjacency.get(nodeId) || [];
      outgoing.forEach(function(edge) {
        var target = edge.target;
        if (!nodeIds.has(target)) return;
        var targetState = state.get(target) || 0;
        if (targetState === 0) {
          dfs(target);
        } else if (targetState === 1) {
          feedbackEdges.add(buildEdgeKey(edge.source, edge.target));
        }
      });
      state.set(nodeId, 2);
    };

    nodes.forEach(function(n) {
      if (state.get(n.id) === 0) dfs(n.id);
    });

    return feedbackEdges;
  }

  /**
   * Group nodes by their parent node ID
   * @param {Array} nodes - Array of nodes
   * @returns {Map} parentId -> array of child nodes
   */
  function groupNodesByParent(nodes) {
    var groups = new Map();
    nodes.forEach(function(node) {
      var parentId = node.parentNode || null;
      if (!groups.has(parentId)) groups.set(parentId, []);
      groups.get(parentId).push(node);
    });
    return groups;
  }

  /**
   * Get expanded graph nodes sorted by depth (deepest first)
   * This ensures we layout children before parents
   * @param {Array} nodes - Array of nodes
   * @param {Map} expansionState - Which pipelines are expanded
   * @returns {Array} Graph nodes sorted deepest-first
   */
  function getLayoutOrder(nodes, expansionState) {
    var nodeById = new Map(nodes.map(function(n) { return [n.id, n]; }));

    var getDepth = function(nodeId, depth) {
      depth = depth || 0;
      var node = nodeById.get(nodeId);
      if (!node || !node.parentNode) return depth;
      return getDepth(node.parentNode, depth + 1);
    };

    return nodes
      .filter(function(n) {
        return n.data && n.data.nodeType === 'PIPELINE' &&
               expansionState.get(n.id) === true && !n.hidden;
      })
      .sort(function(a, b) {
        return getDepth(b.id) - getDepth(a.id);
      });
  }

  /**
   * Layout hook using constraint-based layout algorithm
   * Supports recursive layout for nested graphs
   * @param {Array} nodes - Nodes with visibility applied
   * @param {Array} edges - Edges
   * @param {Map} expansionState - Optional expansion state for recursive layout
   * @param {Object} routingData - Optional routing data for edge re-routing
   * @returns {Object} { layoutedNodes, layoutedEdges, layoutError, graphHeight, graphWidth, layoutVersion, isLayouting }
   */
  function useLayout(nodes, edges, expansionState, routingData) {
    var layoutedNodesState = useState([]);
    var layoutedNodes = layoutedNodesState[0];
    var setLayoutedNodes = layoutedNodesState[1];

    var layoutedEdgesState = useState([]);
    var layoutedEdges = layoutedEdgesState[0];
    var setLayoutedEdges = layoutedEdgesState[1];

    var layoutErrorState = useState(null);
    var layoutError = layoutErrorState[0];
    var setLayoutError = layoutErrorState[1];

    var graphHeightState = useState(600);
    var graphHeight = graphHeightState[0];
    var setGraphHeight = graphHeightState[1];

    var graphWidthState = useState(600);
    var graphWidth = graphWidthState[0];
    var setGraphWidth = graphWidthState[1];

    var layoutVersionState = useState(0);
    var layoutVersion = layoutVersionState[0];
    var setLayoutVersion = layoutVersionState[1];

    var isLayoutingState = useState(false);
    var isLayouting = isLayoutingState[0];
    var setIsLayouting = isLayoutingState[1];

    useEffect(function() {
      var debugMode = root.__hypergraph_debug_viz || false;
      if (debugMode) console.log('[useLayout] nodes:', nodes.length, 'edges:', edges.length);
      if (!nodes.length) {
        if (debugMode) console.log('[useLayout] No nodes, returning early');
        setIsLayouting(false);
        return;
      }

      setIsLayouting(true);

      try {
        // Filter visible nodes
        var visibleNodes = nodes.filter(function(n) { return !n.hidden; });

        // If we have expansion state, use recursive layout
        if (expansionState && expansionState.size > 0) {
          var result = performRecursiveLayout(visibleNodes, edges, expansionState, debugMode, routingData);
          setLayoutedNodes(result.nodes);
          setLayoutedEdges(result.edges);
          setLayoutVersion(function(v) { return v + 1; });
          setIsLayouting(false);
          setLayoutError(null);
          if (result.size) {
            setGraphWidth(result.size.width);
            setGraphHeight(result.size.height);
          }
          return;
        }

        // Flat layout (no nesting)
        var flatVisibleNodes = visibleNodes.filter(function(n) { return !n.parentNode; });
        var visibleNodeIds = new Set(flatVisibleNodes.map(function(n) { return n.id; }));
        var visibleEdges = edges.filter(function(e) {
          return visibleNodeIds.has(e.source) && visibleNodeIds.has(e.target);
        });
        if (debugMode) console.log('[useLayout] visible:', flatVisibleNodes.length, 'edges:', visibleEdges.length);
        var feedbackEdgeKeys = computeFeedbackEdgeKeys(flatVisibleNodes, visibleEdges);

        // Prepare nodes for constraint layout
        var layoutNodes = flatVisibleNodes.map(function(n) {
          var dims = calculateDimensions(n);
          return {
            id: n.id,
            width: dims.width,
            height: dims.height,
            x: 0,
            y: 0,
            data: n.data,  // Pass data so constraint-layout can access nodeType for edge routing
            _original: n,
          };
        });

        // Prepare edges for constraint layout
        var layoutEdges = visibleEdges
          .filter(function(e) { return !feedbackEdgeKeys.has(buildEdgeKey(e.source, e.target)); })
          .map(function(e) {
          return {
            id: e.id,
            source: e.source,
            target: e.target,
            _original: e,
          };
        });

        // Run constraint layout with appropriate spacing
        var result = ConstraintLayout.graph(
          layoutNodes,
          layoutEdges,
          null,
          'vertical',
          getLayoutOptions(layoutNodes)
        );

        if (debugMode) console.log('[useLayout] layout result:', result);

        // Convert back to React Flow format
        var positionedNodes = result.nodes.map(function(n) {
          var w = n.width;
          var h = n.height;
          var x = n.x - w / 2;
          var y = n.y - h / 2;

          return {
            ...n._original,
            position: { x: x, y: y },
            width: w,
            height: h,
            style: { ...n._original.style, width: w, height: h },
            handles: [
              { type: 'target', position: 'top', x: w / 2, y: 0, width: 8, height: 8, id: null },
              { type: 'source', position: 'bottom', x: w / 2, y: h, width: 8, height: 8, id: null },
            ],
          };
        });

        var positionedEdges = result.edges.map(function(e) {
          return {
            ...e._original,
            data: {
              ...e._original.data,
              points: e.points,
            },
          };
        });

        var nodePositions = new Map();
        var nodeDimensions = new Map();
        var nodeTypes = new Map();
        positionedNodes.forEach(function(n) {
          nodePositions.set(n.id, n.position);
          nodeDimensions.set(n.id, { width: n.width, height: n.height });
          var nodeType = n.data && n.data.nodeType ? n.data.nodeType : 'FUNCTION';
          if (nodeType === 'PIPELINE' && !(n.data && n.data.isExpanded)) {
            nodeType = 'FUNCTION';
          }
          nodeTypes.set(n.id, nodeType);
        });

        var feedbackEdges = visibleEdges
          .filter(function(e) { return feedbackEdgeKeys.has(buildEdgeKey(e.source, e.target)); })
          .map(function(e) {
            var points = buildFeedbackEdgePoints(e, nodePositions, nodeDimensions, nodeTypes);
            if (!points) return null;
            return {
              ...e,
              data: {
                ...e.data,
                points: points,
                isFeedbackEdge: true,
              },
            };
          })
          .filter(function(e) { return e; });

        setLayoutedNodes(positionedNodes);
        setLayoutedEdges(positionedEdges.concat(feedbackEdges));
        setLayoutVersion(function(v) { return v + 1; });
        setIsLayouting(false);
        setLayoutError(null);

        if (result.size) {
          setGraphWidth(result.size.width);
          setGraphHeight(result.size.height);
        }
      } catch (err) {
        console.error('Constraint layout error:', err);
        setLayoutError(err && err.message ? err.message : 'Layout error');

        // Fallback layout (grid)
        var fallbackNodes = nodes.map(function(n, idx) {
          var w = (n.style && n.style.width) || 200;
          var h = (n.style && n.style.height) || 68;
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
        setLayoutVersion(function(v) { return v + 1; });
        setIsLayouting(false);
      }
    }, [nodes, edges, expansionState, routingData]);

    return {
      layoutedNodes: layoutedNodes,
      layoutedEdges: layoutedEdges,
      layoutError: layoutError,
      graphHeight: graphHeight,
      graphWidth: graphWidth,
      layoutVersion: layoutVersion,
      isLayouting: isLayouting
    };
  }

  /**
   * Recalculate bounds after post-layout shifts
   * Mirrors the logic in constraint-layout.js bounds()
   */
  function recalculateBounds(nodes, padding) {
    var size = {
      min: { x: Infinity, y: Infinity },
      max: { x: -Infinity, y: -Infinity },
    };

    nodes.forEach(function(node) {
      var left = node.x - node.width / 2;
      var right = node.x + node.width / 2;
      var top = node.y - node.height / 2;
      var bottom = node.y + node.height / 2;

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

  /**
   * Perform recursive layout for nested graphs
   * Layouts children first (deepest), then uses their bounds to size parent nodes
   * @param {Array} visibleNodes - All visible nodes
   * @param {Array} edges - All edges
   * @param {Map} expansionState - Which pipelines are expanded
   * @param {boolean} debugMode - Whether to log debug info
   * @param {Object} routingData - Optional routing data for edge re-routing to actual internal nodes
   * @returns {Object} { nodes, edges, size }
   */
  // Node type to wrapper offset mapping (matches constraint-layout.js)
  var NODE_TYPE_OFFSETS = VizConstants.NODE_TYPE_OFFSETS || {
    'PIPELINE': 26,
    'GRAPH': 26,
    'FUNCTION': 14,
    'DATA': 6,
    'INPUT': 6,
    'INPUT_GROUP': 6,
    'BRANCH': 10,
  };
  var DEFAULT_OFFSET = VizConstants.DEFAULT_OFFSET || 10;

  function getNodeTypeOffset(nodeType) {
    return NODE_TYPE_OFFSETS[nodeType] ?? DEFAULT_OFFSET;
  }

  function buildFeedbackEdgePoints(edge, nodePositions, nodeDimensions, nodeTypes) {
    var srcPos = nodePositions.get(edge.source);
    var tgtPos = nodePositions.get(edge.target);
    var srcDims = nodeDimensions.get(edge.source);
    var tgtDims = nodeDimensions.get(edge.target);

    if (!srcPos || !tgtPos || !srcDims || !tgtDims) return null;

    var srcCenterX = srcPos.x + srcDims.width / 2;
    var srcNodeType = nodeTypes.get(edge.source) || 'FUNCTION';
    var srcBottomY = srcPos.y + srcDims.height - getNodeTypeOffset(srcNodeType);
    var tgtCenterX = tgtPos.x + tgtDims.width / 2;
    var tgtTopY = tgtPos.y;

    var srcLeft = srcPos.x;
    var tgtLeft = tgtPos.x;
    var gutterX = Math.min(srcLeft, tgtLeft) - FEEDBACK_EDGE_GUTTER;
    if (gutterX < 0) gutterX = 0;

    var stemY = srcBottomY + FEEDBACK_EDGE_STEM;
    var loopY = Math.min(srcPos.y, tgtPos.y) - FEEDBACK_EDGE_HEADROOM;
    if (loopY < 0) loopY = 0;
    if (loopY >= stemY) {
      loopY = Math.max(0, stemY - FEEDBACK_EDGE_HEADROOM);
    }

    return [
      { x: srcCenterX, y: srcBottomY },
      { x: srcCenterX, y: stemY },
      { x: gutterX, y: stemY },
      { x: gutterX, y: loopY },
      { x: tgtCenterX, y: loopY },
      { x: tgtCenterX, y: tgtTopY },
    ];
  }

  // Get layout options (keep a single spacing profile)
  function getLayoutOptions() {
    return ConstraintLayout.defaultOptions;
  }

  function buildNodeDimensionsAndTypes(visibleNodes) {
    var nodeDimensions = new Map();
    var nodeTypes = new Map();
    visibleNodes.forEach(function(n) {
      nodeDimensions.set(n.id, calculateDimensions(n));
      var nodeType = n.data?.nodeType || 'FUNCTION';
      if (nodeType === 'PIPELINE' && !n.data?.isExpanded) {
        nodeType = 'FUNCTION';
      }
      nodeTypes.set(n.id, nodeType);
    });
    return { nodeDimensions: nodeDimensions, nodeTypes: nodeTypes };
  }

  function buildParentMap(visibleNodes) {
    var parentMap = new Map();
    visibleNodes.forEach(function(n) {
      if (n.parentNode) parentMap.set(n.id, n.parentNode);
    });
    return parentMap;
  }

  function buildInputNodesInContainers(visibleNodes, expansionState) {
    var inputNodesInContainers = new Map();
    var parentMap = buildParentMap(visibleNodes);

    visibleNodes.forEach(function(n) {
      var nodeType = n.data && n.data.nodeType;
      var isInput = nodeType === 'INPUT' || nodeType === 'INPUT_GROUP';
      if (!isInput) return;

      // Use deepestOwnerContainer if available, fall back to ownerContainer
      var deepestOwner = n.data.deepestOwnerContainer || n.data.ownerContainer;
      if (!deepestOwner) return;

      // Walk up from deepestOwner to find the deepest EXPANDED container
      var current = deepestOwner;
      while (current) {
        if (expansionState.get(current)) {
          inputNodesInContainers.set(n.id, current);
          break;
        }
        current = parentMap.get(current);
      }
    });

    return { inputNodesInContainers: inputNodesInContainers, parentMap: parentMap };
  }

  function buildLayoutNodes(nodes, nodeDimensions) {
    return nodes.map(function(n) {
      var dims = nodeDimensions.get(n.id);
      return {
        id: n.id,
        width: dims.width,
        height: dims.height,
        x: 0,
        y: 0,
        data: n.data,
        _original: n,
      };
    });
  }

  function buildDeepToChildMap(visibleNodes, childIds) {
    var deepToChild = new Map();
    var nodeByIdLocal = new Map(visibleNodes.map(function(n) { return [n.id, n]; }));

    visibleNodes.forEach(function(n) {
      if (childIds.has(n.id)) return;

      var current = n;
      var visited = [];
      while (current && current.parentNode) {
        visited.push(current.id);
        if (childIds.has(current.parentNode)) {
          visited.forEach(function(nodeId) {
            deepToChild.set(nodeId, current.parentNode);
          });
          break;
        }
        current = nodeByIdLocal.get(current.parentNode);
      }
    });

    return deepToChild;
  }

  function collectInternalEdges(edges, childIds, deepToChild) {
    var internalEdgeSet = new Set();
    var internalEdges = [];

    edges.forEach(function(e) {
      var source = e.source;
      var target = e.target;

      if (deepToChild.has(source)) {
        source = deepToChild.get(source);
      }
      if (deepToChild.has(target)) {
        target = deepToChild.get(target);
      }

      // Filter self-loops: feedback edges (cycles) are detected and styled separately
      // in the cycle detection pass - this layout pass handles only forward edges
      if (childIds.has(source) && childIds.has(target) && source !== target) {
        var edgeKey = source + '->' + target;
        if (!internalEdgeSet.has(edgeKey)) {
          internalEdgeSet.add(edgeKey);
          internalEdges.push({
            id: edgeKey,
            source: source,
            target: target,
            _original: e,
          });
        }
      }
    });

    return internalEdges;
  }

  function applyVerticalGapFix(layoutNodes, layoutEdges, gapSize, debugMode, label) {
    var nodeById = new Map(layoutNodes.map(function(n) { return [n.id, n]; }));

    layoutEdges.forEach(function(e) {
      var srcNode = nodeById.get(e.source);
      var tgtNode = nodeById.get(e.target);
      if (!srcNode || !tgtNode) return;

      var srcOffset = getNodeTypeOffset(srcNode.data && srcNode.data.nodeType, srcNode.data && srcNode.data.isExpanded);
      var srcBottom = srcNode.y + srcNode.height / 2 - srcOffset;
      var tgtTop = tgtNode.y - tgtNode.height / 2;
      var gap = tgtTop - srcBottom;

      if (gap < gapSize) {
        var shift = gapSize - gap;
        var targetY = tgtNode.y;
        if (debugMode) {
          console.log('[recursive layout] shifting', label, tgtNode.id, 'down by', shift);
        }
        layoutNodes.forEach(function(n) {
          if (n.y >= targetY) {
            n.y += shift;
          }
        });
      }
    });
  }

  function snapInputsTowardTargets(layoutNodes, layoutEdges, debugMode) {
    if (!layoutNodes || !layoutEdges || layoutNodes.length === 0) return;
    var nodeById = new Map(layoutNodes.map(function(n) { return [n.id, n]; }));
    var inputTargets = new Map();

    layoutEdges.forEach(function(e) {
      var src = nodeById.get(e.source);
      var tgt = nodeById.get(e.target);
      if (!src || !tgt) return;
      var srcType = src.data && src.data.nodeType;
      if (srcType !== 'INPUT' && srcType !== 'INPUT_GROUP') return;
      var xs = inputTargets.get(src.id) || [];
      xs.push(tgt.x);
      inputTargets.set(src.id, xs);
    });

    inputTargets.forEach(function(targetXs, inputId) {
      var inputNode = nodeById.get(inputId);
      if (!inputNode || targetXs.length === 0) return;
      if (targetXs.length > 2) return;
      var desiredX = targetXs.reduce(function(sum, x) { return sum + x; }, 0) / targetXs.length;
      var before = inputNode.x;
      inputNode.x = before + (desiredX - before) * 0.95;
      if (debugMode) {
        console.log('[recursive layout] input-align', inputId, 'x', before, '->', inputNode.x, '(target avg', desiredX + ')');
      }
    });
  }

  function resolveNodeOverlaps(layoutNodes, debugMode, label) {
    if (!layoutNodes || layoutNodes.length < 2) return;

    var MIN_GAP_X = 24;
    var OVERLAP_CLEARANCE = 8;
    var MAX_PASSES = layoutNodes.length + 2;

    var rect = function(n) {
      return {
        left: n.x - n.width / 2 - OVERLAP_CLEARANCE,
        right: n.x + n.width / 2 + OVERLAP_CLEARANCE,
        top: n.y - n.height / 2 - OVERLAP_CLEARANCE,
        bottom: n.y + n.height / 2 + OVERLAP_CLEARANCE,
      };
    };

    for (var pass = 0; pass < MAX_PASSES; pass += 1) {
      var changed = false;
      layoutNodes.sort(function(a, b) { return a.x - b.x; });

      for (var i = 0; i < layoutNodes.length - 1; i += 1) {
        for (var j = i + 1; j < layoutNodes.length; j += 1) {
          var a = layoutNodes[i];
          var b = layoutNodes[j];
          var ra = rect(a);
          var rb = rect(b);

          var overlapsY = !(ra.bottom <= rb.top || rb.bottom <= ra.top);
          if (!overlapsY) continue;

          var neededShift = (ra.right + MIN_GAP_X) - rb.left;
          if (neededShift > 0) {
            b.x += neededShift;
            changed = true;
            if (debugMode) {
              console.log('[recursive layout] overlap-fix shifting', label || 'nodes', b.id, 'right by', neededShift, '(vs', a.id + ')');
            }
          }
        }
      }

      if (!changed) break;
    }
  }

  function layoutChildrenPhase(visibleNodes, edges, layoutOrder, nodeGroups, inputNodesInContainers, nodeDimensions, debugMode) {
    var childLayoutResults = new Map();
    var CHILD_EDGE_GAP = VERTICAL_GAP;

    layoutOrder.forEach(function(graphNode) {
      var children = nodeGroups.get(graphNode.id) || [];

      visibleNodes.forEach(function(n) {
        if (inputNodesInContainers.get(n.id) === graphNode.id) {
          children.push(n);
        }
      });

      if (children.length === 0) return;

      var childIds = new Set(children.map(function(c) { return c.id; }));
      var deepToChild = buildDeepToChildMap(visibleNodes, childIds);
      var internalEdges = collectInternalEdges(edges, childIds, deepToChild);

      var childLayoutNodes = buildLayoutNodes(children, nodeDimensions);
      var feedbackEdgeKeys = computeFeedbackEdgeKeys(children, internalEdges);
      var childLayoutEdges = internalEdges
        .filter(function(e) { return !feedbackEdgeKeys.has(buildEdgeKey(e.source, e.target)); })
        .map(function(e) {
          return { id: e.id, source: e.source, target: e.target, _original: e._original || e };
        });

      var childResult = ConstraintLayout.graph(
        childLayoutNodes,
        childLayoutEdges,
        null,
        'vertical',
        getLayoutOptions(childLayoutNodes)
      );

      applyVerticalGapFix(childResult.nodes, childLayoutEdges, CHILD_EDGE_GAP, debugMode, graphNode.id);
      snapInputsTowardTargets(childResult.nodes, childLayoutEdges, debugMode);
      resolveNodeOverlaps(childResult.nodes, debugMode, graphNode.id);

      childResult.size = recalculateBounds(childResult.nodes, GRAPH_PADDING);
      childLayoutResults.set(graphNode.id, childResult);

      if (debugMode) {
        console.log('[recursive layout] graph', graphNode.id, 'children:', children.length, 'size:', childResult.size);
      }

      nodeDimensions.set(graphNode.id, {
        width: childResult.size.width,
        height: childResult.size.height + HEADER_HEIGHT,
      });
    });

    return { childLayoutResults: childLayoutResults };
  }

  function buildChildToRootAncestor(visibleNodes, rootNodeIds, inputNodesInContainers) {
    var childToRootAncestor = new Map();
    var nodeByIdForLifting = new Map(visibleNodes.map(function(n) { return [n.id, n]; }));

    visibleNodes.forEach(function(n) {
      if (rootNodeIds.has(n.id)) return;

      if (inputNodesInContainers.has(n.id)) {
        var ownerId = inputNodesInContainers.get(n.id);
        if (rootNodeIds.has(ownerId)) {
          childToRootAncestor.set(n.id, ownerId);
        }
        return;
      }

      var current = n;
      while (current && current.parentNode) {
        if (rootNodeIds.has(current.parentNode)) {
          childToRootAncestor.set(n.id, current.parentNode);
          break;
        }
        current = nodeByIdForLifting.get(current.parentNode);
      }
    });

    return childToRootAncestor;
  }

  function collectRootEdges(edges, rootNodeIds, childToRootAncestor) {
    var rootEdgeSet = new Set();
    var rootEdges = [];

    edges.forEach(function(e) {
      var source = e.source;
      var target = e.target;

      if (childToRootAncestor.has(source)) {
        source = childToRootAncestor.get(source);
      }
      if (childToRootAncestor.has(target)) {
        target = childToRootAncestor.get(target);
      }

      // Filter self-loops: feedback edges (cycles) are detected and styled separately
      // in the cycle detection pass - this layout pass handles only forward edges
      if (rootNodeIds.has(source) && rootNodeIds.has(target) && source !== target) {
        var edgeKey = source + '->' + target;
        if (!rootEdgeSet.has(edgeKey)) {
          rootEdgeSet.add(edgeKey);
          rootEdges.push({
            id: edgeKey,
            source: source,
            target: target,
            _original: e,
          });
        }
      }
    });

    return rootEdges;
  }

  function layoutRootPhase(visibleNodes, edges, nodeGroups, inputNodesInContainers, nodeDimensions, debugMode) {
    var rootNodes = (nodeGroups.get(null) || []).filter(function(n) {
      return !inputNodesInContainers.has(n.id);
    });
    var rootNodeIds = new Set(rootNodes.map(function(n) { return n.id; }));

    if (debugMode) {
      console.log('[recursive layout] rootNodes:', rootNodes.map(function(n) { return n.id; }));
      console.log('[recursive layout] nodeDimensions:', Array.from(nodeDimensions.entries()).map(function(e) {
        return { id: e[0], w: e[1].width, h: e[1].height };
      }));
    }

    var childToRootAncestor = buildChildToRootAncestor(visibleNodes, rootNodeIds, inputNodesInContainers);
    var rootLayoutEdges = collectRootEdges(edges, rootNodeIds, childToRootAncestor);
    var rootLayoutNodes = buildLayoutNodes(rootNodes, nodeDimensions);

    var feedbackEdgeKeys = computeFeedbackEdgeKeys(rootNodes, rootLayoutEdges);
    var filteredRootLayoutEdges = rootLayoutEdges.filter(function(e) {
      return !feedbackEdgeKeys.has(buildEdgeKey(e.source, e.target));
    });

    var rootResult = ConstraintLayout.graph(
      rootLayoutNodes,
      filteredRootLayoutEdges,
      null,
      'vertical',
      getLayoutOptions(rootLayoutNodes)
    );

    if (debugMode) {
      console.log('[recursive layout] BEFORE SHIFTS - rootResult.nodes:', JSON.stringify(rootResult.nodes.map(function(n) {
        return { id: n.id, x: n.x, y: n.y, h: n.height };
      })));
    }

    applyVerticalGapFix(rootResult.nodes, filteredRootLayoutEdges, VERTICAL_GAP, debugMode, 'root');
    snapInputsTowardTargets(rootResult.nodes, filteredRootLayoutEdges, debugMode);
    resolveNodeOverlaps(rootResult.nodes, debugMode, 'root');
    rootResult.size = recalculateBounds(rootResult.nodes, GRAPH_PADDING);

    return {
      rootNodes: rootNodes,
      rootNodeIds: rootNodeIds,
      rootResult: rootResult,
      rootLayoutNodes: rootLayoutNodes,
      rootLayoutEdges: filteredRootLayoutEdges,
      childToRootAncestor: childToRootAncestor,
    };
  }

  function composePositionsPhase(layoutOrder, childLayoutResults, rootResult, rootLayoutNodes, rootLayoutEdges, inputNodesInContainers, nodeDimensions, nodeTypes, debugMode) {
    var nodePositions = new Map();
    var allPositionedNodes = [];
    var allPositionedEdges = [];

    if (debugMode) {
      console.log('[recursive layout] rootLayoutNodes (input):', JSON.stringify(rootLayoutNodes.map(function(n) {
        return { id: n.id, w: n.width, h: n.height };
      })));
      console.log('[recursive layout] rootLayoutEdges (input):', JSON.stringify(rootLayoutEdges.map(function(e) {
        return { source: e.source, target: e.target };
      })));
      console.log('[recursive layout] rootResult.nodes (output):', JSON.stringify(rootResult.nodes.map(function(n) {
        return { id: n.id, x: n.x, y: n.y, w: n.width, h: n.height };
      })));
      console.log('[recursive layout] rootResult.size:', rootResult.size);
    }

    rootResult.nodes.forEach(function(n) {
      var w = n.width;
      var h = n.height;
      var x = n.x - w / 2 - rootResult.size.min.x;
      var y = n.y - h / 2 - rootResult.size.min.y;
      nodePositions.set(n.id, { x: x, y: y });

      allPositionedNodes.push({
        ...n._original,
        position: { x: x, y: y },
        width: w,
        height: h,
        style: { ...n._original.style, width: w, height: h },
        handles: [
          { type: 'target', position: 'top', x: w / 2, y: 0, width: 8, height: 8, id: null },
          { type: 'source', position: 'bottom', x: w / 2, y: h, width: 8, height: 8, id: null },
        ],
      });
    });

    var reverseLayoutOrder = layoutOrder.slice().reverse();
    reverseLayoutOrder.forEach(function(graphNode) {
      var childResult = childLayoutResults.get(graphNode.id);
      if (!childResult) return;

      var parentPos = nodePositions.get(graphNode.id);
      if (!parentPos) return;

      var absOffsetX = parentPos.x;
      var absOffsetY = parentPos.y + HEADER_HEIGHT;

      childResult.nodes.forEach(function(n) {
        var w = n.width;
        var h = n.height;
        var childX = n.x - w / 2 - childResult.size.min.x;
        var childY = n.y - h / 2 - childResult.size.min.y;

        nodePositions.set(n.id, { x: absOffsetX + childX, y: absOffsetY + childY });
        nodeDimensions.set(n.id, { width: w, height: h });

        if (debugMode) {
          console.log('[recursive layout] child', n.id, 'position:', { x: childX, y: childY + HEADER_HEIGHT }, 'parentNode:', n._original.parentNode);
        }

        var nodeWithParent = { ...n._original };
        if (inputNodesInContainers.has(n.id)) {
          nodeWithParent.parentNode = inputNodesInContainers.get(n.id);
          nodeWithParent.extent = 'parent';
        }

        allPositionedNodes.push({
          ...nodeWithParent,
          position: {
            x: childX,
            y: childY + HEADER_HEIGHT
          },
          width: w,
          height: h,
          style: { ...nodeWithParent.style, width: w, height: h },
          handles: [
            { type: 'target', position: 'top', x: w / 2, y: 0, width: 8, height: 8, id: null },
            { type: 'source', position: 'bottom', x: w / 2, y: h, width: 8, height: 8, id: null },
          ],
        });
      });

      childResult.edges.forEach(function(e) {
        var offsetPoints = (e.points || []).map(function(pt) {
          return {
            x: pt.x - childResult.size.min.x + absOffsetX,
            y: pt.y - childResult.size.min.y + absOffsetY
          };
        });

        allPositionedEdges.push({
          ...e._original,
          data: { ...e._original.data, points: offsetPoints },
        });
      });
    });

    return {
      nodePositions: nodePositions,
      allPositionedNodes: allPositionedNodes,
      allPositionedEdges: allPositionedEdges,
    };
  }

  function buildRoutingLookups(visibleNodes, routingData) {
    var inputGroupActualTargets = new Map();
    visibleNodes.forEach(function(n) {
      if (n.data && n.data.nodeType === 'INPUT_GROUP' && n.data.actualTargets) {
        inputGroupActualTargets.set(n.id, n.data.actualTargets);
      }
    });

    var inputNodeActualTargets = new Map();
    var paramToConsumer = (routingData && routingData.param_to_consumer) || {};
    visibleNodes.forEach(function(n) {
      if (n.data && n.data.nodeType === 'INPUT') {
        var paramName = n.data.label;
        var actualConsumer = paramToConsumer[paramName];
        if (actualConsumer) {
          inputNodeActualTargets.set(n.id, actualConsumer);
        }
      }
    });

    return {
      inputGroupActualTargets: inputGroupActualTargets,
      inputNodeActualTargets: inputNodeActualTargets,
      outputToProducer: (routingData && routingData.output_to_producer) || {},
      nodeToParent: (routingData && routingData.node_to_parent) || {},
    };
  }

  function routeCrossBoundaryEdgesPhase(edges, allPositionedEdges, nodePositions, nodeDimensions, nodeTypes, routingLookups, feedbackEdgeKeys, debugMode) {
    var handledEdges = new Set(allPositionedEdges.map(function(e) {
      return e.source + '->' + e.target;
    }));

    var inputGroupActualTargets = routingLookups.inputGroupActualTargets;
    var inputNodeActualTargets = routingLookups.inputNodeActualTargets;
    var outputToProducer = routingLookups.outputToProducer;
    var nodeToParent = routingLookups.nodeToParent;

    edges.forEach(function(e) {
      var edgeKey = e.source + '->' + e.target;
      if (handledEdges.has(edgeKey)) return;

      var actualSrc = e.source;
      var actualTgt = e.target;
      var actualSrcPos = null;
      var actualTgtPos = null;
      var actualSrcDims = null;
      var actualTgtDims = null;

      var valueName = e.data && e.data.valueName;
      if (valueName && outputToProducer[valueName]) {
        var actualProducer = outputToProducer[valueName];
        var actualProducerIsAncestor = false;
        if (nodeToParent) {
          var current = e.source;
          while (current && nodeToParent[current]) {
            if (nodeToParent[current] === actualProducer) {
              actualProducerIsAncestor = true;
              break;
            }
            current = nodeToParent[current];
          }
        }

        if (debugMode) {
          console.log('[Step 4 DEBUG] Edge:', e.source, '->', e.target,
            'valueName:', valueName, 'actualProducer:', actualProducer,
            'actualProducerIsAncestor:', actualProducerIsAncestor,
            'nodeToParent[e.source]:', nodeToParent[e.source]);
        }

        if (!actualProducerIsAncestor) {
          var actualDataNodeId = 'data_' + actualProducer + '_' + valueName;

          if (nodePositions.has(actualDataNodeId) && nodeDimensions.has(actualDataNodeId)) {
            actualSrc = actualDataNodeId;
            actualSrcPos = nodePositions.get(actualDataNodeId);
            actualSrcDims = nodeDimensions.get(actualDataNodeId);
          } else if (nodePositions.has(actualProducer) && nodeDimensions.has(actualProducer)) {
            actualSrc = actualProducer;
            actualSrcPos = nodePositions.get(actualProducer);
            actualSrcDims = nodeDimensions.get(actualProducer);
          }
        }
      }

      if (!actualSrcPos) {
        actualSrcPos = nodePositions.get(e.source);
        actualSrcDims = nodeDimensions.get(e.source);
        actualSrc = e.source;
      }

      if (inputGroupActualTargets.has(e.source)) {
        var actualTargets = inputGroupActualTargets.get(e.source);
        for (var i = 0; i < actualTargets.length; i++) {
          var at = actualTargets[i];
          if (nodePositions.has(at) && nodeDimensions.has(at)) {
            actualTgt = at;
            actualTgtPos = nodePositions.get(at);
            actualTgtDims = nodeDimensions.get(at);
            break;
          }
        }
      }

      if (inputNodeActualTargets.has(e.source)) {
        var actualConsumer = inputNodeActualTargets.get(e.source);
        if (nodePositions.has(actualConsumer) && nodeDimensions.has(actualConsumer)) {
          actualTgt = actualConsumer;
          actualTgtPos = nodePositions.get(actualConsumer);
          actualTgtDims = nodeDimensions.get(actualConsumer);
        }
      }

      if (!actualTgtPos) {
        actualTgtPos = nodePositions.get(e.target);
        actualTgtDims = nodeDimensions.get(e.target);
        actualTgt = e.target;
      }

      if (!actualSrcPos || !actualTgtPos || !actualSrcDims || !actualTgtDims) {
        return;
      }

      var srcCenterX = actualSrcPos.x + actualSrcDims.width / 2;
      var srcNodeType = nodeTypes.get(actualSrc) || 'FUNCTION';
      var srcBottomY = actualSrcPos.y + actualSrcDims.height - getNodeTypeOffset(srcNodeType);
      var tgtCenterX = actualTgtPos.x + actualTgtDims.width / 2;
      var tgtTopY = actualTgtPos.y;

      var isFeedbackEdge = feedbackEdgeKeys && feedbackEdgeKeys.has(edgeKey);
      var points = null;

      if (isFeedbackEdge) {
        points = buildFeedbackEdgePoints(
          { source: actualSrc, target: actualTgt },
          nodePositions,
          nodeDimensions,
          nodeTypes
        );
      }

      if (!points) {
        points = [
          { x: srcCenterX, y: srcBottomY },
          { x: tgtCenterX, y: tgtTopY }
        ];
      }

      if (debugMode) {
        var rerouted = (actualSrc !== e.source || actualTgt !== e.target);
        console.log('[recursive layout] cross-boundary edge', e.source, '->', e.target,
          rerouted ? '(rerouted to ' + actualSrc + ' -> ' + actualTgt + ')' : '',
          'srcBottom:', srcBottomY, 'tgtTop:', tgtTopY);
      }

      allPositionedEdges.push({
        ...e,
        data: {
          ...e.data,
          points: points,
          actualSource: actualSrc,
          actualTarget: actualTgt,
          isFeedbackEdge: isFeedbackEdge,
        },
      });
    });
  }

  function applyEdgeReroutesPhase(allPositionedEdges, nodePositions, nodeDimensions, nodeTypes, routingLookups, debugMode) {
    var inputNodeActualTargets = routingLookups.inputNodeActualTargets;
    var outputToProducer = routingLookups.outputToProducer;
    var nodeToParent = routingLookups.nodeToParent;
    var EDGE_REROUTE_CLEARANCE = 10;

    var segmentIntersectsRect = function(ax, ay, bx, by, rect) {
      if (ax === bx && ay === by) {
        return ax >= rect.left && ax <= rect.right && ay >= rect.top && ay <= rect.bottom;
      }

      var t0 = 0;
      var t1 = 1;
      var dx = bx - ax;
      var dy = by - ay;
      var p = [-dx, dx, -dy, dy];
      var q = [ax - rect.left, rect.right - ax, ay - rect.top, rect.bottom - ay];

      for (var i = 0; i < 4; i += 1) {
        var pi = p[i];
        var qi = q[i];
        if (pi === 0) {
          if (qi < 0) return false;
          continue;
        }
        var r = qi / pi;
        if (pi < 0) {
          if (r > t1) return false;
          if (r > t0) t0 = r;
        } else {
          if (r < t0) return false;
          if (r < t1) t1 = r;
        }
      }
      return true;
    };

    var findCrossedNodes = function(points, sourceId, targetId) {
      var crossed = [];
      if (!points || points.length < 2) return crossed;

      nodePositions.forEach(function(pos, nodeId) {
        if (nodeId === sourceId || nodeId === targetId) return;
        var dims = nodeDimensions.get(nodeId);
        if (!dims) return;

        var rect = {
          left: pos.x - EDGE_REROUTE_CLEARANCE,
          right: pos.x + dims.width + EDGE_REROUTE_CLEARANCE,
          top: pos.y - EDGE_REROUTE_CLEARANCE,
          bottom: pos.y + dims.height + EDGE_REROUTE_CLEARANCE,
        };

        for (var i = 0; i < points.length - 1; i += 1) {
          var a = points[i];
          var b = points[i + 1];
          if (segmentIntersectsRect(a.x, a.y, b.x, b.y, rect)) {
            crossed.push(rect);
            return;
          }
        }
      });

      return crossed;
    };

    var rerouteAroundCrossedNodes = function(points, sourceId, targetId) {
      if (!points || points.length < 2) return points;
      var originalCrossed = findCrossedNodes(points, sourceId, targetId);
      if (!originalCrossed.length) return points;

      var start = points[0];
      var end = points[points.length - 1];
      if (!(end.y > start.y + 24)) return points;

      var minTop = originalCrossed.reduce(function(best, rect) {
        return Math.min(best, rect.top);
      }, Infinity);
      var minLeft = originalCrossed.reduce(function(best, rect) {
        return Math.min(best, rect.left);
      }, Infinity);
      var maxRight = originalCrossed.reduce(function(best, rect) {
        return Math.max(best, rect.right);
      }, -Infinity);

      var yCandidates = [
        Math.min(end.y - 20, Math.max(start.y + 20, minTop - 18)),
        Math.min(end.y - 28, Math.max(start.y + 28, minTop - 30)),
      ];

      var buildDetourPath = function(laneX, detourY) {
        if (!Number.isFinite(detourY) || detourY <= start.y + 4 || detourY >= end.y - 4) {
          return null;
        }
        if (!Number.isFinite(laneX) || Math.abs(laneX - start.x) < 1) {
          return [
            { x: start.x, y: start.y },
            { x: start.x, y: detourY },
            { x: end.x, y: detourY },
            { x: end.x, y: end.y },
          ];
        }
        return [
          { x: start.x, y: start.y },
          { x: laneX, y: start.y + 12 },
          { x: laneX, y: detourY },
          { x: end.x, y: detourY },
          { x: end.x, y: end.y },
        ];
      };

      var laneCandidates = [start.x, minLeft - 20, maxRight + 20];
      var candidates = [];
      yCandidates.forEach(function(detourY) {
        laneCandidates.forEach(function(laneX) {
          var candidate = buildDetourPath(laneX, detourY);
          if (candidate) candidates.push(candidate);
        });
      });
      if (!candidates.length) return points;

      var bestPath = points;
      var bestScore = originalCrossed.length;
      var bestLaneShift = Infinity;

      candidates.forEach(function(candidate) {
        var score = findCrossedNodes(candidate, sourceId, targetId).length;
        var laneShift = Math.abs((candidate[1] && candidate[1].x) - start.x);
        if (score < bestScore || (score === bestScore && laneShift < bestLaneShift)) {
          bestPath = candidate;
          bestScore = score;
          bestLaneShift = laneShift;
        }
      });

      return bestPath;
    };

    return allPositionedEdges.map(function(e) {
      if (e.data && e.data.actualTarget && e.data.actualTarget !== e.target) {
        return e;
      }

      var valueName = e.data && e.data.valueName;
      var actualProducer = (valueName && outputToProducer[valueName]) ? outputToProducer[valueName] : null;

      var sourceIsDataNode = e.source && e.source.startsWith('data_');

      var actualProducerIsAncestor = false;
      if (actualProducer && nodeToParent) {
        var current = e.source;
        while (current && nodeToParent[current]) {
          if (nodeToParent[current] === actualProducer) {
            actualProducerIsAncestor = true;
            break;
          }
          current = nodeToParent[current];
        }
      }

      var needsStartReroute = !sourceIsDataNode && !actualProducerIsAncestor &&
        actualProducer && actualProducer !== e.source &&
        nodePositions.has(actualProducer) && nodeDimensions.has(actualProducer);

      var actualConsumer = inputNodeActualTargets.get(e.source);
      var needsTargetReroute = actualConsumer && actualConsumer !== e.target &&
        nodePositions.has(actualConsumer) && nodeDimensions.has(actualConsumer);

      var needsStartFix = !needsStartReroute && nodePositions.has(e.source) && nodeDimensions.has(e.source);
      var needsEndFix = nodePositions.has(e.target) && nodeDimensions.has(e.target);

      if (!needsStartReroute && !needsStartFix && !needsTargetReroute && !needsEndFix) {
        return e;
      }

      var newPoints = (e.data.points || []).slice();
      var actualSrc = e.source;
      var actualTgt = e.target;

      if (needsStartReroute) {
        var producerPos = nodePositions.get(actualProducer);
        var producerDims = nodeDimensions.get(actualProducer);
        var newStartX = producerPos.x + producerDims.width / 2;
        var producerNodeType = nodeTypes.get(actualProducer) || 'FUNCTION';
        var newStartY = producerPos.y + producerDims.height - getNodeTypeOffset(producerNodeType);
        if (newPoints.length > 0) {
          newPoints[0] = { x: newStartX, y: newStartY };
        }
        actualSrc = actualProducer;
      } else if (needsStartFix) {
        var sourcePos = nodePositions.get(e.source);
        var sourceDims = nodeDimensions.get(e.source);
        var newStartX = sourcePos.x + sourceDims.width / 2;
        var sourceNodeType = nodeTypes.get(e.source) || 'FUNCTION';
        var newStartY = sourcePos.y + sourceDims.height - getNodeTypeOffset(sourceNodeType);
        if (newPoints.length > 0) {
          newPoints[0] = { x: newStartX, y: newStartY };
        }
      }

      if (needsTargetReroute) {
        var consumerPos = nodePositions.get(actualConsumer);
        var consumerDims = nodeDimensions.get(actualConsumer);
        var newEndX = consumerPos.x + consumerDims.width / 2;
        var newEndY = consumerPos.y;
        if (newPoints.length > 0) {
          newPoints[newPoints.length - 1] = { x: newEndX, y: newEndY };
        }
        actualTgt = actualConsumer;
      } else if (needsEndFix) {
        var targetPos = nodePositions.get(e.target);
        var targetDims = nodeDimensions.get(e.target);
        var newEndX = targetPos.x + targetDims.width / 2;
        var newEndY = targetPos.y;
        if (newPoints.length > 0) {
          newPoints[newPoints.length - 1] = { x: newEndX, y: newEndY };
        }
        actualTgt = e.target;
      }

      if (debugMode) {
        console.log('[recursive layout] Step 5: re-routed', e.source, '->', e.target,
          'to actualSource:', actualSrc, 'actualTarget:', actualTgt);
      }

      newPoints = rerouteAroundCrossedNodes(newPoints, actualSrc, actualTgt);

      return {
        ...e,
        data: {
          ...e.data,
          points: newPoints,
          actualSource: actualSrc,
          actualTarget: actualTgt,
        },
      };
    });
  }

  function mergeSharedTargetEdgesPhase(allPositionedEdges, nodePositions, nodeDimensions, debugMode) {
    var edgesByTarget = new Map();

    allPositionedEdges.forEach(function(e) {
      var points = e.data && e.data.points;
      if (!points || points.length < 2) return;
      if (points.length > 2) return;

      var actualTarget = (e.data && e.data.actualTarget) || e.target;
      if (!nodePositions.has(actualTarget) || !nodeDimensions.has(actualTarget)) return;

      if (!edgesByTarget.has(actualTarget)) edgesByTarget.set(actualTarget, []);
      edgesByTarget.get(actualTarget).push(e);
    });

    var layoutOptions = getLayoutOptions();
    var stemMinTarget = (layoutOptions && layoutOptions.routing && layoutOptions.routing.stemMinTarget) || 15;

    edgesByTarget.forEach(function(targetEdges, targetId) {
      if (targetEdges.length < 2) return;

      var targetPos = nodePositions.get(targetId);
      var targetDims = nodeDimensions.get(targetId);
      if (!targetPos || !targetDims) return;

      var targetX = targetPos.x + targetDims.width / 2;
      var targetTopY = targetPos.y;
      var convergeY = targetTopY - stemMinTarget - EDGE_CONVERGENCE_OFFSET;

      targetEdges.forEach(function(e) {
        var points = (e.data && e.data.points) ? e.data.points.slice() : [];
        if (points.length < 2) return;

        points[points.length - 1] = { x: targetX, y: targetTopY };

        var convergePoint = { x: targetX, y: convergeY };
        var insertIndex = points.length - 1;
        var prev = points[insertIndex - 1];
        var alreadyConverged = prev &&
          Math.abs(prev.x - convergePoint.x) < 0.5 &&
          Math.abs(prev.y - convergePoint.y) < 0.5;

        if (!alreadyConverged) {
          points.splice(insertIndex, 0, convergePoint);
        }

        e.data = { ...(e.data || {}), points: points };
      });

      if (debugMode) {
        console.log('[recursive layout] merged', targetEdges.length, 'edges into target', targetId);
      }
    });

    return allPositionedEdges;
  }

  function validateEdgesPhase(allPositionedEdges, nodePositions, nodeDimensions, nodeTypes) {
    allPositionedEdges.forEach(function(e) {
      var points = e.data && e.data.points;
      if (!points || points.length < 2) {
        console.error('[EDGE VALIDATION] Edge missing points:', e.id, e.source, '->', e.target);
        return;
      }

      var actualSrc = (e.data && e.data.actualSource) || e.source;
      var actualTgt = (e.data && e.data.actualTarget) || e.target;

      var srcPos = nodePositions.get(actualSrc);
      var srcDims = nodeDimensions.get(actualSrc);
      if (!srcPos || !srcDims) {
        console.error('[EDGE VALIDATION] Source node not found:', actualSrc,
          'for edge', e.id, '| original source:', e.source);
      } else {
        var srcType = nodeTypes.get(actualSrc) || 'FUNCTION';
        var expectedSrcBottomY = srcPos.y + srcDims.height - getNodeTypeOffset(srcType);
        var actualStartY = points[0].y;
        var srcYDiff = Math.abs(actualStartY - expectedSrcBottomY);
        if (srcYDiff > 20) {
          console.error('[EDGE VALIDATION] Edge start Y mismatch:', e.id,
            '| edge starts at y=' + actualStartY,
            '| but source', actualSrc, 'bottom is y=' + expectedSrcBottomY,
            '| diff=' + srcYDiff + 'px');
        }
      }

      var tgtPos = nodePositions.get(actualTgt);
      var tgtDims = nodeDimensions.get(actualTgt);
      if (!tgtPos || !tgtDims) {
        console.error('[EDGE VALIDATION] Target node not found:', actualTgt,
          'for edge', e.id, '| original target:', e.target);
      } else {
        var expectedTgtTopY = tgtPos.y;
        var actualEndY = points[points.length - 1].y;
        var tgtYDiff = Math.abs(actualEndY - expectedTgtTopY);
        if (tgtYDiff > 20) {
          console.error('[EDGE VALIDATION] Edge end Y mismatch:', e.id,
            '| edge ends at y=' + actualEndY,
            '| but target', actualTgt, 'top is y=' + expectedTgtTopY,
            '| diff=' + tgtYDiff + 'px');
        }
      }
    });
  }

  function performRecursiveLayout(visibleNodes, edges, expansionState, debugMode, routingData) {
    var nodeGroups = groupNodesByParent(visibleNodes);
    var layoutOrder = getLayoutOrder(visibleNodes, expansionState);
    var dimensionResult = buildNodeDimensionsAndTypes(visibleNodes);
    var nodeDimensions = dimensionResult.nodeDimensions;
    var nodeTypes = dimensionResult.nodeTypes;
    var feedbackEdgeKeys = computeFeedbackEdgeKeys(visibleNodes, edges);

    var inputResult = buildInputNodesInContainers(visibleNodes, expansionState);
    var inputNodesInContainers = inputResult.inputNodesInContainers;

    var childPhase = layoutChildrenPhase(
      visibleNodes,
      edges,
      layoutOrder,
      nodeGroups,
      inputNodesInContainers,
      nodeDimensions,
      debugMode
    );
    var childLayoutResults = childPhase.childLayoutResults;

    var rootPhase = layoutRootPhase(
      visibleNodes,
      edges,
      nodeGroups,
      inputNodesInContainers,
      nodeDimensions,
      debugMode
    );

    var composition = composePositionsPhase(
      layoutOrder,
      childLayoutResults,
      rootPhase.rootResult,
      rootPhase.rootLayoutNodes,
      rootPhase.rootLayoutEdges,
      inputNodesInContainers,
      nodeDimensions,
      nodeTypes,
      debugMode
    );
    var nodePositions = composition.nodePositions;
    var allPositionedNodes = composition.allPositionedNodes;
    var allPositionedEdges = composition.allPositionedEdges;

    var routingLookups = buildRoutingLookups(visibleNodes, routingData);
    if (debugMode) {
      console.log('[Step 4 SETUP] routingData:', !!routingData,
        'node_to_parent keys:', Object.keys(routingLookups.nodeToParent),
        'compute_recall parent:', routingLookups.nodeToParent['compute_recall']);
    }

    routeCrossBoundaryEdgesPhase(
      edges,
      allPositionedEdges,
      nodePositions,
      nodeDimensions,
      nodeTypes,
      routingLookups,
      feedbackEdgeKeys,
      debugMode
    );

    allPositionedEdges = applyEdgeReroutesPhase(
      allPositionedEdges,
      nodePositions,
      nodeDimensions,
      nodeTypes,
      routingLookups,
      debugMode
    );

    allPositionedEdges = mergeSharedTargetEdgesPhase(
      allPositionedEdges,
      nodePositions,
      nodeDimensions,
      debugMode
    );

    if (debugMode) {
      validateEdgesPhase(allPositionedEdges, nodePositions, nodeDimensions, nodeTypes);
    }

    return {
      nodes: allPositionedNodes,
      edges: allPositionedEdges,
      size: rootPhase.rootResult.size,
    };
  }

  // Export API
  return {
    useLayout: useLayout,
    calculateDimensions: calculateDimensions,
    groupNodesByParent: groupNodesByParent,
    getLayoutOrder: getLayoutOrder,
    performRecursiveLayout: performRecursiveLayout,
    // Constants
    TYPE_HINT_MAX_CHARS: TYPE_HINT_MAX_CHARS,
    NODE_LABEL_MAX_CHARS: NODE_LABEL_MAX_CHARS,
    CHAR_WIDTH_PX: CHAR_WIDTH_PX,
    NODE_BASE_PADDING: NODE_BASE_PADDING,
    FUNCTION_NODE_BASE_PADDING: FUNCTION_NODE_BASE_PADDING,
    MAX_NODE_WIDTH: MAX_NODE_WIDTH,
    GRAPH_PADDING: GRAPH_PADDING,
    HEADER_HEIGHT: HEADER_HEIGHT,
  };
});
