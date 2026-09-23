/**
 * Hypergraph visualization app bootstrap.
 */
(function(root) {
  'use strict';

  var HG = root.HypergraphViz = root.HypergraphViz || {};
  var R = root.HypergraphVizRuntime;
  var Layout = root.HypergraphVizLayout;
  var Edges = root.HypergraphVizEdges;
  var Nodes = root.HypergraphVizNodes;
  var Controls = root.HypergraphVizControls;
  var VizDebug = root.HypergraphVizDebug;
  var Ghosts = root.HypergraphVizGhosts;

  if (!R || !Layout || !Edges || !Nodes || !Controls || !VizDebug || !Ghosts) {
    console.error('HypergraphViz: Missing first-party visualization modules');
    return;
  }

  var ReactDOM = R.ReactDOM;
  var useState = R.useState;
  var useEffect = R.useEffect;
  var useMemo = R.useMemo;
  var useCallback = R.useCallback;
  var useRef = R.useRef;
  var ReactFlowComp = R.ReactFlowComp;
  var Background = R.Background;
  var Position = R.Position;
  var MarkerType = R.MarkerType;
  var ReactFlowProvider = R.ReactFlowProvider;
  var useNodesState = R.useNodesState;
  var useEdgesState = R.useEdgesState;
  var useReactFlow = R.useReactFlow;
  var useUpdateNodeInternals = R.useUpdateNodeInternals;
  var html = R.html;
  var EMPTY_ARR = R.EMPTY_ARR;
  var EDGE_ENDPOINT_PADDING = R.EDGE_ENDPOINT_PADDING;
  var LAYOUT_RANKSEP = R.LAYOUT_RANKSEP;
  var detectHostTheme = R.detectHostTheme;
  var normalizeThemePref = R.normalizeThemePref;
  var useLayout = Layout.useLayout;
  var CustomEdge = Edges.CustomEdge;
  var CustomNode = Nodes.CustomNode;
  var CustomControls = Controls.CustomControls;
  var DevLayoutControls = Controls.DevLayoutControls;

  // ╔═══════════════════════════════════════════════════════════╗
  // ║  Section 7: App + Init                                   ║
  // ╚═══════════════════════════════════════════════════════════╝

  var nodeTypes = { custom: CustomNode, pipelineGroup: CustomNode };
  var edgeTypes = { custom: CustomEdge };

  var PIN_PAN_MS = 200;

  // The view as rendered: the transform React Flow wrote on its viewport element.
  function renderedView() {
    var el = document.querySelector('.react-flow__viewport');
    if (!el) return null;
    try {
      var m = new DOMMatrixReadOnly(getComputedStyle(el).transform);
      return { x: m.e, y: m.f, zoom: m.a };
    } catch (e) { return null; }
  }

  // Calls done() one animation frame after the rendered view reaches
  // `target`, while alive() holds. Returns a cancel function.
  function whenViewAt(target, alive, done) {
    var raf = 0;
    var check = function() {
      raf = 0;
      if (!alive()) return;
      var v = renderedView();
      if (v && Math.abs(v.x - target.x) < 0.05 && Math.abs(v.y - target.y) < 0.05 && Math.abs(v.zoom - target.zoom) < 1e-4) {
        raf = requestAnimationFrame(function() { raf = 0; if (alive()) done(); });
        return;
      }
      raf = requestAnimationFrame(check);
    };
    raf = requestAnimationFrame(check);
    return function() { if (raf) cancelAnimationFrame(raf); };
  }

  var FIT_MARGIN = 16;  // screen px kept clear at the canvas top, bottom and left

  // Bounds of everything drawn, in flow coordinates: the node boxes (a child
  // is positioned relative to its container) and the edge route points.
  function contentBounds(nodes, edges) {
    var byId = Object.create(null);
    nodes.forEach(function(n) { byId[n.id] = n; });
    var b = { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity };
    var grow = function(x, y) { b.x0 = Math.min(b.x0, x); b.y0 = Math.min(b.y0, y); b.x1 = Math.max(b.x1, x); b.y1 = Math.max(b.y1, y); };
    nodes.forEach(function(n) {
      if (n.hidden) return;
      var x = 0, y = 0;
      for (var p = n; p; p = p.parentNode ? byId[p.parentNode] : null) {
        x += (p.position && p.position.x) || 0; y += (p.position && p.position.y) || 0;
      }
      grow(x, y);
      grow(x + (n.width || (n.style && n.style.width) || 200), y + (n.height || (n.style && n.style.height) || 50));
    });
    edges.forEach(function(e) {
      ((e.data && e.data.points) || []).forEach(function(pt) { if (pt.x !== undefined && pt.y !== undefined) grow(pt.x, pt.y); });
    });
    return b;
  }

  // The opening view (#598, ruling D58), for a W x H canvas: the whole graph
  // inside the canvas at zoom <= 1, never below MIN_READABLE_ZOOM. A graph
  // that cannot fit opens at that zoom on its first steps (its top), never
  // clipped there. Horizontally it is centred on the canvas; a graph that fits
  // beside the toolbar strip shifts left as far as it takes to clear it.
  function fittedViewport(b, W, H) {
    var left = FIT_MARGIN, right = W - R.TOOLBAR_RESERVE, top = FIT_MARGIN, bottom = H - FIT_MARGIN;
    var cw = Math.max(1, b.x1 - b.x0), ch = Math.max(1, b.y1 - b.y0);
    var z = Math.max(R.MIN_READABLE_ZOOM, Math.min(1, (right - left) / cw, (bottom - top) / ch));
    var w = cw * z, h = ch * z;
    var sx = (W - w) / 2;
    if (w <= right - left) sx = Math.max(left, Math.min(sx, right - w));
    var sy = h <= bottom - top ? top + (bottom - top - h) / 2 : top;
    return { x: sx - b.x0 * z, y: sy - b.y0 * z, zoom: z };
  }

  var App = function(props) {
    var initialData = props.initialData;
    var themePreference = props.themePreference;
    var panOnScroll = props.panOnScroll;

    // Render-count tripwire (PR #88, Stage 5). The pre-IR live widget
    // had a hook-deps bug that triggered ~10,000 App renders per click;
    // tests reset this counter, click an expandable, then assert the
    // delta stays under a small ceiling.
    var renderCountRef = useRef(0);
    renderCountRef.current += 1;
    root.__hypergraphAppRenderCount = renderCountRef.current;

    var sepState = useState(props.initialSeparateOutputs);
    var separateOutputs = sepState[0], setSeparateOutputs = sepState[1];
    var typState = useState(props.initialShowTypes);
    var showTypes = typState[0], setShowTypes = typState[1];
    var inputsState = useState(props.initialShowInputs);
    var showInputs = inputsState[0], setShowInputs = inputsState[1];
    var simplifyState = useState(props.initialSimplify);
    var simplify = simplifyState[0], setSimplify = simplifyState[1];
    var showBoundedInputs = !!props.initialShowBoundedInputs;

    var padState = useState(EDGE_ENDPOINT_PADDING);
    var endpointPadding = padState[0], setEndpointPadding = padState[1];
    var rsState = useState(LAYOUT_RANKSEP);
    var ranksep = rsState[0], setRanksep = rsState[1];

    var onToggleSep = useCallback(function(v) {
      root.__hypergraphVizReady = false;
      setSeparateOutputs(function(p) { return typeof v === 'boolean' ? v : !p; });
    }, []);
    var onToggleTyp = useCallback(function(v) {
      root.__hypergraphVizReady = false;
      setShowTypes(function(p) { return typeof v === 'boolean' ? v : !p; });
    }, []);
    var onToggleInputs = useCallback(function(v) {
      root.__hypergraphVizReady = false;
      setShowInputs(function(p) { return typeof v === 'boolean' ? v : !p; });
    }, []);
    var onToggleSimplify = useCallback(function(v) {
      root.__hypergraphVizReady = false;
      setSimplify(function(p) { return typeof v === 'boolean' ? v : !p; });
    }, []);

    // Render options hook for tests and dev gallery
    useEffect(function() {
      var applyOpts = function(opts) {
        if (!opts) return;
        if (Object.prototype.hasOwnProperty.call(opts, 'separateOutputs')) onToggleSep(!!opts.separateOutputs);
        if (Object.prototype.hasOwnProperty.call(opts, 'showTypes')) onToggleTyp(!!opts.showTypes);
        if (Object.prototype.hasOwnProperty.call(opts, 'showInputs')) onToggleInputs(!!opts.showInputs);
        if (Object.prototype.hasOwnProperty.call(opts, 'simplify')) onToggleSimplify(!!opts.simplify);
        if (Object.prototype.hasOwnProperty.call(opts, 'endpointPadding')) {
          root.__hypergraphVizReady = false;
          setEndpointPadding(Number(opts.endpointPadding));
        }
        if (Object.prototype.hasOwnProperty.call(opts, 'ranksep')) {
          root.__hypergraphVizReady = false;
          setRanksep(Number(opts.ranksep));
        }
      };
      root.__hypergraphVizSetRenderOptions = applyOpts;

      // Listen for postMessage from parent (gallery page) — works cross-origin
      var onMessage = function(event) {
        if (event.data && event.data.type === 'hypergraph-set-options') {
          applyOpts(event.data.options);
        }
      };
      root.addEventListener('message', onMessage);

      return function() {
        delete root.__hypergraphVizSetRenderOptions;
        root.removeEventListener('message', onMessage);
      };
    }, [onToggleSep, onToggleTyp, onToggleInputs, onToggleSimplify, setEndpointPadding, setRanksep]);

    var detState = useState(function() { return detectHostTheme(); });
    var detectedTheme = detState[0], setDetectedTheme = detState[1];
    var manState = useState(null);
    var manualTheme = manState[0], setManualTheme = manState[1];
    var expState = useState(function() {
      var map = new Map();
      // IR mode: seed from meta.initial_expansion (Python computed it from depth=N).
      var initial = initialData.meta && initialData.meta.initial_expansion;
      if (initial) {
        Object.keys(initial).forEach(function(k) { map.set(k, !!initial[k]); });
      }
      // Legacy mode: seed from PIPELINE node isExpanded flags.
      initialData.nodes.forEach(function(n) {
        if (n.data && n.data.nodeType === 'PIPELINE') map.set(n.id, n.data.isExpanded || false);
      });
      return map;
    });
    var expansionState = expState[0], setExpansionState = expState[1];

    // Pure-graph facts shipped in meta.ir; scene_builder re-derives the
    // visible nodes/edges client-side on every state change. The legacy
    // edgesByState/nodesByState 2^N precompute is gone (PR #88, stage 1).
    var ir = (initialData.meta && initialData.meta.ir) || null;

    var nsState = useNodesState([]);
    var rfNodes = nsState[0], setNodes = nsState[1], onNodesChange = nsState[2];
    var esState = useEdgesState([]);
    var rfEdges = esState[0], setEdges = esState[1], onEdgesChange = esState[2];
    var nodesRef = useRef(initialData.nodes);

    var resolved = detectedTheme || { theme: themePreference === 'auto' ? 'dark' : themePreference, background: 'transparent', luminance: null, source: 'init' };
    var activeTheme = useMemo(function() { return manualTheme || (themePreference === 'auto' ? (resolved.theme || 'dark') : themePreference); }, [manualTheme, resolved.theme, themePreference]);
    var activeBg = useMemo(function() {
      var themeCanvasBg = activeTheme === 'light' ? '#f8fafc' : '#020617';
      // Explicit visualize(theme='light'|'dark') should always force canvas background.
      if (manualTheme || themePreference === 'light' || themePreference === 'dark') return themeCanvasBg;

      var bg = resolved.background;
      var transparent = !bg || bg === 'transparent' || bg === 'rgba(0, 0, 0, 0)';
      if (transparent) return themeCanvasBg;

      // Auto mode safety: if detected background contradicts active theme,
      // prefer the theme canvas color to avoid dark-controls-on-white-canvas.
      var lum = typeof resolved.luminance === 'number' ? resolved.luminance : null;
      if (lum !== null) {
        var bgLooksLight = lum > 150;
        if ((activeTheme === 'dark' && bgLooksLight) || (activeTheme === 'light' && !bgLooksLight)) return themeCanvasBg;
      }
      return bg;
    }, [manualTheme, themePreference, resolved.background, resolved.luminance, activeTheme]);

    var theme = activeTheme;

    // Expansion toggle
    var onToggleExpand = useCallback(function(nodeId) {
      root.__hypergraphVizReady = false;
      setExpansionState(function(prev) {
        var m = new Map(prev);
        var will = !(m.get(nodeId) || false);
        m.set(nodeId, will);
        if (!will) {
          var curNodes = nodesRef.current || [];
          var childMap = new Map();
          curNodes.forEach(function(n) { if (n.parentNode) { if (!childMap.has(n.parentNode)) childMap.set(n.parentNode, []); childMap.get(n.parentNode).push(n.id); } });
          var getDesc = function(id) { var ch = childMap.get(id) || []; var r = ch.slice(); ch.forEach(function(c) { r = r.concat(getDesc(c)); }); return r; };
          getDesc(nodeId).forEach(function(d) { if (m.has(d)) m.set(d, false); });
        }
        root.__hypergraphVizExpansionState = m;
        return m;
      });
    }, []);

    // Build the scene once per (state, options, ir) tuple; nodes/edges
    // are projected from the same memoized result so we don't double the
    // derivation work and so schemaVersionMismatch is observed exactly once.
    var sceneOpts = useMemo(function() {
      var stateObj = {};
      expansionState.forEach(function(v, k) { stateObj[k] = v; });
      return {
        expansionState: stateObj,
        separateOutputs: separateOutputs,
        showInputs: showInputs,
        showBoundedInputs: showBoundedInputs,
        simplify: simplify,
      };
    }, [expansionState, separateOutputs, showInputs, showBoundedInputs, simplify]);
    var scene = useMemo(function() {
      if (!ir || !root.HypergraphSceneBuilder) return null;
      return root.HypergraphSceneBuilder.buildInitialScene(ir, sceneOpts);
    }, [sceneOpts, ir]);

    var schemaMismatch = scene && scene.schemaVersionMismatch ? scene.schemaVersionMismatch : null;

    // Select scene nodes for the current state via scene_builder.
    var selectedNodes = useMemo(function() {
      if (!scene) {
        return (initialData.nodes || EMPTY_ARR).map(function(n) {
          return { ...n, data: { ...n.data, theme: activeTheme, showTypes: showTypes, separateOutputs: separateOutputs } };
        });
      }
      return scene.nodes.map(function(n) { return { ...n, data: { ...n.data, theme: activeTheme, showTypes: showTypes, separateOutputs: separateOutputs } }; });
    }, [scene, activeTheme, showTypes, separateOutputs, initialData.nodes]);

    var nodesWithCb = useMemo(function() {
      return selectedNodes.map(function(n) {
        return { ...n, data: { ...n.data, onToggleExpand: (n.data && n.data.nodeType === 'PIPELINE') ? function() { onToggleExpand(n.id); } : n.data.onToggleExpand } };
      });
    }, [selectedNodes, onToggleExpand]);

    // ── Ghost inputs + focus (#595) ──
    // Only while inputs are hidden: hovering a step, or tapping it on a touch
    // screen, draws its inputs as ghost pills and lights its path. A touch
    // never goes through hover (iOS turns the first tap into a hover), so a
    // tap pins directly. A click or tap pins; an empty-canvas click, Escape,
    // any re-layout and the Show/Hide Inputs toggle clear.
    var ghostsOn = !showInputs && !!scene && !scene.schemaVersionMismatch;
    var ghostsOnRef = useRef(ghostsOn);
    ghostsOnRef.current = ghostsOn;
    var ghostSets = useMemo(function() {
      return ghostsOn ? Ghosts.ghostSets(ir, scene, sceneOpts) : null;
    }, [ghostsOn, ir, scene, sceneOpts]);
    var ghostState = useState(null);  // { id, pinned }
    var setGhostActive = ghostState[1];
    var ghostActive = ghostsOn ? ghostState[0] : null;
    var lastPointerRef = useRef('mouse');
    useEffect(function() {
      var onPointer = function(e) { if (e.pointerType) lastPointerRef.current = e.pointerType; };
      var onKey = function(e) { if (e.key === 'Escape') setGhostActive(null); };
      root.addEventListener('pointerdown', onPointer, true);
      root.addEventListener('pointermove', onPointer, true);
      root.addEventListener('keydown', onKey);
      return function() {
        root.removeEventListener('pointerdown', onPointer, true);
        root.removeEventListener('pointermove', onPointer, true);
        root.removeEventListener('keydown', onKey);
      };
    }, []);
    useEffect(function() { setGhostActive(null); }, [showInputs]);
    var onGhostEnter = useCallback(function(e, n) {
      if (!ghostsOnRef.current || lastPointerRef.current === 'touch' || !Ghosts.isStep(n)) return;
      setGhostActive(function(p) { return p && p.pinned ? p : { id: n.id, pinned: false }; });
    }, []);
    var onGhostLeave = useCallback(function(e, n) {
      if (lastPointerRef.current === 'touch') return;
      setGhostActive(function(p) { return p && !p.pinned && p.id === n.id ? null : p; });
    }, []);
    var onGhostClick = useCallback(function(n) {
      if (!ghostsOnRef.current || !Ghosts.isStep(n)) return;
      var touch = lastPointerRef.current === 'touch';
      // A second mouse click on the pinned step unpins it (it stays lit while hovered).
      setGhostActive(function(p) {
        return (!touch && p && p.pinned && p.id === n.id) ? { id: n.id, pinned: false } : { id: n.id, pinned: true };
      });
    }, []);
    var onPaneClick = useCallback(function() { setGhostActive(null); }, []);

    // Theme detection listener
    useEffect(function() {
      var apply = function() { setDetectedTheme(detectHostTheme()); };
      apply();
      var observers = [];
      try {
        var pd = root.parent && root.parent.document;
        if (pd) {
          var o = new MutationObserver(apply);
          o.observe(pd.body, { attributes: true, attributeFilter: ['class', 'data-vscode-theme-kind', 'style'] });
          o.observe(pd.documentElement, { attributes: true, attributeFilter: ['class', 'data-vscode-theme-kind', 'style'] });
          observers.push(o);
        }
      } catch (e) {}
      var mq = root.matchMedia ? root.matchMedia('(prefers-color-scheme: dark)') : null;
      var mqH = function() { apply(); };
      if (mq && mq.addEventListener) mq.addEventListener('change', mqH);
      return function() { observers.forEach(function(o) { o.disconnect(); }); if (mq && mq.removeEventListener) mq.removeEventListener('change', mqH); };
    }, []);

    // Apply theme
    useEffect(function() {
      document.body.classList.toggle('light-mode', activeTheme === 'light');
      document.body.style.backgroundColor = activeBg;

      // Some hosts/ReactFlow layers keep a white canvas background unless
      // explicitly painted. Force all canvas layers to the selected bg.
      var selectors = ['#root', '.react-flow', '.react-flow__renderer', '.react-flow__pane'];
      selectors.forEach(function(sel) {
        document.querySelectorAll(sel).forEach(function(el) { el.style.backgroundColor = activeBg; });
      });
    }, [activeTheme, activeBg]);

    var toggleTheme = useCallback(function() {
      if (manualTheme === null) setManualTheme(activeTheme === 'dark' ? 'light' : 'dark');
      else setManualTheme(null);
    }, [manualTheme, activeTheme]);

    // Select scene edges for the current state via scene_builder.
    var selectedEdges = useMemo(function() {
      if (!scene) return (initialData.edges || EMPTY_ARR);
      return scene.edges;
    }, [scene, initialData.edges]);

    useEffect(function() {
      nodesRef.current = nodesWithCb;
      setNodes(nodesWithCb); setEdges(selectedEdges);
    }, [nodesWithCb, selectedEdges, setNodes, setEdges]);

    var routingData = useMemo(function() {
      return {
        output_to_producer: (initialData.meta && initialData.meta.output_to_producer) || {},
        param_to_consumer: (initialData.meta && initialData.meta.param_to_consumer) || {},
        node_to_parent: (initialData.meta && initialData.meta.node_to_parent) || {},
      };
    }, [initialData]);

    var layoutResult = useLayout(nodesWithCb, selectedEdges, expansionState, endpointPadding, ranksep);
    var layoutedNodes = layoutResult.layoutedNodes;
    var layoutedEdges = layoutResult.layoutedEdges;
    var layoutError = layoutResult.layoutError;
    var layoutVersion = layoutResult.layoutVersion;
    var isLayouting = layoutResult.isLayouting;

    var rf = useReactFlow();
    var updateNI = useUpdateNodeInternals();

    // ── Opening view (#598, ruling D58) ──
    // The view opens fitted (see fittedViewport). A toolbar toggle that
    // re-lays out the graph, and a resize, fit again, unless the user has
    // panned or zoomed since the last fit: a drag, wheel or pinch (React
    // Flow's onMoveEnd, which fires only for user gestures) or a zoom button.
    // Fit View always fits and re-arms. A fit and a pinned step's pan are
    // programmatic, so neither counts. The latest programmatic move owns the
    // view until a user gesture takes it over; __hypergraphVizReady turns true
    // once a fit is on screen.
    var userMovedRef = useRef(false);
    var userGestureRef = useRef(0);
    var viewMoveRef = useRef(0);
    var fitSeqRef = useRef(0);
    var onUserMoveStart = useCallback(function() { userGestureRef.current += 1; }, []);
    var onUserMoveEnd = useCallback(function(event) { if (event) userMovedRef.current = true; }, []);
    var onUserZoom = useCallback(function() { userMovedRef.current = true; userGestureRef.current += 1; }, []);
    var fitView = useCallback(function() {
      var pane = document.querySelector('.react-flow');
      if (!layoutedNodes.length || !pane) return;
      root.__hypergraphVizReady = false;
      userMovedRef.current = false;
      var fit = ++fitSeqRef.current, move = ++viewMoveRef.current, gesture = userGestureRef.current;
      var rect = pane.getBoundingClientRect();
      var target = fittedViewport(contentBounds(layoutedNodes, layoutedEdges), rect.width, rect.height);
      rf.setViewport(target, { duration: 0 });
      whenViewAt(target, function() {
        if (fit !== fitSeqRef.current) return false;  // a newer fit reports instead
        if (move !== viewMoveRef.current || gesture !== userGestureRef.current) { root.__hypergraphVizReady = true; return false; }
        return true;
      }, function() { root.__hypergraphVizReady = true; });
    }, [layoutedNodes, layoutedEdges, rf]);

    // Force handle recalculation on expansion/mode changes
    var expansionKey = useMemo(function() {
      return Array.from(expansionState.entries()).filter(function(e) { return !e[1]; }).map(function(e) { return e[0]; }).sort().join(',');
    }, [expansionState]);
    var renderModeKey = useMemo(function() {
      return 'sep:' + (separateOutputs ? '1' : '0') + '|types:' + (showTypes ? '1' : '0') + '|inputs:' + (showInputs ? '1' : '0') + '|simp:' + (simplify ? '1' : '0');
    }, [separateOutputs, showTypes, showInputs, simplify]);
    var refreshKey = useMemo(function() { return expansionKey + '|' + renderModeKey; }, [expansionKey, renderModeKey]);
    var prevRefresh = useRef(null);
    useEffect(function() {
      if (prevRefresh.current === null) { prevRefresh.current = refreshKey; return; }
      if (prevRefresh.current === refreshKey) return;
      prevRefresh.current = refreshKey;
      var t = setTimeout(function() { requestAnimationFrame(function() { requestAnimationFrame(function() {
        var ids = layoutedNodes.filter(function(n) { return !n.hidden; }).map(function(n) { return n.id; });
        if (ids.length) ids.forEach(function(id) { updateNI(id); });
      }); }); }, 500);
      return function() { clearTimeout(t); };
    }, [refreshKey, layoutedNodes, updateNI]);

    // Debug API
    useEffect(function() {
      VizDebug.installDebugApi({
        layoutedNodes: layoutedNodes,
        layoutedEdges: layoutedEdges,
        layoutVersion: layoutVersion,
        routingData: routingData,
      });
    }, [layoutedNodes, layoutedEdges, layoutVersion, routingData]);

    // Iframe resize
    useEffect(function() {
      if (layoutResult.graphHeight && layoutResult.graphWidth) {
        try { if (root.frameElement) { root.frameElement.style.height = Math.max(400, layoutResult.graphHeight + 50) + 'px'; root.frameElement.style.width = Math.max(400, layoutResult.graphWidth + 150) + 'px'; } } catch (e) {}
      }
    }, [layoutResult.graphHeight, layoutResult.graphWidth]);

    // Resize handler
    useEffect(function() {
      var h = function() { if (!userMovedRef.current) fitView(); };
      root.addEventListener('resize', h);
      return function() { root.removeEventListener('resize', h); };
    }, [fitView]);

    // The first layout opens fitted; a layout from a toolbar toggle (a new
    // renderModeKey) fits again unless the user has moved the view.
    // Expanding or collapsing a container keeps the view, as before.
    var fittedModeRef = useRef(null);
    useEffect(function() {
      if (!layoutedNodes.length) return;
      var first = fittedModeRef.current === null;
      var toggled = !first && fittedModeRef.current !== renderModeKey;
      fittedModeRef.current = renderModeKey;
      if (first || (toggled && !userMovedRef.current)) requestAnimationFrame(fitView);
      else requestAnimationFrame(function() { requestAnimationFrame(function() { root.__hypergraphVizReady = true; }); });
    }, [layoutedNodes, fitView]);

    // Any re-layout (expand/collapse, types, inputs, output mode) clears the
    // ghosts: their placement was measured against the previous layout.
    useEffect(function() { setGhostActive(null); }, [layoutVersion]);

    var ghostNode = useMemo(function() {
      if (!ghostActive) return null;
      return layoutedNodes.find(function(n) { return n.id === ghostActive.id && !n.hidden; }) || null;
    }, [ghostActive, layoutedNodes]);
    var ghostFocus = useMemo(function() {
      return ghostNode ? Ghosts.focusSets(ghostNode.id, layoutedEdges, layoutedNodes) : null;
    }, [ghostNode, layoutedEdges, layoutedNodes]);
    var displayNodes = useMemo(function() {
      if (!ghostFocus) return layoutedNodes;
      return layoutedNodes.map(function(n) {
        var cls = n.id === ghostFocus.id ? 'hg-focus' : (ghostFocus.nodes[n.id] ? '' : 'hg-dim');
        return cls ? { ...n, className: [n.className, cls].filter(Boolean).join(' ') } : n;
      });
    }, [layoutedNodes, ghostFocus]);

    // Placement is measured once per focused step, after the dimming commit,
    // from the rendered node and edge-label boxes (see viz_ghosts.js).
    var planState = useState(null);
    var ghostPlan = planState[0], setGhostPlan = planState[1];
    var ghostNodeId = ghostNode ? ghostNode.id : null;
    useEffect(function() {
      if (!ghostNodeId || !ghostSets) { setGhostPlan(null); return; }
      var parentOf = {};
      layoutedNodes.forEach(function(n) { if (n.parentNode) parentOf[n.id] = n.parentNode; });
      var ancestors = {};
      for (var a = parentOf[ghostNodeId]; a; a = parentOf[a]) ancestors[a] = true;
      var nodeType = ghostNode && ghostNode.data ? ghostNode.data.nodeType : null;
      setGhostPlan(Ghosts.planGhosts({
        stepId: ghostNodeId, nodeType: nodeType, ancestors: ancestors,
        items: ghostSets[ghostNodeId] || EMPTY_ARR, showTypes: showTypes, transform: rf.getViewport(),
      }));
    }, [ghostNodeId, ghostSets, showTypes, layoutedNodes, rf]);
    var activePlan = ghostPlan && ghostNodeId && ghostPlan.stepId === ghostNodeId ? ghostPlan : null;

    // A pinned step pans (and zooms out if it must) so it and its ghosts are
    // on screen. Never on plain hover: moving the view under the mouse flickers.
    // The pan glides, so its end is observable only on screen: once the
    // rendered view reaches the target, window.__hypergraphVizPinFramed counts
    // up. A tap's click can reach the page late, so a fixed wait after the tap
    // may read the view mid-glide; tests wait on the count instead. A user
    // gesture or a newer fit during the glide takes the view over, and the
    // count stays put.
    var ghostPinned = !!(ghostActive && ghostActive.pinned);
    useEffect(function() {
      if (!ghostPinned || !activePlan) return;
      var pane = document.querySelector('.react-flow');
      if (!pane) return;
      var next = Ghosts.frameViewport(activePlan, rf.getViewport(), pane.getBoundingClientRect());
      if (next) rf.setViewport(next, { duration: PIN_PAN_MS });
      var move = ++viewMoveRef.current, gesture = userGestureRef.current;
      return whenViewAt(next || rf.getViewport(), function() { return viewMoveRef.current === move && userGestureRef.current === gesture; }, function() {
        root.__hypergraphVizPinFramed = (root.__hypergraphVizPinFramed || 0) + 1;
      });
    }, [ghostPinned, activePlan, rf]);

    // Edge styling
    var edgeOpts = {
      type: 'custom', sourcePosition: Position.Bottom, targetPosition: Position.Top,
      style: { stroke: theme === 'light' ? 'rgba(148,163,184,0.9)' : 'rgba(100,116,139,0.9)', strokeWidth: 1.5 },
      markerEnd: { type: MarkerType.ArrowClosed, color: theme === 'light' ? '#94a3b8' : '#64748b' },
    };

    var styledEdges = useMemo(function() {
      if (isLayouting) return [];
      return layoutedEdges.map(function(e) {
        var edgeType = e.data && e.data.edgeType;
        var isControl = edgeType === 'control';
        var isOrdering = edgeType === 'ordering';
        var isExclusive = !!(e.data && e.data.exclusive);
        var st = { ...edgeOpts.style, strokeWidth: (e.data && e.data.isDataLink) ? 1.5 : 2 };
        if (isControl) st.strokeDasharray = '6 4';
        if (isOrdering) { st.stroke = '#8b5cf6'; st.strokeWidth = 1.5; st.strokeDasharray = '6 3'; }
        if (isExclusive && !isControl && !isOrdering) st.strokeDasharray = '4 4';
        var data = ghostFocus ? { ...e.data, dimmed: !ghostFocus.edges[e.id] } : e.data;
        return { ...e, id: e.id + '_exp_' + (expansionKey ? expansionKey.replace(/,/g, '_') : 'none') + '_mode_' + renderModeKey,
          ...edgeOpts, style: st, markerEnd: edgeOpts.markerEnd, data: data };
      });
    }, [layoutedEdges, theme, isLayouting, expansionKey, renderModeKey, ghostFocus]);

    return html`
      <div className="w-full relative overflow-hidden transition-colors duration-300"
           style=${{ backgroundColor: activeBg, height: '100vh', width: '100vw' }}
           onClick=${function() { try { root.parent.postMessage({ type: 'hypergraph-viz-click' }, '*'); } catch(e) {} }}>
        <${ReactFlowComp}
          nodes=${displayNodes} edges=${styledEdges} nodeTypes=${nodeTypes} edgeTypes=${edgeTypes}
          onNodesChange=${onNodesChange} onEdgesChange=${onEdgesChange}
          onNodeMouseEnter=${onGhostEnter} onNodeMouseLeave=${onGhostLeave} onPaneClick=${onPaneClick}
          onMoveStart=${onUserMoveStart} onMoveEnd=${onUserMoveEnd}
          onNodeClick=${function(e, n) { if (n.data && n.data.nodeType === 'PIPELINE' && !n.data.isExpanded && n.data.onToggleExpand) { e.stopPropagation(); n.data.onToggleExpand(); return; } onGhostClick(n); }}
          minZoom=${0.1} maxZoom=${2} className="bg-transparent" panOnScroll=${panOnScroll}
          zoomOnScroll=${false} panOnDrag=${true} zoomOnPinch=${true} preventScrolling=${false}
          style=${{ width: '100%', height: '100%', backgroundColor: activeBg }}>
          <${Background} color=${theme === 'light' ? '#94a3b8' : '#334155'} gap=${24} size=${1} variant="dots" />
          <${CustomControls} theme=${theme} onToggleTheme=${toggleTheme} separateOutputs=${separateOutputs}
            onToggleSeparate=${function() { onToggleSep(); }} showTypes=${showTypes}
            onToggleTypes=${function() { onToggleTyp(); }} showInputs=${showInputs}
            onToggleInputs=${function() { onToggleInputs(); }} simplify=${simplify}
            onToggleSimplify=${function() { onToggleSimplify(); }} onFitView=${fitView} onUserZoom=${onUserZoom} />
          ${root.__hypergraph_debug_viz ? html`
            <${DevLayoutControls} theme=${theme} endpointPadding=${endpointPadding} ranksep=${ranksep}
              onChangePadding=${function(v) { root.__hypergraphVizReady = false; setEndpointPadding(v); }}
              onChangeRanksep=${function(v) { root.__hypergraphVizReady = false; setRanksep(v); }} />
          ` : null}
        <//>
        <style>${Ghosts.GHOST_CSS}</style>
        ${activePlan ? html`<${Ghosts.GhostLayer} plan=${activePlan} isLight=${theme === 'light'} />` : null}
        ${schemaMismatch ? html`
          <div data-testid="hypergraph-schema-banner"
               className="absolute top-2 left-1/2 -translate-x-1/2 px-3 py-1.5 rounded-md text-xs font-mono bg-slate-900/85 text-amber-200 border border-amber-500/40 shadow pointer-events-auto z-50">
            Visualization needs an updated runtime — showing static view.
            <span className="ml-2 text-amber-400/80">(IR v${schemaMismatch.got || '?'}, runtime v${schemaMismatch.supported})</span>
          </div>
        ` : null}
        ${(!isLayouting && (layoutError || !layoutedNodes.length)) ? html`
          <div className="absolute inset-0 pointer-events-none flex items-center justify-center">
            <div className="px-4 py-2 rounded-lg border text-xs font-mono bg-slate-900/80 text-amber-200 border-amber-500/40 shadow-lg pointer-events-auto">
              ${layoutError ? 'Layout error: ' + layoutError : 'No graph data'}
              <button className="ml-4 underline text-amber-400 hover:text-amber-100" onClick=${function() { root.location.reload(); }}>Reload</button>
            </div>
          </div>
        ` : null}
      </div>`;
  };

  function init() {
    var graphDataEl = document.getElementById('graph-data');
    var initialData = JSON.parse((graphDataEl && graphDataEl.textContent) || '{"nodes":[],"edges":[]}');
    var themePreference = normalizeThemePref((initialData.meta && initialData.meta.theme_preference) || 'auto');
    var rootEl = document.getElementById('root');
    var bootMessage = document.getElementById('boot-message');
    ReactDOM.createRoot(rootEl).render(html`
      <${ReactFlowProvider}>
        <${App} initialData=${initialData} themePreference=${themePreference}
          panOnScroll=${Boolean(initialData.meta && initialData.meta.pan_on_scroll)}
          initialSeparateOutputs=${Boolean(initialData.meta && initialData.meta.separate_outputs)}
          initialShowTypes=${Boolean((initialData.meta && initialData.meta.show_types) !== false)}
          initialShowInputs=${Boolean(initialData.meta && initialData.meta.show_inputs)}
          initialSimplify=${Boolean((initialData.meta && initialData.meta.simplify) !== false)}
          initialShowBoundedInputs=${Boolean((initialData.meta && initialData.meta.show_bounded_inputs) !== false)} />
      <//>
    `);
    if (bootMessage) bootMessage.remove();
  }

  HG.init = init;
})(typeof window !== 'undefined' ? window : this);
