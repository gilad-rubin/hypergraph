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
  var GRAPH_PADDING = 24;
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
  function performRecursiveLayout(visibleNodes, edges, expansionState, debugMode, routingData) {
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

      // Post-layout correction for tall nodes within nested graph
      // Same fix as Step 2.5 for root level - ensure node edges don't overlap
      var CHILD_EDGE_GAP = 30;
      var childNodeById = new Map(childResult.nodes.map(function(n) { return [n.id, n]; }));

      childLayoutEdges.forEach(function(e) {
        var srcNode = childNodeById.get(e.source);
        var tgtNode = childNodeById.get(e.target);
        if (!srcNode || !tgtNode) return;

        var srcBottom = srcNode.y + srcNode.height / 2;
        var tgtTop = tgtNode.y - tgtNode.height / 2;
        var gap = tgtTop - srcBottom;

        if (gap < CHILD_EDGE_GAP) {
          var shift = CHILD_EDGE_GAP - gap;
          var targetY = tgtNode.y;
          if (debugMode) {
            console.log('[recursive layout] child shift:', graphNode.id, '- shifting', tgtNode.id, 'down by', shift);
          }
          childResult.nodes.forEach(function(n) {
            if (n.y >= targetY) {
              n.y += shift;
            }
          });
        }
      });

      // Recalculate bounds after shifts
      // Use GRAPH_PADDING (not layout.padding) - this is the single source of padding
      childResult.size = recalculateBounds(childResult.nodes, GRAPH_PADDING);

      childLayoutResults.set(graphNode.id, childResult);

      if (debugMode) {
        console.log('[recursive layout] graph', graphNode.id, 'children:', children.length, 'size:', childResult.size);
      }

      // Update graph node size from children bounds
      // Padding already included in childResult.size from recalculateBounds
      nodeDimensions.set(graphNode.id, {
        width: childResult.size.width,
        height: childResult.size.height + HEADER_HEIGHT,
      });
    });

    // Step 2: Layout root level nodes
    var rootNodes = nodeGroups.get(null) || [];
    var rootNodeIds = new Set(rootNodes.map(function(n) { return n.id; }));

    if (debugMode) {
      console.log('[recursive layout] rootNodes:', rootNodes.map(function(n) { return n.id; }));
      console.log('[recursive layout] nodeDimensions:', Array.from(nodeDimensions.entries()).map(function(e) {
        return { id: e[0], w: e[1].width, h: e[1].height };
      }));
    }

    // Build map of child -> root-level ancestor for edge lifting
    // This ensures edges from/to deeply nested nodes are lifted all the way up
    // (e.g., step1 inside inner inside middle -> lifts to middle)
    var childToRootAncestor = new Map();
    var nodeByIdForLifting = new Map(visibleNodes.map(function(n) { return [n.id, n]; }));

    visibleNodes.forEach(function(n) {
      if (rootNodeIds.has(n.id)) return; // Already root-level

      // Walk up to find root-level ancestor
      var current = n;
      while (current && current.parentNode) {
        if (rootNodeIds.has(current.parentNode)) {
          childToRootAncestor.set(n.id, current.parentNode);
          break;
        }
        current = nodeByIdForLifting.get(current.parentNode);
      }
    });

    // Collect and lift edges that cross into/out of expanded nested graphs
    // If an edge connects a root node to a child of another root node,
    // treat it as connecting to the parent container for layout purposes
    var rootEdgeSet = new Set();
    var rootEdges = [];

    edges.forEach(function(e) {
      var source = e.source;
      var target = e.target;

      // Lift source if it's inside a nested graph (any depth)
      if (childToRootAncestor.has(source)) {
        source = childToRootAncestor.get(source);
      }
      // Lift target if it's inside a nested graph (any depth)
      if (childToRootAncestor.has(target)) {
        target = childToRootAncestor.get(target);
      }

      // Only include if both endpoints are now root-level and it's not a self-loop
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

    // rootEdges already has the lifted structure we need
    var rootLayoutEdges = rootEdges;

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

    if (debugMode) {
      console.log('[recursive layout] BEFORE SHIFTS - rootResult.nodes:', JSON.stringify(rootResult.nodes.map(function(n) {
        return { id: n.id, x: n.x, y: n.y, h: n.height };
      })));
    }

    // Step 2.5: Post-layout correction for tall nodes
    // The constraint layout uses center coordinates, but tall expanded nodes
    // may still overlap if the spacing isn't sufficient. Shift nodes down to fix.
    var EDGE_GAP = 30; // Minimum gap between source bottom and target top
    var nodeById = new Map(rootResult.nodes.map(function(n) { return [n.id, n]; }));

    rootLayoutEdges.forEach(function(e) {
      var srcNode = nodeById.get(e.source);
      var tgtNode = nodeById.get(e.target);
      if (!srcNode || !tgtNode) return;

      var srcBottom = srcNode.y + srcNode.height / 2;
      var tgtTop = tgtNode.y - tgtNode.height / 2;
      var gap = tgtTop - srcBottom;

      if (gap < EDGE_GAP) {
        // Need to shift target down
        var shift = EDGE_GAP - gap;
        // Capture target Y BEFORE modification (tgtNode.y will change during iteration)
        var targetY = tgtNode.y;
        if (debugMode) {
          console.log('[recursive layout] shifting', tgtNode.id, 'down by', shift, 'for proper spacing (nodes at y >=', targetY, ')');
        }
        // Shift this node and all nodes below it
        rootResult.nodes.forEach(function(n) {
          if (n.y >= targetY) {
            n.y += shift;
          }
        });
      }
    });

    // Recalculate root bounds with GRAPH_PADDING for consistent padding
    rootResult.size = recalculateBounds(rootResult.nodes, GRAPH_PADDING);

    // Step 3: Compose final positions
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

    // Position root nodes - subtract size.min to normalize to bounds origin
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

    // Note: rootResult.edges are LIFTED edges for layout ordering only.
    // We don't output them directly - actual cross-boundary edges will be
    // handled in Step 4 after all node positions are computed.

    // Position children within their parents
    // Process in REVERSE order (shallowest first) because we need parent positions
    // to be available before positioning children. layoutOrder is deepest-first
    // for size calculation, but we need shallowest-first for position composition.
    var reverseLayoutOrder = layoutOrder.slice().reverse();
    reverseLayoutOrder.forEach(function(graphNode) {
      var childResult = childLayoutResults.get(graphNode.id);
      if (!childResult) return;

      var parentPos = nodePositions.get(graphNode.id);
      if (!parentPos) return;

      // For absolute positioning (edge routing), offset from parent's top-left
      // Note: size.min already includes GRAPH_PADDING, so we don't add it separately
      var absOffsetX = parentPos.x;
      var absOffsetY = parentPos.y + HEADER_HEIGHT;

      childResult.nodes.forEach(function(n) {
        var w = n.width;
        var h = n.height;
        // Convert from center to top-left, relative to the padded bounds
        // Subtracting size.min.x/y normalizes positions to start from the bounds origin
        var childX = n.x - w / 2 - childResult.size.min.x;
        var childY = n.y - h / 2 - childResult.size.min.y;

        // Store absolute position for edge routing
        nodePositions.set(n.id, { x: absOffsetX + childX, y: absOffsetY + childY });

        // Store dimensions for edge re-routing (Step 4.5)
        nodeDimensions.set(n.id, { width: w, height: h });

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
      // Edge points are in the same coordinate space as nodes
      // Transform them to absolute coordinates for edge rendering
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

    // Step 4: Handle cross-boundary edges
    // These are edges where source and target are in different scopes
    // (e.g., root level to inside a nested graph, or between nested graphs)
    var handledEdges = new Set(allPositionedEdges.map(function(e) {
      return e.source + '->' + e.target;
    }));

    // Build lookup for INPUT_GROUP actualTargets
    var inputGroupActualTargets = new Map();
    visibleNodes.forEach(function(n) {
      if (n.data && n.data.nodeType === 'INPUT_GROUP' && n.data.actualTargets) {
        inputGroupActualTargets.set(n.id, n.data.actualTargets);
      }
    });

    // Get output_to_producer from routing data
    var outputToProducer = (routingData && routingData.output_to_producer) || {};

    edges.forEach(function(e) {
      var edgeKey = e.source + '->' + e.target;
      if (handledEdges.has(edgeKey)) return; // Already positioned

      var srcPos = nodePositions.get(e.source);
      var tgtPos = nodePositions.get(e.target);

      if (!srcPos || !tgtPos) return; // One or both endpoints not visible

      // Get dimensions for proper anchor points
      var srcDims = nodeDimensions.get(e.source);
      var tgtDims = nodeDimensions.get(e.target);

      if (!srcDims || !tgtDims) return;

      // Step 4.5: Re-route edges to actual internal nodes when expanded
      var actualSrc = e.source;
      var actualTgt = e.target;
      var actualSrcPos = srcPos;
      var actualTgtPos = tgtPos;
      var actualSrcDims = srcDims;
      var actualTgtDims = tgtDims;

      // For INPUT_GROUP edges, route to actual target if available
      if (inputGroupActualTargets.has(e.source)) {
        var actualTargets = inputGroupActualTargets.get(e.source);
        // Find which actual target has a position (is visible)
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

      // For data edges, route from actual producer if available
      var valueName = e.data && e.data.valueName;
      if (valueName && outputToProducer[valueName]) {
        var actualProducer = outputToProducer[valueName];
        // Try to find the DATA node for the actual producer
        var actualDataNodeId = 'data_' + actualProducer + '_' + valueName;

        if (nodePositions.has(actualDataNodeId) && nodeDimensions.has(actualDataNodeId)) {
          // Use the actual data node position
          actualSrc = actualDataNodeId;
          actualSrcPos = nodePositions.get(actualDataNodeId);
          actualSrcDims = nodeDimensions.get(actualDataNodeId);
        } else if (nodePositions.has(actualProducer) && nodeDimensions.has(actualProducer)) {
          // Fall back to the producer function node (edge starts from its bottom)
          actualSrc = actualProducer;
          actualSrcPos = nodePositions.get(actualProducer);
          actualSrcDims = nodeDimensions.get(actualProducer);
        }
      }

      // Compute edge points using actual (re-routed) positions
      var srcCenterX = actualSrcPos.x + actualSrcDims.width / 2;
      var srcBottomY = actualSrcPos.y + actualSrcDims.height;
      var tgtCenterX = actualTgtPos.x + actualTgtDims.width / 2;
      var tgtTopY = actualTgtPos.y;

      // Simple 2-point edge for cross-boundary connections
      var points = [
        { x: srcCenterX, y: srcBottomY },
        { x: tgtCenterX, y: tgtTopY }
      ];

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
          // Store actual routing targets for edge validation
          actualSource: actualSrc,
          actualTarget: actualTgt,
        },
      });
    });

    // Step 5: Apply actualSource/actualTarget routing to internal edges from child layouts
    // These edges were already positioned by child layouts, but may need re-routing
    // when their source is an expanded container
    allPositionedEdges = allPositionedEdges.map(function(e) {
      // Skip if already has actualSource set (from cross-boundary handling)
      if (e.data && e.data.actualSource && e.data.actualSource !== e.source) {
        return e;
      }

      var valueName = e.data && e.data.valueName;
      var actualProducer = (valueName && outputToProducer[valueName]) ? outputToProducer[valueName] : null;

      // Check if we need to re-route the start
      var needsStartReroute = actualProducer && actualProducer !== e.source &&
        nodePositions.has(actualProducer) && nodeDimensions.has(actualProducer);

      // Check if we need to re-route the end (target node position might differ)
      var needsEndReroute = nodePositions.has(e.target) && nodeDimensions.has(e.target);

      if (!needsStartReroute && !needsEndReroute) {
        return e;
      }

      var newPoints = (e.data.points || []).slice();
      var actualSrc = e.source;
      var actualTgt = e.target;

      // Re-route edge start to actual producer
      if (needsStartReroute) {
        var producerPos = nodePositions.get(actualProducer);
        var producerDims = nodeDimensions.get(actualProducer);
        var newStartX = producerPos.x + producerDims.width / 2;
        var newStartY = producerPos.y + producerDims.height;
        if (newPoints.length > 0) {
          newPoints[0] = { x: newStartX, y: newStartY };
        }
        actualSrc = actualProducer;
      }

      // Re-route edge end to target's center-top (always fix this for internal edges)
      if (needsEndReroute) {
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
