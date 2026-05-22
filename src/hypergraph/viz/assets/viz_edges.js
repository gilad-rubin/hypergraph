/**
 * Edge rendering for Hypergraph visualization.
 */
(function(root) {
  'use strict';

  var R = root.HypergraphVizRuntime;
  if (!R) {
    console.error('HypergraphVizEdges: Missing HypergraphVizRuntime');
    return;
  }

  var React = R.React;
  var html = R.html;
  var BaseEdge = R.BaseEdge;
  var EdgeLabelRenderer = R.EdgeLabelRenderer;
  var getBezierPath = R.getBezierPath;

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

  var CustomEdge = function(props) {
    var id = props.id, data = props.data, label = props.label;
    var sourceX = props.sourceX, sourceY = props.sourceY;
    var style = { strokeLinejoin: 'round', strokeLinecap: 'round', ...(props.style || {}) };
    var markerEnd = props.markerEnd;
    var edgePath, labelX, labelY;

    var edgeLabel = label || (data && data.label);

    if (data && data.points && data.points.length > 0) {
      var points = normalizePoints(data.points.slice());
      edgePath = curveBasis(points);
      var isBranch = edgeLabel === 'True' || edgeLabel === 'False';
      var lp = isBranch
        ? pointAlongPolyline(points, 0.5)
        : pointAlongPolyline(points, 0.35);
      labelX = lp.x; labelY = lp.y;
    } else {
      var r = getBezierPath({ sourceX: props.sourceX, sourceY: props.sourceY, sourcePosition: props.sourcePosition,
        targetX: props.targetX, targetY: props.targetY, targetPosition: props.targetPosition });
      edgePath = r[0];
      var isBranchDecisionLabel = edgeLabel === 'True' || edgeLabel === 'False';
      var t = isBranchDecisionLabel ? 0.5 : 0.35;
      labelX = sourceX + (props.targetX - sourceX) * t;
      labelY = sourceY + (props.targetY - sourceY) * t;
    }
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

  var Edges = {
    curveBasis: curveBasis,
    normalizePoints: normalizePoints,
    pointAlongPolyline: pointAlongPolyline,
    CustomEdge: CustomEdge,
  };

  root.HypergraphVizEdges = Edges;
  root.HypergraphViz = root.HypergraphViz || {};
  root.HypergraphViz.Edges = Edges;
})(typeof window !== 'undefined' ? window : this);
