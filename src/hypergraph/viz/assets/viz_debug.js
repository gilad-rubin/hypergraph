/**
 * Debug API installation for Hypergraph visualization tests and diagnostics.
 */
(function(root) {
  'use strict';

  var R = root.HypergraphVizRuntime;
  if (!R) {
    console.error('HypergraphVizDebug: Missing HypergraphVizRuntime');
    return;
  }

  var resolveNodeType = R.resolveNodeType;
  var getOffset = R.getOffset;

  function installDebugApi(options) {
    var layoutedNodes = options.layoutedNodes || [];
    var layoutedEdges = options.layoutedEdges || [];
    var layoutVersion = options.layoutVersion;
    var routingData = options.routingData || {};
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
  }

  var Debug = { installDebugApi: installDebugApi };
  root.HypergraphVizDebug = Debug;
  root.HypergraphViz = root.HypergraphViz || {};
  root.HypergraphViz.Debug = Debug;
})(typeof window !== 'undefined' ? window : this);
