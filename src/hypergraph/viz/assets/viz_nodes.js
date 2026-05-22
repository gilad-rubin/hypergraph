/**
 * Node rendering for Hypergraph visualization.
 */
(function(root) {
  'use strict';

  var R = root.HypergraphVizRuntime;
  if (!R) {
    console.error('HypergraphVizNodes: Missing HypergraphVizRuntime');
    return;
  }

  var html = R.html;
  var useState = R.useState;
  var useEffect = R.useEffect;
  var useUpdateNodeInternals = R.useUpdateNodeInternals;
  var Handle = R.Handle;
  var Position = R.Position;
  var truncateTypeHint = R.truncateTypeHint;
  var truncateLabel = R.truncateLabel;
  var getOffset = R.getOffset;
  var getTopInset = R.getTopInset;

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
    ExternalInputs: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><path d="M4 7h16"></path><path d="M4 12h10"></path><path d="M4 17h7"></path><circle cx="18" cy="12" r="3"></circle></svg>`; },
    Type: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4"><polyline points="4 7 4 4 20 4 20 7"></polyline><line x1="9" y1="20" x2="15" y2="20"></line><line x1="12" y1="4" x2="12" y2="20"></line></svg>`; },
    Start: function() { return html`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-3 h-3"><circle cx="12" cy="12" r="10"></circle><polygon points="10 8 16 12 10 16 10 8" fill="currentColor"></polygon></svg>`; },
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
    var hoverState = useState(false);

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

    // ── START node ──
    if (nodeType === 'START') {
      var startTone = isLight
        ? { borderColor: '#7dd3fc', color: '#0369a1' }
        : { borderColor: 'rgba(14, 165, 233, 0.5)', color: '#38bdf8' };
      return html`
        <div className="w-full h-full relative" style=${wrapStyle}>
          <div className=${'px-3 py-2 w-full h-full relative rounded-full border shadow-sm flex items-center justify-center gap-2 transition-colors transition-shadow duration-200 hover:shadow-lg' + (isLight ? ' bg-white shadow-slate-200' : ' bg-slate-900 shadow-black/50')}
               style=${startTone}>
            <${Icons.Start} /> <span className="text-xs font-semibold uppercase tracking-wide">Start</span>
          </div>
          <${Handle} type="source" position=${Position.Bottom} className="!w-2 !h-2 !opacity-0" style=${srcStyle} />
        </div>`;
    }

    // ── END node ──
    if (nodeType === 'END') {
      var endTone = isLight
        ? { borderColor: '#6ee7b7', color: '#047857' }
        : { borderColor: 'rgba(16, 185, 129, 0.5)', color: '#34d399' };
      return html`
        <div className="w-full h-full relative" style=${wrapStyle}>
          <div className=${'px-3 py-2 w-full h-full relative rounded-full border shadow-sm flex items-center justify-center gap-2 transition-colors transition-shadow duration-200 hover:shadow-lg' + (isLight ? ' bg-white shadow-slate-200' : ' bg-slate-900 shadow-black/50')}
               style=${endTone}>
            <${Icons.End} /> <span className="text-xs font-semibold uppercase tracking-wide">End</span>
          </div>
          <${Handle} type="target" position=${Position.Top} className="!w-2 !h-2 !opacity-0" style=${tgtStyle} />
        </div>`;
    }

    // ── BRANCH node (diamond) ──
    if (nodeType === 'BRANCH') {
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

  var Nodes = {
    Icons: Icons,
    OutputsSection: OutputsSection,
    getSourceHandleStyle: getSourceHandleStyle,
    getTargetHandleStyle: getTargetHandleStyle,
    CustomNode: CustomNode,
  };

  root.HypergraphVizNodes = Nodes;
  root.HypergraphViz = root.HypergraphViz || {};
  root.HypergraphViz.Nodes = Nodes;
})(typeof window !== 'undefined' ? window : this);
