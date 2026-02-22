/**
 * Hypergraph Visualization — single-file React Flow app using dagre natively.
 *
 * Replaces 6 custom JS files by reading dagre's native edge routing points
 * (g.edge().points) instead of custom post-processing.
 *
 * Sections: Constants, Theme, Layout, Edge, Node, Controls, App+Init
 */
(function(root) {
  'use strict';

  // ── Dependencies ──────────────────────────────────────────
  var React = root.React;
  var ReactDOM = root.ReactDOM;
  var RF = root.ReactFlow;
  var htm = root.htm;

  if (!React || !ReactDOM || !RF || !htm) {
    console.error('HypergraphViz: Missing required globals (React, ReactDOM, ReactFlow, htm)');
    return;
  }

  var useState = React.useState;
  var useEffect = React.useEffect;
  var useMemo = React.useMemo;
  var useCallback = React.useCallback;
  var useRef = React.useRef;

  var ReactFlowComp = RF.ReactFlow;
  var Background = RF.Background;
  var Panel = RF.Panel;
  var Position = RF.Position;
  var MarkerType = RF.MarkerType;
  var ReactFlowProvider = RF.ReactFlowProvider;
  var Handle = RF.Handle;
  var BaseEdge = RF.BaseEdge;
  var EdgeLabelRenderer = RF.EdgeLabelRenderer;
  var useNodesState = RF.useNodesState;
  var useEdgesState = RF.useEdgesState;
  var useReactFlow = RF.useReactFlow;
  var useUpdateNodeInternals = RF.useUpdateNodeInternals;
  var getBezierPath = RF.getBezierPath;

  var html = htm.bind(React.createElement);

  // ╔═══════════════════════════════════════════════════════════╗
  // ║  Section 1: Constants + Helpers                          ║
  // ╚═══════════════════════════════════════════════════════════╝

  var TYPE_HINT_MAX_CHARS = 25;
  var NODE_LABEL_MAX_CHARS = 25;
  var CHAR_WIDTH_PX = 7;
  var NODE_BASE_PADDING = 52;
  var FUNCTION_NODE_BASE_PADDING = 48;
  var MAX_NODE_WIDTH = 280;
  var GRAPH_PADDING = 24;
  var HEADER_HEIGHT = 32;
  var LAYOUT_PADDING = 70;
  var EDGE_CONVERGE_TO_CENTER = false;
  var EDGE_CONVERGENCE_OFFSET = 20;
  var EDGE_ENDPOINT_PADDING = 0.25;  // fraction of node width (0-0.5)
  var LAYOUT_RANKSEP = 140;
  var FEEDBACK_EDGE_GUTTER = 70;
  var FEEDBACK_EDGE_HEADROOM = 40;
  var FEEDBACK_EDGE_STEM = 32;

  var NODE_TYPE_OFFSETS = {
    PIPELINE: 26, GRAPH: 26, FUNCTION: 14,
    DATA: 6, INPUT: 6, INPUT_GROUP: 6, BRANCH: 10, END: 6,
  };
  var NODE_TYPE_TOP_INSETS = {
    PIPELINE: 0, GRAPH: 0, FUNCTION: 0,
    DATA: 0, INPUT: 0, INPUT_GROUP: 0, BRANCH: 3, END: 0,
  };
  var DEFAULT_OFFSET = 10;
  var DEFAULT_TOP_INSET = 0;

  function resolveNodeType(nodeType, isExpanded) {
    if (nodeType === 'PIPELINE' && !isExpanded) return 'FUNCTION';
    return nodeType || 'FUNCTION';
  }
  function getOffset(nodeType) { return NODE_TYPE_OFFSETS[nodeType] ?? DEFAULT_OFFSET; }
  function getTopInset(nodeType) { return NODE_TYPE_TOP_INSETS[nodeType] ?? DEFAULT_TOP_INSET; }
  function getVisibleTop(pos, nodeType) { return pos.y + getTopInset(nodeType); }

  function truncateTypeHint(type) {
    return type && type.length > TYPE_HINT_MAX_CHARS ? type.substring(0, TYPE_HINT_MAX_CHARS) + '...' : type;
  }
  function truncateLabel(label) {
    return label && label.length > NODE_LABEL_MAX_CHARS ? label.substring(0, NODE_LABEL_MAX_CHARS) + '...' : label;
  }

  function calculateDimensions(n) {
    var width = 80, height = 90;
    if (n.data && (n.data.nodeType === 'DATA' || n.data.nodeType === 'INPUT')) {
      height = 36;
      var labelLen = Math.min((n.data.label || '').length, NODE_LABEL_MAX_CHARS);
      var typeLen = (n.data.showTypes && n.data.typeHint) ? Math.min(n.data.typeHint.length, TYPE_HINT_MAX_CHARS) + 2 : 0;
      width = Math.min(MAX_NODE_WIDTH, (labelLen + typeLen) * CHAR_WIDTH_PX + NODE_BASE_PADDING);
    } else if (n.data && n.data.nodeType === 'INPUT_GROUP') {
      var params = n.data.params || [];
      var paramTypes = n.data.paramTypes || [];
      var maxLen = 6;
      params.forEach(function(p, i) {
        var pLen = Math.min(p.length, NODE_LABEL_MAX_CHARS);
        var tLen = (n.data.showTypes && paramTypes[i]) ? Math.min(paramTypes[i].length, TYPE_HINT_MAX_CHARS) + 2 : 0;
        maxLen = Math.max(maxLen, 3 + pLen + tLen);
      });
      width = Math.min(MAX_NODE_WIDTH, maxLen * CHAR_WIDTH_PX + 32);
      var numP = Math.max(1, params.length);
      var igOffset = NODE_TYPE_OFFSETS.INPUT_GROUP || 6;
      height = 16 + (numP * 16) + ((numP - 1) * 4) + igOffset;
    } else if (n.data && n.data.nodeType === 'BRANCH') {
      width = 140; height = 140;
    } else {
      var labelLen = Math.min((n.data && n.data.label || '').length, NODE_LABEL_MAX_CHARS);
      var maxCLen = labelLen;
      var outputs = (n.data && n.data.outputs) || [];
      if (n.data && !n.data.separateOutputs && outputs.length > 0) {
        outputs.forEach(function(o) {
          var oName = o.name || o.label || '';
          var oType = o.type || o.typeHint || '';
          var oLen = Math.min(oName.length, NODE_LABEL_MAX_CHARS);
          var otLen = (n.data.showTypes && oType) ? Math.min(oType.length, TYPE_HINT_MAX_CHARS) + 2 : 0;
          maxCLen = Math.max(maxCLen, oLen + otLen + 4);
        });
      }
      width = Math.min(MAX_NODE_WIDTH, maxCLen * CHAR_WIDTH_PX + FUNCTION_NODE_BASE_PADDING);
      height = 56;
      if (n.data && !n.data.separateOutputs && outputs.length > 0) {
        var rc = outputs.length;
        height = 56 + 16 + (rc * 16) + (Math.max(0, rc - 1) * 6) + 6;
      }
    }
    if (n.style && n.style.width) width = n.style.width;
    if (n.style && n.style.height) height = n.style.height;
    return { width: width, height: height };
  }

  // ╔═══════════════════════════════════════════════════════════╗
  // ║  Section 2: Theme Detection                              ║
  // ╚═══════════════════════════════════════════════════════════╝

  function parseColorString(value) {
    if (!value) return null;
    var scratch = document.createElement('div');
    scratch.style.color = value;
    scratch.style.backgroundColor = value;
    scratch.style.display = 'none';
    document.body.appendChild(scratch);
    var resolved = getComputedStyle(scratch).color || '';
    scratch.remove();
    var nums = resolved.match(/[\d.]+/g);
    if (nums && nums.length >= 3) {
      var r = Number(nums[0]), g = Number(nums[1]), b = Number(nums[2]);
      if (nums.length >= 4 && Number(nums[3]) < 0.1) return null;
      var luminance = 0.299 * r + 0.587 * g + 0.114 * b;
      return { r: r, g: g, b: b, luminance: luminance, resolved: resolved, raw: value };
    }
    return null;
  }

  function detectHostTheme() {
    var attempts = [];
    var push = function(v, s) {
      if (v && v !== 'transparent' && v !== 'rgba(0, 0, 0, 0)') attempts.push({ value: v.trim(), source: s });
    };

    var hostEnv = 'unknown';
    var parentDoc;
    try {
      parentDoc = root.parent && root.parent.document;
      if (parentDoc) {
        if (parentDoc.body.getAttribute('data-vscode-theme-kind') ||
            (parentDoc.body.className && parentDoc.body.className.includes('vscode'))) hostEnv = 'vscode';
        else if (parentDoc.body.dataset.jpThemeLight !== undefined ||
                 parentDoc.querySelector('.jp-Notebook')) hostEnv = 'jupyterlab';
        else if (parentDoc.body.dataset.theme || parentDoc.body.dataset.mode ||
                 (parentDoc.body.className && parentDoc.body.className.includes('marimo'))) hostEnv = 'marimo';
      }
    } catch (e) {}

    try {
      parentDoc = root.parent && root.parent.document;
      if (parentDoc) {
        var rs = getComputedStyle(parentDoc.documentElement);
        var bs = getComputedStyle(parentDoc.body);
        if (hostEnv === 'vscode') push(rs.getPropertyValue('--vscode-editor-background'), '--vscode-editor-background');
        else if (hostEnv === 'jupyterlab') {
          var jpNb = parentDoc.querySelector('.jp-Notebook');
          if (jpNb) push(getComputedStyle(jpNb).backgroundColor, '.jp-Notebook background');
          push(rs.getPropertyValue('--jp-layout-color0'), '--jp-layout-color0');
          push(rs.getPropertyValue('--jp-layout-color1'), '--jp-layout-color1');
        } else {
          push(rs.getPropertyValue('--vscode-editor-background'), '--vscode-editor-background');
          push(rs.getPropertyValue('--jp-layout-color0'), '--jp-layout-color0');
        }
        push(bs.backgroundColor, 'parent body background');
        push(rs.backgroundColor, 'parent root background');
      }
    } catch (e) {}
    push(getComputedStyle(document.body).backgroundColor, 'iframe body');

    var chosen = attempts.find(function(c) { return parseColorString(c.value); }) || { value: 'transparent', source: 'default' };
    var parsed = parseColorString(chosen.value);
    var luminance = parsed ? parsed.luminance : null;
    var autoTheme = luminance !== null ? (luminance > 150 ? 'light' : 'dark') : null;
    var source = luminance !== null ? (chosen.source + ' luminance') : chosen.source;

    // JupyterLab overrides
    try {
      parentDoc = root.parent && root.parent.document;
      if (parentDoc) {
        var jpTL = parentDoc.body.dataset.jpThemeLight;
        if (jpTL === 'true') { autoTheme = 'light'; source = 'jupyterlab data-jp-theme-light'; }
        else if (jpTL === 'false') { autoTheme = 'dark'; source = 'jupyterlab data-jp-theme-light'; }
        var bc = parentDoc.body.className || '';
        if (!autoTheme && bc.includes('jp-mod-dark')) { autoTheme = 'dark'; source = 'jupyterlab jp-mod-dark'; }
        else if (!autoTheme && bc.includes('jp-mod-light')) { autoTheme = 'light'; source = 'jupyterlab jp-mod-light'; }
      }
    } catch (e) {}

    // VS Code overrides
    try {
      parentDoc = root.parent && root.parent.document;
      if (parentDoc) {
        var tk = parentDoc.body.getAttribute('data-vscode-theme-kind');
        if (tk) { autoTheme = tk.includes('light') ? 'light' : 'dark'; source = 'vscode-theme-kind'; }
        else if (parentDoc.body.className && parentDoc.body.className.includes('vscode-light')) { autoTheme = 'light'; source = 'vscode body class'; }
        else if (parentDoc.body.className && parentDoc.body.className.includes('vscode-dark')) { autoTheme = 'dark'; source = 'vscode body class'; }
      }
    } catch (e) {}

    // Marimo overrides
    try {
      parentDoc = root.parent && root.parent.document;
      if (parentDoc && !autoTheme) {
        var dt = parentDoc.body.dataset.theme || parentDoc.documentElement.dataset.theme;
        var dm = parentDoc.body.dataset.mode || parentDoc.documentElement.dataset.mode;
        if (dt === 'dark' || dm === 'dark') { autoTheme = 'dark'; source = 'marimo data-theme/mode'; }
        else if (dt === 'light' || dm === 'light') { autoTheme = 'light'; source = 'marimo data-theme/mode'; }
        var bc = parentDoc.body.className || '';
        if (!autoTheme && (bc.includes('dark-mode') || bc.includes('dark'))) { autoTheme = 'dark'; source = 'marimo dark-mode class'; }
        if (!autoTheme) {
          var cs = getComputedStyle(parentDoc.documentElement).getPropertyValue('color-scheme').trim();
          if (cs.includes('dark')) { autoTheme = 'dark'; source = 'color-scheme property'; }
          else if (cs.includes('light')) { autoTheme = 'light'; source = 'color-scheme property'; }
        }
      }
    } catch (e) {}

    if (!autoTheme && root.matchMedia) {
      if (root.matchMedia('(prefers-color-scheme: light)').matches) { autoTheme = 'light'; source = 'prefers-color-scheme'; }
      else if (root.matchMedia('(prefers-color-scheme: dark)').matches) { autoTheme = 'dark'; source = 'prefers-color-scheme'; }
    }

    return {
      theme: autoTheme || 'dark',
      background: parsed ? (parsed.resolved || parsed.raw || chosen.value) : chosen.value,
      luminance: luminance,
      source: source,
    };
  }

  function normalizeThemePref(pref) {
    var l = (pref || '').toLowerCase();
    return ['light', 'dark', 'auto'].includes(l) ? l : 'auto';
  }

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
  function layoutGraph(nodes, edges, convergeToCenter, convergenceOffset, endpointPadding, ranksep) {
    if (convergeToCenter === undefined) convergeToCenter = EDGE_CONVERGE_TO_CENTER;
    if (convergenceOffset === undefined) convergenceOffset = EDGE_CONVERGENCE_OFFSET;
    if (endpointPadding === undefined) endpointPadding = EDGE_ENDPOINT_PADDING;
    if (ranksep === undefined) ranksep = LAYOUT_RANKSEP;
    var g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: 'TB', nodesep: 42, ranksep: ranksep, marginx: 0, marginy: 0 });
    g.setDefaultEdgeLabel(function() { return {}; });

    var nodeIds = new Set();
    nodes.forEach(function(n) {
      nodeIds.add(n.id);
      g.setNode(n.id, { width: n.width, height: n.height });
    });

    var edgeSet = new Set();
    edges.forEach(function(e) {
      if (nodeIds.has(e.source) && nodeIds.has(e.target)) {
        var key = e.source + '\0' + e.target;
        if (!edgeSet.has(key)) { edgeSet.add(key); g.setEdge(e.source, e.target); }
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

      // BRANCH/END nodes always use center-x (diamond has single entry/exit point)
      var srcForceCenterX = srcType === 'BRANCH' || srcType === 'END';
      var tgtForceCenterX = tgtType === 'BRANCH' || tgtType === 'END';

      var dagreEdge = g.edge(e.source, e.target);
      if (dagreEdge && dagreEdge.points && dagreEdge.points.length > 0) {
        var pts = dagreEdge.points.map(function(p) { return { x: p.x, y: p.y }; });
        if (convergeToCenter) {
          // Center mode: all edges go to node center-x
          pts[0] = { x: src.x, y: srcBottom };
          pts[pts.length - 1] = { x: tgt.x, y: tgtTop };
        } else {
          // Dagre mode: keep dagre's native x-positions, clamp Y and pad X
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
        }
        e.points = pts;
      } else {
        // Fallback: no dagre edge data, use center-x for both modes
        e.points = [{ x: src.x, y: srcBottom }, { x: tgt.x, y: tgtTop }];
      }
    });

    // Add convergence/divergence stems when edges converge to center
    if (convergeToCenter) {
      addConvergenceStems(edges, nodeById, convergenceOffset);
    }

    var size = computeBounds(nodes, LAYOUT_PADDING);
    offsetAll(nodes, edges, size.min);
    return { nodes: nodes, edges: edges, size: size };
  }

  /**
   * Insert convergence/divergence stem points for edges that share endpoints.
   * When multiple edges arrive at the same target center, inserting a convergence
   * point creates the V-shape merge effect. Same for divergence from sources.
   */
  function addConvergenceStems(edges, nodeById, offset) {
    // Group edges by target
    var byTarget = {};
    edges.forEach(function(e) {
      if (!e.points || e.points.length < 2) return;
      if (!byTarget[e.target]) byTarget[e.target] = [];
      byTarget[e.target].push(e);
    });

    // Add convergence points for targets with 2+ incoming edges
    Object.keys(byTarget).forEach(function(targetId) {
      var group = byTarget[targetId];
      if (group.length < 2) return;
      var tgt = nodeById[targetId];
      if (!tgt) return;

      var tgtType = resolveNodeType(tgt.data && tgt.data.nodeType, tgt.data && tgt.data.isExpanded);
      var tgtTop = tgt.y - tgt.height * 0.5 + getTopInset(tgtType);
      var convergeY = tgtTop - offset;

      group.forEach(function(e) {
        var last = e.points[e.points.length - 1];
        // Insert convergence point before the final target point
        e.points.splice(e.points.length - 1, 0, { x: last.x, y: convergeY });
      });
    });

    // Group edges by source
    var bySource = {};
    edges.forEach(function(e) {
      if (!e.points || e.points.length < 2) return;
      if (!bySource[e.source]) bySource[e.source] = [];
      bySource[e.source].push(e);
    });

    // Add divergence points for sources with 2+ outgoing edges
    Object.keys(bySource).forEach(function(sourceId) {
      var group = bySource[sourceId];
      if (group.length < 2) return;
      var src = nodeById[sourceId];
      if (!src) return;

      var srcType = resolveNodeType(src.data && src.data.nodeType, src.data && src.data.isExpanded);
      var srcBottom = src.y + src.height * 0.5 - getOffset(srcType);
      var divergeY = srcBottom + offset;

      group.forEach(function(e) {
        var first = e.points[0];
        // Insert divergence point after the initial source point
        e.points.splice(1, 0, { x: first.x, y: divergeY });
      });
    });
  }

  // ── Feedback edge detection (DFS cycle detection) ──

  function computeFeedbackEdgeKeys(nodes, edges) {
    var nodeIds = new Set(nodes.map(function(n) { return n.id; }));
    var adj = new Map();
    nodes.forEach(function(n) { adj.set(n.id, []); });
    edges.forEach(function(e) {
      if (nodeIds.has(e.source) && nodeIds.has(e.target)) adj.get(e.source).push(e);
    });

    var state = new Map();
    nodes.forEach(function(n) { state.set(n.id, 0); });
    var feedback = new Set();

    var dfs = function(id) {
      state.set(id, 1);
      (adj.get(id) || []).forEach(function(edge) {
        if (!nodeIds.has(edge.target)) return;
        var s = state.get(edge.target) || 0;
        if (s === 0) dfs(edge.target);
        else if (s === 1) feedback.add(edge.source + '->' + edge.target);
      });
      state.set(id, 2);
    };
    nodes.forEach(function(n) { if (state.get(n.id) === 0) dfs(n.id); });
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

  // ── Recursive layout helpers ──

  function groupNodesByParent(nodes) {
    var groups = new Map();
    nodes.forEach(function(n) {
      var pid = n.parentNode || null;
      if (!groups.has(pid)) groups.set(pid, []);
      groups.get(pid).push(n);
    });
    return groups;
  }

  function getLayoutOrder(nodes, expansionState) {
    var nodeById = new Map(nodes.map(function(n) { return [n.id, n]; }));
    var getDepth = function(id, d) {
      var n = nodeById.get(id);
      return (!n || !n.parentNode) ? (d || 0) : getDepth(n.parentNode, (d || 0) + 1);
    };
    return nodes
      .filter(function(n) { return n.data && n.data.nodeType === 'PIPELINE' && expansionState.get(n.id) === true && !n.hidden; })
      .sort(function(a, b) { return getDepth(b.id) - getDepth(a.id); });
  }

  function buildDeepToChildMap(visibleNodes, childIds) {
    var deepToChild = new Map();
    var byId = new Map(visibleNodes.map(function(n) { return [n.id, n]; }));
    visibleNodes.forEach(function(n) {
      if (childIds.has(n.id)) return;
      var cur = n, visited = [];
      while (cur && cur.parentNode) {
        visited.push(cur.id);
        if (childIds.has(cur.parentNode)) {
          visited.forEach(function(id) { deepToChild.set(id, cur.parentNode); });
          break;
        }
        cur = byId.get(cur.parentNode);
      }
    });
    return deepToChild;
  }

  function collectEdgesForGroup(edges, memberIds, deepToChild) {
    var seen = new Set(), result = [];
    edges.forEach(function(e) {
      var s = deepToChild.has(e.source) ? deepToChild.get(e.source) : e.source;
      var t = deepToChild.has(e.target) ? deepToChild.get(e.target) : e.target;
      if (memberIds.has(s) && memberIds.has(t) && s !== t) {
        var key = s + '->' + t;
        if (!seen.has(key)) { seen.add(key); result.push({ id: key, source: s, target: t, _original: e }); }
      }
    });
    return result;
  }

  function buildLayoutNodes(nodes, nodeDimensions) {
    return nodes.map(function(n) {
      var d = nodeDimensions.get(n.id);
      return { id: n.id, width: d.width, height: d.height, x: 0, y: 0, data: n.data, _original: n };
    });
  }

  // ── Recursive layout ──

  function performRecursiveLayout(visibleNodes, edges, expansionState, debugMode, routingData, convergeToCenter, convergenceOffset, endpointPadding, ranksep) {
    var nodeGroups = groupNodesByParent(visibleNodes);
    var layoutOrder = getLayoutOrder(visibleNodes, expansionState);

    // Build dimensions and type maps
    var nodeDimensions = new Map();
    var nodeTypes = new Map();
    visibleNodes.forEach(function(n) {
      nodeDimensions.set(n.id, calculateDimensions(n));
      var nt = (n.data && n.data.nodeType) || 'FUNCTION';
      if (nt === 'PIPELINE' && !(n.data && n.data.isExpanded)) nt = 'FUNCTION';
      nodeTypes.set(n.id, nt);
    });

    var feedbackEdgeKeys = computeFeedbackEdgeKeys(visibleNodes, edges);

    // Find input nodes owned by containers
    var inputNodesInContainers = new Map();
    var parentMap = new Map();
    visibleNodes.forEach(function(n) { if (n.parentNode) parentMap.set(n.id, n.parentNode); });
    visibleNodes.forEach(function(n) {
      var nt = n.data && n.data.nodeType;
      if (nt !== 'INPUT' && nt !== 'INPUT_GROUP') return;
      var owner = n.data.deepestOwnerContainer || n.data.ownerContainer;
      if (!owner) return;
      var cur = owner;
      while (cur) {
        if (expansionState.get(cur)) { inputNodesInContainers.set(n.id, cur); break; }
        cur = parentMap.get(cur);
      }
    });

    // Phase 1: Layout children (deepest first)
    var childLayoutResults = new Map();
    layoutOrder.forEach(function(graphNode) {
      var children = (nodeGroups.get(graphNode.id) || []).slice();
      visibleNodes.forEach(function(n) {
        if (inputNodesInContainers.get(n.id) === graphNode.id) children.push(n);
      });
      if (!children.length) return;

      var childIds = new Set(children.map(function(c) { return c.id; }));
      var deep = buildDeepToChildMap(visibleNodes, childIds);
      var intEdges = collectEdgesForGroup(edges, childIds, deep);
      var fbKeys = computeFeedbackEdgeKeys(children, intEdges);
      var layoutEdges = intEdges.filter(function(e) { return !fbKeys.has(e.source + '->' + e.target); });

      var result = layoutGraph(buildLayoutNodes(children, nodeDimensions), layoutEdges, convergeToCenter, convergenceOffset, endpointPadding, ranksep);
      result.size = computeBounds(result.nodes, GRAPH_PADDING);
      childLayoutResults.set(graphNode.id, result);

      nodeDimensions.set(graphNode.id, {
        width: result.size.width,
        height: result.size.height + HEADER_HEIGHT,
      });
    });

    // Phase 2: Layout root
    var rootNodes = (nodeGroups.get(null) || []).filter(function(n) { return !inputNodesInContainers.has(n.id); });
    var rootNodeIds = new Set(rootNodes.map(function(n) { return n.id; }));

    // Lift deep edges to root ancestors
    var childToRoot = new Map();
    var byIdForLift = new Map(visibleNodes.map(function(n) { return [n.id, n]; }));
    visibleNodes.forEach(function(n) {
      if (rootNodeIds.has(n.id)) return;
      if (inputNodesInContainers.has(n.id)) {
        var oid = inputNodesInContainers.get(n.id);
        if (rootNodeIds.has(oid)) childToRoot.set(n.id, oid);
        return;
      }
      var cur = n;
      while (cur && cur.parentNode) {
        if (rootNodeIds.has(cur.parentNode)) { childToRoot.set(n.id, cur.parentNode); break; }
        cur = byIdForLift.get(cur.parentNode);
      }
    });

    var rootEdges = collectEdgesForGroup(edges, rootNodeIds, childToRoot);
    var rootFbKeys = computeFeedbackEdgeKeys(rootNodes, rootEdges);
    var filteredRootEdges = rootEdges.filter(function(e) { return !rootFbKeys.has(e.source + '->' + e.target); });

    var rootResult = layoutGraph(buildLayoutNodes(rootNodes, nodeDimensions), filteredRootEdges, convergeToCenter, convergenceOffset, endpointPadding, ranksep);
    rootResult.size = computeBounds(rootResult.nodes, GRAPH_PADDING);

    // Phase 3: Compose positions
    var nodePositions = new Map();
    var allNodes = [], allEdges = [];

    rootResult.nodes.forEach(function(n) {
      var w = n.width, h = n.height;
      var x = n.x - w / 2 - rootResult.size.min.x;
      var y = n.y - h / 2 - rootResult.size.min.y;
      nodePositions.set(n.id, { x: x, y: y });
      allNodes.push({
        ...n._original, position: { x: x, y: y }, width: w, height: h,
        style: { ...n._original.style, width: w, height: h },
        handles: [
          { type: 'target', position: 'top', x: w / 2, y: 0, width: 8, height: 8, id: null },
          { type: 'source', position: 'bottom', x: w / 2, y: h, width: 8, height: 8, id: null },
        ],
      });
    });

    // Add edges from root dagre pass (with native routing points)
    // Skip lifted edges — their dagre routing connects containers, not actual nodes.
    // Cross-boundary phase (Phase 4) will handle them with correct internal endpoints.
    rootResult.edges.forEach(function(e) {
      if (!e.points || !e.points.length) return;
      if (e._original && (e._original.source !== e.source || e._original.target !== e.target)) return;
      var offsetPts = e.points.map(function(pt) {
        return { x: pt.x - rootResult.size.min.x, y: pt.y - rootResult.size.min.y };
      });
      allEdges.push({ ...(e._original || e), data: { ...(e._original || e).data, points: offsetPts } });
    });

    layoutOrder.slice().reverse().forEach(function(graphNode) {
      var childResult = childLayoutResults.get(graphNode.id);
      if (!childResult) return;
      var parentPos = nodePositions.get(graphNode.id);
      if (!parentPos) return;

      var offX = parentPos.x, offY = parentPos.y + HEADER_HEIGHT;

      childResult.nodes.forEach(function(n) {
        var w = n.width, h = n.height;
        var cx = n.x - w / 2 - childResult.size.min.x;
        var cy = n.y - h / 2 - childResult.size.min.y;
        nodePositions.set(n.id, { x: offX + cx, y: offY + cy });
        nodeDimensions.set(n.id, { width: w, height: h });

        var nwp = { ...n._original };
        if (inputNodesInContainers.has(n.id)) {
          nwp.parentNode = inputNodesInContainers.get(n.id);
          nwp.extent = 'parent';
        }
        allNodes.push({
          ...nwp, position: { x: cx, y: cy + HEADER_HEIGHT }, width: w, height: h,
          style: { ...nwp.style, width: w, height: h },
          handles: [
            { type: 'target', position: 'top', x: w / 2, y: 0, width: 8, height: 8, id: null },
            { type: 'source', position: 'bottom', x: w / 2, y: h, width: 8, height: 8, id: null },
          ],
        });
      });

      childResult.edges.forEach(function(e) {
        // Skip lifted edges — cross-boundary phase handles them with correct endpoints
        if (e._original && (e._original.source !== e.source || e._original.target !== e.target)) return;
        var pts = (e.points || []).map(function(pt) {
          return { x: pt.x - childResult.size.min.x + offX, y: pt.y - childResult.size.min.y + offY };
        });
        allEdges.push({ ...(e._original || e), data: { ...(e._original || e).data, points: pts } });
      });
    });

    // Phase 4: Route cross-boundary edges
    var handledKeys = new Set(allEdges.map(function(e) { return e.source + '->' + e.target; }));

    var outputToProducer = (routingData && routingData.output_to_producer) || {};
    var paramToConsumer = (routingData && routingData.param_to_consumer) || {};
    var nodeToParent = (routingData && routingData.node_to_parent) || {};

    var inputGroupTargets = new Map();
    visibleNodes.forEach(function(n) {
      if (n.data && n.data.nodeType === 'INPUT_GROUP' && n.data.actualTargets)
        inputGroupTargets.set(n.id, n.data.actualTargets);
    });
    var inputNodeTargets = new Map();
    visibleNodes.forEach(function(n) {
      if (n.data && n.data.nodeType === 'INPUT') {
        var c = paramToConsumer[n.data.label];
        if (c) inputNodeTargets.set(n.id, c);
      }
    });

    edges.forEach(function(e) {
      if (handledKeys.has(e.source + '->' + e.target)) return;

      var actualSrc = e.source, actualTgt = e.target;
      var srcPos = null, tgtPos = null, srcDims = null, tgtDims = null;

      // Resolve actual source via output_to_producer
      var valueName = e.data && e.data.valueName;
      if (valueName && outputToProducer[valueName]) {
        var producer = outputToProducer[valueName];
        var isAncestor = false;
        var cur = e.source;
        while (cur && nodeToParent[cur]) {
          if (nodeToParent[cur] === producer) { isAncestor = true; break; }
          cur = nodeToParent[cur];
        }
        if (!isAncestor) {
          var dataId = 'data_' + producer + '_' + valueName;
          if (nodePositions.has(dataId)) { actualSrc = dataId; srcPos = nodePositions.get(dataId); srcDims = nodeDimensions.get(dataId); }
          else if (nodePositions.has(producer)) { actualSrc = producer; srcPos = nodePositions.get(producer); srcDims = nodeDimensions.get(producer); }
        }
      }
      if (!srcPos) { srcPos = nodePositions.get(e.source); srcDims = nodeDimensions.get(e.source); actualSrc = e.source; }

      // Resolve actual target via input groups / input nodes
      if (inputGroupTargets.has(e.source)) {
        var targets = inputGroupTargets.get(e.source);
        for (var i = 0; i < targets.length; i++) {
          if (nodePositions.has(targets[i])) { actualTgt = targets[i]; tgtPos = nodePositions.get(targets[i]); tgtDims = nodeDimensions.get(targets[i]); break; }
        }
      }
      if (inputNodeTargets.has(e.source)) {
        var consumer = inputNodeTargets.get(e.source);
        if (nodePositions.has(consumer)) { actualTgt = consumer; tgtPos = nodePositions.get(consumer); tgtDims = nodeDimensions.get(consumer); }
      }
      if (!tgtPos) { tgtPos = nodePositions.get(e.target); tgtDims = nodeDimensions.get(e.target); actualTgt = e.target; }

      if (!srcPos || !tgtPos || !srcDims || !tgtDims) return;

      var srcCX = srcPos.x + srcDims.width / 2;
      var srcNT = nodeTypes.get(actualSrc) || 'FUNCTION';
      var srcBY = srcPos.y + srcDims.height - getOffset(srcNT);
      var tgtCX = tgtPos.x + tgtDims.width / 2;
      var tgtNT = nodeTypes.get(actualTgt) || 'FUNCTION';
      var tgtTY = getVisibleTop(tgtPos, tgtNT);

      var isFb = feedbackEdgeKeys.has(e.source + '->' + e.target);
      var points = isFb
        ? buildFeedbackEdgePoints({ source: actualSrc, target: actualTgt }, nodePositions, nodeDimensions, nodeTypes)
        : [{ x: srcCX, y: srcBY }, { x: tgtCX, y: tgtTY }];

      if (!points) points = [{ x: srcCX, y: srcBY }, { x: tgtCX, y: tgtTY }];

      allEdges.push({
        ...e,
        data: { ...e.data, points: points, actualSource: actualSrc, actualTarget: actualTgt, isFeedbackEdge: isFb },
      });
    });

    return { nodes: allNodes, edges: allEdges, size: rootResult.size };
  }

  // ── useLayout hook ──

  function useLayout(nodes, edges, expansionState, routingData, convergeToCenter, convergenceOffset, endpointPadding, ranksep) {
    var nodesState = useState([]), edgesState = useState([]);
    var errorState = useState(null), versionState = useState(0);
    var heightState = useState(600), widthState = useState(600), busyState = useState(false);

    useEffect(function() {
      var debug = root.__hypergraph_debug_viz || false;
      if (!nodes.length) { busyState[1](false); return; }
      busyState[1](true);
      try {
        var visible = nodes.filter(function(n) { return !n.hidden; });

        if (expansionState && expansionState.size > 0) {
          var res = performRecursiveLayout(visible, edges, expansionState, debug, routingData, convergeToCenter, convergenceOffset, endpointPadding, ranksep);
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
          .filter(function(e) { return !fbKeys.has(e.source + '->' + e.target); })
          .map(function(e) { return { id: e.id, source: e.source, target: e.target, _original: e }; });

        var result = layoutGraph(layoutNodes, layoutEdges, convergeToCenter, convergenceOffset, endpointPadding, ranksep);

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
        visEdges.filter(function(e) { return fbKeys.has(e.source + '->' + e.target); }).forEach(function(e) {
          var pts = buildFeedbackEdgePoints(e, nodePos, nodeDim, nodeTyp);
          if (pts) posEdges.push({ ...e, data: { ...e.data, points: pts, isFeedbackEdge: true } });
        });

        nodesState[1](positioned); edgesState[1](posEdges);
        versionState[1](function(v) { return v + 1; }); busyState[1](false); errorState[1](null);
        if (result.size) { widthState[1](result.size.width); heightState[1](result.size.height); }
      } catch (err) {
        console.error('Layout error:', err);
        errorState[1](err && err.message ? err.message : 'Layout error');
        var fallback = nodes.map(function(n, i) {
          var w = (n.style && n.style.width) || 200, h = (n.style && n.style.height) || 68;
          return { ...n, position: { x: 80 * (i % 4), y: 120 * Math.floor(i / 4) }, width: w, height: h };
        });
        nodesState[1](fallback); edgesState[1](edges);
        versionState[1](function(v) { return v + 1; }); busyState[1](false);
      }
    }, [nodes, edges, expansionState, routingData, convergeToCenter, convergenceOffset, endpointPadding, ranksep]);

    return {
      layoutedNodes: nodesState[0], layoutedEdges: edgesState[0], layoutError: errorState[0],
      graphHeight: heightState[0], graphWidth: widthState[0], layoutVersion: versionState[0], isLayouting: busyState[0],
    };
  }

  // ╔═══════════════════════════════════════════════════════════╗
  // ║  Section 4: Edge Component                               ║
  // ╚═══════════════════════════════════════════════════════════╝

  /** Clamped B-spline through points (same as D3's curveBasis). 2-point input → S-curve. */
  function curveBasis(pts) {
    if (pts.length < 2) return 'M ' + pts[0].x + ' ' + pts[0].y;
    if (pts.length === 2) {
      var p0 = pts[0], p1 = pts[1], midY = (p0.y + p1.y) / 2;
      return 'M ' + p0.x + ' ' + p0.y + ' C ' + p0.x + ' ' + midY + ' ' + p1.x + ' ' + midY + ' ' + p1.x + ' ' + p1.y;
    }
    var c = [pts[0]].concat(pts).concat([pts[pts.length - 1]]);
    var path = 'M ' + c[0].x + ' ' + c[0].y;
    var x0 = c[0].x, y0 = c[0].y, x1 = c[1].x, y1 = c[1].y;
    path += ' L ' + ((5 * x0 + x1) / 6) + ' ' + ((5 * y0 + y1) / 6);
    for (var i = 2; i < c.length; i++) {
      var x = c[i].x, y = c[i].y;
      path += ' C ' + ((2 * x0 + x1) / 3) + ' ' + ((2 * y0 + y1) / 3) + ' ' +
              ((x0 + 2 * x1) / 3) + ' ' + ((y0 + 2 * y1) / 3) + ' ' +
              ((x0 + 4 * x1 + x) / 6) + ' ' + ((y0 + 4 * y1 + y) / 6);
      x0 = x1; y0 = y1; x1 = x; y1 = y;
    }
    path += ' C ' + ((2 * x0 + x1) / 3) + ' ' + ((2 * y0 + y1) / 3) + ' ' +
            ((x0 + 2 * x1) / 3) + ' ' + ((y0 + 2 * y1) / 3) + ' ' + x1 + ' ' + y1;
    return path;
  }

  function normalizePoints(pts) {
    if (!pts || pts.length < 2) return pts || [];
    var deduped = [pts[0]];
    for (var i = 1; i < pts.length; i++) {
      var p = deduped[deduped.length - 1], c = pts[i];
      if (Math.sqrt((c.x - p.x) * (c.x - p.x) + (c.y - p.y) * (c.y - p.y)) > 0.75) deduped.push(c);
    }
    if (deduped.length < 3) return deduped;
    var cleaned = [deduped[0]];
    for (var j = 1; j < deduped.length - 1; j++) {
      var a = cleaned[cleaned.length - 1], b = deduped[j], cn = deduped[j + 1];
      var l1 = Math.sqrt((b.x - a.x) * (b.x - a.x) + (b.y - a.y) * (b.y - a.y));
      var l2 = Math.sqrt((cn.x - b.x) * (cn.x - b.x) + (cn.y - b.y) * (cn.y - b.y));
      if (l1 >= 2 && l2 >= 2) cleaned.push(b);
    }
    cleaned.push(deduped[deduped.length - 1]);
    return cleaned.length >= 2 ? cleaned : deduped;
  }

  function pointAlongPolyline(pts, t) {
    if (!pts || pts.length < 2) return pts && pts[0] ? { x: pts[0].x, y: pts[0].y } : { x: 0, y: 0 };
    var total = 0;
    for (var i = 0; i < pts.length - 1; i++) {
      total += Math.sqrt(Math.pow(pts[i + 1].x - pts[i].x, 2) + Math.pow(pts[i + 1].y - pts[i].y, 2));
    }
    if (total < 1e-6) return { x: (pts[0].x + pts[pts.length - 1].x) / 2, y: (pts[0].y + pts[pts.length - 1].y) / 2 };
    var target = total * Math.max(0, Math.min(1, t)), walked = 0;
    for (var j = 0; j < pts.length - 1; j++) {
      var dx = pts[j + 1].x - pts[j].x, dy = pts[j + 1].y - pts[j].y;
      var seg = Math.sqrt(dx * dx + dy * dy);
      if (seg < 1e-6) continue;
      if (walked + seg >= target) {
        var lt = (target - walked) / seg;
        return { x: pts[j].x + dx * lt, y: pts[j].y + dy * lt };
      }
      walked += seg;
    }
    return { x: pts[pts.length - 1].x, y: pts[pts.length - 1].y };
  }

  function outgoingMidpointDistance(pts) {
    if (!pts || pts.length < 2) return 0;
    var segments = [], cumulative = 0;
    for (var i = 0; i < pts.length - 1; i++) {
      var dx = pts[i + 1].x - pts[i].x, dy = pts[i + 1].y - pts[i].y;
      var len = Math.sqrt(dx * dx + dy * dy);
      cumulative += len;
      segments.push({ x: dx, y: dy, len: len, end: cumulative });
    }
    var first = -1;
    for (var j = 0; j < segments.length; j++) { if (segments[j].len >= 6) { first = j; break; } }
    if (first < 0) return cumulative * 0.5;
    var base = segments[first], bx = base.x / (base.len || 1), by = base.y / (base.len || 1);
    var outLen = cumulative;
    for (var k = first + 1; k < segments.length; k++) {
      var s = segments[k];
      if (s.len < 6) continue;
      var dot = Math.max(-1, Math.min(1, bx * (s.x / s.len) + by * (s.y / s.len)));
      if (Math.acos(dot) * 180 / Math.PI >= 38) { outLen = segments[k - 1].end; break; }
    }
    return outLen * 0.5;
  }

  var CustomEdge = function(props) {
    var id = props.id, data = props.data, label = props.label;
    var sourceX = props.sourceX, sourceY = props.sourceY;
    var style = { strokeLinejoin: 'round', strokeLinecap: 'round', ...(props.style || {}) };
    var markerEnd = props.markerEnd;
    var edgePath, labelX, labelY;

    if (data && data.points && data.points.length > 0) {
      var points = normalizePoints(data.points.slice());
      edgePath = curveBasis(points);
      var edgeLabel = label || (data && data.label);
      var isBranch = edgeLabel === 'True' || edgeLabel === 'False';
      var lp = isBranch
        ? pointAlongPolyline(points, outgoingMidpointDistance(points) / (function() {
            var t = 0; for (var i = 0; i < points.length - 1; i++) t += Math.sqrt(Math.pow(points[i+1].x-points[i].x,2)+Math.pow(points[i+1].y-points[i].y,2)); return t || 1;
          })())
        : pointAlongPolyline(points, 0.35);
      labelX = lp.x; labelY = lp.y;
    } else {
      var r = getBezierPath({ sourceX: props.sourceX, sourceY: props.sourceY, sourcePosition: props.sourcePosition,
        targetX: props.targetX, targetY: props.targetY, targetPosition: props.targetPosition });
      edgePath = r[0];
      var edgeLabel = label || (data && data.label);
      labelX = sourceX + (props.targetX - sourceX) * 0.35;
      labelY = sourceY + (props.targetY - sourceY) * 0.35;
    }

    var edgeLabel = label || (data && data.label);
    var labelStyle = {};
    if (edgeLabel === 'True') labelStyle = { background: 'rgba(16,185,129,0.9)', border: '1px solid #34d399', color: '#fff', boxShadow: '0 2px 6px rgba(16,185,129,0.3)' };
    else if (edgeLabel === 'False') labelStyle = { background: 'rgba(239,68,68,0.9)', border: '1px solid #f87171', color: '#fff', boxShadow: '0 2px 6px rgba(239,68,68,0.3)' };
    else if (edgeLabel) labelStyle = { background: 'rgba(15,23,42,0.9)', border: '1px solid #334155', color: '#cbd5e1', boxShadow: '0 2px 4px rgba(0,0,0,0.2)' };

    return html`
      <${React.Fragment}>
        <${BaseEdge} path=${edgePath} markerEnd=${markerEnd} style=${style} />
        ${edgeLabel ? html`
          <${EdgeLabelRenderer}>
            <div style=${{ position: 'absolute', transform: 'translate(-50%,-50%) translate(' + labelX + 'px,' + labelY + 'px)',
              pointerEvents: 'all', display: 'flex', alignItems: 'center', gap: '4px', padding: '3px 10px',
              borderRadius: '10px', fontSize: '10px', fontFamily: 'ui-monospace, monospace', fontWeight: '600', letterSpacing: '0.02em', ...labelStyle }}>
              ${edgeLabel === 'True' ? html`<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>` :
                edgeLabel === 'False' ? html`<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>` : null}
              ${edgeLabel}
            </div>
          <//>
        ` : null}
      <//>
    `;
  };

  // ╔═══════════════════════════════════════════════════════════╗
  // ║  Section 5: Node Components                              ║
  // ╚═══════════════════════════════════════════════════════════╝

  var Icons = {
    Moon: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>`; },
    Sun: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line></svg>`; },
    ZoomIn: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="8" y1="11" x2="14" y2="11"/></svg>`; },
    ZoomOut: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="8" y1="11" x2="14" y2="11"/></svg>`; },
    Center: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>`; },
    Function: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><rect x="2" y="2" width="20" height="20" rx="2.18" ry="2.18"></rect><line x1="7" y1="2" x2="7" y2="22"></line><line x1="17" y1="2" x2="17" y2="22"></line><line x1="2" y1="12" x2="22" y2="12"></line></svg>`; },
    Pipeline: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><polygon points="12 2 2 7 12 12 22 7 12 2"></polygon><polyline points="2 17 12 22 22 17"></polyline><polyline points="2 12 12 17 22 12"></polyline></svg>`; },
    Dual: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><path d="M12 2a10 10 0 1 0 10 10H12V2z"></path><path d="M12 12L2 12"></path><path d="M12 12L12 22"></path></svg>`; },
    Branch: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><path d="M6 3v12"></path><circle cx="18" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><path d="M18 9a9 9 0 0 1-9 9"></path></svg>`; },
    Input: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="16"></line><line x1="8" y1="12" x2="16" y2="12"></line></svg>`; },
    Data: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-3 h-3"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><line x1="10" y1="9" x2="8" y2="9"></line></svg>`; },
    SplitOutputs: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><path d="M16 3h5v5"></path><path d="M8 3H3v5"></path><path d="M12 22v-8.3a4 4 0 0 0-1.172-2.872L3 3"></path><path d="m15 9 6-6"></path></svg>`; },
    MergeOutputs: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><path d="M8 3H3v5"></path><path d="m3 3 5.586 5.586a2 2 0 0 1 .586 1.414V22"></path><path d="M16 3h5v5"></path><path d="m21 3-5.586 5.586a2 2 0 0 0-.586 1.414V22"></path></svg>`; },
    Type: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><polyline points="4 7 4 4 20 4 20 7"></polyline><line x1="9" y1="20" x2="15" y2="20"></line><line x1="12" y1="4" x2="12" y2="20"></line></svg>`; },
    End: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-3 h-3"><circle cx="12" cy="12" r="10"></circle><circle cx="12" cy="12" r="4" fill="currentColor"></circle></svg>`; },
  };

  var OutputsSection = function(props) {
    var outputs = props.outputs, showTypes = props.showTypes, isLight = props.isLight;
    if (!outputs || !outputs.length) return null;
    var bg = isLight ? 'bg-slate-50/80' : 'bg-slate-900/50';
    var txt = isLight ? 'text-slate-600' : 'text-slate-400';
    var arr = isLight ? 'text-emerald-500' : 'text-emerald-400';
    var typ = isLight ? 'text-slate-400' : 'text-slate-500';
    var brd = isLight ? 'border-slate-100' : 'border-slate-800/50';
    return html`
      <div className=${'px-2 py-2 border-t transition-colors duration-300 overflow-hidden ' + bg + ' ' + brd}>
        <div className="flex flex-col items-center gap-1.5">
          ${outputs.map(function(o) {
            return html`<div key=${o.name} className=${'flex items-center gap-1.5 text-xs max-w-full ' + txt}>
              <span className=${'shrink-0 ' + arr}>→</span>
              <span className="font-mono font-medium shrink-0">${o.name}</span>
              ${showTypes && o.type ? html`<span className=${'font-mono truncate ' + typ} title=${o.type}>: ${truncateTypeHint(o.type)}</span>` : null}
            </div>`;
          })}
        </div>
      </div>`;
  };

  // Handle positioning
  function getSourceHandleStyle(nodeType) { return { bottom: (getOffset(nodeType)) + 'px' }; }
  function getTargetHandleStyle(nodeType) { return { top: (getTopInset(nodeType)) + 'px' }; }

  var CustomNode = function(props) {
    var data = props.data, id = props.id;
    var isExpanded = data.isExpanded;
    var theme = data.theme || 'dark';
    var isLight = theme === 'light';
    var updateNodeInternals = useUpdateNodeInternals();
    var nodeType = data.nodeType || 'FUNCTION';
    var visualType = (nodeType === 'PIPELINE' && !isExpanded) ? 'FUNCTION' : nodeType;
    var srcStyle = getSourceHandleStyle(visualType);
    var tgtStyle = getTargetHandleStyle(visualType);
    var bottomOff = getOffset(visualType);
    var wrapStyle = (nodeType !== 'BRANCH' && !(nodeType === 'PIPELINE' && isExpanded)) ? { paddingBottom: bottomOff + 'px' } : null;

    useEffect(function() { updateNodeInternals(id); }, [id, data.separateOutputs, data.showTypes, data.outputs ? data.outputs.length : 0, data.inputs ? data.inputs.length : 0, isExpanded, theme]);

    // Color config by type
    var colors = { bg: 'indigo', border: 'indigo' };
    var Icon = Icons.Function;
    if (nodeType === 'PIPELINE') { colors = { bg: 'amber', border: 'amber' }; Icon = Icons.Pipeline; }
    else if (nodeType === 'DUAL') { colors = { bg: 'fuchsia', border: 'fuchsia' }; Icon = Icons.Dual; }
    else if (nodeType === 'BRANCH') { colors = { bg: 'yellow', border: 'yellow' }; Icon = Icons.Branch; }
    else if (nodeType === 'INPUT' || nodeType === 'INPUT_GROUP') { colors = { bg: 'cyan', border: 'cyan' }; Icon = Icons.Input; }
    else if (nodeType === 'DATA') { colors = { bg: 'slate', border: 'slate' }; Icon = Icons.Data; }

    // ── DATA node ──
    if (nodeType === 'DATA') {
      var isOutput = data.sourceId != null;
      var showAsOutput = data.separateOutputs && isOutput;
      var tc = isLight ? 'text-slate-400' : 'text-slate-500';
      return html`
        <div className="w-full h-full relative" style=${wrapStyle}>
          <div className=${'px-3 py-1.5 w-full h-full relative rounded-full border shadow-sm flex items-center justify-center gap-2 transition-colors transition-shadow duration-200 hover:shadow-lg overflow-hidden' + (showAsOutput ? ' ring-2 ring-emerald-500/30' : '') + (isLight ? ' bg-white border-slate-200 text-slate-700 shadow-slate-200 hover:border-slate-300' : ' bg-slate-900 border-slate-700 text-slate-300 shadow-black/50 hover:border-slate-600')}>
            <span className=${'shrink-0 ' + (isLight ? 'text-slate-400' : 'text-slate-500')}><${Icon} /></span>
            <span className="text-xs font-mono font-medium shrink-0">${data.label}</span>
            ${data.showTypes && data.typeHint ? html`<span className=${'text-[10px] font-mono truncate min-w-0 ' + tc} title=${data.typeHint}>: ${truncateTypeHint(data.typeHint)}</span>` : null}
          </div>
          <${Handle} type="target" position=${Position.Top} className="!w-2 !h-2 !opacity-0" style=${tgtStyle} />
          <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" style=${srcStyle} />
        </div>`;
    }

    // ── INPUT node ──
    if (nodeType === 'INPUT') {
      var tc = isLight ? 'text-slate-400' : 'text-slate-500';
      return html`
        <div className="w-full h-full relative" style=${wrapStyle}>
          <div className=${'px-3 py-1.5 w-full h-full relative rounded-full border shadow-sm flex items-center justify-center gap-2 transition-colors transition-shadow duration-200 hover:shadow-lg overflow-hidden' + (data.isBound ? ' border-dashed' : '') + (isLight ? ' bg-white border-slate-200 text-slate-700 shadow-slate-200 hover:border-slate-300' : ' bg-slate-900 border-slate-700 text-slate-300 shadow-black/50 hover:border-slate-600')}>
            <span className=${'shrink-0 ' + (isLight ? 'text-slate-400' : 'text-slate-500')}><${Icons.Data} /></span>
            <span className="text-xs font-mono font-medium shrink-0">${data.label}</span>
            ${data.showTypes && data.typeHint ? html`<span className=${'text-xs font-mono truncate min-w-0 ' + tc} title=${data.typeHint}>: ${truncateTypeHint(data.typeHint)}</span>` : null}
          </div>
          <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" style=${srcStyle} />
        </div>`;
    }

    // ── INPUT_GROUP node ──
    if (nodeType === 'INPUT_GROUP') {
      var params = data.params || [], paramTypes = data.paramTypes || [];
      var tc = isLight ? 'text-slate-400' : 'text-slate-500';
      return html`
        <div className="w-full h-full relative" style=${wrapStyle}>
          <div className=${'px-3 py-2 w-full h-full relative rounded-xl border shadow-sm flex flex-col gap-1 transition-colors transition-shadow duration-200 hover:shadow-lg' + (data.isBound ? ' border-dashed' : '') + (isLight ? ' bg-white border-slate-200 text-slate-700 shadow-slate-200 hover:border-slate-300' : ' bg-slate-900 border-slate-700 text-slate-300 shadow-black/50 hover:border-slate-600')}>
            ${params.map(function(p, i) {
              return html`<div className="flex items-center gap-2 whitespace-nowrap">
                <span className=${isLight ? 'text-slate-400' : 'text-slate-500'}><${Icons.Data} /></span>
                <div className="text-xs font-mono leading-tight">${p}</div>
                ${data.showTypes && paramTypes[i] ? html`<span className=${'text-xs font-mono ' + tc} title=${paramTypes[i]}>: ${truncateTypeHint(paramTypes[i])}</span>` : null}
              </div>`;
            })}
          </div>
          <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" style=${srcStyle} />
        </div>`;
    }

    // ── END node ──
    if (nodeType === 'END') {
      return html`
        <div className="w-full h-full relative" style=${wrapStyle}>
          <div className=${'px-3 py-2 w-full h-full relative rounded-full border shadow-sm flex items-center justify-center gap-2 transition-colors transition-shadow duration-200 hover:shadow-lg' + (isLight ? ' bg-white border-emerald-300 text-emerald-600 shadow-slate-200 hover:border-emerald-400' : ' bg-slate-900 border-emerald-500/50 text-emerald-400 shadow-black/50 hover:border-emerald-400/70')}>
            <${Icons.End} /> <span className="text-xs font-semibold uppercase tracking-wide">End</span>
          </div>
          <${Handle} type="target" position=${Position.Top} className="!w-2 !h-2 !opacity-0" style=${tgtStyle} />
        </div>`;
    }

    // ── BRANCH node (diamond) ──
    if (nodeType === 'BRANCH') {
      var hoverState = useState(false);
      var diamondBg = isLight ? '#ecfeff' : '#083344';
      var diamondBorder = isLight ? '#22d3ee' : 'rgba(6,182,212,0.6)';
      var diamondHover = isLight ? '#06b6d4' : 'rgba(34,211,238,0.8)';
      var labelColor = isLight ? '#0e7490' : '#a5f3fc';
      return html`
        <div className="relative flex items-center justify-center cursor-pointer" style=${{ width: '140px', height: '140px' }}
             onMouseEnter=${function() { hoverState[1](true); }} onMouseLeave=${function() { hoverState[1](false); }}>
          <div style=${{ filter: 'drop-shadow(0 10px 8px rgb(0 0 0 / 0.04)) drop-shadow(0 4px 3px rgb(0 0 0 / 0.1))' }}>
            <div className="transition-colors transition-shadow duration-200 ease-out border"
                 style=${{ width: '95px', height: '95px', transform: 'rotate(45deg)', borderRadius: '10px',
                   backgroundColor: diamondBg, borderColor: hoverState[0] ? diamondHover : diamondBorder,
                   boxShadow: hoverState[0] ? '0 0 15px rgba(6,182,212,0.4)' : '0 0 0 rgba(6,182,212,0)' }}></div>
          </div>
          <div style=${{ position: 'absolute', inset: '0', display: 'flex', alignItems: 'center', justifyContent: 'center', pointerEvents: 'none', padding: '0 10px' }}>
            <span className="text-sm font-semibold text-center" style=${{ color: labelColor, maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title=${data.label}>${data.label}</span>
          </div>
          <${Handle} type="target" position=${Position.Top} className="!w-2 !h-2 !opacity-0" style=${tgtStyle} />
          <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" style=${srcStyle} id="branch-source" />
        </div>`;
    }

    // ── Expanded PIPELINE ──
    if (nodeType === 'PIPELINE' && isExpanded) {
      return html`
        <div className=${'relative w-full h-full rounded-2xl border-2 border-dashed p-6 transition-colors duration-200' + (isLight ? ' border-amber-300 bg-amber-50/30' : ' border-amber-500/30 bg-amber-500/5')}>
          <button type="button"
            className=${'absolute -top-3 left-4 px-3 py-0.5 rounded-full text-xs font-bold uppercase tracking-wider flex items-center gap-2 cursor-pointer transition-colors z-10 whitespace-nowrap' + (isLight ? ' bg-amber-100 text-amber-700 hover:bg-amber-200 border border-amber-200' : ' bg-slate-950 text-amber-400 hover:text-amber-300 border border-amber-500/50')}
            onClick=${function(e) { e.stopPropagation(); e.preventDefault(); if (data.onToggleExpand) data.onToggleExpand(); }}
            title=${data.label}>
            <${Icon} /> ${truncateLabel(data.label)}
          </button>
          <${Handle} type="target" position=${Position.Top} className="!w-2 !h-2 !opacity-0" style=${tgtStyle} />
          <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" style=${srcStyle} />
        </div>`;
    }

    // ── Default: FUNCTION / collapsed PIPELINE ──
    var boundInputs = data.inputs ? data.inputs.filter(function(i) { return i.is_bound; }).length : 0;
    var outputs = data.outputs || [];
    var showCombined = !data.separateOutputs && outputs.length > 0;
    return html`
      <div className="w-full h-full relative" style=${wrapStyle}>
        <div className=${'group relative w-full h-full rounded-lg border shadow-lg backdrop-blur-sm transition-colors transition-shadow duration-200 cursor-pointer node-function-' + theme + ' overflow-hidden' +
             (isLight ? ' bg-white/90 border-' + colors.border + '-300 shadow-slate-200 hover:border-' + colors.border + '-400 hover:shadow-' + colors.border + '-200 hover:shadow-lg'
                      : ' bg-slate-950/90 border-' + colors.border + '-500/40 shadow-black/50 hover:border-' + colors.border + '-500/70 hover:shadow-' + colors.border + '-500/20 hover:shadow-lg')}
             onClick=${nodeType === 'PIPELINE' ? function(e) { e.stopPropagation(); if (data.onToggleExpand) data.onToggleExpand(); } : undefined}>
          <div className=${'px-3 py-2.5 flex flex-col items-center justify-center overflow-hidden' + (showCombined ? (isLight ? ' border-b border-slate-100' : ' border-b border-slate-800/50') : '')}>
            <div className=${'text-sm font-semibold truncate max-w-full text-center flex items-center justify-center gap-2' + (isLight ? ' text-slate-800' : ' text-slate-100')} title=${data.label}>${truncateLabel(data.label)}</div>
            ${boundInputs > 0 ? html`<div className=${'absolute top-2 right-2 w-2 h-2 rounded-full ring-2 ring-offset-1' + (isLight ? ' bg-indigo-400 ring-indigo-100 ring-offset-white' : ' bg-indigo-500 ring-indigo-500/30 ring-offset-slate-950')} title="${boundInputs} bound inputs"></div>` : null}
          </div>
          ${showCombined ? html`<${OutputsSection} outputs=${outputs} showTypes=${data.showTypes} isLight=${isLight} />` : null}
          ${nodeType === 'PIPELINE' ? html`<div className="absolute -bottom-5 left-1/2 -translate-x-1/2 text-[9px] text-slate-400 opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap">Click to expand</div>` : null}
        </div>
        <${Handle} type="target" position=${Position.Top} className="!w-2 !h-2 !opacity-0" style=${tgtStyle} />
        <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" style=${srcStyle} />
      </div>`;
  };

  // ╔═══════════════════════════════════════════════════════════╗
  // ║  Section 6: Controls                                     ║
  // ╚═══════════════════════════════════════════════════════════╝

  var TooltipButton = function(props) {
    var showTooltip = useState(false);
    var isLight = props.theme === 'light';
    var btn = 'p-2 rounded-lg shadow-lg border transition-all duration-200 ' +
      (isLight ? 'bg-white border-slate-200 text-slate-600 hover:bg-slate-50 hover:text-slate-900' : 'bg-slate-900 border-slate-700 text-slate-400 hover:bg-slate-800 hover:text-slate-100');
    var active = isLight ? 'bg-slate-100 text-indigo-600' : 'bg-slate-800 text-indigo-400';
    var tip = isLight ? 'bg-slate-800 text-white' : 'bg-white text-slate-800';
    return html`
      <div className="relative" onMouseEnter=${function() { showTooltip[1](true); }} onMouseLeave=${function() { showTooltip[1](false); }}>
        <button className=${btn + ' ' + (props.isActive ? active : '')} onClick=${props.onClick}>${props.children}</button>
        ${showTooltip[0] && html`<div className=${'absolute right-full mr-2 top-1/2 -translate-y-1/2 px-2 py-1 text-xs font-medium rounded shadow-lg whitespace-nowrap pointer-events-none z-50 ' + tip}>
          ${props.tooltip}
          <div className=${'absolute left-full top-1/2 -translate-y-1/2 border-4 border-transparent ' + (isLight ? 'border-l-slate-800' : 'border-l-white')}></div>
        </div>`}
      </div>`;
  };

  var CustomControls = function(props) {
    var rf = useReactFlow();
    return html`
      <${Panel} position="bottom-right" className="flex flex-col gap-2 pb-4 mr-6">
        <${TooltipButton} onClick=${function() { rf.zoomIn(); }} tooltip="Zoom In" theme=${props.theme}><${Icons.ZoomIn} /><//>
        <${TooltipButton} onClick=${function() { rf.zoomOut(); }} tooltip="Zoom Out" theme=${props.theme}><${Icons.ZoomOut} /><//>
        <${TooltipButton} onClick=${props.onFitView} tooltip="Fit View" theme=${props.theme}><${Icons.Center} /><//>
        <div className=${'h-px my-1 ' + (props.theme === 'light' ? 'bg-slate-200' : 'bg-slate-700')}></div>
        <${TooltipButton} onClick=${props.onToggleSeparate} tooltip=${props.separateOutputs ? "Merge Outputs" : "Separate Outputs"} isActive=${props.separateOutputs} theme=${props.theme}>
          ${props.separateOutputs ? html`<${Icons.MergeOutputs} />` : html`<${Icons.SplitOutputs} />`}
        <//>
        <${TooltipButton} onClick=${props.onToggleTypes} tooltip=${props.showTypes ? "Hide Types" : "Show Types"} isActive=${props.showTypes} theme=${props.theme}><${Icons.Type} /><//>
        <div className=${'h-px my-1 ' + (props.theme === 'light' ? 'bg-slate-200' : 'bg-slate-700')}></div>
        <${TooltipButton} onClick=${props.onToggleTheme} tooltip=${props.theme === 'dark' ? "Switch to Light Theme" : "Switch to Dark Theme"} theme=${props.theme}>
          ${props.theme === 'dark' ? html`<${Icons.Sun} />` : html`<${Icons.Moon} />`}
        <//>
      <//>`;
  };

  // ── Dev-only edge convergence controls (DialKit) ──

  var DevEdgeControls = function(props) {
    var isLight = props.theme === 'light';
    var bg = isLight ? 'bg-white/90 border-slate-300' : 'bg-slate-900/90 border-slate-700';
    var text = isLight ? 'text-slate-700' : 'text-slate-300';
    var muted = isLight ? 'text-slate-500' : 'text-slate-500';
    var accent = isLight ? 'accent-indigo-500' : 'accent-indigo-400';

    return html`
      <${Panel} position="top-left" className=${'p-3 rounded-lg border shadow-lg backdrop-blur-sm ' + bg}>
        <div className=${'text-xs font-semibold mb-2 ' + muted}>Edge Routing</div>
        <label className=${'flex items-center gap-2 text-xs cursor-pointer ' + text}>
          <input type="checkbox" checked=${props.convergeToCenter}
            className=${accent}
            onChange=${function(e) { props.onToggleConverge(e.target.checked); }} />
          Converge to center
        </label>
        ${props.convergeToCenter ? html`
          <div className="mt-2">
            <div className=${'flex items-center justify-between text-xs mb-1 ' + muted}>
              <span>Stem height</span>
              <span className=${'font-mono ' + text}>${props.convergenceOffset}px</span>
            </div>
            <input type="range" min="0" max="60" step="1"
              value=${props.convergenceOffset}
              className=${'w-full h-1 rounded-lg appearance-none cursor-pointer ' + accent}
              onInput=${function(e) { props.onChangeOffset(Number(e.target.value)); }} />
          </div>
        ` : null}
        ${!props.convergeToCenter ? html`
          <div className="mt-2">
            <div className=${'flex items-center justify-between text-xs mb-1 ' + muted}>
              <span>Endpoint padding</span>
              <span className=${'font-mono ' + text}>${Math.round(props.endpointPadding * 100)}%</span>
            </div>
            <input type="range" min="0" max="0.45" step="0.01"
              value=${props.endpointPadding}
              className=${'w-full h-1 rounded-lg appearance-none cursor-pointer ' + accent}
              onInput=${function(e) { props.onChangePadding(Number(e.target.value)); }} />
          </div>
        ` : null}
        <div className="mt-2">
          <div className=${'flex items-center justify-between text-xs mb-1 ' + muted}>
            <span>Vertical gap</span>
            <span className=${'font-mono ' + text}>${props.ranksep}px</span>
          </div>
          <input type="range" min="40" max="300" step="5"
            value=${props.ranksep}
            className=${'w-full h-1 rounded-lg appearance-none cursor-pointer ' + accent}
            onInput=${function(e) { props.onChangeRanksep(Number(e.target.value)); }} />
        </div>
      <//>`;
  };

  // ╔═══════════════════════════════════════════════════════════╗
  // ║  Section 7: App + Init                                   ║
  // ╚═══════════════════════════════════════════════════════════╝

  var nodeTypes = { custom: CustomNode, pipelineGroup: CustomNode };
  var edgeTypes = { custom: CustomEdge };

  var App = function(props) {
    var initialData = props.initialData;
    var themePreference = props.themePreference;
    var panOnScroll = props.panOnScroll;

    var sepState = useState(props.initialSeparateOutputs);
    var separateOutputs = sepState[0], setSeparateOutputs = sepState[1];
    var typState = useState(props.initialShowTypes);
    var showTypes = typState[0], setShowTypes = typState[1];

    // Edge convergence state (dev-only controls)
    var convState = useState(EDGE_CONVERGE_TO_CENTER);
    var convergeToCenter = convState[0], setConvergeToCenter = convState[1];
    var convOffState = useState(EDGE_CONVERGENCE_OFFSET);
    var convergenceOffset = convOffState[0], setConvergenceOffset = convOffState[1];
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

    // Render options hook for tests and dev gallery
    useEffect(function() {
      var applyOpts = function(opts) {
        if (!opts) return;
        if (Object.prototype.hasOwnProperty.call(opts, 'separateOutputs')) onToggleSep(!!opts.separateOutputs);
        if (Object.prototype.hasOwnProperty.call(opts, 'showTypes')) onToggleTyp(!!opts.showTypes);
        if (Object.prototype.hasOwnProperty.call(opts, 'convergeToCenter')) {
          root.__hypergraphVizReady = false;
          setConvergeToCenter(!!opts.convergeToCenter);
        }
        if (Object.prototype.hasOwnProperty.call(opts, 'convergenceOffset')) {
          root.__hypergraphVizReady = false;
          setConvergenceOffset(Number(opts.convergenceOffset));
        }
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
    }, [onToggleSep, onToggleTyp, setConvergeToCenter, setConvergenceOffset, setEndpointPadding, setRanksep]);

    var detState = useState(function() { return detectHostTheme(); });
    var detectedTheme = detState[0], setDetectedTheme = detState[1];
    var manState = useState(null);
    var manualTheme = manState[0], setManualTheme = manState[1];
    var bgState = useState((detectedTheme && detectedTheme.background) || 'transparent');
    var bgColor = bgState[0], setBgColor = bgState[1];

    var expState = useState(function() {
      var map = new Map();
      initialData.nodes.forEach(function(n) {
        if (n.data && n.data.nodeType === 'PIPELINE') map.set(n.id, n.data.isExpanded || false);
      });
      return map;
    });
    var expansionState = expState[0], setExpansionState = expState[1];

    var edgesByState = (initialData.meta && initialData.meta.edgesByState) || {};
    var nodesByState = (initialData.meta && initialData.meta.nodesByState) || {};
    var expandableNodes = (initialData.meta && initialData.meta.expandableNodes) || [];

    var expansionStateToKey = function(es, sep) {
      var sepKey = 'sep:' + (sep ? '1' : '0');
      if (!expandableNodes.length) return sepKey;
      var parts = [];
      expandableNodes.forEach(function(id) { parts.push(id + ':' + (es.get(id) ? '1' : '0')); });
      return parts.join(',') + '|' + sepKey;
    };

    var nsState = useNodesState([]);
    var rfNodes = nsState[0], setNodes = nsState[1], onNodesChange = nsState[2];
    var esState = useEdgesState([]);
    var rfEdges = esState[0], setEdges = esState[1], onEdgesChange = esState[2];
    var nodesRef = useRef(initialData.nodes);

    var resolved = detectedTheme || { theme: themePreference === 'auto' ? 'dark' : themePreference, background: 'transparent', luminance: null, source: 'init' };
    var activeTheme = useMemo(function() { return manualTheme || (themePreference === 'auto' ? (resolved.theme || 'dark') : themePreference); }, [manualTheme, resolved.theme, themePreference]);
    var activeBg = useMemo(function() {
      if (manualTheme) return manualTheme === 'light' ? '#f8fafc' : '#020617';
      var bg = resolved.background;
      return (!bg || bg === 'transparent' || bg === 'rgba(0, 0, 0, 0)') ? (activeTheme === 'light' ? '#f8fafc' : '#020617') : bg;
    }, [manualTheme, resolved.background, activeTheme]);

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

    // Select precomputed nodes
    var selectedNodes = useMemo(function() {
      var key = expansionStateToKey(expansionState, separateOutputs);
      var pre = nodesByState[key];
      var base = (pre && pre.length > 0) ? pre : initialData.nodes;
      return base.map(function(n) { return { ...n, data: { ...n.data, theme: activeTheme, showTypes: showTypes, separateOutputs: separateOutputs } }; });
    }, [expansionState, separateOutputs, showTypes, activeTheme, nodesByState, initialData.nodes]);

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
    useEffect(function() { setBgColor(activeBg); document.body.classList.toggle('light-mode', activeTheme === 'light'); }, [activeTheme, activeBg]);

    var toggleTheme = useCallback(function() {
      if (manualTheme === null) setManualTheme((resolved.theme || 'dark') === 'dark' ? 'light' : 'dark');
      else setManualTheme(null);
    }, [manualTheme, resolved.theme]);

    // Select edges
    var selectedEdges = useMemo(function() {
      var key = expansionStateToKey(expansionState, separateOutputs);
      var pre = edgesByState[key];
      return (pre && pre.length > 0) ? pre : initialData.edges || [];
    }, [expansionState, separateOutputs, edgesByState, initialData.edges]);

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

    var layoutResult = useLayout(nodesWithCb, selectedEdges, expansionState, routingData, convergeToCenter, convergenceOffset, endpointPadding, ranksep);
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
    var renderModeKey = useMemo(function() { return 'sep:' + (separateOutputs ? '1' : '0') + '|types:' + (showTypes ? '1' : '0'); }, [separateOutputs, showTypes]);
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
      var nodeMap = new Map(layoutedNodes.map(function(n) { return [n.id, n]; }));
      var getAbs = function(node) {
        var x = (node.position && node.position.x) || 0, y = (node.position && node.position.y) || 0;
        var cur = node;
        while (cur.parentNode) { var p = nodeMap.get(cur.parentNode); if (!p) break; x += (p.position && p.position.x) || 0; y += (p.position && p.position.y) || 0; cur = p; }
        return { x: x, y: y };
      };

      var npm = {};
      layoutedNodes.forEach(function(n) {
        if (n.hidden) return;
        var abs = getAbs(n);
        npm[n.id] = { x: abs.x, y: abs.y, width: n.style && n.style.width || 200, height: n.style && n.style.height || 68,
          nodeType: n.data && n.data.nodeType, isExpanded: n.data && n.data.isExpanded, label: (n.data && n.data.label) || n.id };
      });

      var getOff = function(nt, exp) { var t = resolveNodeType(nt || 'FUNCTION', exp); return getOffset(t); };

      root.__hypergraphVizDebug = {
        version: layoutVersion, timestamp: Date.now(),
        nodes: Object.keys(npm).map(function(id) {
          var n = npm[id]; var o = getOff(n.nodeType, n.isExpanded); var vh = n.height - o;
          return { id: id, label: n.label, x: n.x, y: n.y, width: n.width, height: vh, bottom: n.y + vh,
            nodeType: n.nodeType, wrapperHeight: n.height, wrapperBottom: n.y + n.height, offset: o };
        }),
        edges: layoutedEdges.map(function(e) {
          var aSrc = (e.data && e.data.actualSource) || e.source;
          var aTgt = (e.data && e.data.actualTarget) || e.target;
          var s = npm[aSrc], t = npm[aTgt];
          if (!s || !t) return { id: e.id, source: e.source, target: e.target, status: 'MISSING', issue: !s ? 'Source not visible' : 'Target not visible' };
          var srcBot = s.y + s.height - getOff(s.nodeType, s.isExpanded);
          var tgtTop = t.y;
          var srcCX = s.x + s.width / 2, tgtCX = t.x + t.width / 2;
          var vd = tgtTop - srcBot;
          var hd = tgtCX - srcCX;
          var issues = [];
          if (vd < 0) issues.push('Target above source (' + vd + 'px)');
          if (Math.abs(hd) > 500) issues.push('Large horizontal gap (' + hd + 'px)');
          if (vd > 300) issues.push('Large vertical gap (' + vd + 'px)');
          return { id: e.id, source: e.source, target: e.target, sourceLabel: s.label, targetLabel: t.label,
            srcBottom: srcBot, tgtTop: tgtTop, vertDist: vd, horizDist: hd,
            status: issues.length ? 'WARN' : 'OK', issue: issues.join('; ') || null, data: e.data };
        }),
        edgePaths: (function() {
          var paths = [];
          document.querySelectorAll('.react-flow__edge').forEach(function(g) {
            var path = g.querySelector('path'); if (!path) return;
            var d = path.getAttribute('d'); if (!d) return;
            var coords = d.match(/-?[\d.]+/g); if (!coords || coords.length < 4) return;
            var fc = coords.map(parseFloat);
            var tid = (g.getAttribute('data-testid') || '').replace('rf__edge-', '');
            var cid = tid.replace(/_exp_.*$/, '');

            // Edge lookup
            var edgeData = null;
            layoutedEdges.forEach(function(e) {
              var base = e.id.replace(/_exp_.*$/, '');
              if (base === cid || e.id === tid) edgeData = e;
            });
            var source = edgeData ? ((edgeData.data && edgeData.data.actualSource) || edgeData.source) : null;
            var target = edgeData ? ((edgeData.data && edgeData.data.actualTarget) || edgeData.target) : null;
            if (source) source = source.replace(/_exp_.*$/, '');
            if (target) target = target.replace(/_exp_.*$/, '');

            paths.push({ id: tid, source: source, target: target,
              pathStart: { x: fc[0], y: fc[1] }, pathEnd: { x: fc[fc.length - 2], y: fc[fc.length - 1] }, pathD: d });
          });
          return paths;
        })(),
        layoutedEdges: layoutedEdges.map(function(e) { return { id: e.id, source: e.source, target: e.target, data: e.data }; }),
        summary: { totalNodes: Object.keys(npm).length, totalEdges: layoutedEdges.length,
          edgeIssues: layoutedEdges.filter(function(e) { var a = (e.data && e.data.actualSource) || e.source; return !npm[a]; }).length },
        routingData: routingData,
      };
      // Live DOM query for edge paths (tests call this for fresh data)
      root.__hypergraphVizExtractEdgePaths = function() {
        var paths = [];
        document.querySelectorAll('.react-flow__edge').forEach(function(g) {
          var path = g.querySelector('path'); if (!path) return;
          var d = path.getAttribute('d'); if (!d) return;
          var coords = d.match(/-?[\d.]+/g); if (!coords || coords.length < 4) return;
          var fc = coords.map(parseFloat);
          var tid = (g.getAttribute('data-testid') || '').replace('rf__edge-', '');
          var cid = tid.replace(/_exp_.*$/, '');
          var edgeData = null;
          layoutedEdges.forEach(function(e) {
            var base = e.id.replace(/_exp_.*$/, '');
            if (base === cid || e.id === tid) edgeData = e;
          });
          var source = edgeData ? ((edgeData.data && edgeData.data.actualSource) || edgeData.source) : null;
          var target = edgeData ? ((edgeData.data && edgeData.data.actualTarget) || edgeData.target) : null;
          if (source) source = source.replace(/_exp_.*$/, '');
          if (target) target = target.replace(/_exp_.*$/, '');
          paths.push({ id: tid, source: source, target: target,
            pathStart: { x: fc[0], y: fc[1] }, pathEnd: { x: fc[fc.length - 2], y: fc[fc.length - 1] }, pathD: d });
        });
        return paths;
      };
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
        var isControl = e.data && e.data.edgeType === 'control';
        var st = { ...edgeOpts.style, strokeWidth: (e.data && e.data.isDataLink) ? 1.5 : 2 };
        if (isControl) st.strokeDasharray = '6 4';
        return { ...e, id: e.id + '_exp_' + (expansionKey ? expansionKey.replace(/,/g, '_') : 'none') + '_mode_' + renderModeKey,
          ...edgeOpts, style: st, markerEnd: edgeOpts.markerEnd, data: e.data };
      });
    }, [layoutedEdges, theme, isLayouting, expansionKey, renderModeKey]);

    return html`
      <div className="w-full relative overflow-hidden transition-colors duration-300"
           style=${{ backgroundColor: bgColor, height: '100vh', width: '100vw' }}
           onClick=${function() { try { root.parent.postMessage({ type: 'hypergraph-viz-click' }, '*'); } catch(e) {} }}>
        <${ReactFlowComp}
          nodes=${layoutedNodes} edges=${styledEdges} nodeTypes=${nodeTypes} edgeTypes=${edgeTypes}
          onNodesChange=${onNodesChange} onEdgesChange=${onEdgesChange}
          onNodeClick=${function(e, n) { if (n.data && n.data.nodeType === 'PIPELINE' && !n.data.isExpanded && n.data.onToggleExpand) { e.stopPropagation(); n.data.onToggleExpand(); } }}
          minZoom=${0.1} maxZoom=${2} className="bg-transparent" panOnScroll=${panOnScroll}
          zoomOnScroll=${false} panOnDrag=${true} zoomOnPinch=${true} preventScrolling=${false}
          style=${{ width: '100%', height: '100%' }}>
          <${Background} color=${theme === 'light' ? '#94a3b8' : '#334155'} gap=${24} size=${1} variant="dots" />
          <${CustomControls} theme=${theme} onToggleTheme=${toggleTheme} separateOutputs=${separateOutputs}
            onToggleSeparate=${function() { onToggleSep(); }} showTypes=${showTypes}
            onToggleTypes=${function() { onToggleTyp(); }} onFitView=${fitWithFixedPadding} />
          ${root.__hypergraph_debug_viz ? html`
            <${DevEdgeControls} theme=${theme} convergeToCenter=${convergeToCenter}
              convergenceOffset=${convergenceOffset} endpointPadding=${endpointPadding} ranksep=${ranksep}
              onToggleConverge=${function(v) { root.__hypergraphVizReady = false; setConvergeToCenter(v); }}
              onChangeOffset=${function(v) { root.__hypergraphVizReady = false; setConvergenceOffset(v); }}
              onChangePadding=${function(v) { root.__hypergraphVizReady = false; setEndpointPadding(v); }}
              onChangeRanksep=${function(v) { root.__hypergraphVizReady = false; setRanksep(v); }} />
          ` : null}
        <//>
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
    var initialData = JSON.parse(document.getElementById('graph-data').textContent || '{"nodes":[],"edges":[]}');
    var themePreference = normalizeThemePref((initialData.meta && initialData.meta.theme_preference) || 'auto');
    var rootEl = document.getElementById('root');
    var fallback = document.getElementById('fallback');
    ReactDOM.createRoot(rootEl).render(html`
      <${ReactFlowProvider}>
        <${App} initialData=${initialData} themePreference=${themePreference}
          panOnScroll=${Boolean(initialData.meta && initialData.meta.pan_on_scroll)}
          initialSeparateOutputs=${Boolean(initialData.meta && initialData.meta.separate_outputs)}
          initialShowTypes=${Boolean((initialData.meta && initialData.meta.show_types) !== false)} />
      <//>
    `);
    if (fallback) fallback.remove();
  }

  root.HypergraphViz = { init: init };
})(typeof window !== 'undefined' ? window : this);
