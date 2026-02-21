/**
 * Layout engine — dagre positions nodes, we add edge stems.
 * Exports: window.ConstraintLayout, window.HypergraphVizLayout
 */
(function(root) {
  'use strict';
  var dagre = root.dagre, React = root.React, V = root.HypergraphVizConstants || {};
  if (!dagre || !React) { root.ConstraintLayout = {}; root.HypergraphVizLayout = {}; return; }

  // --- Node sizing constants ---
  var THC = V.TYPE_HINT_MAX_CHARS||25, NLC = V.NODE_LABEL_MAX_CHARS||25, CW = V.CHAR_WIDTH_PX||7;
  var NBP = V.NODE_BASE_PADDING||52, FBP = V.FUNCTION_NODE_BASE_PADDING||48, MNW = V.MAX_NODE_WIDTH||280;
  var GP = V.GRAPH_PADDING||24, HH = V.HEADER_HEIGHT||32, VG = V.VERTICAL_GAP||95;
  var OFF = V.NODE_TYPE_OFFSETS || { PIPELINE:26,GRAPH:26,FUNCTION:14,DATA:6,INPUT:6,INPUT_GROUP:6,BRANCH:10 };
  var INS = V.NODE_TYPE_TOP_INSETS || { PIPELINE:0,GRAPH:0,FUNCTION:0,DATA:0,INPUT:0,INPUT_GROUP:0,BRANCH:3,END:0 };

  // --- Visible bounds (discount shadow/glow) ---
  function ntype(n) { var t=(n.data&&n.data.nodeType)||'FUNCTION'; return t==='PIPELINE'&&n.data&&!n.data.isExpanded?'FUNCTION':t; }
  function vBot(n) { return n.y + n.height/2 - (OFF[ntype(n)]||10); }
  function vTop(n) { return n.y - n.height/2 + (INS[ntype(n)]||0); }

  // --- Node sizing ---
  function calcDims(n) {
    var w=80, h=90, d=n.data||{}, t=d.nodeType;
    if (t==='DATA'||t==='INPUT') {
      h=36; var ll=Math.min((d.label||'').length,NLC), tl=(d.showTypes&&d.typeHint)?Math.min(d.typeHint.length,THC)+2:0;
      w=Math.min(MNW,(ll+tl)*CW+NBP);
    } else if (t==='INPUT_GROUP') {
      var ps=d.params||[], pt=d.paramTypes||[], ml=0;
      ps.forEach(function(p,i){ var v=3+Math.min(p.length,NLC)+((d.showTypes&&pt[i])?Math.min(pt[i].length,THC)+2:0); if(v>ml)ml=v; });
      w=Math.min(MNW,Math.max(ml,6)*CW+32); h=16+(Math.max(1,ps.length)*16)+((Math.max(1,ps.length)-1)*4)+(OFF.INPUT_GROUP||6);
    } else if (t==='BRANCH') { w=140;h=140; } else {
      var ll=Math.min((d.label||'').length,NLC), mc=ll, oo=d.outputs||[];
      if(!d.separateOutputs&&oo.length>0) oo.forEach(function(o){
        var v=Math.min((o.name||o.label||'').length,NLC)+((d.showTypes&&(o.type||o.typeHint))?Math.min((o.type||o.typeHint).length,THC)+2:0)+4;
        if(v>mc)mc=v;
      });
      w=Math.min(MNW,mc*CW+FBP); h=56;
      if(!d.separateOutputs&&oo.length>0) h=56+16+(oo.length*16)+(Math.max(0,oo.length-1)*6)+6;
    }
    if(n.style&&n.style.width) w=n.style.width; if(n.style&&n.style.height) h=n.style.height;
    return {width:w,height:h};
  }

  // --- Layout: dagre + centering + edge routing ---
  function layout(nodes, edges, padding) {
    var g = new dagre.graphlib.Graph();
    g.setGraph({rankdir:'TB', nodesep:42, ranksep:VG});
    g.setDefaultEdgeLabel(function(){return {};});
    var ids = new Set();
    nodes.forEach(function(n){ g.setNode(n.id,{width:n.width,height:n.height}); ids.add(n.id); });
    edges.forEach(function(e){ if(ids.has(e.source)&&ids.has(e.target)) g.setEdge(e.source,e.target); });
    dagre.layout(g);

    var byId = {};
    nodes.forEach(function(n){ var p=g.node(n.id); n.x=p.x; n.y=p.y; n.targets=[]; n.sources=[]; byId[n.id]=n; });

    // --- Center fan-out targets under their source ---
    var tgtMap = {};
    edges.forEach(function(e){
      var s=byId[e.source], t=byId[e.target];
      if(s&&t){ if(!tgtMap[e.source]) tgtMap[e.source]=[]; tgtMap[e.source].push(t); }
    });
    Object.keys(tgtMap).forEach(function(sid){
      var src=byId[sid], tgts=tgtMap[sid];
      var seen={}, unique=[]; tgts.forEach(function(t){ if(!seen[t.id]){seen[t.id]=1;unique.push(t);} }); tgts=unique;
      if(tgts.length<2) return;
      var byY={};
      tgts.forEach(function(t){ var ry=Math.round(t.y); if(!byY[ry])byY[ry]=[]; byY[ry].push(t); });
      Object.keys(byY).forEach(function(ry){
        var grp=byY[ry]; if(grp.length<2) return;
        var minX=1/0,maxX=-1/0;
        grp.forEach(function(t){ if(t.x<minX)minX=t.x; if(t.x>maxX)maxX=t.x; });
        var mid=(minX+maxX)/2, dx=src.x-mid;
        grp.forEach(function(t){ t.x+=dx; });
      });
    });

    edges.forEach(function(e){
      e.sourceNode=byId[e.source]; e.targetNode=byId[e.target];
      if(e.sourceNode) e.sourceNode.targets.push(e);
      if(e.targetNode) e.targetNode.sources.push(e);
      if(!e.sourceNode||!e.targetNode){e.points=[];return;}
      var de=g.edge(e.source,e.target), pts=de&&de.points?de.points.map(function(p){return{x:p.x,y:p.y};}):[],
          tx=e.targetNode.x, ty=vTop(e.targetNode);
      var TI=V.EDGE_TARGET_INSET||12, tl=e.targetNode.x-e.targetNode.width/2+TI, tr=e.targetNode.x+e.targetNode.width/2-TI;
      if(pts.length>=2){ pts[0]={x:e.sourceNode.x,y:vBot(e.sourceNode)}; pts[pts.length-1]={x:Math.max(tl,Math.min(tr,pts[pts.length-1].x)),y:ty}; }
      else pts=[{x:e.sourceNode.x,y:vBot(e.sourceNode)},{x:Math.max(tl,Math.min(tr,tx)),y:ty}];
      e.points=pts;
    });

    var mn={x:1/0,y:1/0}, mx={x:-1/0,y:-1/0};
    nodes.forEach(function(n){ var l=n.x-n.width/2,r=n.x+n.width/2,t=n.y-n.height/2,b=n.y+n.height/2;
      if(l<mn.x)mn.x=l;if(r>mx.x)mx.x=r;if(t<mn.y)mn.y=t;if(b>mx.y)mx.y=b; });
    mn.x-=padding; mn.y-=padding;
    var size={min:mn,max:mx,width:mx.x-mn.x+2*padding,height:mx.y-mn.y+2*padding};
    nodes.forEach(function(n){n.x-=mn.x;n.y-=mn.y;n.order=n.x+n.y*9999;});
    edges.forEach(function(e){e.points.forEach(function(p){p.x-=mn.x;p.y-=mn.y;});});
    return {nodes:nodes,edges:edges,size:size};
  }

  // --- React hook ---
  var OPTS = {layout:{spaceX:42,spaceY:140,layerSpaceY:120,spreadX:2,padding:70,iterations:30},
              routing:{spaceX:66,spaceY:44,minPassageGap:90,stemUnit:5,stemMinSource:0,stemMinTarget:15,stemMax:6,stemSpaceSource:8,stemSpaceTarget:3}};

  function useLayout(nodes,edges,expState,routeData) {
    var s={n:React.useState([]),e:React.useState([]),err:React.useState(null),sz:React.useState({w:600,h:600}),v:React.useState(0),b:React.useState(false)};
    React.useEffect(function(){
      if(!nodes.length){s.b[1](false);return;} s.b[1](true);
      try {
        var vis=nodes.filter(function(n){return !n.hidden&&!n.parentNode;}),
            ids=new Set(vis.map(function(n){return n.id;})),
            ve=edges.filter(function(e){return ids.has(e.source)&&ids.has(e.target);}),
            ln=vis.map(function(n){var d=calcDims(n);return{id:n.id,width:d.width,height:d.height,x:0,y:0,data:n.data,_o:n};}),
            le=ve.map(function(e){return{id:e.id,source:e.source,target:e.target,_o:e};}),
            r=layout(ln,le,70);
        s.n[1](r.nodes.map(function(n){return{...n._o,position:{x:n.x-n.width/2,y:n.y-n.height/2},width:n.width,height:n.height,
          style:{...n._o.style,width:n.width,height:n.height},
          handles:[{type:'target',position:'top',x:n.width/2,y:0,width:8,height:8,id:null},{type:'source',position:'bottom',x:n.width/2,y:n.height,width:8,height:8,id:null}]};}));
        s.e[1](r.edges.map(function(e){return{...e._o,data:{...(e._o.data||{}),points:e.points}};}));
        s.v[1](function(v){return v+1;}); s.b[1](false); s.err[1](null);
        if(r.size) s.sz[1]({w:r.size.width,h:r.size.height});
      } catch(err){ console.error('Layout error:',err); s.err[1](err.message||'Layout error'); s.b[1](false); }
    },[nodes,edges,expState,routeData]);
    return {layoutedNodes:s.n[0],layoutedEdges:s.e[0],layoutError:s.err[0],graphHeight:s.sz[0].h,graphWidth:s.sz[0].w,layoutVersion:s.v[0],isLayouting:s.b[0]};
  }

  // --- Exports ---
  root.ConstraintLayout = {graph:function(n,e,l,o,opts){var r=layout(n,e,(opts&&opts.layout&&opts.layout.padding)||70);return{nodes:r.nodes,edges:r.edges,layers:l,size:r.size};},defaultOptions:OPTS};
  root.HypergraphVizLayout = {useLayout:useLayout,calculateDimensions:calcDims,TYPE_HINT_MAX_CHARS:THC,NODE_LABEL_MAX_CHARS:NLC,CHAR_WIDTH_PX:CW,NODE_BASE_PADDING:NBP,FUNCTION_NODE_BASE_PADDING:FBP,MAX_NODE_WIDTH:MNW,GRAPH_PADDING:GP,HEADER_HEIGHT:HH};
})(typeof window !== 'undefined' ? window : this);
