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
  var TYPE_HINT_MAX_CHARS = 25;
  var NODE_LABEL_MAX_CHARS = 25;
  var CHAR_WIDTH_PX = 7;
  var NODE_BASE_PADDING = 52;
  var FUNCTION_NODE_BASE_PADDING = 48;
  var MAX_NODE_WIDTH = 280;

  // Constants for nested graph layout
  var GRAPH_PADDING = 40;
  var HEADER_HEIGHT = 32;

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
      height = 16 + (numParams * 20) + ((numParams - 1) * 4);
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
      height = 52;
      if (n.data && !n.data.separateOutputs && outputs.length > 0) {
        height = 48 + 42 + ((outputs.length - 1) * 28);
      }
    }

    if (n.style && n.style.width) width = n.style.width;
    if (n.style && n.style.height) height = n.style.height;

    return { width: width, height: height };
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
   * @returns {Object} { layoutedNodes, layoutedEdges, layoutError, graphHeight, graphWidth, layoutVersion, isLayouting }
   */
  function useLayout(nodes, edges, expansionState) {
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
          var result = performRecursiveLayout(visibleNodes, edges, expansionState, debugMode);
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

        // Prepare nodes for constraint layout
        var layoutNodes = flatVisibleNodes.map(function(n) {
          var dims = calculateDimensions(n);
          return {
            id: n.id,
            width: dims.width,
            height: dims.height,
            x: 0,
            y: 0,
            _original: n,
          };
        });

        // Prepare edges for constraint layout
        var layoutEdges = visibleEdges.map(function(e) {
          return {
            id: e.id,
            source: e.source,
            target: e.target,
            _original: e,
          };
        });

        // Detect if we're in separate outputs mode
        var isSeparateOutputs = layoutNodes.some(function(n) {
          return n._original && n._original.data && n._original.data.separateOutputs;
        });

        var layoutOptions = isSeparateOutputs
          ? {
              ...ConstraintLayout.defaultOptions,
              layout: {
                ...ConstraintLayout.defaultOptions.layout,
                spaceY: 100,
                layerSpaceY: 90,
              }
            }
          : ConstraintLayout.defaultOptions;

        // Run constraint layout
        var result = ConstraintLayout.graph(
          layoutNodes,
          layoutEdges,
          null,
          'vertical',
          layoutOptions
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

        setLayoutedNodes(positionedNodes);
        setLayoutedEdges(positionedEdges);
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
    }, [nodes, edges, expansionState]);

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
   * Perform recursive layout for nested graphs
   * Layouts children first (deepest), then uses their bounds to size parent nodes
   * @param {Array} visibleNodes - All visible nodes
   * @param {Array} edges - All edges
   * @param {Map} expansionState - Which pipelines are expanded
   * @param {boolean} debugMode - Whether to log debug info
   * @returns {Object} { nodes, edges, size }
   */
  function performRecursiveLayout(visibleNodes, edges, expansionState, debugMode) {
    var nodeGroups = groupNodesByParent(visibleNodes);
    var layoutOrder = getLayoutOrder(visibleNodes, expansionState);
    var nodeDimensions = new Map();
    var childLayoutResults = new Map();

    // Calculate base dimensions for all nodes
    visibleNodes.forEach(function(n) {
      nodeDimensions.set(n.id, calculateDimensions(n));
    });

    // Step 1: Layout children bottom-up (deepest expanded graphs first)
    layoutOrder.forEach(function(graphNode) {
      var children = nodeGroups.get(graphNode.id) || [];
      if (children.length === 0) return;

      var childIds = new Set(children.map(function(c) { return c.id; }));
      var internalEdges = edges.filter(function(e) {
        return childIds.has(e.source) && childIds.has(e.target);
      });

      // Prepare children for layout
      var childLayoutNodes = children.map(function(n) {
        var dims = nodeDimensions.get(n.id);
        return {
          id: n.id,
          width: dims.width,
          height: dims.height,
          x: 0,
          y: 0,
          _original: n,
        };
      });

      var childLayoutEdges = internalEdges.map(function(e) {
        return { id: e.id, source: e.source, target: e.target, _original: e };
      });

      // Detect separate outputs mode
      var isSeparateOutputs = children.some(function(n) {
        return n.data && n.data.separateOutputs;
      });

      var layoutOptions = isSeparateOutputs
        ? {
            ...ConstraintLayout.defaultOptions,
            layout: {
              ...ConstraintLayout.defaultOptions.layout,
              spaceY: 100,
              layerSpaceY: 90,
            }
          }
        : ConstraintLayout.defaultOptions;

      // Run layout for children
      var childResult = ConstraintLayout.graph(
        childLayoutNodes,
        childLayoutEdges,
        null,
        'vertical',
        layoutOptions
      );

      childLayoutResults.set(graphNode.id, childResult);

      // childResult.size includes the constraint layout's internal padding (layoutPadding)
      // We need to replace that padding with our GRAPH_PADDING
      var layoutPadding = ConstraintLayout.defaultOptions.layout.padding || 50;
      var contentWidth = childResult.size.width - 2 * layoutPadding;
      var contentHeight = childResult.size.height - 2 * layoutPadding;

      if (debugMode) {
        console.log('[recursive layout] graph', graphNode.id, 'children:', children.length,
                    'layoutSize:', childResult.size, 'contentSize:', { width: contentWidth, height: contentHeight });
      }

      // Update graph node size: content + our padding + header
      nodeDimensions.set(graphNode.id, {
        width: contentWidth + GRAPH_PADDING * 2,
        height: contentHeight + GRAPH_PADDING * 2 + HEADER_HEIGHT,
      });
    });

    // Step 2: Layout root level nodes
    var rootNodes = nodeGroups.get(null) || [];
    var rootNodeIds = new Set(rootNodes.map(function(n) { return n.id; }));
    var rootEdges = edges.filter(function(e) {
      return rootNodeIds.has(e.source) && rootNodeIds.has(e.target);
    });

    var rootLayoutNodes = rootNodes.map(function(n) {
      var dims = nodeDimensions.get(n.id);
      return {
        id: n.id,
        width: dims.width,
        height: dims.height,
        x: 0,
        y: 0,
        _original: n,
      };
    });

    var rootLayoutEdges = rootEdges.map(function(e) {
      return { id: e.id, source: e.source, target: e.target, _original: e };
    });

    // Detect separate outputs mode for root
    var isSeparateOutputs = rootNodes.some(function(n) {
      return n.data && n.data.separateOutputs;
    });

    var layoutOptions = isSeparateOutputs
      ? {
          ...ConstraintLayout.defaultOptions,
          layout: {
            ...ConstraintLayout.defaultOptions.layout,
            spaceY: 100,
            layerSpaceY: 90,
          }
        }
      : ConstraintLayout.defaultOptions;

    var rootResult = ConstraintLayout.graph(
      rootLayoutNodes,
      rootLayoutEdges,
      null,
      'vertical',
      layoutOptions
    );

    // Step 3: Compose final positions
    var nodePositions = new Map();
    var allPositionedNodes = [];
    var allPositionedEdges = [];

    // Position root nodes
    rootResult.nodes.forEach(function(n) {
      var w = n.width;
      var h = n.height;
      var x = n.x - w / 2;
      var y = n.y - h / 2;
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

    // Position root edges
    // For edges with innerTargets (INPUT_GROUP -> expanded pipeline), visually route
    // to the inner node instead of the container boundary
    rootResult.edges.forEach(function(e) {
      var edgeData = e._original.data || {};
      var innerTargets = edgeData.innerTargets;

      // Check if this edge should route to an inner node
      if (innerTargets && innerTargets.length > 0) {
        // Route to the first inner target
        var innerTargetId = innerTargets[0];
        var innerPos = nodePositions.get(innerTargetId);
        var innerDims = nodeDimensions.get(innerTargetId);

        if (innerPos && innerDims) {
          // Get source position from the edge points (first point is source)
          var sourcePoint = e.points && e.points.length > 0 ? e.points[0] : null;

          if (sourcePoint) {
            // Create new path to inner node
            var innerX = innerPos.x + innerDims.width / 2;
            var innerY = innerPos.y;

            var newPoints = [
              sourcePoint,
              { x: innerX, y: innerY }
            ];

            if (debugMode) {
              console.log('[recursive layout] rerouting edge to inner target:', e.id, innerTargetId, newPoints);
            }

            allPositionedEdges.push({
              ...e._original,
              data: { ...edgeData, points: newPoints, reroutedToInner: true },
            });
            return;
          }
        }
      }

      // Default: use the original routed points
      allPositionedEdges.push({
        ...e._original,
        data: { ...e._original.data, points: e.points },
      });
    });

    // Position children within their parents
    layoutOrder.forEach(function(graphNode) {
      var childResult = childLayoutResults.get(graphNode.id);
      if (!childResult) return;

      var parentPos = nodePositions.get(graphNode.id);
      if (!parentPos) return;

      // Get the layout's internal padding (used by ConstraintLayout.graph)
      // The constraint layout already offsets nodes by size.min, so returned positions
      // are already normalized. We just need to convert from center to top-left.
      var layoutPadding = ConstraintLayout.defaultOptions.layout.padding || 50;

      // For absolute positioning (edge routing), offset from parent's top-left
      var absOffsetX = parentPos.x + GRAPH_PADDING;
      var absOffsetY = parentPos.y + GRAPH_PADDING + HEADER_HEIGHT;

      childResult.nodes.forEach(function(n) {
        var w = n.width;
        var h = n.height;
        // Convert from center to top-left
        // The constraint layout positions are already offset with internal padding (50px default)
        // We want positions relative to parent's content area (which has GRAPH_PADDING)
        // So we adjust: subtract layout padding, add our GRAPH_PADDING
        var childX = n.x - w / 2 - layoutPadding + GRAPH_PADDING;
        var childY = n.y - h / 2 - layoutPadding + GRAPH_PADDING;

        // Store absolute position for edge routing
        nodePositions.set(n.id, { x: absOffsetX + childX, y: absOffsetY + childY });

        if (debugMode) {
          console.log('[recursive layout] child', n.id, 'position:', { x: childX, y: childY + HEADER_HEIGHT }, 'parentNode:', n._original.parentNode);
        }

        allPositionedNodes.push({
          ...n._original,
          // React Flow child positions are relative to parent's top-left corner
          // childX/childY already include GRAPH_PADDING adjustment
          position: {
            x: childX,
            y: childY + HEADER_HEIGHT
          },
          width: w,
          height: h,
          style: { ...n._original.style, width: w, height: h },
          handles: [
            { type: 'target', position: 'top', x: w / 2, y: 0, width: 8, height: 8, id: null },
            { type: 'source', position: 'bottom', x: w / 2, y: h, width: 8, height: 8, id: null },
          ],
        });
      });

      // Position child edges with offset
      // Edge points are in the same coordinate space as nodes (already include layout padding)
      // Transform them to absolute coordinates for edge rendering
      childResult.edges.forEach(function(e) {
        var offsetPoints = (e.points || []).map(function(pt) {
          return {
            x: pt.x - layoutPadding + absOffsetX,
            y: pt.y - layoutPadding + absOffsetY
          };
        });

        allPositionedEdges.push({
          ...e._original,
          data: { ...e._original.data, points: offsetPoints },
        });
      });
    });

    // Step 4: Handle cross-hierarchy edges (edges between root and child nodes)
    // These edges were filtered out from both rootEdges and internalEdges
    var processedEdgeIds = new Set(allPositionedEdges.map(function(e) { return e.id; }));
    var crossHierarchyEdges = edges.filter(function(e) {
      return !processedEdgeIds.has(e.id);
    });

    if (debugMode && crossHierarchyEdges.length > 0) {
      console.log('[recursive layout] cross-hierarchy edges:', crossHierarchyEdges.length);
    }

    crossHierarchyEdges.forEach(function(e) {
      var sourcePos = nodePositions.get(e.source);
      var targetPos = nodePositions.get(e.target);

      if (!sourcePos || !targetPos) {
        if (debugMode) {
          console.log('[recursive layout] skipping cross-hierarchy edge - missing position:', e.id, 'source:', e.source, 'target:', e.target);
        }
        return;
      }

      var sourceDims = nodeDimensions.get(e.source);
      var targetDims = nodeDimensions.get(e.target);

      if (!sourceDims || !targetDims) {
        if (debugMode) {
          console.log('[recursive layout] skipping cross-hierarchy edge - missing dimensions:', e.id);
        }
        return;
      }

      // Calculate edge points: from source center-bottom to target center-top
      var sourceX = sourcePos.x + sourceDims.width / 2;
      var sourceY = sourcePos.y + sourceDims.height;
      var targetX = targetPos.x + targetDims.width / 2;
      var targetY = targetPos.y;

      var points = [
        { x: sourceX, y: sourceY },
        { x: targetX, y: targetY }
      ];

      if (debugMode) {
        console.log('[recursive layout] cross-hierarchy edge:', e.id, 'points:', points);
      }

      allPositionedEdges.push({
        ...e,
        data: { ...e.data, points: points },
      });
    });

    return {
      nodes: allPositionedNodes,
      edges: allPositionedEdges,
      size: rootResult.size,
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
