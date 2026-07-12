/* hypergraph-checkpointer-explorer */
(function(){
  "use strict";
  var script=document.currentScript;
  var root=script&&script.parentElement;
  if(!root) throw new Error("Checkpointer explorer asset has no owning root.");
  if(root.__hgExplorerBound) return;
  var dataEl=root.querySelector("[data-hg-explorer-data]");
  var configEl=root.querySelector("[data-hg-explorer-config]");
  var headerEl=root.querySelector("[data-hg-explorer-header]");
  var runListEl=root.querySelector("[data-hg-explorer-runs]");
  var emptyEl=root.querySelector("[data-hg-explorer-empty]");
  var summaryEl=root.querySelector("[data-hg-explorer-summary]");
  var navEl=root.querySelector("[data-hg-explorer-nav]");
  var bodyEl=root.querySelector("[data-hg-explorer-body]");
  if(!dataEl||!configEl||!headerEl||!runListEl||!emptyEl||!summaryEl||!navEl||!bodyEl) {
    throw new Error("Checkpointer explorer markup is incomplete.");
  }
  var data=JSON.parse(dataEl.textContent||"{}");
  var config=JSON.parse(configEl.textContent||"{}");
  var hasOwn=Object.prototype.hasOwnProperty;
  var ownValue=function(object, key){
    return object&&hasOwn.call(object,key)?object[key]:undefined;
  };
  var runs=Array.isArray(data.runs)?data.runs:[];
  var stepsByRun=data.steps_by_run&&typeof data.steps_by_run==="object"?data.steps_by_run:null;
  var stepsFor=function(runId){
    var steps=ownValue(stepsByRun,runId);
    return Array.isArray(steps)?steps:[];
  };
  var statusColors=config.status_palette;
  var defaultStatusColors=config.default_status_colors;
  if(!statusColors||!defaultStatusColors) {
    throw new Error("Checkpointer explorer status palette is missing.");
  }
  var runsById=Object.create(null);
  var childrenByParent=Object.create(null);
  var runFor=function(runId){
    return ownValue(runsById,runId)||null;
  };
  var childrenFor=function(runId){
    var children=ownValue(childrenByParent,runId);
    return Array.isArray(children)?children:[];
  };
  for(var i=0;i<runs.length;i++) {
    var run=runs[i];
    runsById[run.id]=run;
    if(run.parent_run_id) {
      if(!hasOwn.call(childrenByParent,run.parent_run_id)) childrenByParent[run.parent_run_id]=[];
      childrenByParent[run.parent_run_id].push(run.id);
    }
  }
  var state={runId:data.initial_run_id||null, panel:"overview", stepIndex:null};
  var esc=function(value){
    return String(value===undefined||value===null?"":value)
      .replace(/&/g,"&amp;")
      .replace(/</g,"&lt;")
      .replace(/>/g,"&gt;")
      .replace(/"/g,"&quot;")
      .replace(/'/g,"&#39;");
  };
  var fmtDuration=function(ms){
    if(ms===null||ms===undefined||ms==="") return "—";
    var n=Number(ms);
    if(!isFinite(n)||n<=0) return "0ms";
    if(n<1000) return n.toFixed(n<10?2:(n<100?1:0))+"ms";
    return (n/1000).toFixed((n/1000)<10?2:1)+"s";
  };
  var fmtDate=function(value){
    if(!value) return "—";
    var d=new Date(value);
    if(isNaN(d.getTime())) return esc(value);
    return esc(d.toISOString().replace("T"," ").replace("Z"," UTC"));
  };
  var badge=function(status){
    var colors=statusColors[status]||defaultStatusColors;
    return '<span style="display:inline-block; padding:2px 8px; border-radius:999px; font-size:0.85em; font-weight:600; color:'+colors[0]+'; background:'+colors[1]+'">'+esc(status)+'</span>';
  };
  var runButton=function(run){
    var active=state.runId===run.id;
    var stepCount=stepsFor(run.id).length;
    var childCount=childrenFor(run.id).length;
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
  };
  var relatedLineage=function(run){
    var ids=[];
    var seen=Object.create(null);
    var current=run;
    while(current){
      if(seen[current.id]) break;
      seen[current.id]=true;
      ids.unshift(current.id);
      var parentId=current.forked_from||current.retry_of;
      current=parentId?runFor(parentId):null;
    }
    var descendants=[];
    for(var i=0;i<runs.length;i++) {
      var candidate=runs[i];
      var p=candidate.forked_from||candidate.retry_of;
      if(p===run.id) descendants.push(candidate.id);
    }
    return {ancestors:ids, descendants:descendants};
  };
  var summaryCard=function(label, value){
    return '<div style="border:1px solid #e5e7eb; border-radius:10px; background:#ffffff; padding:10px 12px">' +
      '<div style="color:#6b7280; font-size:0.82em">'+esc(label)+'</div>' +
      '<div style="margin-top:4px; color:#111827; font-weight:700">'+value+'</div></div>';
  };
  var renderRunList=function(){
    if(!runs.length){
      runListEl.innerHTML='';
      emptyEl.style.display='block';
      return;
    }
    emptyEl.style.display='none';
    runListEl.innerHTML=runs.map(runButton).join('');
  };
  var renderHeader=function(run){
    if(!run){
      headerEl.innerHTML='<div style="color:#6b7280">No run selected.</div>';
      summaryEl.innerHTML='';
      navEl.innerHTML='';
      bodyEl.innerHTML='<div style="color:#6b7280">Select a run to inspect it.</div>';
      return;
    }
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
    var steps=stepsFor(run.id);
    var children=childrenFor(run.id);
    summaryEl.innerHTML = [
      summaryCard('Duration', esc(fmtDuration(run.duration_ms))),
      summaryCard('Steps', esc(String(steps.length||run.node_count||0))),
      summaryCard('Errors', esc(String(run.error_count||0))),
      summaryCard('Children', esc(String(children.length)))
    ].join('');
    var panels=[['overview','Overview'],['steps','Steps'],['lineage','Lineage']];
    navEl.innerHTML=panels.map(function(pair){
      var active=state.panel===pair[0];
      return '<button type="button" data-panel="'+pair[0]+'" style="padding:6px 10px; border-radius:8px; border:1px solid '+(active?'#2563eb':'#e5e7eb')+'; background:'+(active?'#eff6ff':'#ffffff')+'; cursor:pointer">'+pair[1]+'</button>';
    }).join('');
  };
  var renderOverview=function(run){
    var steps=stepsFor(run.id);
    var children=childrenFor(run.id);
    var items=[
      ['Created', fmtDate(run.created_at)],
      ['Completed', fmtDate(run.completed_at)],
      ['Parent Run', run.parent_run_id?'<button type="button" data-run-target="'+esc(run.parent_run_id)+'" style="padding:0; border:none; background:none; color:#2563eb; cursor:pointer; font-family:ui-monospace, monospace">'+esc(run.parent_run_id)+'</button>':'—'],
      ['Forked From', run.forked_from?'<button type="button" data-run-target="'+esc(run.forked_from)+'" style="padding:0; border:none; background:none; color:#2563eb; cursor:pointer; font-family:ui-monospace, monospace">'+esc(run.forked_from)+(run.fork_superstep!==null&&run.fork_superstep!==undefined?('@'+esc(run.fork_superstep)):'')+'</button>':'—'],
      ['Retry Of', run.retry_of?'<button type="button" data-run-target="'+esc(run.retry_of)+'" style="padding:0; border:none; background:none; color:#2563eb; cursor:pointer; font-family:ui-monospace, monospace">'+esc(run.retry_of)+'</button>':'—']
    ];
    var meta='<div style="display:grid; grid-template-columns:minmax(140px, 180px) 1fr; gap:8px 12px">';
    for(var i=0;i<items.length;i++) {
      meta += '<div style="color:#6b7280">'+items[i][0]+'</div><div style="color:#111827">'+items[i][1]+'</div>';
    }
    meta += '</div>';
    var childHtml = children.length
      ? '<div style="margin-top:12px"><div style="font-weight:700; margin-bottom:6px">Child Runs</div>' +
        children.map(function(id){ var child=runFor(id); return child?'<div style="margin:4px 0"><button type="button" data-run-target="'+esc(id)+'" style="padding:0; border:none; background:none; color:#2563eb; cursor:pointer; font-family:ui-monospace, monospace">'+esc(id)+'</button> '+badge(child.status)+'</div>':''; }).join('') +
        '</div>'
      : '';
    var stepSummary = steps.length
      ? '<div style="margin-top:12px"><div style="font-weight:700; margin-bottom:6px">Recent Steps</div>' +
        steps.slice(0,5).map(function(step){ return '<div style="margin:4px 0"><button type="button" data-panel="steps" data-step-index="'+esc(step.index)+'" style="padding:0; border:none; background:none; color:#2563eb; cursor:pointer">['+esc(step.index)+'] '+esc(step.node_name)+'</button> '+badge(step.cached?'cached':step.status)+'</div>'; }).join('') +
        (steps.length>5?'<div style="color:#6b7280; margin-top:4px">+'+(steps.length-5)+' more steps</div>':'') + '</div>'
      : '<div style="margin-top:12px; color:#6b7280">No steps loaded for this run.</div>';
    return meta + childHtml + stepSummary;
  };
  var renderSteps=function(run){
    var steps=stepsFor(run.id);
    if(!steps.length) return '<div style="color:#6b7280">No steps loaded for this run.</div>';
    if(state.stepIndex===null) state.stepIndex=steps[0].index;
    var selected=null;
    for(var i=0;i<steps.length;i++) if(steps[i].index===state.stepIndex) selected=steps[i];
    if(!selected) selected=steps[0];
    var rows = steps.map(function(step){
      var active=selected&&selected.index===step.index;
      return '<tr data-step-index="'+esc(step.index)+'" style="cursor:pointer; background:'+(active?'#eff6ff':'transparent')+'">' +
        '<td style="padding:6px 8px; border-bottom:1px solid #f3f4f6">'+esc(step.index)+'</td>' +
        '<td style="padding:6px 8px; border-bottom:1px solid #f3f4f6">'+esc(step.node_name)+'</td>' +
        '<td style="padding:6px 8px; border-bottom:1px solid #f3f4f6">'+badge(step.cached?'cached':step.status)+'</td>' +
        '<td style="padding:6px 8px; border-bottom:1px solid #f3f4f6">'+esc(fmtDuration(step.duration_ms))+'</td>' +
        '<td style="padding:6px 8px; border-bottom:1px solid #f3f4f6">'+esc(step.superstep)+'</td>' +
        '</tr>';
    }).join('');
    var pretty=function(value){ return esc(JSON.stringify(value, null, 2)); };
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
      '<pre style="margin:0; padding:8px; border-radius:8px; background:#f8fafc; overflow:auto">'+pretty(selected.input_versions||{})+'</pre>' +
      '</div>' +
      '<div style="margin-top:10px">' +
      '<div style="font-weight:700; margin-bottom:4px">Values</div>' +
      '<pre style="margin:0; padding:8px; border-radius:8px; background:#f8fafc; overflow:auto">'+pretty(selected.values||{})+'</pre>' +
      '</div>' +
      '<div style="margin-top:10px">' +
      '<div style="font-weight:700; margin-bottom:4px">Error</div>' +
      '<pre style="margin:0; padding:8px; border-radius:8px; background:#f8fafc; overflow:auto">'+esc(selected.error||'')+'</pre>' +
      '</div></div></div>';
  };
  var renderLineage=function(run){
    var rel=relatedLineage(run);
    var ancestorHtml=rel.ancestors.length
      ? rel.ancestors.map(function(id){
          var item=runFor(id);
          if(!item) return '';
          var selected=(id===run.id);
          return '<div style="margin:4px 0">'+(selected?'&rarr; ':'')+'<button type="button" data-run-target="'+esc(id)+'" style="padding:0; border:none; background:none; color:#2563eb; cursor:pointer; font-family:ui-monospace, monospace">'+esc(id)+'</button> '+badge(item.status)+'</div>';
        }).join('')
      : '<div style="color:#6b7280">No lineage in loaded data.</div>';
    var descendantHtml=rel.descendants.length
      ? rel.descendants.map(function(id){
          var item=runFor(id);
          return item?'<div style="margin:4px 0"><button type="button" data-run-target="'+esc(id)+'" style="padding:0; border:none; background:none; color:#2563eb; cursor:pointer; font-family:ui-monospace, monospace">'+esc(id)+'</button> '+badge(item.status)+'</div>':'';
        }).join('')
      : '<div style="color:#6b7280">No direct fork/retry descendants in loaded data.</div>';
    return '<div style="display:grid; grid-template-columns:minmax(220px, 1fr) minmax(220px, 1fr); gap:12px">' +
      '<div style="border:1px solid #e5e7eb; border-radius:10px; background:#ffffff; padding:10px 12px"><div style="font-weight:700; margin-bottom:8px">Ancestry</div>'+ancestorHtml+'</div>' +
      '<div style="border:1px solid #e5e7eb; border-radius:10px; background:#ffffff; padding:10px 12px"><div style="font-weight:700; margin-bottom:8px">Descendants</div>'+descendantHtml+'</div>' +
      '</div>';
  };
  var renderBody=function(run){
    if(!run) {
      bodyEl.innerHTML='<div style="color:#6b7280">Select a run to inspect it.</div>';
      return;
    }
    if(state.panel==='steps') {
      bodyEl.innerHTML=renderSteps(run);
      return;
    }
    if(state.panel==='lineage') {
      bodyEl.innerHTML=renderLineage(run);
      return;
    }
    bodyEl.innerHTML=renderOverview(run);
  };
  var render=function(){
    renderRunList();
    var run=state.runId?runFor(state.runId):null;
    renderHeader(run);
    renderBody(run);
  };
  var onClick=function(event){
    var runTarget=event.target.closest('[data-run-target]');
    if(runTarget){
      state.runId=runTarget.getAttribute('data-run-target');
      state.stepIndex=null;
      render();
      return;
    }
    var panelTarget=event.target.closest('[data-panel]');
    if(panelTarget){
      state.panel=panelTarget.getAttribute('data-panel');
      var stepIndex=panelTarget.getAttribute('data-step-index');
      state.stepIndex=stepIndex!==null?Number(stepIndex):state.stepIndex;
      render();
      return;
    }
    var stepTarget=event.target.closest('[data-step-index]');
    if(stepTarget){
      state.panel='steps';
      state.stepIndex=Number(stepTarget.getAttribute('data-step-index'));
      render();
    }
  };
  render();
  root.addEventListener('click',onClick);
  root.__hgExplorerBound=true;
})();
