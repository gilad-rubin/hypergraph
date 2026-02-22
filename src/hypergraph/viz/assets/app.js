/**
 * Main App component for Hypergraph visualization
 * Orchestrates all visualization components and state management
 */
(function(root, factory) {
  var api = factory(root);
  if (root) root.HypergraphVizApp = api;
})(typeof window !== 'undefined' ? window : this, function(root) {
  'use strict';

  // Get dependencies from globals
  var React = root.React;
  var ReactDOM = root.ReactDOM;
  var RF = root.ReactFlow;
  var htm = root.htm;

  // Get our modules
  var VizTheme = root.HypergraphVizTheme;
  var VizLayout = root.HypergraphVizLayout;
  var VizComponents = root.HypergraphVizComponents;

  if (!React || !ReactDOM || !RF || !htm) {
    console.error('HypergraphVizApp: Missing required globals (React, ReactDOM, ReactFlow, htm)');
    return {};
  }

  if (!VizTheme || !VizLayout || !VizComponents) {
    console.error('HypergraphVizApp: Missing required modules (VizTheme, VizLayout, VizComponents)');
    return {};
  }

  var useState = React.useState;
  var useEffect = React.useEffect;
  var useMemo = React.useMemo;
  var useCallback = React.useCallback;
  var useRef = React.useRef;

  var ReactFlow = RF.ReactFlow;
  var Background = RF.Background;
  var Panel = RF.Panel;
  var Position = RF.Position;
  var MarkerType = RF.MarkerType;
  var ReactFlowProvider = RF.ReactFlowProvider;
  var useNodesState = RF.useNodesState;
  var useEdgesState = RF.useEdgesState;
  var useReactFlow = RF.useReactFlow;
  var useUpdateNodeInternals = RF.useUpdateNodeInternals;

  var html = htm.bind(React.createElement);

  // Import from modules
  var detectHostTheme = VizTheme.detectHostTheme;
  var normalizeThemePref = VizTheme.normalizeThemePref;
  var useLayout = VizLayout.useLayout;
  var CustomNode = VizComponents.CustomNode;
  var CustomEdge = VizComponents.CustomEdge;
  var CustomControls = VizComponents.CustomControls;

  // Node and edge types
  var nodeTypes = { custom: CustomNode, pipelineGroup: CustomNode };
  var edgeTypes = { custom: CustomEdge };

  // Node type to wrapper offset mapping (matches constraint-layout.js and layout.js)
  // Used to compute visible bounds from wrapper bounds
  var VizConstants = root.HypergraphVizConstants || {};
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

  function getNodeTypeOffset(nodeType, isExpanded) {
    if (nodeType === 'PIPELINE' && !isExpanded) {
      nodeType = 'FUNCTION';
    }
    return NODE_TYPE_OFFSETS[nodeType] ?? DEFAULT_OFFSET;
  }

  function getVisibleBottom(y, height, nodeType, isExpanded) {
    var offset = getNodeTypeOffset(nodeType, isExpanded);
    return y + height - offset;
  }

  // === MAIN APP COMPONENT ===
  var App = function(props) {
    var initialData = props.initialData;
    var themePreference = props.themePreference;
    var showThemeDebug = props.showThemeDebug;
    var panOnScroll = props.panOnScroll;
    var initialSeparateOutputs = props.initialSeparateOutputs;
    var initialShowTypes = props.initialShowTypes;
    var separateOutputsState = useState(initialSeparateOutputs);
    var separateOutputs = separateOutputsState[0];
    var setSeparateOutputs = separateOutputsState[1];

    var showTypesState = useState(initialShowTypes);
    var showTypes = showTypesState[0];
    var setShowTypes = showTypesState[1];

    var themeDebugState = useState({ source: 'init', luminance: null, background: 'transparent', appliedTheme: themePreference });
    var themeDebug = themeDebugState[0];
    var setThemeDebug = themeDebugState[1];

    var onToggleSeparateOutputs = useCallback(function(nextValue) {
      root.__hypergraphVizReady = false;
      setSeparateOutputs(function(prev) {
        return typeof nextValue === 'boolean' ? nextValue : !prev;
      });
    }, [setSeparateOutputs]);

    var onToggleShowTypes = useCallback(function(nextValue) {
      root.__hypergraphVizReady = false;
      setShowTypes(function(prev) {
        return typeof nextValue === 'boolean' ? nextValue : !prev;
      });
    }, [setShowTypes]);

    // Debug/test hook to toggle render options without UI interaction.
    useEffect(function() {
      root.__hypergraphVizSetRenderOptions = function(options) {
        if (!options || typeof options !== 'object') return;
        if (Object.prototype.hasOwnProperty.call(options, 'separateOutputs')) {
          onToggleSeparateOutputs(!!options.separateOutputs);
        }
        if (Object.prototype.hasOwnProperty.call(options, 'showTypes')) {
          onToggleShowTypes(!!options.showTypes);
        }
      };
      return function() {
        delete root.__hypergraphVizSetRenderOptions;
      };
    }, [onToggleSeparateOutputs, onToggleShowTypes]);

    var detectedThemeState = useState(function() { return detectHostTheme(); });
    var detectedTheme = detectedThemeState[0];
    var setDetectedTheme = detectedThemeState[1];

    var manualThemeState = useState(null);
    var manualTheme = manualThemeState[0];
    var setManualTheme = manualThemeState[1];

    var bgColorState = useState((detectedTheme && detectedTheme.background) || 'transparent');
    var bgColor = bgColorState[0];
    var setBgColor = bgColorState[1];

    // Track expansion state
    var expansionStateState = useState(function() {
      var map = new Map();
      initialData.nodes.forEach(function(n) {
        if (n.data && n.data.nodeType === 'PIPELINE') {
          map.set(n.id, n.data.isExpanded || false);
        }
      });
      return map;
    });
    var expansionState = expansionStateState[0];
    var setExpansionState = expansionStateState[1];

    // Track when we're in the middle of centering (to hide content during viewport adjustment)
    var isCenteringState = useState(false);
    var isCentering = isCenteringState[0];
    var setIsCentering = isCenteringState[1];

    // Pre-computed nodes/edges for all expansion states (from Python)
    var edgesByState = (initialData.meta && initialData.meta.edgesByState) || {};
    var nodesByState = (initialData.meta && initialData.meta.nodesByState) || {};
    var expandableNodes = (initialData.meta && initialData.meta.expandableNodes) || [];

    // Helper: convert expansionState Map to canonical key format
    // Key format: "nodeId:0|sep:X" or "sep:X" (when no expandable nodes)
    var expansionStateToKey = function(expState, separateOutputsFlag) {
      var sepKey = 'sep:' + (separateOutputsFlag ? '1' : '0');

      if (expandableNodes.length === 0) {
        return sepKey;
      }

      var parts = [];
      expandableNodes.forEach(function(nodeId) {
        var isExpanded = expState.get(nodeId) || false;
        parts.push(nodeId + ':' + (isExpanded ? '1' : '0'));
      });
      // expandableNodes is already sorted (from Python), so parts will be sorted
      var expKey = parts.join(',');
      return expKey + '|' + sepKey;
    };

    // React Flow state
    var nodesState = useNodesState([]);
    var rfNodes = nodesState[0];
    var setNodes = nodesState[1];
    var onNodesChange = nodesState[2];

    var edgesState = useEdgesState([]);
    var rfEdges = edgesState[0];
    var setEdges = edgesState[1];
    var onEdgesChange = edgesState[2];

    var nodesRef = useRef(initialData.nodes);

    var resolvedDetected = detectedTheme || {
      theme: themePreference === 'auto' ? 'dark' : themePreference,
      background: 'transparent',
      luminance: null,
      source: 'init'
    };

    var activeTheme = useMemo(function() {
      if (manualTheme) return manualTheme;
      var base = themePreference === 'auto' ? (resolvedDetected.theme || 'dark') : themePreference;
      return base;
    }, [manualTheme, resolvedDetected.theme, themePreference]);

    var activeBackground = useMemo(function() {
      if (manualTheme) return manualTheme === 'light' ? '#f8fafc' : '#020617';
      var bg = resolvedDetected.background;
      if (!bg || bg === 'transparent' || bg === 'rgba(0, 0, 0, 0)') {
        return activeTheme === 'light' ? '#f8fafc' : '#020617';
      }
      return bg;
    }, [manualTheme, resolvedDetected.background, activeTheme]);

    var theme = activeTheme;

    // Expansion toggle handler
    var onToggleExpand = useCallback(function(nodeId) {
      // Signal that layout is changing (for tests to wait on)
      root.__hypergraphVizReady = false;

      setExpansionState(function(prev) {
        var newMap = new Map(prev);
        var isCurrentlyExpanded = newMap.get(nodeId) || false;
        var willExpand = !isCurrentlyExpanded;
        newMap.set(nodeId, willExpand);

        if (!willExpand) {
          var currentNodes = nodesRef.current || [];
          var childrenMap = new Map();
          currentNodes.forEach(function(n) {
            if (n.parentNode) {
              if (!childrenMap.has(n.parentNode)) childrenMap.set(n.parentNode, []);
              childrenMap.get(n.parentNode).push(n.id);
            }
          });

          var getDescendants = function(id) {
            var children = childrenMap.get(id) || [];
            var res = children.slice();
            children.forEach(function(childId) {
              res = res.concat(getDescendants(childId));
            });
            return res;
          };

          getDescendants(nodeId).forEach(function(descId) {
            if (newMap.has(descId)) newMap.set(descId, false);
          });
        }

        root.__hypergraphVizExpansionState = newMap;
        return newMap;
      });
    }, []);

    // Select precomputed nodes for current expansion state
    var selectedNodes = useMemo(function() {
      var key = expansionStateToKey(expansionState, separateOutputs);
      var precomputed = nodesByState[key];

      if (precomputed && precomputed.length > 0) {
        if (root.__hypergraph_debug_viz) {
          console.log('[App] Using pre-computed nodes for key:', key, '- count:', precomputed.length);
        }
      } else if (root.__hypergraph_debug_viz) {
        console.log('[App] No pre-computed nodes for key:', key, '- using initialData.nodes');
      }

      var baseNodes = (precomputed && precomputed.length > 0) ? precomputed : initialData.nodes;
      return baseNodes.map(function(n) {
        return {
          ...n,
          data: {
            ...n.data,
            theme: activeTheme,
            showTypes: showTypes,
            separateOutputs: separateOutputs,
          },
        };
      });
    }, [expansionState, separateOutputs, showTypes, activeTheme, nodesByState, initialData.nodes]);

    // Add callbacks to nodes
    var nodesWithCallbacks = useMemo(function() {
      return selectedNodes.map(function(n) {
        return {
          ...n,
          data: {
            ...n.data,
            onToggleExpand: (n.data && n.data.nodeType === 'PIPELINE') ? function() { onToggleExpand(n.id); } : n.data.onToggleExpand
          },
        };
      });
    }, [selectedNodes, onToggleExpand]);

    // Theme detection listener
    useEffect(function() {
      var applyThemeDetection = function() {
        var detected = detectHostTheme();
        setDetectedTheme(detected);
      };

      applyThemeDetection();

      var observers = [];
      try {
        var parentDoc = root.parent && root.parent.document;
        if (parentDoc) {
          var config = { attributes: true, attributeFilter: ['class', 'data-vscode-theme-kind', 'style'] };
          var observer = new MutationObserver(applyThemeDetection);
          observer.observe(parentDoc.body, config);
          observer.observe(parentDoc.documentElement, config);
          observers.push(observer);
        }
      } catch(e) {}

      var mq = root.matchMedia ? root.matchMedia('(prefers-color-scheme: dark)') : null;
      var mqHandler = function() { applyThemeDetection(); };
      if (mq && mq.addEventListener) mq.addEventListener('change', mqHandler);
      else if (mq && mq.addListener) mq.addListener(mqHandler);

      return function() {
        observers.forEach(function(o) { o.disconnect(); });
        if (mq && mq.removeEventListener) mq.removeEventListener('change', mqHandler);
        else if (mq && mq.removeListener) mq.removeListener(mqHandler);
      };
    }, []);

    // Apply effective theme + background
    useEffect(function() {
      setBgColor(activeBackground);
      document.body.classList.toggle('light-mode', activeTheme === 'light');

      var checkNodeStyles = function() {
        var node = document.querySelector('.react-flow__node');
        if (node) {
          var computed = getComputedStyle(node.querySelector('div') || node);
          var nodeBg = computed.backgroundColor;
          var nodeClass = ((node.querySelector('div') || node).className || '');
          setThemeDebug(function(prev) {
            return {
              ...prev,
              nodeBg: nodeBg,
              nodeClass: nodeClass.split(' ').find(function(c) { return c.startsWith('node-function-'); }) || 'unknown'
            };
          });
        }
      };

      checkNodeStyles();
      var interval = setInterval(checkNodeStyles, 1000);

      if (showThemeDebug) {
        setThemeDebug(function(prev) {
          return {
            ...prev,
            source: manualTheme ? 'manual toggle' : resolvedDetected.source,
            luminance: resolvedDetected.luminance,
            background: activeBackground,
            appliedTheme: activeTheme,
          };
        });
      }
      return function() { clearInterval(interval); };
    }, [activeTheme, activeBackground, resolvedDetected, showThemeDebug, themePreference, manualTheme]);

    // Theme toggle
    var toggleTheme = useCallback(function() {
      if (manualTheme === null) {
        var detected = resolvedDetected.theme || 'dark';
        setManualTheme(detected === 'dark' ? 'light' : 'dark');
      } else {
        setManualTheme(null);
      }
    }, [manualTheme, resolvedDetected.theme]);

    // Select edges from pre-computed edge sets based on current expansion state
    // This ensures collapse/expand produces EXACTLY the same edges as depth=0/1 render
    var selectedEdges = useMemo(function() {
      var key = expansionStateToKey(expansionState, separateOutputs);
      var precomputed = edgesByState[key];

      if (precomputed && precomputed.length > 0) {
        // Use pre-computed edges - guaranteed to match Python's calculation
        if (root.__hypergraph_debug_viz) {
          console.log('[App] Using pre-computed edges for key:', key, '- count:', precomputed.length);
        }
        return precomputed;
      }

      // Fallback to initial edges for compatibility with older renders
      if (root.__hypergraph_debug_viz) {
        console.log('[App] No pre-computed edges for key:', key, '- using initialData.edges');
      }
      return initialData.edges || [];
    }, [expansionState, separateOutputs, edgesByState, initialData.edges]);

    // Update React Flow state
    useEffect(function() {
      nodesRef.current = nodesWithCallbacks;
      setNodes(nodesWithCallbacks);
      setEdges(selectedEdges);
    }, [nodesWithCallbacks, selectedEdges, setNodes, setEdges]);

    // Grouped nodes/edges
    var grouped = useMemo(function() {
      return { nodes: nodesWithCallbacks, edges: selectedEdges };
    }, [nodesWithCallbacks, selectedEdges]);

    // Get routing data from initialData meta for edge re-routing
    var routingData = useMemo(function() {
      return {
        output_to_producer: (initialData.meta && initialData.meta.output_to_producer) || {},
        param_to_consumer: (initialData.meta && initialData.meta.param_to_consumer) || {},
        node_to_parent: (initialData.meta && initialData.meta.node_to_parent) || {},
      };
    }, [initialData]);

    // Run layout
    var layoutResult = useLayout(grouped.nodes, grouped.edges, expansionState, routingData);
    var rawLayoutedNodes = layoutResult.layoutedNodes;
    var layoutedEdges = layoutResult.layoutedEdges;
    var layoutError = layoutResult.layoutError;
    var graphHeight = layoutResult.graphHeight;
    var graphWidth = layoutResult.graphWidth;
    var layoutVersion = layoutResult.layoutVersion;
    var isLayouting = layoutResult.isLayouting;

    var rf = useReactFlow();
    var fitView = rf.fitView;
    var fitBounds = rf.fitBounds;
    var getViewport = rf.getViewport;
    var setViewport = rf.setViewport;
    var updateNodeInternals = useUpdateNodeInternals();

    // Viewport padding constants
    var PADDING_TOP = 16;
    var PADDING_BOTTOM = 16;
    var PADDING_LEFT = 20;
    var PADDING_RIGHT = 100;

    // Custom fit function with fixed pixel padding
    var fitWithFixedPadding = useCallback(function() {
      if (rawLayoutedNodes.length === 0) return;

      // Signal that we're centering (for tests to wait on)
      root.__hypergraphVizReady = false;

      // Calculate bounds from nodes
      var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      rawLayoutedNodes.forEach(function(node) {
        var x = (node.position && node.position.x) || 0;
        var y = (node.position && node.position.y) || 0;
        var w = node.width || (node.style && node.style.width) || 200;
        var h = node.height || (node.style && node.style.height) || 50;
        minX = Math.min(minX, x);
        minY = Math.min(minY, y);
        maxX = Math.max(maxX, x + w);
        maxY = Math.max(maxY, y + h);
      });

      // Include edge waypoints in bounds
      layoutedEdges.forEach(function(edge) {
        var points = (edge.data && edge.data.points) || [];
        points.forEach(function(pt) {
          if (pt.x !== undefined) {
            minX = Math.min(minX, pt.x);
            maxX = Math.max(maxX, pt.x);
          }
          if (pt.y !== undefined) {
            minY = Math.min(minY, pt.y);
            maxY = Math.max(maxY, pt.y);
          }
        });
      });

      var contentWidth = maxX - minX;
      var contentHeight = maxY - minY;

      // Get viewport dimensions
      var viewportEl = document.querySelector('.react-flow__viewport');
      viewportEl = viewportEl && viewportEl.parentElement;
      var viewportWidth = (viewportEl && viewportEl.clientWidth) || 800;
      var viewportHeight = (viewportEl && viewportEl.clientHeight) || 600;

      var zoom = 1;

      // Y: center vertically
      var contentCenterY = (minY + maxY) / 2;
      var targetScreenCenterY = viewportHeight / 2;
      var idealNewY = targetScreenCenterY - contentCenterY * zoom;
      var minNewY = PADDING_TOP - minY * zoom;
      var newY = Math.max(idealNewY, minNewY);

      // X: center in viewport
      var buttonPanel = document.querySelector('.react-flow__panel.bottom-right');
      var buttonPanelSpace = PADDING_RIGHT;
      if (buttonPanel && viewportEl) {
        var panelRect = buttonPanel.getBoundingClientRect();
        var containerRect = viewportEl.getBoundingClientRect();
        buttonPanelSpace = containerRect.right - panelRect.left;
      }

      var contentCenterX = (minX + maxX) / 2;
      var targetScreenCenterX = viewportWidth / 2;
      var idealNewX = targetScreenCenterX - contentCenterX * zoom;
      var minNewX = PADDING_LEFT - minX * zoom;
      var newX = Math.max(idealNewX, minNewX);

      setViewport({ x: newX, y: newY, zoom: zoom }, { duration: 0 });

      // Wait for DOM update, then apply centering corrections
      requestAnimationFrame(function() {
        requestAnimationFrame(function() {
          var vpEl = document.querySelector('.react-flow__viewport');
          vpEl = vpEl && vpEl.parentElement;
          var nodeWrappers = document.querySelectorAll('.react-flow__node');

          if (!vpEl || nodeWrappers.length === 0) {
            root.__hypergraphVizReady = true;
            return;
          }

          var vpRect = vpEl.getBoundingClientRect();
          var nodeBounds = [];
          nodeWrappers.forEach(function(wrapper) {
            var innerNode = wrapper.querySelector('.group.rounded-lg') || wrapper.firstElementChild;
            if (innerNode) nodeBounds.push(innerNode.getBoundingClientRect());
          });

          if (nodeBounds.length === 0) {
            root.__hypergraphVizReady = true;
            return;
          }

          var topmostEdge = Math.min.apply(null, nodeBounds.map(function(r) { return r.top; }));
          var bottommostEdge = Math.max.apply(null, nodeBounds.map(function(r) { return r.bottom; }));

          // Use ALL visible nodes for horizontal center ("center of mass" approach)
          // This ensures INPUT nodes that extend further left are accounted for
          var leftmostNode = Math.min.apply(null, nodeBounds.map(function(r) { return r.left; }));
          var rightmostNode = Math.max.apply(null, nodeBounds.map(function(r) { return r.right; }));
          var contentCenterX = (leftmostNode + rightmostNode) / 2;
          var viewportCenterX = vpRect.left + vpRect.width / 2;

          var topMarginBefore = Math.round(topmostEdge - vpRect.top);
          var bottomMarginBefore = Math.round(vpRect.bottom - bottommostEdge);
          var diffY = topMarginBefore - bottomMarginBefore;
          var diffX = Math.round(contentCenterX - viewportCenterX);

          var currentVp = getViewport();
          var needsYCorrection = Math.abs(diffY) > 2;
          var needsXCorrection = Math.abs(diffX) > 2;

          var finalY = needsYCorrection ? currentVp.y - diffY / 2 : currentVp.y;
          var finalX = needsXCorrection ? currentVp.x - diffX : currentVp.x;

          // Check left margin constraint first (ensures left content isn't clipped)
          var xShift = finalX - currentVp.x;
          var newLeftmost = leftmostNode + xShift;
          var leftMarginAfterCenter = newLeftmost - vpRect.left;

          if (leftMarginAfterCenter < PADDING_LEFT) {
            // Content would be clipped on left, shift right to preserve minimum margin
            finalX += (PADDING_LEFT - leftMarginAfterCenter);
            xShift = finalX - currentVp.x;  // Recalculate xShift after adjustment
          }

          // Check right margin constraint (ensures buttons aren't overlapped)
          var newRightmost = rightmostNode + xShift;
          var rightMarginAfterCenter = vpRect.right - newRightmost;

          if (rightMarginAfterCenter < PADDING_RIGHT) {
            finalX -= (PADDING_RIGHT - rightMarginAfterCenter);
          }

          var needsAnyCorrection = finalX !== currentVp.x || finalY !== currentVp.y;
          if (needsAnyCorrection) {
            setViewport({ x: finalX, y: finalY, zoom: currentVp.zoom }, { duration: 0 });
          }

          // Signal that graph is fully ready (for tests to wait on)
          // Use one more RAF to ensure the viewport change has been painted
          requestAnimationFrame(function() {
            root.__hypergraphVizReady = true;
          });
        });
      });
    }, [rawLayoutedNodes, layoutedEdges, setViewport, getViewport]);

    // Force edge/handle recalculation after expansion or render-mode changes.
    var prevLayoutRefreshRef = useRef(null);
    var expansionKey = useMemo(function() {
      return Array.from(expansionState.entries())
        .filter(function(entry) { return !entry[1]; })
        .map(function(entry) { return entry[0]; })
        .sort()
        .join(',');
    }, [expansionState]);
    var renderModeKey = useMemo(function() {
      return 'sep:' + (separateOutputs ? '1' : '0') + '|types:' + (showTypes ? '1' : '0');
    }, [separateOutputs, showTypes]);
    var layoutRefreshKey = useMemo(function() {
      return expansionKey + '|' + renderModeKey;
    }, [expansionKey, renderModeKey]);

    useEffect(function() {
      if (prevLayoutRefreshRef.current === null) {
        prevLayoutRefreshRef.current = layoutRefreshKey;
        return;
      }

      if (prevLayoutRefreshRef.current === layoutRefreshKey) return;
      prevLayoutRefreshRef.current = layoutRefreshKey;

      var timer = setTimeout(function() {
        requestAnimationFrame(function() {
          requestAnimationFrame(function() {
            var visibleNodeIds = rawLayoutedNodes
              .filter(function(n) { return !n.hidden; })
              .map(function(n) { return n.id; });

            if (visibleNodeIds.length > 0) {
              visibleNodeIds.forEach(function(id) { updateNodeInternals(id); });
            }
          });
        });
      }, 500);

      return function() { clearTimeout(timer); };
    }, [layoutRefreshKey, rawLayoutedNodes, updateNodeInternals]);

    var layoutedNodes = rawLayoutedNodes;

    // Expose debug layout info
    useEffect(function() {
      var nodeMap = new Map(layoutedNodes.map(function(n) { return [n.id, n]; }));

      var getAbsolutePosition = function(node) {
        var absX = (node.position && node.position.x) || 0;
        var absY = (node.position && node.position.y) || 0;
        var current = node;

        while (current.parentNode) {
          var parent = nodeMap.get(current.parentNode);
          if (!parent) break;
          absX += (parent.position && parent.position.x) || 0;
          absY += (parent.position && parent.position.y) || 0;
          current = parent;
        }
        return { x: absX, y: absY };
      };

      // Build node position map for edge validation
      // Uses actual node dimensions for debug overlay
      var nodePositionMap = {};
      layoutedNodes.forEach(function(n) {
        if (n.hidden) return;
        var absPos = getAbsolutePosition(n);
        var rawHeight = n.style && n.style.height || 68;
        nodePositionMap[n.id] = {
          x: absPos.x,
          y: absPos.y,
          width: n.style && n.style.width || 200,
          height: rawHeight,
          nodeType: n.data && n.data.nodeType,
          isExpanded: n.data && n.data.isExpanded,
          label: (n.data && n.data.label) || n.id,
        };
      });

      // Validate all edges
      var edgeValidation = layoutedEdges.map(function(e) {
        // Use actual routing targets if available (for re-routed edges)
        var actualSrcId = (e.data && e.data.actualSource) || e.source;
        var actualTgtId = (e.data && e.data.actualTarget) || e.target;

        var srcNode = nodePositionMap[actualSrcId];
        var tgtNode = nodePositionMap[actualTgtId];

        if (!srcNode || !tgtNode) {
          return {
            id: e.id,
            source: e.source,
            target: e.target,
            status: 'MISSING',
            issue: !srcNode ? 'Source not visible' : 'Target not visible',
          };
        }

        var srcCenterX = srcNode.x + srcNode.width / 2;
        var tgtCenterX = tgtNode.x + tgtNode.width / 2;
        var srcBottom = getVisibleBottom(srcNode.y, srcNode.height, srcNode.nodeType, srcNode.isExpanded);
        var tgtTop = tgtNode.y;
        var vertDist = tgtTop - srcBottom;
        var horizDist = tgtCenterX - srcCenterX;

        var issues = [];
        if (vertDist < 0) issues.push('Target above source (' + vertDist + 'px)');
        if (Math.abs(horizDist) > 500) issues.push('Large horizontal gap (' + horizDist + 'px)');
        if (vertDist > 300) issues.push('Large vertical gap (' + vertDist + 'px)');

        return {
          id: e.id,
          source: e.source,
          target: e.target,
          sourceLabel: srcNode.label,
          targetLabel: tgtNode.label,
          srcBottom: srcBottom,
          tgtTop: tgtTop,
          vertDist: vertDist,
          horizDist: horizDist,
          status: issues.length > 0 ? 'WARN' : 'OK',
          issue: issues.length > 0 ? issues.join('; ') : null,
          // Include edge data for actualSource/actualTarget access
          data: e.data,
        };
      });

      root.__hypergraphVizLayout = {
        nodes: layoutedNodes.map(function(n) {
          var absPos = getAbsolutePosition(n);
          return {
            id: n.id,
            x: absPos.x,
            y: absPos.y,
            width: n.style && n.style.width,
            height: n.style && n.style.height,
            hidden: n.hidden,
            nodeType: n.data && n.data.nodeType,
            isExpanded: n.data && n.data.isExpanded,
            parentNode: n.parentNode || null,
          };
        }),
        edges: layoutedEdges.map(function(e) {
          return { id: e.id, source: e.source, target: e.target };
        }),
        version: layoutVersion,
      };

      // Build edge ID to source/target lookup from layouted edges
      // Use actualSource/actualTarget if available (for edges routed to internal nodes)
      var edgeLookup = {};
      layoutedEdges.forEach(function(e) {
        // For cross-boundary edges, use actual routing targets (the internal nodes
        // the edge visually connects to, not the container nodes)
        var source = (e.data && e.data.actualSource) || e.source;
        var target = (e.data && e.data.actualTarget) || e.target;

        // Strip expansion suffix from source/target (e.g., "preprocess_exp_preprocess" -> "preprocess")
        source = source.replace(/_exp_.*$/, '');
        target = target.replace(/_exp_.*$/, '');

        // Handle ID suffix from expansion key (e.g., "e_node_a_node_b_exp_")
        var baseId = e.id.replace(/_exp_.*$/, '');
        edgeLookup[baseId] = { source: source, target: target };
        edgeLookup[e.id] = { source: source, target: target };
      });

      // Extract edge path endpoints from rendered SVG for precise validation
      var extractEdgePathEndpoints = function() {
        var edgePaths = [];
        var edgeGroups = document.querySelectorAll('.react-flow__edge');
        edgeGroups.forEach(function(group) {
          var path = group.querySelector('path');
          if (!path) return;

          var d = path.getAttribute('d');
          if (!d) return;

          // Parse all numeric values from the path
          var coords = d.match(/-?[\d.]+/g);
          if (!coords || coords.length < 4) return;

          var floatCoords = coords.map(parseFloat);

          // Get edge ID from data-testid (format: rf__edge-{edgeId})
          var testId = group.getAttribute('data-testid') || '';
          var edgeId = testId.replace('rf__edge-', '');

          // Strip expansion suffix before lookup (suffix is added for React re-rendering)
          var cleanEdgeId = edgeId.replace(/_exp_.*$/, '');

          // Look up source and target from our edge data
          var edgeData = edgeLookup[cleanEdgeId];
          var source = edgeData ? edgeData.source : null;
          var target = edgeData ? edgeData.target : null;

          // Fallback: try parsing from ID if lookup still fails
          if (!source || !target) {

            // Try e_{source}_to_{target} format
            var toMatch = cleanEdgeId.match(/^e_(.+)_to_(.+)$/);
            if (toMatch) {
              source = toMatch[1];
              target = toMatch[2];
            } else {
              // Try e_{source}_{target}_{value} format (underscore-separated)
              var parts = cleanEdgeId.replace(/^e_/, '').split('_');
              if (parts.length >= 2) {
                // Last part might be value, second-to-last is target
                // This is a heuristic - may not always be correct
                source = parts[0];
                target = parts[1];
              }
            }
          }

          // Final cleanup: strip any remaining expansion suffixes
          if (source) source = source.replace(/_exp_.*$/, '');
          if (target) target = target.replace(/_exp_.*$/, '');

          edgePaths.push({
            id: edgeId,
            source: source,
            target: target,
            pathStart: { x: floatCoords[0], y: floatCoords[1] },
            pathEnd: { x: floatCoords[floatCoords.length - 2], y: floatCoords[floatCoords.length - 1] },
            pathD: d,
          });
        });
        return edgePaths;
      };

      // Expose debug data for Python/Playwright extraction
      // Reports VISIBLE bounds (excluding wrapper/shadow offset)
      root.__hypergraphVizDebug = {
        version: layoutVersion,
        timestamp: Date.now(),
        nodes: Object.keys(nodePositionMap).map(function(id) {
          var n = nodePositionMap[id];
          // Default to FUNCTION when nodeType is undefined (matches constraint-layout.js)
          var offset = getNodeTypeOffset(n.nodeType || 'FUNCTION', n.isExpanded);
          var visibleHeight = n.height - offset;
          return {
            id: id,
            label: n.label,
            x: n.x,
            y: n.y,
            width: n.width,
            height: visibleHeight,  // Visible height (excludes wrapper offset)
            bottom: n.y + visibleHeight,  // Visible bottom
            nodeType: n.nodeType,
            // Also expose raw wrapper bounds for debugging
            wrapperHeight: n.height,
            wrapperBottom: n.y + n.height,
            offset: offset,
          };
        }),
        edges: edgeValidation,
        edgePaths: extractEdgePathEndpoints(),
        // Raw layouted edges for debugging edge data (valueName, actualSource, etc.)
        layoutedEdges: layoutedEdges.map(function(e) {
          return {
            id: e.id,
            source: e.source,
            target: e.target,
            data: e.data,
          };
        }),
        summary: {
          totalNodes: Object.keys(nodePositionMap).length,
          totalEdges: edgeValidation.length,
          edgeIssues: edgeValidation.filter(function(e) { return e.status !== 'OK'; }).length,
        },
        // Routing data for debugging edge re-routing
        routingData: {
          output_to_producer: (initialData.meta && initialData.meta.output_to_producer) || {},
          param_to_consumer: (initialData.meta && initialData.meta.param_to_consumer) || {},
          node_to_parent: (initialData.meta && initialData.meta.node_to_parent) || {},
        },
      };

      // Also expose function for on-demand extraction
      root.__hypergraphVizExtractEdgePaths = extractEdgePathEndpoints;
    }, [layoutedNodes, layoutedEdges, layoutVersion]);

    // Iframe resize logic
    useEffect(function() {
      if (graphHeight && graphWidth) {
        var desiredHeight = Math.max(400, graphHeight + 50);
        var desiredWidth = Math.max(400, graphWidth + 150);
        try {
          if (root.frameElement) {
            root.frameElement.style.height = desiredHeight + 'px';
            root.frameElement.style.width = desiredWidth + 'px';
          }
        } catch (e) {}
      }
    }, [graphHeight, graphWidth]);

    // Resize handling
    useEffect(function() {
      var handleResize = function() { fitWithFixedPadding(); };
      root.addEventListener('resize', handleResize);
      return function() { root.removeEventListener('resize', handleResize); };
    }, [fitWithFixedPadding]);

    // Fit view only on INITIAL load, not on every layout change
    // This prevents the "hop" during expand/collapse - user can manually fit if needed
    var hasInitialFitRef = useRef(false);
    useEffect(function() {
      if (layoutedNodes.length > 0) {
        if (!hasInitialFitRef.current) {
          hasInitialFitRef.current = true;
          requestAnimationFrame(function() { fitWithFixedPadding(); });
        } else {
          // Not initial load - still signal ready for tests, but don't re-center
          requestAnimationFrame(function() {
            requestAnimationFrame(function() {
              root.__hypergraphVizReady = true;
            });
          });
        }
      }
    }, [layoutedNodes, fitWithFixedPadding]);

    // Edge options
    var edgeOptions = {
      type: 'custom',
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
      style: { stroke: theme === 'light' ? 'rgba(148, 163, 184, 0.9)' : 'rgba(100, 116, 139, 0.9)', strokeWidth: 1.5 },
      markerEnd: { type: MarkerType.ArrowClosed, color: theme === 'light' ? '#94a3b8' : '#64748b' },
    };

    // Style edges
    var styledEdges = useMemo(function() {
      if (isLayouting) return [];

      return layoutedEdges.map(function(e) {
        var isDataLink = e.data && e.data.isDataLink;
        var isControlEdge = e.data && e.data.edgeType === 'control';
        var edgeStyle = {
          ...edgeOptions.style,
          strokeWidth: isDataLink ? 1.5 : 2,
        };
        if (isControlEdge) {
          edgeStyle.strokeDasharray = '6 4';
        }
        return {
          ...e,
          id: e.id +
            '_exp_' + (expansionKey ? expansionKey.replace(/,/g, '_') : 'none') +
            '_mode_' + renderModeKey,
          ...edgeOptions,
          style: edgeStyle,
          markerEnd: edgeOptions.markerEnd,
          data: e.data,
        };
      });
    }, [layoutedEdges, theme, isLayouting, expansionKey, renderModeKey]);

    // Notify parent of click
    var notifyParentClick = useCallback(function() {
      try {
        root.parent.postMessage({ type: 'hypergraph-viz-click' }, '*');
      } catch (e) {}
    }, []);

    return html`
      <div
        className="w-full relative overflow-hidden transition-colors duration-300"
        style=${{ backgroundColor: bgColor, height: '100vh', width: '100vw' }}
        onClick=${notifyParentClick}
      >

        <${ReactFlow}
          nodes=${layoutedNodes}
          edges=${styledEdges}
          nodeTypes=${nodeTypes}
          edgeTypes=${edgeTypes}
          onNodesChange=${onNodesChange}
          onEdgesChange=${onEdgesChange}
          onNodeClick=${function(e, node) {
            if (node.data && node.data.nodeType === 'PIPELINE' && !node.data.isExpanded && node.data.onToggleExpand) {
              e.stopPropagation();
              node.data.onToggleExpand();
            }
          }}
          minZoom=${0.1}
          maxZoom=${2}
          className="bg-transparent"
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
            onToggleSeparate=${function() { onToggleSeparateOutputs(); }}
            showTypes=${showTypes}
            onToggleTypes=${function() { onToggleShowTypes(); }}
            onFitView=${fitWithFixedPadding}
          />
          ${(showThemeDebug) ? html`
          <${Panel} position="bottom-left" className=${'backdrop-blur-sm rounded-lg shadow-lg border text-xs px-3 py-2 mb-3 ml-3 max-w-xs pointer-events-auto ' +
                (theme === 'light' ? 'bg-white/95 border-slate-200 text-slate-700' : 'bg-slate-900/90 border-slate-700 text-slate-200')}>
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
        <//>
        ${(!isLayouting && (layoutError || (!layoutedNodes.length && rfNodes.length) || (!rfNodes.length))) ? html`
            <div className="absolute inset-0 pointer-events-none flex items-center justify-center">
              <div className="px-4 py-2 rounded-lg border text-xs font-mono bg-slate-900/80 text-amber-200 border-amber-500/40 shadow-lg pointer-events-auto">
                ${layoutError ? ('Layout error: ' + layoutError) : (!rfNodes.length ? 'No graph data' : 'Layout produced no nodes. Showing fallback.')}
                <button className="ml-4 underline text-amber-400 hover:text-amber-100" onClick=${function() { root.location.reload(); }}>Reload</button>
              </div>
            </div>
        ` : null}
      </div>
    `;
  };

  /**
   * Initialize and render the visualization app
   */
  function init() {
    var initialData = JSON.parse(document.getElementById('graph-data').textContent || '{"nodes":[],"edges":[]}');
    var themePreference = normalizeThemePref((initialData.meta && initialData.meta.theme_preference) || 'auto');
    var showThemeDebug = Boolean(initialData.meta && initialData.meta.theme_debug);
    var panOnScroll = Boolean(initialData.meta && initialData.meta.pan_on_scroll);
    var initialSeparateOutputs = Boolean((initialData.meta && initialData.meta.separate_outputs) || false);
    var initialShowTypes = Boolean((initialData.meta && initialData.meta.show_types) !== false);

    var rootEl = document.getElementById('root');
    var fallback = document.getElementById('fallback');

    var reactRoot = ReactDOM.createRoot(rootEl);
    reactRoot.render(html`
      <${ReactFlowProvider}>
        <${App}
          initialData=${initialData}
          themePreference=${themePreference}
          showThemeDebug=${showThemeDebug}
          panOnScroll=${panOnScroll}
          initialSeparateOutputs=${initialSeparateOutputs}
          initialShowTypes=${initialShowTypes}
        />
      <//>
    `);

    if (fallback) fallback.remove();
  }

  // Export API
  return {
    init: init,
    App: App,
  };
});
