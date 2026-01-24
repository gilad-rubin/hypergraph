/**
 * React components for Hypergraph visualization
 * Includes Icons, CustomNode, CustomEdge, and UI controls
 */
(function(root, factory) {
  var api = factory(root);
  if (root) root.HypergraphVizComponents = api;
})(typeof window !== 'undefined' ? window : this, function(root) {
  'use strict';

  // Get dependencies from globals
  var React = root.React;
  var RF = root.ReactFlow;
  var htm = root.htm;

  if (!React || !RF || !htm) {
    console.error('HypergraphVizComponents: Missing required globals (React, ReactFlow, htm)');
    return {};
  }

  var useState = React.useState;
  var useEffect = React.useEffect;
  var useCallback = React.useCallback;
  var Handle = RF.Handle;
  var Position = RF.Position;
  var Panel = RF.Panel;
  var BaseEdge = RF.BaseEdge;
  var getBezierPath = RF.getBezierPath;
  var EdgeLabelRenderer = RF.EdgeLabelRenderer;
  var useReactFlow = RF.useReactFlow;
  var useUpdateNodeInternals = RF.useUpdateNodeInternals;

  var html = htm.bind(React.createElement);

  // === LAYOUT CONSTANTS ===
  var TYPE_HINT_MAX_CHARS = 25;
  var NODE_LABEL_MAX_CHARS = 25;

  // Helper to truncate type hints consistently
  var truncateTypeHint = function(type) {
    return type && type.length > TYPE_HINT_MAX_CHARS
      ? type.substring(0, TYPE_HINT_MAX_CHARS) + '...'
      : type;
  };

  // Helper to truncate node labels consistently
  var truncateLabel = function(label) {
    return label && label.length > NODE_LABEL_MAX_CHARS
      ? label.substring(0, NODE_LABEL_MAX_CHARS) + '...'
      : label;
  };

  // === ICONS ===
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
    Map: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6"></polygon><line x1="8" y1="2" x2="8" y2="18"></line><line x1="16" y1="6" x2="16" y2="22"></line></svg>`; },
    Bug: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><path d="m8 2 1.88 1.88"></path><path d="M14.12 3.88 16 2"></path><path d="M9 7.13v-1a3.003 3.003 0 1 1 6 0v1"></path><path d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6"></path><path d="M12 20v-9"></path><path d="M6.53 9C4.6 8.8 3 7.1 3 5"></path><path d="M6 13H2"></path><path d="M3 21c0-2.1 1.7-3.9 3.8-4"></path><path d="M20.97 5c0 2.1-1.6 3.8-3.5 4"></path><path d="M22 13h-4"></path><path d="M17.2 17c2.1.1 3.8 1.9 3.8 4"></path></svg>`; },
    SplitOutputs: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><path d="M16 3h5v5"></path><path d="M8 3H3v5"></path><path d="M12 22v-8.3a4 4 0 0 0-1.172-2.872L3 3"></path><path d="m15 9 6-6"></path></svg>`; },
    MergeOutputs: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><path d="M8 3H3v5"></path><path d="m3 3 5.586 5.586a2 2 0 0 1 .586 1.414V22"></path><path d="M16 3h5v5"></path><path d="m21 3-5.586 5.586a2 2 0 0 0-.586 1.414V22"></path></svg>`; },
    Type: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><polyline points="4 7 4 4 20 4 20 7"></polyline><line x1="9" y1="20" x2="15" y2="20"></line><line x1="12" y1="4" x2="12" y2="20"></line></svg>`; }
  };

  // === TOOLTIP BUTTON COMPONENT ===
  var TooltipButton = function(props) {
    var onClick = props.onClick;
    var tooltip = props.tooltip;
    var isActive = props.isActive;
    var theme = props.theme;
    var children = props.children;
    var showTooltip = useState(false);
    var setShowTooltip = showTooltip[1];
    showTooltip = showTooltip[0];
    var isLight = theme === 'light';

    var btnClass = 'p-2 rounded-lg shadow-lg border transition-all duration-200 ' +
        (isLight
        ? 'bg-white border-slate-200 text-slate-600 hover:bg-slate-50 hover:text-slate-900'
        : 'bg-slate-900 border-slate-700 text-slate-400 hover:bg-slate-800 hover:text-slate-100');
    var activeClass = isLight ? 'bg-slate-100 text-indigo-600' : 'bg-slate-800 text-indigo-400';
    var tooltipClass = isLight
        ? 'bg-slate-800 text-white'
        : 'bg-white text-slate-800';

    return html`
        <div className="relative" onMouseEnter=${function() { setShowTooltip(true); }} onMouseLeave=${function() { setShowTooltip(false); }}>
            <button className=${btnClass + ' ' + (isActive ? activeClass : '')} onClick=${onClick}>
                ${children}
            </button>
            ${showTooltip && html`
                <div className=${'absolute right-full mr-2 top-1/2 -translate-y-1/2 px-2 py-1 text-xs font-medium rounded shadow-lg whitespace-nowrap pointer-events-none z-50 ' + tooltipClass}>
                    ${tooltip}
                    <div className=${'absolute left-full top-1/2 -translate-y-1/2 border-4 border-transparent ' + (isLight ? 'border-l-slate-800' : 'border-l-white')}></div>
                </div>
            `}
        </div>
    `;
  };

  // === CUSTOM CONTROLS ===
  var CustomControls = function(props) {
    var theme = props.theme;
    var onToggleTheme = props.onToggleTheme;
    var separateOutputs = props.separateOutputs;
    var onToggleSeparate = props.onToggleSeparate;
    var showTypes = props.showTypes;
    var onToggleTypes = props.onToggleTypes;
    var onFitView = props.onFitView;
    var rf = useReactFlow();
    var zoomIn = rf.zoomIn;
    var zoomOut = rf.zoomOut;

    return html`
        <${Panel} position="bottom-right" className="flex flex-col gap-2 pb-4 mr-6">
            <${TooltipButton} onClick=${function() { zoomIn(); }} tooltip="Zoom In" theme=${theme}>
                <${Icons.ZoomIn} />
            <//>
            <${TooltipButton} onClick=${function() { zoomOut(); }} tooltip="Zoom Out" theme=${theme}>
                <${Icons.ZoomOut} />
            <//>
            <${TooltipButton} onClick=${onFitView} tooltip="Fit View" theme=${theme}>
                <${Icons.Center} />
            <//>
            <div className=${'h-px my-1 ' + (theme === 'light' ? 'bg-slate-200' : 'bg-slate-700')}></div>
            <${TooltipButton} onClick=${onToggleSeparate} tooltip=${separateOutputs ? "Merge Outputs" : "Separate Outputs"} isActive=${separateOutputs} theme=${theme}>
                ${separateOutputs ? html`<${Icons.MergeOutputs} />` : html`<${Icons.SplitOutputs} />`}
            <//>
            <${TooltipButton} onClick=${onToggleTypes} tooltip=${showTypes ? "Hide Types" : "Show Types"} isActive=${showTypes} theme=${theme}>
                <${Icons.Type} />
            <//>
            <div className=${'h-px my-1 ' + (theme === 'light' ? 'bg-slate-200' : 'bg-slate-700')}></div>
            <${TooltipButton} onClick=${onToggleTheme} tooltip=${theme === 'dark' ? "Switch to Light Theme" : "Switch to Dark Theme"} theme=${theme}>
                ${theme === 'dark' ? html`<${Icons.Sun} />` : html`<${Icons.Moon} />`}
            <//>
        <//>
    `;
  };

  // === OUTPUTS SECTION (combined outputs display in function nodes) ===
  var OutputsSection = function(props) {
    var outputs = props.outputs;
    var showTypes = props.showTypes;
    var isLight = props.isLight;
    if (!outputs || outputs.length === 0) return null;
    var bgClass = isLight ? "bg-slate-50/80" : "bg-slate-900/50";
    var textClass = isLight ? "text-slate-600" : "text-slate-400";
    var arrowClass = isLight ? "text-emerald-500" : "text-emerald-400";
    var typeClass = isLight ? "text-slate-400" : "text-slate-500";
    var borderClass = isLight ? "border-slate-100" : "border-slate-800/50";

    return html`
        <div className=${'px-2 py-2 border-t transition-colors duration-300 overflow-hidden ' + bgClass + ' ' + borderClass}>
            <div className="flex flex-col items-center gap-1.5">
                ${outputs.map(function(out) {
                    return html`
                        <div key=${out.name} className=${'flex items-center gap-1.5 text-xs max-w-full ' + textClass}>
                            <span className=${'shrink-0 ' + arrowClass}>→</span>
                            <span className="font-mono font-medium shrink-0">${out.name}</span>
                            ${showTypes && out.type ? html`<span className=${'font-mono truncate ' + typeClass} title=${out.type}>: ${truncateTypeHint(out.type)}</span>` : null}
                        </div>
                    `;
                })}
            </div>
        </div>
    `;
  };

  // === CUSTOM EDGE COMPONENT ===
  var CustomEdge = function(props) {
    var id = props.id;
    var sourceX = props.sourceX;
    var sourceY = props.sourceY;
    var targetX = props.targetX;
    var targetY = props.targetY;
    var sourcePosition = props.sourcePosition;
    var targetPosition = props.targetPosition;
    var style = props.style || {};
    var markerEnd = props.markerEnd;
    var label = props.label;
    var data = props.data;
    var source = props.source;
    var target = props.target;

    // Debug logging
    useEffect(function() {
      if (root.__hypergraph_debug_edges) {
        console.log('[Edge ' + id + '] source=' + source + ' target=' + target + ' points=' + ((data && data.points && data.points.length) || 0));
      }
    }, [id, sourceX, sourceY, targetX, targetY, source, target, data && data.points]);

    var showDebug = (data && data.debugMode) || root.__hypergraph_debug_overlays;

    // Use polyline path from constraint layout if available, otherwise fall back to bezier
    var edgePath, labelX, labelY;

    if (data && data.points && data.points.length > 0) {
      // Build SVG path using B-spline (curveBasis) - same algorithm as kedro-viz
      // Use constraint layout points directly - they already have correct coordinates
      // from either the constraint solver (internal edges) or Step 4 (cross-boundary)
      var points = data.points.slice();

      // Use our points directly for position calculations
      var startPt = points[0];
      var endPt = points[points.length - 1];

      // Simplify "mostly vertical" edges
      var dx = Math.abs(endPt.x - startPt.x);
      var dy = Math.abs(endPt.y - startPt.y);
      var isNearlyVertical = dx < 30 && dy > dx * 2;

      // curveBasis: B-spline interpolation
      var curveBasis = function(pts) {
        if (pts.length < 2) return 'M ' + pts[0].x + ' ' + pts[0].y;
        if (pts.length === 2) return 'M ' + pts[0].x + ' ' + pts[0].y + ' L ' + pts[1].x + ' ' + pts[1].y;

        var clamped = [pts[0]].concat(pts).concat([pts[pts.length - 1]]);
        var path = 'M ' + clamped[0].x + ' ' + clamped[0].y;
        var x0 = clamped[0].x, y0 = clamped[0].y;
        var x1 = clamped[1].x, y1 = clamped[1].y;

        path += ' L ' + ((5 * x0 + x1) / 6) + ' ' + ((5 * y0 + y1) / 6);

        for (var i = 2; i < clamped.length; i++) {
          var x = clamped[i].x, y = clamped[i].y;
          path += ' C ' + ((2 * x0 + x1) / 3) + ' ' + ((2 * y0 + y1) / 3) + ' ' +
                  ((x0 + 2 * x1) / 3) + ' ' + ((y0 + 2 * y1) / 3) + ' ' +
                  ((x0 + 4 * x1 + x) / 6) + ' ' + ((y0 + 4 * y1 + y) / 6);
          x0 = x1; y0 = y1;
          x1 = x; y1 = y;
        }

        path += ' C ' + ((2 * x0 + x1) / 3) + ' ' + ((2 * y0 + y1) / 3) + ' ' +
                ((x0 + 2 * x1) / 3) + ' ' + ((y0 + 2 * y1) / 3) + ' ' + x1 + ' ' + y1;

        return path;
      };

      if (isNearlyVertical) {
        // Use actual points for nearly-vertical edges (including re-routed ones)
        var midY = (startPt.y + endPt.y) / 2;
        edgePath = 'M ' + startPt.x + ' ' + startPt.y + ' C ' + startPt.x + ' ' + midY + ' ' + endPt.x + ' ' + midY + ' ' + endPt.x + ' ' + endPt.y;
      } else {
        edgePath = curveBasis(points);
      }

      // Position label at 35% along the path (closer to source) to keep it away from the arrow
      var labelPos = 0.35;
      var totalLength = points.length - 1;
      var labelIdx = Math.floor(totalLength * labelPos);
      var labelFrac = (totalLength * labelPos) - labelIdx;
      if (points.length > 1 && labelIdx < points.length - 1) {
        labelX = points[labelIdx].x + (points[labelIdx + 1].x - points[labelIdx].x) * labelFrac;
        labelY = points[labelIdx].y + (points[labelIdx + 1].y - points[labelIdx].y) * labelFrac;
      } else if (points.length > 1) {
        labelX = (points[0].x + points[1].x) / 2;
        labelY = (points[0].y + points[1].y) / 2;
      } else {
        labelX = points[0].x;
        labelY = points[0].y;
      }
    } else {
      var result = getBezierPath({
        sourceX: sourceX, sourceY: sourceY, sourcePosition: sourcePosition,
        targetX: targetX, targetY: targetY, targetPosition: targetPosition
      });
      edgePath = result[0];
      // Position label at 35% along the path (closer to source) to keep it away from the arrow
      labelX = sourceX + (targetX - sourceX) * 0.35;
      labelY = sourceY + (targetY - sourceY) * 0.35;
    }

    var edgeLabel = label || (data && data.label);
    var labelStyle = {};
    if (edgeLabel === 'True') {
      labelStyle = {
        background: 'rgba(16, 185, 129, 0.9)',
        border: '1px solid #34d399',
        color: '#ffffff',
        boxShadow: '0 2px 6px rgba(16, 185, 129, 0.3)',
      };
    } else if (edgeLabel === 'False') {
      labelStyle = {
        background: 'rgba(239, 68, 68, 0.9)',
        border: '1px solid #f87171',
        color: '#ffffff',
        boxShadow: '0 2px 6px rgba(239, 68, 68, 0.3)',
      };
    } else if (edgeLabel) {
      labelStyle = {
        background: 'rgba(15,23,42,0.9)',
        border: '1px solid #334155',
        color: '#cbd5e1',
        boxShadow: '0 2px 4px rgba(0,0,0,0.2)',
      };
    }

    return html`
      <${React.Fragment}>
        <${BaseEdge} path=${edgePath} markerEnd=${markerEnd} style=${style} />
        ${showDebug ? html`
          <circle cx=${sourceX} cy=${sourceY} r="5" fill="#22c55e" stroke="#15803d" strokeWidth="1" />
          <circle cx=${targetX} cy=${targetY} r="5" fill="#3b82f6" stroke="#1d4ed8" strokeWidth="1" />
          <${EdgeLabelRenderer}>
            <div
              style=${{
                position: 'absolute',
                transform: 'translate(-50%, -50%) translate(' + labelX + 'px,' + labelY + 'px)',
                pointerEvents: 'none',
              }}
              className="px-1.5 py-0.5 rounded bg-slate-900/95 border border-slate-600 text-[8px] text-slate-300 font-mono whitespace-nowrap"
            >
              RF:(${Math.round(sourceX)},${Math.round(sourceY)}) Pts:(${Math.round(startPt.x)},${Math.round(startPt.y)})
            </div>
          <//>
        ` : null}
        ${edgeLabel && !showDebug ? html`
          <${EdgeLabelRenderer}>
            <div
              style=${{
                position: 'absolute',
                transform: 'translate(-50%, -50%) translate(' + labelX + 'px,' + labelY + 'px)',
                pointerEvents: 'all',
                display: 'flex',
                alignItems: 'center',
                gap: '4px',
                padding: '3px 10px',
                borderRadius: '10px',
                fontSize: '10px',
                fontFamily: 'ui-monospace, monospace',
                fontWeight: '600',
                letterSpacing: '0.02em',
                ...labelStyle,
              }}
            >
              ${edgeLabel === 'True' ? html`
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="20 6 9 17 4 12"></polyline>
                </svg>
              ` : edgeLabel === 'False' ? html`
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18"></line>
                  <line x1="6" y1="6" x2="18" y2="18"></line>
                </svg>
              ` : null}
              ${edgeLabel}
            </div>
          <//>
        ` : null}
      <//>
    `;
  };

  // === CUSTOM NODE COMPONENT ===
  var CustomNode = function(props) {
    var data = props.data;
    var id = props.id;
    var isExpanded = data.isExpanded;
    var theme = data.theme || 'dark';
    var updateNodeInternals = useUpdateNodeInternals();
    var showDebug = data.debugMode || root.__hypergraph_debug_overlays;

    // Debug wrapper
    var DebugWrapper = function(wrapperProps) {
      if (!showDebug) return wrapperProps.children;
      return html`
        <div className="relative">
          <div className="absolute -inset-0.5 border-2 border-dashed border-red-500 rounded pointer-events-none z-50">
            <span className="absolute -top-4 left-0 text-[8px] bg-red-500 text-white px-1 rounded font-mono whitespace-nowrap">
              ${id}
            </span>
          </div>
          ${wrapperProps.children}
        </div>
      `;
    };

    // Style Configuration
    var colors = { bg: "slate", border: "slate", text: "slate", icon: "slate" };
    var Icon = Icons.Function;
    var labelType = "NODE";

    if (data.nodeType === 'PIPELINE') {
      colors = { bg: "amber", border: "amber", text: "amber", icon: "amber" };
      Icon = Icons.Pipeline;
      labelType = "PIPELINE";
    } else if (data.nodeType === 'DUAL') {
      colors = { bg: "fuchsia", border: "fuchsia", text: "fuchsia", icon: "fuchsia" };
      Icon = Icons.Dual;
      labelType = "DUAL NODE";
    } else if (data.nodeType === 'BRANCH') {
      colors = { bg: "yellow", border: "yellow", text: "yellow", icon: "yellow" };
      Icon = Icons.Branch;
      labelType = "BRANCH";
    } else if (data.nodeType === 'INPUT') {
      colors = { bg: "cyan", border: "cyan", text: "cyan", icon: "cyan" };
      Icon = Icons.Input;
      labelType = "INPUT";
    } else if (data.nodeType === 'DATA') {
      colors = { bg: "slate", border: "slate", text: "slate", icon: "slate" };
      Icon = Icons.Data;
      labelType = "DATA";
    } else if (data.nodeType === 'INPUT_GROUP') {
      colors = { bg: "cyan", border: "cyan", text: "cyan", icon: "cyan" };
      Icon = Icons.Input;
      labelType = "INPUT GROUP";
    } else {
      colors = { bg: "indigo", border: "indigo", text: "indigo", icon: "indigo" };
      Icon = Icons.Function;
      labelType = "FUNCTION";
    }

    useEffect(function() {
      updateNodeInternals(id);
    }, [
      id,
      data.separateOutputs,
      data.showTypes,
      data.outputs ? data.outputs.length : 0,
      data.inputs ? data.inputs.length : 0,
      isExpanded,
      theme,
    ]);

    // --- Render Data Node (Compact) ---
    if (data.nodeType === 'DATA') {
      var isLight = theme === 'light';
      var isOutput = data.sourceId != null;
      var showAsOutput = data.separateOutputs && isOutput;
      var showTypes = data.showTypes;
      var typeClass = isLight ? 'text-slate-400' : 'text-slate-500';
      var hasTypeHint = showTypes && data.typeHint;
      var displayTypeHint = truncateTypeHint(data.typeHint);
      return html`
          <div className=${'px-3 py-1.5 w-full relative rounded-full border shadow-sm flex items-center justify-center gap-2 transition-colors transition-shadow duration-200 hover:shadow-lg overflow-hidden' +
              (showAsOutput ? ' ring-2 ring-emerald-500/30' : '') +
              (isLight
                  ? ' bg-white border-slate-200 text-slate-700 shadow-slate-200 hover:border-slate-300'
                  : ' bg-slate-900 border-slate-700 text-slate-300 shadow-black/50 hover:border-slate-600')
          }>
               <span className=${'shrink-0 ' + (isLight ? 'text-slate-400' : 'text-slate-500')}><${Icon} /></span>
               <span className="text-xs font-mono font-medium shrink-0">${data.label}</span>
               ${hasTypeHint ? html`<span className=${'text-[10px] font-mono truncate min-w-0 ' + typeClass} title=${data.typeHint}>: ${displayTypeHint}</span>` : null}
               <${Handle} type="target" position=${Position.Top} className="!w-2 !h-2 !opacity-0" style=${{ top: '-2px' }} />
               <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" style=${{ bottom: '-2px' }} />
          </div>
      `;
    }

    // --- Render Input Node (Compact - styled as DATA) ---
    if (data.nodeType === 'INPUT') {
      var isLight = theme === 'light';
      var isBound = Boolean(data.isBound);
      var showTypes = data.showTypes;
      var typeHint = data.typeHint;
      var hasType = showTypes && typeHint;
      var typeClass = isLight ? 'text-slate-400' : 'text-slate-500';
      var displayType = truncateTypeHint(typeHint);
      return html`
          <div className=${'px-3 py-1.5 w-full relative rounded-full border shadow-sm flex items-center justify-center gap-2 transition-colors transition-shadow duration-200 hover:shadow-lg overflow-hidden' +
              (isBound ? ' border-dashed' : '') +
              (isLight
                  ? ' bg-white border-slate-200 text-slate-700 shadow-slate-200 hover:border-slate-300'
                  : ' bg-slate-900 border-slate-700 text-slate-300 shadow-black/50 hover:border-slate-600')
          }>
              <span className=${'shrink-0 ' + (isLight ? 'text-slate-400' : 'text-slate-500')}><${Icons.Data} /></span>
              <span className="text-xs font-mono font-medium shrink-0">${data.label}</span>
              ${hasType ? html`<span className=${'text-[10px] font-mono truncate min-w-0 ' + typeClass} title=${typeHint}>: ${displayType}</span>` : null}
              <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" style=${{ bottom: '-2px' }} />
          </div>
      `;
    }

    // --- Render Input Group Node ---
    if (data.nodeType === 'INPUT_GROUP') {
      var isLight = theme === 'light';
      var params = data.params || [];
      var paramTypes = data.paramTypes || [];
      var isBound = data.isBound;
      var showTypes = data.showTypes;
      var typeClass = isLight ? 'text-slate-400' : 'text-slate-500';

      return html`
          <div className=${'px-3 py-2 w-full relative rounded-xl border shadow-sm flex flex-col gap-1 transition-colors transition-shadow duration-200 hover:shadow-lg' +
              (isBound ? ' border-dashed' : '') +
              (isLight
                  ? ' bg-white border-slate-200 text-slate-700 shadow-slate-200 hover:border-slate-300'
                  : ' bg-slate-900 border-slate-700 text-slate-300 shadow-black/50 hover:border-slate-600')
          }>
              ${params.map(function(p, i) {
                  return html`
                      <div className="flex items-center gap-2 whitespace-nowrap">
                          <span className=${isLight ? 'text-slate-400' : 'text-slate-500'}><${Icons.Data} className="w-3 h-3" /></span>
                          <div className="text-xs font-mono leading-tight">${p}</div>
                          ${showTypes && paramTypes[i] ? html`<span className=${'text-[10px] font-mono ' + typeClass} title=${paramTypes[i]}>: ${truncateTypeHint(paramTypes[i])}</span>` : null}
                      </div>
                  `;
              })}
              <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" style=${{ bottom: '-2px' }} />
          </div>
      `;
    }

    // --- Render Branch Node (Diamond Shape) ---
    if (data.nodeType === 'BRANCH') {
      var isLight = theme === 'light';
      var hoverState = useState(false);
      var isHovered = hoverState[0];
      var setIsHovered = hoverState[1];

      var diamondBgColor = isLight ? '#ecfeff' : '#083344';
      var diamondBorderColor = isLight ? '#22d3ee' : 'rgba(6,182,212,0.6)';
      var diamondHoverBorderColor = isLight ? '#06b6d4' : 'rgba(34,211,238,0.8)';
      var glowColor = 'rgba(6,182,212,0.4)';
      var labelColor = isLight ? '#0e7490' : '#a5f3fc';
      var diamondTipOffset = '3px';

      return html`
        <${DebugWrapper}>
          <div className="relative flex items-center justify-center cursor-pointer"
               style=${{ width: '140px', height: '140px' }}
               onMouseEnter=${function() { setIsHovered(true); }}
               onMouseLeave=${function() { setIsHovered(false); }}
               onTransitionEnd=${function(e) { if (e.target === e.currentTarget) updateNodeInternals(id); }}>

            <div style=${{ filter: 'drop-shadow(0 10px 8px rgb(0 0 0 / 0.04)) drop-shadow(0 4px 3px rgb(0 0 0 / 0.1))' }}>
                <div className="transition-colors transition-shadow duration-200 ease-out border"
                     style=${{
                        width: '95px',
                        height: '95px',
                        transform: 'rotate(45deg)',
                        borderRadius: '10px',
                        backgroundColor: diamondBgColor,
                        borderColor: isHovered ? diamondHoverBorderColor : diamondBorderColor,
                        boxShadow: isHovered ? ('0 0 15px ' + glowColor) : '0 0 0 rgba(6,182,212,0)',
                     }}>
                </div>
            </div>

            <div style=${{
              position: 'absolute',
              inset: '0',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              pointerEvents: 'none',
              padding: '0 10px',
            }}>
              <span className="text-sm font-semibold text-center"
                    style=${{
                      color: labelColor,
                      maxWidth: '100%',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }} title=${data.label}>${data.label}</span>
            </div>

            <${Handle} type="target" position=${Position.Top} className="!w-2 !h-2 !opacity-0" style=${{ top: diamondTipOffset }} />
            <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" style=${{ bottom: diamondTipOffset }} id="branch-source" />
          </div>
        <//>
      `;
    }

    // --- Render Expanded Pipeline Group ---
    if (data.nodeType === 'PIPELINE' && isExpanded) {
      var isLight = theme === 'light';
      var handleCollapseClick = function(e) {
        e.stopPropagation();
        e.preventDefault();
        if (data.onToggleExpand) data.onToggleExpand();
      };

      return html`
        <div className=${'relative w-full h-full rounded-2xl border-2 border-dashed p-6 transition-colors duration-200' +
            (isLight
                ? ' border-amber-300 bg-amber-50/30'
                : ' border-amber-500/30 bg-amber-500/5')
        }>
          <button
               type="button"
               className=${'absolute -top-3 left-4 px-3 py-0.5 rounded-full text-xs font-bold uppercase tracking-wider flex items-center gap-2 cursor-pointer transition-colors z-10 whitespace-nowrap' +
                    (isLight
                        ? ' bg-amber-100 text-amber-700 hover:bg-amber-200 border border-amber-200'
                        : ' bg-slate-950 text-amber-400 hover:text-amber-300 border border-amber-500/50')
               }
               onClick=${handleCollapseClick}
               title=${data.label}>
            <${Icon} />
            ${truncateLabel(data.label)}
          </button>
          <${Handle} type="target" position=${Position.Top} className="!w-2 !h-2 !opacity-0" />
          <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" />
        </div>
      `;
    }

    // --- Render Standard Node ---
    var isLight = theme === 'light';
    var boundInputs = data.inputs ? data.inputs.filter(function(i) { return i.is_bound; }).length : 0;
    var outputs = data.outputs || [];
    var showCombined = !data.separateOutputs && outputs.length > 0;
    var showTypes = data.showTypes;

    return html`
      <div className=${'group relative w-full rounded-lg border shadow-lg backdrop-blur-sm transition-colors transition-shadow duration-200 cursor-pointer node-function-' + theme + ' overflow-hidden' +
           (isLight
             ? ' bg-white/90 border-' + colors.border + '-300 shadow-slate-200 hover:border-' + colors.border + '-400 hover:shadow-' + colors.border + '-200 hover:shadow-lg'
             : ' bg-slate-950/90 border-' + colors.border + '-500/40 shadow-black/50 hover:border-' + colors.border + '-500/70 hover:shadow-' + colors.border + '-500/20 hover:shadow-lg')
           }
           onClick=${data.nodeType === 'PIPELINE' ? function(e) { e.stopPropagation(); if(data.onToggleExpand) data.onToggleExpand(); } : undefined}>

        <div className=${'px-3 py-2.5 flex flex-col items-center justify-center' +
             (showCombined ? (isLight ? ' border-b border-slate-100' : ' border-b border-slate-800/50') : '')}>
          <div className=${'text-sm font-semibold truncate max-w-full text-center' +
               (isLight ? ' text-slate-800' : ' text-slate-100')} title=${data.label}>${truncateLabel(data.label)}</div>

          ${boundInputs > 0 ? html`
              <div className=${'absolute top-2 right-2 w-2 h-2 rounded-full ring-2 ring-offset-1' +
                  (isLight
                      ? ' bg-indigo-400 ring-indigo-100 ring-offset-white'
                      : ' bg-indigo-500 ring-indigo-500/30 ring-offset-slate-950')}
                   title="${boundInputs} bound inputs">
            </div>
          ` : null}
        </div>

        ${showCombined ? html`<${OutputsSection} outputs=${outputs} showTypes=${showTypes} isLight=${isLight} />` : null}

        <${Handle} type="target" position=${Position.Top} className="!w-2 !h-2 !opacity-0" />
        <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" />

        ${data.nodeType === 'PIPELINE' ? html`
           <div className="absolute -bottom-5 left-1/2 -translate-x-1/2 text-[9px] text-slate-400 opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap">
             Click to expand
           </div>
        ` : null}
      </div>
    `;
  };

  // Export API
  return {
    Icons: Icons,
    TooltipButton: TooltipButton,
    CustomControls: CustomControls,
    OutputsSection: OutputsSection,
    CustomEdge: CustomEdge,
    CustomNode: CustomNode,
    truncateTypeHint: truncateTypeHint,
    truncateLabel: truncateLabel,
    TYPE_HINT_MAX_CHARS: TYPE_HINT_MAX_CHARS,
    NODE_LABEL_MAX_CHARS: NODE_LABEL_MAX_CHARS
  };
});
