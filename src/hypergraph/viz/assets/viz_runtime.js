/**
 * Hypergraph visualization runtime shared by the no-build browser modules.
 */
(function(root) {
  'use strict';

  var HG = root.HypergraphViz = root.HypergraphViz || {};

  var React = root.React;
  var ReactDOM = root.ReactDOM;
  var RF = root.ReactFlow;
  var htm = root.htm;
  var dagre = root.dagre;

  if (!React || !ReactDOM || !RF || !htm || !dagre) {
    console.error('HypergraphViz: Missing required globals (React, ReactDOM, ReactFlow, htm, dagre)');
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

  // Frozen singletons for `|| EMPTY_OBJ` / `|| EMPTY_ARR` fallbacks in
  // App body. A fresh `{}` / `[]` literal at component-body scope
  // re-allocates every render and, if it ends up in a hook dep array,
  // produces an unbounded render loop. See DEBUGGING.md § Performance.
  var EMPTY_OBJ = Object.freeze({});
  var EMPTY_ARR = Object.freeze([]);

  var TYPE_HINT_MAX_CHARS = 25;
  var NODE_LABEL_MAX_CHARS = 25;
  var CHAR_WIDTH_PX = 7;
  var NODE_BASE_PADDING = 72;
  var FUNCTION_NODE_BASE_PADDING = 48;
  var MAX_NODE_WIDTH = 280;
  var GRAPH_PADDING = 12;
  var HEADER_HEIGHT = 32;
  var LAYOUT_PADDING = 36;
  var EDGE_ENDPOINT_PADDING = 0.25;  // fraction of node width (0-0.5)
  var LAYOUT_RANKSEP = 80;
  var FEEDBACK_EDGE_GUTTER = 70;
  var FEEDBACK_EDGE_HEADROOM = 40;
  var FEEDBACK_EDGE_STEM = 32;

  var NODE_TYPE_OFFSETS = {
    PIPELINE: 26, GRAPH: 26, FUNCTION: 14,
    DATA: 6, INPUT: 6, INPUT_GROUP: 6, BRANCH: 10, START: 6, END: 6,
  };
  var NODE_TYPE_TOP_INSETS = {
    PIPELINE: 0, GRAPH: 0, FUNCTION: 0,
    DATA: 0, INPUT: 0, INPUT_GROUP: 0, BRANCH: 3, START: 0, END: 0,
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

  var Runtime = {
    React: React,
    ReactDOM: ReactDOM,
    RF: RF,
    htm: htm,
    dagre: dagre,
    useState: useState,
    useEffect: useEffect,
    useMemo: useMemo,
    useCallback: useCallback,
    useRef: useRef,
    ReactFlowComp: ReactFlowComp,
    Background: Background,
    Panel: Panel,
    Position: Position,
    MarkerType: MarkerType,
    ReactFlowProvider: ReactFlowProvider,
    Handle: Handle,
    BaseEdge: BaseEdge,
    EdgeLabelRenderer: EdgeLabelRenderer,
    useNodesState: useNodesState,
    useEdgesState: useEdgesState,
    useReactFlow: useReactFlow,
    useUpdateNodeInternals: useUpdateNodeInternals,
    getBezierPath: getBezierPath,
    html: html,
    EMPTY_OBJ: EMPTY_OBJ,
    EMPTY_ARR: EMPTY_ARR,
    TYPE_HINT_MAX_CHARS: TYPE_HINT_MAX_CHARS,
    NODE_LABEL_MAX_CHARS: NODE_LABEL_MAX_CHARS,
    CHAR_WIDTH_PX: CHAR_WIDTH_PX,
    NODE_BASE_PADDING: NODE_BASE_PADDING,
    FUNCTION_NODE_BASE_PADDING: FUNCTION_NODE_BASE_PADDING,
    MAX_NODE_WIDTH: MAX_NODE_WIDTH,
    GRAPH_PADDING: GRAPH_PADDING,
    HEADER_HEIGHT: HEADER_HEIGHT,
    LAYOUT_PADDING: LAYOUT_PADDING,
    EDGE_ENDPOINT_PADDING: EDGE_ENDPOINT_PADDING,
    LAYOUT_RANKSEP: LAYOUT_RANKSEP,
    FEEDBACK_EDGE_GUTTER: FEEDBACK_EDGE_GUTTER,
    FEEDBACK_EDGE_HEADROOM: FEEDBACK_EDGE_HEADROOM,
    FEEDBACK_EDGE_STEM: FEEDBACK_EDGE_STEM,
    resolveNodeType: resolveNodeType,
    getOffset: getOffset,
    getTopInset: getTopInset,
    getVisibleTop: getVisibleTop,
    truncateTypeHint: truncateTypeHint,
    truncateLabel: truncateLabel,
    calculateDimensions: calculateDimensions,
    detectHostTheme: detectHostTheme,
    normalizeThemePref: normalizeThemePref,
  };

  root.HypergraphVizRuntime = Runtime;
  HG.Runtime = Runtime;
})(typeof window !== 'undefined' ? window : this);
