"""Presentation helpers for checkpointer notebook/HTML displays."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hypergraph.checkpointers.types import Run, StepRecord


def _safe_json_payload(payload: dict[str, Any]) -> str:
    """Serialize JSON safely for embedding inside a script tag."""
    return json.dumps(payload).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_checkpointer_explorer_html(
    *,
    title: str,
    path: str,
    state_key: str,
    run_count: int | None = None,
    step_count: int | None = None,
    size_bytes: int | None = None,
    runs: list[Run] | None = None,
    steps_by_run: dict[str, list[StepRecord]] | None = None,
    run_limit: int | None = None,
) -> str:
    """Render a drill-through explorer for a checkpointer."""
    from hypergraph._repr import (
        BORDER_COLOR,
        FONT_MONO_STYLE,
        MUTED_COLOR,
        PANEL_COLOR,
        TEXT_STRONG_COLOR,
        _code,
        html_kv,
        html_panel,
        theme_wrap,
        unique_dom_id,
    )

    runs = runs or []
    steps_by_run = steps_by_run or {}

    kvs = [html_kv("Path", _code(path))]
    if size_bytes is not None:
        size_mb = size_bytes / (1024 * 1024)
        kvs.append(html_kv("Size", f"{size_mb:.1f} MB" if size_mb >= 1 else f"{size_bytes / 1024:.0f} KB"))
    if run_count is not None:
        kvs.append(html_kv("Runs", str(run_count)))
    if step_count is not None:
        kvs.append(html_kv("Steps", str(step_count)))
    summary = " &nbsp;|&nbsp; ".join(kvs)

    payload = {
        "runs": [run.to_dict() for run in runs],
        "steps_by_run": {run_id: [step.to_dict() for step in run_steps] for run_id, run_steps in steps_by_run.items()},
        "initial_run_id": runs[0].id if runs else None,
        "run_limit": run_limit,
    }

    explorer_id = unique_dom_id("checkpointer-explorer", path)
    data_id = f"{explorer_id}-data"
    header_id = f"{explorer_id}-header"
    run_list_id = f"{explorer_id}-runs"
    empty_id = f"{explorer_id}-empty"
    summary_id = f"{explorer_id}-summary"
    panel_nav_id = f"{explorer_id}-nav"
    panel_body_id = f"{explorer_id}-body"

    panel_style = f"border:1px solid {BORDER_COLOR}; border-radius:10px; background:{PANEL_COLOR}; padding:12px; min-height:120px"
    mono = FONT_MONO_STYLE

    controls_note = "Select a run on the left, then drill into overview, steps, and lineage from one place."
    if run_limit is not None and run_count is not None and run_count > run_limit:
        controls_note += f" Showing the newest {run_limit} runs in the explorer."

    container = (
        f'<div id="{explorer_id}" data-hg-explorer="checkpointer" '
        f'style="display:flex; flex-direction:column; gap:12px">'
        f"{html_panel(title, summary)}"
        f'<div style="color:{MUTED_COLOR}; font-size:0.9em">{controls_note}</div>'
        f'<div style="display:grid; grid-template-columns:minmax(240px, 300px) minmax(420px, 1fr); gap:12px; align-items:start">'
        f'<section style="{panel_style}">'
        f'<div style="display:flex; justify-content:space-between; gap:8px; align-items:center; margin-bottom:8px">'
        f'<div data-hg-panel-title="Run Explorer" style="{mono}; font-weight:700; color:{TEXT_STRONG_COLOR}">Run Explorer</div>'
        f'<div style="color:{MUTED_COLOR}; font-size:0.85em">Explore: {_code(".runs()")} {_code(".steps(run_id)")} {_code(".search(query)")} {_code(".stats(run_id)")}</div>'
        f"</div>"
        f'<div id="{run_list_id}" style="display:flex; flex-direction:column; gap:8px"></div>'
        f'<div id="{empty_id}" style="display:none; color:{MUTED_COLOR}; {mono}">No runs available.</div>'
        f"</section>"
        f'<section style="display:flex; flex-direction:column; gap:12px">'
        f'<div id="{header_id}" style="{panel_style}"></div>'
        f'<div id="{summary_id}" style="display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:8px"></div>'
        f'<div id="{panel_nav_id}" style="display:flex; gap:8px; flex-wrap:wrap"></div>'
        f'<div id="{panel_body_id}" style="{panel_style}"></div>'
        f"</section>"
        f"</div>"
        f'<script type="application/json" id="{data_id}">{_safe_json_payload(payload)}</script>'
        f"<script>{_explorer_script(explorer_id, data_id, header_id, run_list_id, empty_id, summary_id, panel_nav_id, panel_body_id)}</script>"
        f"</div>"
    )
    return theme_wrap(container, state_key=state_key)


def _explorer_script(
    explorer_id: str,
    data_id: str,
    header_id: str,
    run_list_id: str,
    empty_id: str,
    summary_id: str,
    panel_nav_id: str,
    panel_body_id: str,
) -> str:
    """Inline JS for the checkpointer explorer."""
    status_colors = {
        "completed": ("#059669", "#ecfdf5"),
        "failed": ("#dc2626", "#fef2f2"),
        "active": ("#d97706", "#fffbeb"),
        "paused": ("#7c3aed", "#f5f3ff"),
        "partial": ("#d97706", "#fffbeb"),
        "cached": ("#2563eb", "#eff6ff"),
    }
    return f"""
(function(){{
  var root=document.getElementById({json.dumps(explorer_id)});
  var dataEl=document.getElementById({json.dumps(data_id)});
  if(!root||!dataEl||root.__hgExplorerBound) return;
  root.__hgExplorerBound=true;
  var headerEl=document.getElementById({json.dumps(header_id)});
  var runListEl=document.getElementById({json.dumps(run_list_id)});
  var emptyEl=document.getElementById({json.dumps(empty_id)});
  var summaryEl=document.getElementById({json.dumps(summary_id)});
  var navEl=document.getElementById({json.dumps(panel_nav_id)});
  var bodyEl=document.getElementById({json.dumps(panel_body_id)});
  var data=JSON.parse(dataEl.textContent||"{{}}");
  var runs=(data.runs||[]);
  var stepsByRun=(data.steps_by_run||{{}});
  var statusColors={json.dumps(status_colors)};
  var runsById={{}};
  var childrenByParent={{}};
  for(var i=0;i<runs.length;i++) {{
    var run=runs[i];
    runsById[run.id]=run;
    if(run.parent_run_id) {{
      if(!childrenByParent[run.parent_run_id]) childrenByParent[run.parent_run_id]=[];
      childrenByParent[run.parent_run_id].push(run.id);
    }}
  }}
  var state={{runId:data.initial_run_id||null, panel:"overview", stepIndex:null}};
  var esc=function(value){{
    return String(value===undefined||value===null?"":value)
      .replace(/&/g,"&amp;")
      .replace(/</g,"&lt;")
      .replace(/>/g,"&gt;")
      .replace(/"/g,"&quot;")
      .replace(/'/g,"&#39;");
  }};
  var fmtDuration=function(ms){{
    if(ms===null||ms===undefined||ms==="") return "—";
    var n=Number(ms);
    if(!isFinite(n)||n<=0) return "0ms";
    if(n<1000) return n.toFixed(n<10?2:(n<100?1:0))+"ms";
    return (n/1000).toFixed((n/1000)<10?2:1)+"s";
  }};
  var fmtDate=function(value){{
    if(!value) return "—";
    var d=new Date(value);
    if(isNaN(d.getTime())) return esc(value);
    return esc(d.toISOString().replace("T"," ").replace("Z"," UTC"));
  }};
  var badge=function(status){{
    var colors=statusColors[status]||["#6b7280","#f3f4f6"];
    return '<span style="display:inline-block; padding:2px 8px; border-radius:999px; font-size:0.85em; font-weight:600; color:'+colors[0]+'; background:'+colors[1]+'">'+esc(status)+'</span>';
  }};
  var runButton=function(run){{
    var active=state.runId===run.id;
    var stepCount=(stepsByRun[run.id]||[]).length;
    var childCount=(childrenByParent[run.id]||[]).length;
    return (
      '<button type="button" data-run-target="'+esc(run.id)+'" style="text-align:left; padding:10px 12px; border-radius:10px; border:1px solid '+(active?'#2563eb':'#e5e7eb')+'; background:'+(active?'#eff6ff':'#ffffff')+'; cursor:pointer">' +
      '<div style="display:flex; justify-content:space-between; gap:8px; align-items:center">' +
      '<span style="font-weight:700; font-family:ui-monospace, monospace; color:#111827">'+esc(run.id)+'</span>' +
      badge(run.status) +
      '</div>' +
      '<div style="margin-top:6px; color:#6b7280; font-size:0.88em">' +
      esc(run.graph_name||'unnamed') + ' | ' + stepCount + ' step' + (stepCount===1?'':'s') +
      (childCount?(' | '+childCount+' child run'+(childCount===1?'':'s')):'') +
      '</div>' +
      '</button>'
    );
  }};
  var relatedLineage=function(run){{
    var ids=[];
    var seen={{}};
    var current=run;
    while(current){{
      if(seen[current.id]) break;
      seen[current.id]=true;
      ids.unshift(current.id);
      var parentId=current.forked_from||current.retry_of;
      current=parentId ? runsById[parentId] : null;
    }}
    var descendants=[];
    for(var i=0;i<runs.length;i++) {{
      var candidate=runs[i];
      var p=candidate.forked_from||candidate.retry_of;
      if(p===run.id) descendants.push(candidate.id);
    }}
    return {{ancestors:ids, descendants:descendants}};
  }};
  var summaryCard=function(label, value){{
    return '<div style="border:1px solid #e5e7eb; border-radius:10px; background:#ffffff; padding:10px 12px">' +
      '<div style="color:#6b7280; font-size:0.82em">'+esc(label)+'</div>' +
      '<div style="margin-top:4px; color:#111827; font-weight:700">'+value+'</div></div>';
  }};
  var renderRunList=function(){{
    if(!runs.length){{
      runListEl.innerHTML='';
      emptyEl.style.display='block';
      return;
    }}
    emptyEl.style.display='none';
    runListEl.innerHTML=runs.map(runButton).join('');
  }};
  var renderHeader=function(run){{
    if(!run){{
      headerEl.innerHTML='<div style="color:#6b7280">No run selected.</div>';
      summaryEl.innerHTML='';
      navEl.innerHTML='';
      bodyEl.innerHTML='<div style="color:#6b7280">Select a run to inspect it.</div>';
      return;
    }}
    var lineageParent=run.forked_from||run.retry_of;
    headerEl.innerHTML =
      '<div style="display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; align-items:flex-start">' +
      '<div>' +
      '<div style="font-family:ui-monospace, monospace; font-size:1.05em; font-weight:700; color:#111827">'+esc(run.id)+'</div>' +
      '<div style="margin-top:6px; color:#6b7280">'+esc(run.graph_name||'unnamed graph')+'</div>' +
      '</div>' +
      '<div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center">' +
      badge(run.status) +
      (lineageParent?'<button type="button" data-run-target="'+esc(lineageParent)+'" style="padding:6px 10px; border-radius:8px; border:1px solid #e5e7eb; background:#ffffff; cursor:pointer">Open source</button>':'') +
      '</div></div>';
    var steps=(stepsByRun[run.id]||[]);
    var children=(childrenByParent[run.id]||[]);
    summaryEl.innerHTML = [
      summaryCard('Duration', esc(fmtDuration(run.duration_ms))),
      summaryCard('Steps', esc(String(steps.length||run.node_count||0))),
      summaryCard('Errors', esc(String(run.error_count||0))),
      summaryCard('Children', esc(String(children.length)))
    ].join('');
    var panels=[['overview','Overview'],['steps','Steps'],['lineage','Lineage']];
    navEl.innerHTML=panels.map(function(pair){{
      var active=state.panel===pair[0];
      return '<button type="button" data-panel="'+pair[0]+'" style="padding:6px 10px; border-radius:8px; border:1px solid '+(active?'#2563eb':'#e5e7eb')+'; background:'+(active?'#eff6ff':'#ffffff')+'; cursor:pointer">'+pair[1]+'</button>';
    }}).join('');
  }};
  var renderOverview=function(run){{
    var steps=(stepsByRun[run.id]||[]);
    var children=(childrenByParent[run.id]||[]);
    var items=[
      ['Created', fmtDate(run.created_at)],
      ['Completed', fmtDate(run.completed_at)],
      ['Parent Run', run.parent_run_id?'<button type="button" data-run-target="'+esc(run.parent_run_id)+'" style="padding:0; border:none; background:none; color:#2563eb; cursor:pointer; font-family:ui-monospace, monospace">'+esc(run.parent_run_id)+'</button>':'—'],
      ['Forked From', run.forked_from?'<button type="button" data-run-target="'+esc(run.forked_from)+'" style="padding:0; border:none; background:none; color:#2563eb; cursor:pointer; font-family:ui-monospace, monospace">'+esc(run.forked_from)+(run.fork_superstep!==null&&run.fork_superstep!==undefined?('@'+esc(run.fork_superstep)):'')+'</button>':'—'],
      ['Retry Of', run.retry_of?'<button type="button" data-run-target="'+esc(run.retry_of)+'" style="padding:0; border:none; background:none; color:#2563eb; cursor:pointer; font-family:ui-monospace, monospace">'+esc(run.retry_of)+'</button>':'—']
    ];
    var meta='<div style="display:grid; grid-template-columns:minmax(140px, 180px) 1fr; gap:8px 12px">';
    for(var i=0;i<items.length;i++) {{
      meta += '<div style="color:#6b7280">'+items[i][0]+'</div><div style="color:#111827">'+items[i][1]+'</div>';
    }}
    meta += '</div>';
    var childHtml = children.length
      ? '<div style="margin-top:12px"><div style="font-weight:700; margin-bottom:6px">Child Runs</div>' +
        children.map(function(id){{ var child=runsById[id]; return child?'<div style="margin:4px 0"><button type="button" data-run-target="'+esc(id)+'" style="padding:0; border:none; background:none; color:#2563eb; cursor:pointer; font-family:ui-monospace, monospace">'+esc(id)+'</button> '+badge(child.status)+'</div>':''; }}).join('') +
        '</div>'
      : '';
    var stepSummary = steps.length
      ? '<div style="margin-top:12px"><div style="font-weight:700; margin-bottom:6px">Recent Steps</div>' +
        steps.slice(0,5).map(function(step){{ return '<div style="margin:4px 0"><button type="button" data-panel="steps" data-step-index="'+esc(step.index)+'" style="padding:0; border:none; background:none; color:#2563eb; cursor:pointer">['+esc(step.index)+'] '+esc(step.node_name)+'</button> '+badge(step.cached?'cached':step.status)+'</div>'; }}).join('') +
        (steps.length>5?'<div style="color:#6b7280; margin-top:4px">+'+(steps.length-5)+' more steps</div>':'') + '</div>'
      : '<div style="margin-top:12px; color:#6b7280">No steps loaded for this run.</div>';
    return meta + childHtml + stepSummary;
  }};
  var renderSteps=function(run){{
    var steps=(stepsByRun[run.id]||[]);
    if(!steps.length) return '<div style="color:#6b7280">No steps loaded for this run.</div>';
    if(state.stepIndex===null) state.stepIndex=steps[0].index;
    var selected=null;
    for(var i=0;i<steps.length;i++) if(steps[i].index===state.stepIndex) selected=steps[i];
    if(!selected) selected=steps[0];
    var rows = steps.map(function(step){{
      var active=selected&&selected.index===step.index;
      return '<tr data-step-index="'+esc(step.index)+'" style="cursor:pointer; background:'+(active?'#eff6ff':'transparent')+'">' +
        '<td style="padding:6px 8px; border-bottom:1px solid #f3f4f6">'+esc(step.index)+'</td>' +
        '<td style="padding:6px 8px; border-bottom:1px solid #f3f4f6">'+esc(step.node_name)+'</td>' +
        '<td style="padding:6px 8px; border-bottom:1px solid #f3f4f6">'+badge(step.cached?'cached':step.status)+'</td>' +
        '<td style="padding:6px 8px; border-bottom:1px solid #f3f4f6">'+esc(fmtDuration(step.duration_ms))+'</td>' +
        '<td style="padding:6px 8px; border-bottom:1px solid #f3f4f6">'+esc(step.superstep)+'</td>' +
        '</tr>';
    }}).join('');
    var pretty=function(value){{ return esc(JSON.stringify(value, null, 2)); }};
    return '<div style="display:grid; grid-template-columns:minmax(280px, 1fr) minmax(260px, 1fr); gap:12px">' +
      '<div><table style="width:100%; border-collapse:collapse; font-size:0.92em"><thead><tr>' +
      '<th style="text-align:left; padding:6px 8px; border-bottom:2px solid #e5e7eb">#</th>' +
      '<th style="text-align:left; padding:6px 8px; border-bottom:2px solid #e5e7eb">Node</th>' +
      '<th style="text-align:left; padding:6px 8px; border-bottom:2px solid #e5e7eb">Status</th>' +
      '<th style="text-align:left; padding:6px 8px; border-bottom:2px solid #e5e7eb">Duration</th>' +
      '<th style="text-align:left; padding:6px 8px; border-bottom:2px solid #e5e7eb">Superstep</th>' +
      '</tr></thead><tbody>'+rows+'</tbody></table></div>' +
      '<div style="border:1px solid #e5e7eb; border-radius:10px; background:#ffffff; padding:10px 12px">' +
      '<div style="font-weight:700; margin-bottom:8px">Step Detail</div>' +
      '<div style="display:grid; grid-template-columns:110px 1fr; gap:6px 10px">' +
      '<div style="color:#6b7280">Node</div><div>'+esc(selected.node_name)+'</div>' +
      '<div style="color:#6b7280">Status</div><div>'+badge(selected.cached?'cached':selected.status)+'</div>' +
      '<div style="color:#6b7280">Superstep</div><div>'+esc(selected.superstep)+'</div>' +
      '<div style="color:#6b7280">Duration</div><div>'+esc(fmtDuration(selected.duration_ms))+'</div>' +
      '<div style="color:#6b7280">Decision</div><div>'+esc(selected.decision===null||selected.decision===undefined?'—':JSON.stringify(selected.decision))+'</div>' +
      '<div style="color:#6b7280">Child Run</div><div>'+(selected.child_run_id?'<button type="button" data-run-target="'+esc(selected.child_run_id)+'" style="padding:0; border:none; background:none; color:#2563eb; cursor:pointer; font-family:ui-monospace, monospace">'+esc(selected.child_run_id)+'</button>':'—')+'</div>' +
      '</div>' +
      '<div style="margin-top:10px">' +
      '<div style="font-weight:700; margin-bottom:4px">Input Versions</div>' +
      '<pre style="margin:0; padding:8px; border-radius:8px; background:#f8fafc; overflow:auto">'+pretty(selected.input_versions||{{}})+'</pre>' +
      '</div>' +
      '<div style="margin-top:10px">' +
      '<div style="font-weight:700; margin-bottom:4px">Values</div>' +
      '<pre style="margin:0; padding:8px; border-radius:8px; background:#f8fafc; overflow:auto">'+pretty(selected.values||{{}})+'</pre>' +
      '</div>' +
      '<div style="margin-top:10px">' +
      '<div style="font-weight:700; margin-bottom:4px">Error</div>' +
      '<pre style="margin:0; padding:8px; border-radius:8px; background:#f8fafc; overflow:auto">'+esc(selected.error||'')+'</pre>' +
      '</div></div></div>';
  }};
  var renderLineage=function(run){{
    var rel=relatedLineage(run);
    var ancestorHtml=rel.ancestors.length
      ? rel.ancestors.map(function(id){{
          var item=runsById[id];
          if(!item) return '';
          var selected=(id===run.id);
          return '<div style="margin:4px 0">'+(selected?'&rarr; ':'')+'<button type="button" data-run-target="'+esc(id)+'" style="padding:0; border:none; background:none; color:#2563eb; cursor:pointer; font-family:ui-monospace, monospace">'+esc(id)+'</button> '+badge(item.status)+'</div>';
        }}).join('')
      : '<div style="color:#6b7280">No lineage in loaded data.</div>';
    var descendantHtml=rel.descendants.length
      ? rel.descendants.map(function(id){{
          var item=runsById[id];
          return item?'<div style="margin:4px 0"><button type="button" data-run-target="'+esc(id)+'" style="padding:0; border:none; background:none; color:#2563eb; cursor:pointer; font-family:ui-monospace, monospace">'+esc(id)+'</button> '+badge(item.status)+'</div>':'';
        }}).join('')
      : '<div style="color:#6b7280">No direct fork/retry descendants in loaded data.</div>';
    return '<div style="display:grid; grid-template-columns:minmax(220px, 1fr) minmax(220px, 1fr); gap:12px">' +
      '<div style="border:1px solid #e5e7eb; border-radius:10px; background:#ffffff; padding:10px 12px"><div style="font-weight:700; margin-bottom:8px">Ancestry</div>'+ancestorHtml+'</div>' +
      '<div style="border:1px solid #e5e7eb; border-radius:10px; background:#ffffff; padding:10px 12px"><div style="font-weight:700; margin-bottom:8px">Descendants</div>'+descendantHtml+'</div>' +
      '</div>';
  }};
  var renderBody=function(run){{
    if(!run) {{
      bodyEl.innerHTML='<div style="color:#6b7280">Select a run to inspect it.</div>';
      return;
    }}
    if(state.panel==='steps') {{
      bodyEl.innerHTML=renderSteps(run);
      return;
    }}
    if(state.panel==='lineage') {{
      bodyEl.innerHTML=renderLineage(run);
      return;
    }}
    bodyEl.innerHTML=renderOverview(run);
  }};
  var render=function(){{
    renderRunList();
    var run=state.runId ? runsById[state.runId] : null;
    renderHeader(run);
    renderBody(run);
  }};
  root.addEventListener('click', function(event){{
    var runTarget=event.target.closest('[data-run-target]');
    if(runTarget){{
      state.runId=runTarget.getAttribute('data-run-target');
      state.stepIndex=null;
      render();
      return;
    }}
    var panelTarget=event.target.closest('[data-panel]');
    if(panelTarget){{
      state.panel=panelTarget.getAttribute('data-panel');
      var stepIndex=panelTarget.getAttribute('data-step-index');
      state.stepIndex=stepIndex!==null?Number(stepIndex):state.stepIndex;
      render();
      return;
    }}
    var stepTarget=event.target.closest('[data-step-index]');
    if(stepTarget){{
      state.panel='steps';
      state.stepIndex=Number(stepTarget.getAttribute('data-step-index'));
      render();
    }}
  }});
  render();
}})();
"""


def render_step_record_html(step: Any) -> str:
    """Render notebook HTML for a single StepRecord."""
    from hypergraph._repr import ERROR_COLOR, FONT_SANS_STYLE, MUTED_COLOR, duration_html, status_badge, theme_wrap, widget_state_key

    status = "cached" if step.cached else step.status.value
    dur = duration_html(step.duration_ms) if step.duration_ms > 0 else ""
    error = f' <span style="color:{ERROR_COLOR}; font-size:0.85em">{step.error[:80]}</span>' if step.error else ""
    return theme_wrap(
        f'<span style="{FONT_SANS_STYLE}; font-size:0.9em">'
        f"<b>[{step.index}] {step.node_name}</b> {status_badge(status)} {dur}"
        f' <span style="color:{MUTED_COLOR}">superstep {step.superstep}</span>'
        f"{error}</span>",
        state_key=widget_state_key("step-record", step.run_id, step.index, step.node_name, step.superstep),
    )


def render_run_html(run: Any) -> str:
    """Render notebook HTML for a single Run."""
    from hypergraph._repr import (
        ERROR_COLOR,
        MUTED_COLOR,
        _code,
        datetime_html,
        duration_html,
        html_kv,
        html_panel,
        status_badge,
        theme_wrap,
        widget_state_key,
    )

    kvs = [
        html_kv("Status", status_badge(run.status.value)),
        html_kv("Duration", duration_html(run.duration_ms)),
    ]
    if run.node_count:
        kvs.append(html_kv("Steps", str(run.node_count)))
    if run.error_count:
        kvs.append(html_kv("Errors", f'<span style="color:{ERROR_COLOR}; font-weight:600">{run.error_count}</span>'))
    if run.parent_run_id:
        kvs.append(html_kv("Parent", _code(run.parent_run_id)))
    if run.forked_from:
        label = run.forked_from if run.fork_superstep is None else f"{run.forked_from}@{run.fork_superstep}"
        kvs.append(html_kv("Forked From", _code(label)))
    if run.retry_of:
        label = run.retry_of if run.retry_index is None else f"{run.retry_of} (#{run.retry_index})"
        kvs.append(html_kv("Retry Of", _code(label)))
    kvs.append(html_kv("Created", datetime_html(run.created_at)))
    title = f"Run: {run.id}"
    if run.graph_name:
        title += f' <span style="color:{MUTED_COLOR}; font-weight:400">({run.graph_name})</span>'
    body = " &nbsp;|&nbsp; ".join(kvs)
    return theme_wrap(html_panel(title, body), state_key=widget_state_key("run", run.id))


def render_checkpoint_html(checkpoint: Any) -> str:
    """Render notebook HTML for a Checkpoint."""
    from hypergraph._repr import _code, html_panel, theme_wrap, widget_state_key

    keys = ", ".join(_code(k) for k in sorted(checkpoint.values.keys())[:10])
    if len(checkpoint.values) > 10:
        keys += f" ... (+{len(checkpoint.values) - 10} more)"
    body = f"<b>{len(checkpoint.values)}</b> values: {keys}<br><b>{len(checkpoint.steps)}</b> steps"
    return theme_wrap(
        html_panel("Checkpoint", body),
        state_key=widget_state_key("checkpoint", checkpoint.source_run_id or "ad-hoc", checkpoint.source_superstep),
    )


def render_run_table_html(table: Any) -> str:
    """Render notebook HTML for a RunTable."""
    from datetime import datetime, timezone

    from hypergraph._repr import (
        ERROR_COLOR,
        FONT_SANS_STYLE,
        MUTED_COLOR,
        _code,
        duration_html,
        html_detail,
        html_table,
        html_table_controls,
        html_table_controls_script,
        html_table_with_row_attrs,
        status_badge,
        theme_wrap,
        unique_dom_id,
        widget_state_key,
    )

    if not table:
        return theme_wrap(
            f'<div style="color:{MUTED_COLOR}; {FONT_SANS_STYLE}">RunTable: (empty)</div>',
            state_key=widget_state_key("run-table", "empty"),
        )

    def _utcnow():
        return datetime.now(timezone.utc)

    headers = ["ID", "Graph", "Status", "Duration", "Steps", "Errors"]

    def _group_key(run: Any) -> str:
        if run.parent_run_id:
            return run.parent_run_id
        if "/" in run.id:
            return run.id.split("/", 1)[0]
        return run.id

    def _synth_parent(group_id: str, members: list[Any]) -> Any:
        from hypergraph.checkpointers.types import Run, WorkflowStatus

        status = WorkflowStatus.COMPLETED
        if any(r.status == WorkflowStatus.ACTIVE for r in members):
            status = WorkflowStatus.ACTIVE
        elif any(r.status == WorkflowStatus.PAUSED for r in members):
            status = WorkflowStatus.PAUSED
        elif any(r.status == WorkflowStatus.FAILED for r in members):
            status = WorkflowStatus.FAILED
        created = max((r.created_at for r in members), default=_utcnow())
        return Run(
            id=group_id,
            status=status,
            graph_name=members[0].graph_name if members else None,
            duration_ms=max((r.duration_ms or 0.0 for r in members), default=0.0) if members else None,
            node_count=sum(r.node_count for r in members),
            error_count=sum(r.error_count for r in members),
            created_at=created,
        )

    by_id = {run.id: run for run in table}
    groups: dict[str, list[Any]] = {}
    for run in table:
        groups.setdefault(_group_key(run), []).append(run)

    row_items: list[tuple[Any, bool, str]] = []
    for group_id, members in groups.items():
        parent = by_id.get(group_id)
        children = sorted([r for r in members if r.id != group_id], key=lambda r: r.created_at, reverse=True)
        if parent is None:
            parent = _synth_parent(group_id, members)
        row_items.append((parent, False, group_id))
        for child in children:
            row_items.append((child, True, group_id))

    rows: list[list[str]] = []
    row_attrs: list[dict[str, str]] = []
    for run, is_child, _group_id in row_items:
        rows.append(
            [
                _code(run.id),
                run.graph_name or "—",
                status_badge(run.status.value),
                duration_html(run.duration_ms),
                str(run.node_count) if run.node_count else "—",
                f'<span style="color:{ERROR_COLOR}">{run.error_count}</span>' if run.error_count else "0",
            ]
        )
        row_attrs.append(
            {
                "data-id": run.id,
                "data-status": run.status.value,
                "data-parent": "1" if is_child else "0",
                "data-created-ts": str(run.created_at.timestamp()),
                "data-duration-ms": str(run.duration_ms or 0.0),
                "data-errors": str(run.error_count),
            }
        )

    detail_sections: list[str] = []
    for group_id, members in groups.items():
        parent = by_id.get(group_id)
        children = sorted([r for r in members if r.id != group_id], key=lambda r: r.created_at, reverse=True)
        if parent is None:
            parent = _synth_parent(group_id, members)

        summary = f"{_code(group_id)} — {status_badge(parent.status.value)}"
        if children:
            summary += f' <span style="color:{MUTED_COLOR}">({len(children)} child run{"s" if len(children) != 1 else ""})</span>'

        detail_parts = [render_run_html(parent)]
        if children:
            child_rows = [
                [
                    _code(c.id),
                    c.graph_name or "—",
                    status_badge(c.status.value),
                    duration_html(c.duration_ms),
                    str(c.node_count) if c.node_count else "—",
                    f'<span style="color:{ERROR_COLOR}">{c.error_count}</span>' if c.error_count else "0",
                ]
                for c in children
            ]
            detail_parts.append(html_table(["ID", "Graph", "Status", "Duration", "Steps", "Errors"], child_rows))

        run_steps = getattr(table, "_steps_by_run", {}).get(group_id)
        if run_steps:
            detail_parts.append(
                html_detail(
                    f"Steps ({len(run_steps)} step{'s' if len(run_steps) != 1 else ''})",
                    render_step_table_html(run_steps),
                    state_key=f"run-steps-{group_id}",
                )
            )

        detail_sections.append(html_detail(summary, "<br>".join(detail_parts), state_key=f"run-{group_id}"))

    run_ids = ",".join(run.id for run in table)
    table_id = unique_dom_id("run-table-ui", run_ids, len(row_items))
    view_id = f"{table_id}-view"
    status_id = f"{table_id}-status"
    sort_id = f"{table_id}-sort"
    show_id = f"{table_id}-show"

    controls = html_table_controls(
        view_id=view_id,
        status_id=status_id,
        sort_id=sort_id,
        show_id=show_id,
        show_options=[20, 50, 100],
        total_rows=len(row_items),
    )
    table_html = html_table_with_row_attrs(
        headers, rows, row_attrs=row_attrs, title=f"{len(table)} run{'s' if len(table) != 1 else ''}", table_id=table_id
    )
    script = html_table_controls_script(table_id=table_id, view_id=view_id, status_id=status_id, sort_id=sort_id, show_id=show_id)
    traces = html_detail("Run Traces", "".join(detail_sections), state_key="run-traces")
    body = controls + table_html + traces + script
    return theme_wrap(body, state_key=widget_state_key("run-table", run_ids))


def render_step_table_html(table: Any) -> str:
    """Render notebook HTML for a StepTable."""
    from hypergraph._repr import (
        FONT_SANS_STYLE,
        MUTED_COLOR,
        _code,
        datetime_html,
        duration_html,
        html_detail,
        html_kv,
        html_table,
        status_badge,
        theme_wrap,
        values_html,
        widget_state_key,
    )

    if not table:
        return theme_wrap(
            f'<div style="color:{MUTED_COLOR}; {FONT_SANS_STYLE}">StepTable: (empty)</div>',
            state_key=widget_state_key("step-table", "empty"),
        )
    headers = ["#", "Node", "Status", "Duration", "At", "Superstep"]
    rows = []
    for step in table:
        status = "cached" if step.cached else step.status.value
        rows.append(
            [
                str(step.index),
                _code(step.node_name),
                status_badge(status),
                duration_html(step.duration_ms) if step.duration_ms > 0 else "—",
                datetime_html(step.completed_at or step.created_at),
                str(step.superstep),
            ]
        )
    step_fingerprint = ",".join(f"{step.run_id}:{step.superstep}:{step.node_name}" for step in table)
    body = html_table(headers, rows, title=f"{len(table)} step{'s' if len(table) != 1 else ''}")
    for i, step in enumerate(table):
        status = "cached" if step.cached else step.status.value
        meta = " &nbsp;|&nbsp; ".join(
            [
                html_kv("Node", _code(step.node_name)),
                html_kv("Superstep", str(step.superstep)),
                html_kv("Status", status_badge(status)),
                html_kv("Duration", duration_html(step.duration_ms) if step.duration_ms > 0 else "—"),
            ]
        )
        content = meta
        if step.decision is not None:
            content += "<br>" + html_kv("Decision", _code(str(step.decision)))
        if step.error:
            content += "<br>" + html_kv("Error", _code(step.error))
        if step.values:
            content += "<br><br>" + html_detail("Values", values_html(step.values), state_key=f"values-{i}")
        body += html_detail(f"[{step.index}] {step.node_name}", content, state_key=f"step-{i}")
    return theme_wrap(body, state_key=widget_state_key("step-table", step_fingerprint))


def render_lineage_view_html(view: Any) -> str:
    """Render notebook HTML for a LineageView."""
    from hypergraph._repr import (
        _code,
        datetime_html,
        html_detail,
        html_kv,
        html_panel,
        html_table,
        status_badge,
        theme_wrap,
        widget_state_key,
    )

    if not view:
        return theme_wrap("<div>LineageView: (empty)</div>", state_key=widget_state_key("lineage", "empty"))

    headers = ["Lane", "Workflow", "Kind", "Status", "Fork Point", "Created", "Steps", "Cached", "Failed"]
    rows: list[list[str]] = []
    for row in view:
        run = row.run
        lane = _code(row.lane.rstrip() or "●")
        workflow = _code(run.id) + (" &nbsp;<b>(selected)</b>" if row.is_selected else "")
        fork_point = "root"
        if run.forked_from:
            at = f"@{run.fork_superstep}" if run.fork_superstep is not None else ""
            fork_point = _code(f"{run.forked_from}{at}")
        kind = "retry" if run.retry_of else ("fork" if run.forked_from else "root")
        n_steps = len(view.steps_by_run.get(run.id, [])) if view.steps_by_run else run.node_count
        cached = 0
        failed = 0
        if view.steps_by_run and run.id in view.steps_by_run:
            cached = sum(1 for s in view.steps_by_run[run.id] if s.cached)
            failed = sum(1 for s in view.steps_by_run[run.id] if s.status.value == "failed")
        rows.append(
            [
                lane,
                workflow,
                kind,
                status_badge(run.status.value),
                fork_point,
                datetime_html(run.created_at),
                str(n_steps) if n_steps else "0",
                str(cached),
                str(failed),
            ]
        )

    body = html_table(headers, rows, title=f"Lineage from root {view.root_run_id}")
    if view.steps_by_run:
        for row in view:
            run = row.run
            steps = view.steps_by_run.get(run.id)
            if steps is None:
                continue
            cached = sum(1 for s in steps if s.cached)
            failed = sum(1 for s in steps if s.status.value == "failed")
            kind = "retry" if run.retry_of else ("fork" if run.forked_from else "root")
            meta = " &nbsp;|&nbsp; ".join(
                [
                    html_kv("Kind", kind),
                    html_kv("Steps", str(len(steps))),
                    html_kv("Cached", str(cached)),
                    html_kv("Failed", str(failed)),
                ]
            )
            summary = f"{row.lane}{run.id} — {len(steps)} step{'s' if len(steps) != 1 else ''}, {cached} cached, {failed} failed"
            body += html_detail(summary, f"{meta}<br><br>{render_step_table_html(steps)}", state_key=f"steps-{run.id}")
    return theme_wrap(
        html_panel(f"Workflow Lineage: {view.selected_run_id}", body), state_key=widget_state_key("lineage", view.root_run_id, view.selected_run_id)
    )
