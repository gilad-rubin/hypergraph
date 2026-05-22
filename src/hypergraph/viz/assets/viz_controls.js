/**
 * Toolbar and debug controls for Hypergraph visualization.
 */
(function(root) {
  'use strict';

  var R = root.HypergraphVizRuntime;
  var Nodes = root.HypergraphVizNodes;
  if (!R || !Nodes) {
    console.error('HypergraphVizControls: Missing runtime or node module');
    return;
  }

  var html = R.html;
  var useState = R.useState;
  var useReactFlow = R.useReactFlow;
  var Panel = R.Panel;
  var Icons = Nodes.Icons;

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
        <${TooltipButton} onClick=${props.onToggleInputs} tooltip=${props.showInputs ? "Hide Inputs" : "Show Inputs"} isActive=${props.showInputs} theme=${props.theme}><${Icons.ExternalInputs} /><//>
        <${TooltipButton} onClick=${props.onToggleTypes} tooltip=${props.showTypes ? "Hide Types" : "Show Types"} isActive=${props.showTypes} theme=${props.theme}><${Icons.Type} /><//>
        <div className=${'h-px my-1 ' + (props.theme === 'light' ? 'bg-slate-200' : 'bg-slate-700')}></div>
        <${TooltipButton} onClick=${props.onToggleTheme} tooltip=${props.theme === 'dark' ? "Switch to Light Theme" : "Switch to Dark Theme"} theme=${props.theme}>
          ${props.theme === 'dark' ? html`<${Icons.Sun} />` : html`<${Icons.Moon} />`}
        <//>
      <//>`;
  };

  // ── Dev-only layout controls (DialKit) ──

  var DevLayoutControls = function(props) {
    var isLight = props.theme === 'light';
    var bg = isLight ? 'bg-white/90 border-slate-300' : 'bg-slate-900/90 border-slate-700';
    var text = isLight ? 'text-slate-700' : 'text-slate-300';
    var muted = isLight ? 'text-slate-500' : 'text-slate-500';
    var accent = isLight ? 'accent-indigo-500' : 'accent-indigo-400';

    return html`
      <${Panel} position="top-left" className=${'p-3 rounded-lg border shadow-lg backdrop-blur-sm ' + bg}>
        <div className=${'text-xs font-semibold mb-2 ' + muted}>Dagre Layout</div>
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

  var Controls = {
    TooltipButton: TooltipButton,
    CustomControls: CustomControls,
    DevLayoutControls: DevLayoutControls,
  };

  root.HypergraphVizControls = Controls;
  root.HypergraphViz = root.HypergraphViz || {};
  root.HypergraphViz.Controls = Controls;
})(typeof window !== 'undefined' ? window : this);
