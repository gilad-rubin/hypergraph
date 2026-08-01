// Node-side runner for the layout smoke harness (tests/viz/test_layout_smoke.py).
//
// Reads JSON `{scene, expansion}` from stdin, runs the REAL vendored dagre
// through performCompoundLayout exactly as the widget does, and reports
// whether every visible node received a position. A scene that is structurally
// fine can still crash dagre — e.g. an edge pointing at a compound (parent)
// node — and only running the layout catches it.
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const ASSETS = path.join(ROOT, 'src', 'hypergraph', 'viz', 'assets');

globalThis.window = globalThis;
// dagre.min.js is UMD: under Node's CJS it attaches to module.exports, so
// require it and publish the global the viz modules expect.
globalThis.dagre = require(path.join(ASSETS, 'vendor', 'dagre.min.js'));

// Headless stubs: the layout functions under test are plain functions; only
// module-load-time global checks need satisfying.
const noop = function () {};
globalThis.React = {
  useState: function (v) { return [v, noop]; },
  useEffect: noop, useMemo: function (f) { return f(); },
  useCallback: function (f) { return f; }, useRef: function () { return {current: null}; },
  createElement: noop,
};
globalThis.ReactDOM = {};
globalThis.ReactFlow = {
  ReactFlow: noop, Background: noop, Panel: noop, Position: {}, MarkerType: {},
  ReactFlowProvider: noop, Handle: noop, BaseEdge: noop, EdgeLabelRenderer: noop,
  useNodesState: noop, useEdgesState: noop, useReactFlow: noop,
  useUpdateNodeInternals: noop, getBezierPath: function () { return ['', 0, 0]; },
};
globalThis.htm = { bind: function () { return noop; } };

eval(fs.readFileSync(path.join(ASSETS, 'viz_runtime.js'), 'utf-8'));
eval(fs.readFileSync(path.join(ASSETS, 'viz_layout.js'), 'utf-8'));

const {scene, expansion} = JSON.parse(fs.readFileSync(0, 'utf-8'));
const visible = scene.nodes.filter((n) => !n.hidden);
const state = new Map(Object.entries(expansion || {}));
try {
  const res = globalThis.HypergraphVizLayout.performCompoundLayout(visible, scene.edges, state, 0.25, 80);
  const laid = res.nodes || [];
  // Laid-out nodes carry {position: {x, y}}; a node dagre never ranked would
  // arrive without one.
  const unpositioned = laid
    .filter((n) => !n.position || typeof n.position.x !== 'number' || typeof n.position.y !== 'number')
    .map((n) => n.id);
  // Absolute positions (child nodes are parent-relative in the scene) so
  // Python tests can assert rank facts, not merely "it did not crash".
  const positions = {};
  const byId = {};
  laid.forEach((n) => { byId[n.id] = n; });
  laid.forEach((n) => {
    let x = n.position ? n.position.x : NaN;
    let y = n.position ? n.position.y : NaN;
    let parent = n.parentNode;
    let hops = 0;
    while (parent && byId[parent] && hops <= laid.length) {
      x += byId[parent].position.x;
      y += byId[parent].position.y;
      parent = byId[parent].parentNode;
      hops++;
    }
    positions[n.id] = {x: x, y: y, width: n.width, height: n.height};
  });
  process.stdout.write(JSON.stringify({ok: true, laidOut: laid.length, unpositioned: unpositioned, positions: positions}));
} catch (err) {
  process.stdout.write(JSON.stringify({ok: false, error: String((err && err.message) || err)}));
}
