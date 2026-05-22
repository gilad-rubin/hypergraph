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

  if (!R || !Layout || !Edges || !Nodes || !Controls || !VizDebug) {
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

    // Render options hook for tests and dev gallery
    useEffect(function() {
      var applyOpts = function(opts) {
        if (!opts) return;
        if (Object.prototype.hasOwnProperty.call(opts, 'separateOutputs')) onToggleSep(!!opts.separateOutputs);
        if (Object.prototype.hasOwnProperty.call(opts, 'showTypes')) onToggleTyp(!!opts.showTypes);
        if (Object.prototype.hasOwnProperty.call(opts, 'showInputs')) onToggleInputs(!!opts.showInputs);
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
    }, [onToggleSep, onToggleTyp, onToggleInputs, setEndpointPadding, setRanksep]);

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
    var scene = useMemo(function() {
      if (!ir || !root.HypergraphSceneBuilder) return null;
      var stateObj = {};
      expansionState.forEach(function(v, k) { stateObj[k] = v; });
      return root.HypergraphSceneBuilder.buildInitialScene(ir, {
        expansionState: stateObj,
        separateOutputs: separateOutputs,
        showInputs: showInputs,
        showBoundedInputs: showBoundedInputs,
      });
    }, [expansionState, separateOutputs, showInputs, showBoundedInputs, ir]);

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

    // Viewport centering
    var fitWithFixedPadding = useCallback(function() {
      if (!layoutedNodes.length) return;
      root.__hypergraphVizReady = false;

      var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      layoutedNodes.forEach(function(n) {
        var x = (n.position && n.position.x) || 0, y = (n.position && n.position.y) || 0;
        var w = n.width || (n.style && n.style.width) || 200, h = n.height || (n.style && n.style.height) || 50;
        minX = Math.min(minX, x); minY = Math.min(minY, y);
        maxX = Math.max(maxX, x + w); maxY = Math.max(maxY, y + h);
      });
      layoutedEdges.forEach(function(e) {
        ((e.data && e.data.points) || []).forEach(function(pt) {
          if (pt.x !== undefined) { minX = Math.min(minX, pt.x); maxX = Math.max(maxX, pt.x); }
          if (pt.y !== undefined) { minY = Math.min(minY, pt.y); maxY = Math.max(maxY, pt.y); }
        });
      });

      var vpEl = document.querySelector('.react-flow__viewport');
      vpEl = vpEl && vpEl.parentElement;
      var vpW = (vpEl && vpEl.clientWidth) || 800, vpH = (vpEl && vpEl.clientHeight) || 600;
      var contentCY = (minY + maxY) / 2, contentCX = (minX + maxX) / 2;
      var newY = Math.max(16 - minY, vpH / 2 - contentCY);
      var newX = Math.max(20 - minX, vpW / 2 - contentCX);

      rf.setViewport({ x: newX, y: newY, zoom: 1 }, { duration: 0 });

      requestAnimationFrame(function() { requestAnimationFrame(function() {
        var vp = document.querySelector('.react-flow__viewport');
        vp = vp && vp.parentElement;
        var nw = document.querySelectorAll('.react-flow__node');
        if (!vp || !nw.length) { root.__hypergraphVizReady = true; return; }
        var vpR = vp.getBoundingClientRect();
        var bounds = [];
        nw.forEach(function(w) { var inner = w.querySelector('.group.rounded-lg') || w.firstElementChild; if (inner) bounds.push(inner.getBoundingClientRect()); });
        if (!bounds.length) { root.__hypergraphVizReady = true; return; }

        var left = Math.min.apply(null, bounds.map(function(r) { return r.left; }));
        var right = Math.max.apply(null, bounds.map(function(r) { return r.right; }));
        var top = Math.min.apply(null, bounds.map(function(r) { return r.top; }));
        var bottom = Math.max.apply(null, bounds.map(function(r) { return r.bottom; }));
        var cx = (left + right) / 2, vx = vpR.left + vpR.width / 2;
        var diffY = (top - vpR.top) - (vpR.bottom - bottom);
        var diffX = Math.round(cx - vx);
        var cur = rf.getViewport();
        var finalX = Math.abs(diffX) > 2 ? cur.x - diffX : cur.x;
        var finalY = Math.abs(diffY) > 2 ? cur.y - diffY / 2 : cur.y;
        // Margin constraints
        var xShift = finalX - cur.x;
        var newL = left + xShift;
        if (newL - vpR.left < 20) finalX += (20 - (newL - vpR.left));
        var newR = right + (finalX - cur.x);
        if (vpR.right - newR < 100) finalX -= (100 - (vpR.right - newR));
        if (finalX !== cur.x || finalY !== cur.y) rf.setViewport({ x: finalX, y: finalY, zoom: cur.zoom }, { duration: 0 });
        requestAnimationFrame(function() { root.__hypergraphVizReady = true; });
      }); });
    }, [layoutedNodes, layoutedEdges, rf]);

    // Force handle recalculation on expansion/mode changes
    var expansionKey = useMemo(function() {
      return Array.from(expansionState.entries()).filter(function(e) { return !e[1]; }).map(function(e) { return e[0]; }).sort().join(',');
    }, [expansionState]);
    var renderModeKey = useMemo(function() {
      return 'sep:' + (separateOutputs ? '1' : '0') + '|types:' + (showTypes ? '1' : '0') + '|inputs:' + (showInputs ? '1' : '0');
    }, [separateOutputs, showTypes, showInputs]);
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
      var h = function() { fitWithFixedPadding(); };
      root.addEventListener('resize', h);
      return function() { root.removeEventListener('resize', h); };
    }, [fitWithFixedPadding]);

    // Initial fit only
    var hasInitFit = useRef(false);
    useEffect(function() {
      if (layoutedNodes.length > 0) {
        if (!hasInitFit.current) { hasInitFit.current = true; requestAnimationFrame(function() { fitWithFixedPadding(); }); }
        else { requestAnimationFrame(function() { requestAnimationFrame(function() { root.__hypergraphVizReady = true; }); }); }
      }
    }, [layoutedNodes, fitWithFixedPadding]);

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
        return { ...e, id: e.id + '_exp_' + (expansionKey ? expansionKey.replace(/,/g, '_') : 'none') + '_mode_' + renderModeKey,
          ...edgeOpts, style: st, markerEnd: edgeOpts.markerEnd, data: e.data };
      });
    }, [layoutedEdges, theme, isLayouting, expansionKey, renderModeKey]);

    return html`
      <div className="w-full relative overflow-hidden transition-colors duration-300"
           style=${{ backgroundColor: activeBg, height: '100vh', width: '100vw' }}
           onClick=${function() { try { root.parent.postMessage({ type: 'hypergraph-viz-click' }, '*'); } catch(e) {} }}>
        <${ReactFlowComp}
          nodes=${layoutedNodes} edges=${styledEdges} nodeTypes=${nodeTypes} edgeTypes=${edgeTypes}
          onNodesChange=${onNodesChange} onEdgesChange=${onEdgesChange}
          onNodeClick=${function(e, n) { if (n.data && n.data.nodeType === 'PIPELINE' && !n.data.isExpanded && n.data.onToggleExpand) { e.stopPropagation(); n.data.onToggleExpand(); } }}
          minZoom=${0.1} maxZoom=${2} className="bg-transparent" panOnScroll=${panOnScroll}
          zoomOnScroll=${false} panOnDrag=${true} zoomOnPinch=${true} preventScrolling=${false}
          style=${{ width: '100%', height: '100%', backgroundColor: activeBg }}>
          <${Background} color=${theme === 'light' ? '#94a3b8' : '#334155'} gap=${24} size=${1} variant="dots" />
          <${CustomControls} theme=${theme} onToggleTheme=${toggleTheme} separateOutputs=${separateOutputs}
            onToggleSeparate=${function() { onToggleSep(); }} showTypes=${showTypes}
            onToggleTypes=${function() { onToggleTyp(); }} showInputs=${showInputs}
            onToggleInputs=${function() { onToggleInputs(); }} onFitView=${fitWithFixedPadding} />
          ${root.__hypergraph_debug_viz ? html`
            <${DevLayoutControls} theme=${theme} endpointPadding=${endpointPadding} ranksep=${ranksep}
              onChangePadding=${function(v) { root.__hypergraphVizReady = false; setEndpointPadding(v); }}
              onChangeRanksep=${function(v) { root.__hypergraphVizReady = false; setRanksep(v); }} />
          ` : null}
        <//>
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
          initialShowInputs=${Boolean((initialData.meta && initialData.meta.show_inputs) !== false)}
          initialShowBoundedInputs=${Boolean(initialData.meta && initialData.meta.show_bounded_inputs)} />
      <//>
    `);
    if (bootMessage) bootMessage.remove();
  }

  HG.init = init;
})(typeof window !== 'undefined' ? window : this);
