/**
 * Ghost inputs + focus: a hidden-inputs graph reveals each step's inputs on demand.
 *
 * With inputs hidden (the default, `visualize(show_inputs=False)`), hovering a
 * step, or tapping it on a touch screen, draws what the steps-only graph leaves
 * out as ghost pills beside the step, each with a short dashed edge into it:
 *
 *   (a) inputs no step produces, `name : Type`;
 *   (b) bound tools, faded and dashed (left out when show_bounded_inputs=False);
 *   (c) inputs another step produces whose arrow `simplify` hides, labelled
 *       with that producer, `raw ← fetch`. Nothing a step consumes stays
 *       invisible.
 *
 * The step's upstream and downstream path stays lit and everything else dims.
 * This module derives the ghost sets and focus sets and places the pills; the
 * App (viz.js) owns hover/tap/pin/Escape state and the edge component
 * (viz_edges.js) dims its own path and label. Pure overlay: nothing is
 * re-laid out, and pills live in flow coordinates, so they pan and zoom with
 * the graph.
 *
 * Placement rule (`planGhosts`). Pills stack in a column beside the step,
 * because the rank gap above a step is where edges, arrowheads and the gate's
 * True/False labels live. Candidates, in order: the side with more free room,
 * then the other side, each tried at the step's centre and then slid up and
 * down in small steps; then a row above and a row below, slid left and right.
 * A candidate collides when a pill meets another rendered node box or edge
 * label box (measured from the DOM, with a small margin). The first
 * collision-free candidate that is also inside the visible canvas wins, then
 * the first collision-free one anywhere; only when no candidate is free with
 * types does the plan retry with names only, and when even that fails it keeps
 * the candidate with the fewest collisions. Pills of one plan never overlap
 * each other: they are stacked with a gap.
 */
(function(root) {
  'use strict';

  var R = root.HypergraphVizRuntime;
  var D = root.HypergraphDerivation;
  if (!R || !D) {
    console.error('HypergraphVizGhosts: Missing HypergraphVizRuntime / HypergraphDerivation');
    return;
  }
  var html = R.html;
  var EMPTY_ARR = R.EMPTY_ARR;
  var truncateTypeHint = R.truncateTypeHint;

  var PILL_H = 22;         // pill height, flow px
  var PILL_GAP = 6;        // gap between stacked pills
  var PILL_PAD = 22;       // horizontal padding + border inside a pill
  var SIDE_GAP = 30;       // step edge to the pill column
  var CLEARANCE = 6;       // margin kept from other nodes and labels
  var SLIDE_STEP = 10;     // vertical / horizontal slide per candidate
  var TOOLBAR_RESERVE = R.TOOLBAR_RESERVE; // screen px the bottom-right toolbar occupies
  var DIAMOND_HALF = 67;   // half-diagonal of the 95px gate diamond
  var FONT = 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace';

  var GHOST_CSS = [
    '.react-flow__node{transition:opacity .15s ease}',
    '.react-flow__node.hg-dim{opacity:.2}',
    '.react-flow__node.hg-focus .group.rounded-lg{border-color:#6366f1!important;box-shadow:0 0 0 2px rgba(99,102,241,.55),0 10px 24px rgba(99,102,241,.18)!important}',
    '.react-flow__node.hg-focus div[style*="rotate(45deg)"]{border-color:#6366f1!important;box-shadow:0 0 0 2px rgba(99,102,241,.55)!important}',
  ].join('');

  // Scene node types a reader thinks of as steps: they get ghosts and focus.
  var STEP_TYPES = { FUNCTION: true, BRANCH: true, DUAL: true, PIPELINE: true };

  function isStep(node) {
    var d = node && node.data;
    if (!d || !STEP_TYPES[d.nodeType]) return false;
    return !(d.nodeType === 'PIPELINE' && d.isExpanded);
  }

  // ── Ghost sets ────────────────────────────────────────────────────────

  // step id -> [{name, kind: 'input'|'bound'|'hidden-edge', type, source}] for
  // every visible step, in the step's own input order with bound tools last.
  // `opts` is the scene's own option object (expansionState, separateOutputs,
  // showBoundedInputs, simplify) and `scene` the scene drawn with inputs hidden.
  function ghostSets(ir, scene, opts) {
    var sets = Object.create(null);
    if (!ir || !scene) return sets;
    var parentMap = D.buildParentMap(ir);
    var sceneNodes = Object.create(null);
    var visibleIds = Object.create(null);
    (scene.nodes || EMPTY_ARR).forEach(function(n) {
      sceneNodes[n.id] = n;
      if (!n.hidden) visibleIds[n.id] = true;
    });
    var entrypoints = D.expandedContainerEntrypoints(ir, opts.expansionState || {});
    var seen = Object.create(null);
    function add(stepId, item) {
      if (!sceneNodes[stepId] || !isStep(sceneNodes[stepId])) return;
      var key = stepId + '\u0000' + item.name + '\u0000' + item.kind;
      if (seen[key]) return;
      seen[key] = true;
      (sets[stepId] = sets[stepId] || []).push(item);
    }

    // (a) and (b): inputs no step produces, attached to the visible box that
    // consumes them (an inner consumer of a collapsed container surfaces at
    // the container), exactly as the input edges would attach.
    (ir.external_inputs || EMPTY_ARR).forEach(function(ext) {
      if (ext.is_bound && !opts.showBoundedInputs) return;
      var targets = [];
      (ext.consumers || EMPTY_ARR).forEach(function(c) {
        var visible = D.resolveToVisible(c, parentMap, visibleIds);
        if (!visible) return;
        D.resolveExpandedEntrypoints(visible, entrypoints).forEach(function(t) {
          if (targets.indexOf(t) === -1) targets.push(t);
        });
      });
      var hints = ext.type_hints || EMPTY_ARR;
      targets.forEach(function(t) {
        (ext.params || EMPTY_ARR).forEach(function(p, i) {
          add(t, { name: String(p), kind: ext.is_bound ? 'bound' : 'input', type: hints[i] || null, source: null });
        });
      });
    });

    // (c): data edges `simplify` dropped. The same scene without simplify
    // names exactly the edges the drawn scene is missing.
    if (opts.simplify !== false && root.HypergraphSceneBuilder) {
      var full = root.HypergraphSceneBuilder.buildInitialScene(ir, {
        expansionState: opts.expansionState || {},
        separateOutputs: !!opts.separateOutputs,
        showInputs: false,
        showBoundedInputs: !!opts.showBoundedInputs,
        simplify: false,
      });
      var drawn = Object.create(null);
      (scene.edges || EMPTY_ARR).forEach(function(e) { drawn[e.id] = true; });
      (full.edges || EMPTY_ARR).forEach(function(e) {
        var data = e.data || {};
        if (e.hidden || drawn[e.id] || data.edgeType !== 'data') return;
        var src = sceneNodes[e.source];
        var producerId = src && src.data && src.data.nodeType === 'DATA' ? src.data.sourceId : e.source;
        var producer = sceneNodes[producerId];
        var producerLabel = (producer && producer.data && producer.data.label) || String(producerId).split('/').pop();
        var names = data.valueName != null ? [data.valueName] : (data.valueNames || EMPTY_ARR);
        names.forEach(function(name) {
          add(e.target, { name: String(name), kind: 'hidden-edge', type: null, source: producerLabel });
        });
      });
    }

    // The step's own input order, bound tools last.
    Object.keys(sets).forEach(function(stepId) {
      var inputs = ((sceneNodes[stepId].data || {}).inputs) || EMPTY_ARR;
      var order = Object.create(null);
      inputs.forEach(function(inp, i) { if (order[inp.name] === undefined) order[inp.name] = i; });
      sets[stepId] = sets[stepId]
        .map(function(item, i) { return { item: item, i: i }; })
        .sort(function(a, b) {
          var ab = a.item.kind === 'bound' ? 1 : 0, bb = b.item.kind === 'bound' ? 1 : 0;
          if (ab !== bb) return ab - bb;
          var ao = order[a.item.name] === undefined ? 1e9 : order[a.item.name];
          var bo = order[b.item.name] === undefined ? 1e9 : order[b.item.name];
          return ao !== bo ? ao - bo : a.i - b.i;
        })
        .map(function(x) { return x.item; });
    });
    return sets;
  }

  // ── Focus ─────────────────────────────────────────────────────────────

  // The focused step plus everything upstream and downstream of it over the
  // edges actually drawn, and the containers those nodes sit in. An edge is
  // lit when both its ends are upstream, or both downstream.
  function focusSets(focusId, edges, nodes) {
    var fwd = Object.create(null), back = Object.create(null);
    (edges || EMPTY_ARR).forEach(function(e) {
      if (e.hidden) return;
      (fwd[e.source] = fwd[e.source] || []).push(e.target);
      (back[e.target] = back[e.target] || []).push(e.source);
    });
    function walk(adj) {
      var seen = Object.create(null), stack = [focusId];
      seen[focusId] = true;
      while (stack.length) {
        var k = stack.pop();
        (adj[k] || EMPTY_ARR).forEach(function(m) { if (!seen[m]) { seen[m] = true; stack.push(m); } });
      }
      return seen;
    }
    var up = walk(back), down = walk(fwd);
    var parentOf = Object.create(null);
    (nodes || EMPTY_ARR).forEach(function(n) { if (n.parentNode) parentOf[n.id] = n.parentNode; });
    var lit = Object.create(null);
    function light(id) { for (var c = id; c && !lit[c]; c = parentOf[c]) lit[c] = true; }
    Object.keys(up).forEach(light);
    Object.keys(down).forEach(light);
    var litEdges = Object.create(null);
    (edges || EMPTY_ARR).forEach(function(e) {
      if ((up[e.source] && up[e.target]) || (down[e.source] && down[e.target])) litEdges[e.id] = true;
    });
    return { id: focusId, nodes: lit, edges: litEdges };
  }

  // ── Placement ─────────────────────────────────────────────────────────

  var measureCtx = null;
  function textWidth(text, weight) {
    try {
      if (!measureCtx) measureCtx = document.createElement('canvas').getContext('2d');
      measureCtx.font = weight + ' 11px ' + FONT;
      return measureCtx.measureText(text).width;
    } catch (e) {
      return text.length * 6.7;
    }
  }

  function pillParts(item, withTypes) {
    if (item.kind === 'hidden-edge') return { name: item.name, rest: ' ← ' + item.source };
    if (withTypes && item.type) return { name: item.name, rest: ' : ' + truncateTypeHint(item.type) };
    return { name: item.name, rest: '' };
  }

  function pillWidth(parts) {
    return Math.ceil(textWidth(parts.name, 600) + textWidth(parts.rest, 400) + PILL_PAD);
  }

  function overlaps(a, b) {
    return a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;
  }

  // Rendered boxes in flow coordinates: the step, and every other visible
  // node (the step's own expanded containers excepted) and edge label.
  function measureScene(stepId, ancestors, transform) {
    var pane = document.querySelector('.react-flow');
    if (!pane) return null;
    var pr = pane.getBoundingClientRect();
    var tx = transform.x, ty = transform.y, k = transform.zoom || 1;
    function toFlow(r) {
      return { x0: (r.left - pr.left - tx) / k, y0: (r.top - pr.top - ty) / k,
               x1: (r.right - pr.left - tx) / k, y1: (r.bottom - pr.top - ty) / k };
    }
    var step = null, obstacles = [];
    document.querySelectorAll('.react-flow__node').forEach(function(el) {
      var r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return;
      var id = el.getAttribute('data-id');
      if (id === stepId) { step = toFlow(r); return; }
      if (ancestors[id]) return;
      obstacles.push(toFlow(r));
    });
    document.querySelectorAll('.react-flow__edgelabel-renderer > div').forEach(function(el) {
      var r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) obstacles.push(toFlow(r));
    });
    var visible = toFlow({ left: pr.left, top: pr.top, right: pr.right - TOOLBAR_RESERVE, bottom: pr.bottom });
    return { step: step, obstacles: obstacles, visible: visible, paneRect: pr };
  }

  function candidates(step, widths, freeLeft, freeRight) {
    var n = widths.length;
    var colH = n * PILL_H + (n - 1) * PILL_GAP;
    var rowW = widths.reduce(function(s, w) { return s + w; }, 0) + (n - 1) * PILL_GAP;
    var cy = (step.y0 + step.y1) / 2, cx = (step.x0 + step.x1) / 2;
    var sides = freeLeft >= freeRight ? ['left', 'right'] : ['right', 'left'];
    var out = [];
    var slideV = Math.max(step.y1 - step.y0, colH) / 2 + PILL_H;
    sides.forEach(function(side) {
      for (var d = 0; d <= slideV; d += SLIDE_STEP) {
        (d === 0 ? [0] : [-d, d]).forEach(function(dy) {
          var top = cy - colH / 2 + dy;
          out.push({ side: side, pills: widths.map(function(w, i) {
            var y = top + i * (PILL_H + PILL_GAP);
            var x = side === 'left' ? step.x0 - SIDE_GAP - w : step.x1 + SIDE_GAP;
            return { x0: x, y0: y, x1: x + w, y1: y + PILL_H };
          }) });
        });
      }
    });
    var slideH = Math.max(step.x1 - step.x0, rowW) / 2;
    ['above', 'below'].forEach(function(side) {
      for (var d = 0; d <= slideH; d += SLIDE_STEP) {
        (d === 0 ? [0] : [-d, d]).forEach(function(dx) {
          var left = cx - rowW / 2 + dx;
          var y = side === 'above' ? step.y0 - SIDE_GAP - PILL_H : step.y1 + SIDE_GAP;
          var x = left;
          out.push({ side: side, pills: widths.map(function(w) {
            var p = { x0: x, y0: y, x1: x + w, y1: y + PILL_H };
            x += w + PILL_GAP;
            return p;
          }) });
        });
      }
    });
    return out;
  }

  function collisions(cand, obstacles) {
    var count = 0;
    cand.pills.forEach(function(p) {
      var grown = { x0: p.x0 - CLEARANCE, y0: p.y0 - CLEARANCE, x1: p.x1 + CLEARANCE, y1: p.y1 + CLEARANCE };
      obstacles.forEach(function(o) { if (overlaps(grown, o)) count += 1; });
    });
    return count;
  }

  function inside(cand, box) {
    return cand.pills.every(function(p) { return p.x0 >= box.x0 && p.y0 >= box.y0 && p.x1 <= box.x1 && p.y1 <= box.y1; });
  }

  // Free horizontal room beside the step within its own band.
  function freeRoom(step, obstacles, side) {
    var limit = side === 'left' ? -Infinity : Infinity;
    obstacles.forEach(function(o) {
      if (o.y1 < step.y0 || o.y0 > step.y1) return;
      if (side === 'left' && o.x1 <= step.x0) limit = Math.max(limit, o.x1);
      if (side === 'right' && o.x0 >= step.x1) limit = Math.min(limit, o.x0);
    });
    return side === 'left' ? step.x0 - limit : limit - step.x1;
  }

  // Where ghost edge i of n meets the step: the facing border (a gate's
  // facing vertex), the n ends spread evenly along it so no two arrowheads
  // land on one point.
  function anchor(step, pill, side, isGate, i, n) {
    var cx = (step.x0 + step.x1) / 2, cy = (step.y0 + step.y1) / 2;
    var pcx = (pill.x0 + pill.x1) / 2, pcy = (pill.y0 + pill.y1) / 2;
    var f = (i + 1) / (n + 1);
    var alongY = step.y0 + 10 + f * (step.y1 - step.y0 - 24);
    var alongX = step.x0 + 12 + f * (step.x1 - step.x0 - 24);
    if (side === 'left') return { sx: pill.x1, sy: pcy, tx: isGate ? cx - DIAMOND_HALF : step.x0, ty: isGate ? cy : alongY };
    if (side === 'right') return { sx: pill.x0, sy: pcy, tx: isGate ? cx + DIAMOND_HALF : step.x1, ty: isGate ? cy : alongY };
    if (side === 'above') return { sx: pcx, sy: pill.y1, tx: isGate ? cx : alongX, ty: isGate ? cy - DIAMOND_HALF : step.y0 };
    return { sx: pcx, sy: pill.y0, tx: isGate ? cx : alongX, ty: isGate ? cy + DIAMOND_HALF : step.y1 - 6 };
  }

  // The placed pills for one step, in flow coordinates, or null when the
  // step is not rendered. `items` may be empty: the plan still carries the
  // step's box, which a pinned step is framed by.
  function planGhosts(args) {
    var measured = measureScene(args.stepId, args.ancestors || {}, args.transform);
    if (!measured || !measured.step) return null;
    var step = measured.step, obstacles = measured.obstacles, items = args.items || EMPTY_ARR;
    var plan = { stepId: args.stepId, step: step, pills: [], side: null, typesDropped: false, collisions: 0 };
    if (!items.length) return plan;
    var freeLeft = freeRoom(step, obstacles, 'left'), freeRight = freeRoom(step, obstacles, 'right');
    var best = null;
    var passes = args.showTypes ? [true, false] : [false];
    for (var pi = 0; pi < passes.length && !(best && best.collisions === 0); pi++) {
      var withTypes = passes[pi];
      var parts = items.map(function(item) { return pillParts(item, withTypes); });
      var widths = parts.map(pillWidth);
      var free = null, freeVisible = null, fewest = null;
      candidates(step, widths, freeLeft, freeRight).forEach(function(c) {
        c.collisions = collisions(c, obstacles);
        if (c.collisions === 0) {
          if (!free) free = c;
          if (!freeVisible && inside(c, measured.visible)) freeVisible = c;
        }
        if (!fewest || c.collisions < fewest.collisions) fewest = c;
      });
      var chosen = freeVisible || free || fewest;
      if (!best || chosen.collisions < best.collisions) {
        best = { cand: chosen, collisions: chosen.collisions, parts: parts, typesDropped: args.showTypes && !withTypes };
      }
    }
    var isGate = args.nodeType === 'BRANCH';
    plan.side = best.cand.side;
    plan.typesDropped = best.typesDropped;
    plan.collisions = best.collisions;
    plan.pills = best.cand.pills.map(function(box, i) {
      var a = anchor(step, box, best.cand.side, isGate, i, items.length);
      return { item: items[i], parts: best.parts[i], box: box, sx: a.sx, sy: a.sy, tx: a.tx, ty: a.ty };
    });
    return plan;
  }

  // The viewport that brings a pinned step and its pills on screen (camera
  // only), or null when they already are. `pane` is the canvas's screen rect.
  function frameViewport(plan, vp, pane) {
    var b = { x0: plan.step.x0, y0: plan.step.y0, x1: plan.step.x1, y1: plan.step.y1 };
    plan.pills.forEach(function(p) {
      b.x0 = Math.min(b.x0, p.box.x0); b.y0 = Math.min(b.y0, p.box.y0);
      b.x1 = Math.max(b.x1, p.box.x1); b.y1 = Math.max(b.y1, p.box.y1);
    });
    var margin = 12;
    var W = pane.width, H = pane.height;
    var box = { left: margin, top: margin, right: W - TOOLBAR_RESERVE, bottom: H - margin };
    var availW = box.right - box.left, availH = box.bottom - box.top;
    var z = vp.zoom, x = vp.x, y = vp.y;
    var bw = b.x1 - b.x0, bh = b.y1 - b.y0;
    if (bw * z > availW || bh * z > availH) {
      z = Math.max(0.2, Math.min(z, availW / bw, availH / bh));
      x = box.left + (availW - bw * z) / 2 - b.x0 * z;
      y = box.top + (availH - bh * z) / 2 - b.y0 * z;
    } else {
      var l = b.x0 * z + x, r = b.x1 * z + x, t = b.y0 * z + y, btm = b.y1 * z + y;
      if (l < box.left) x += box.left - l; else if (r > box.right) x -= r - box.right;
      if (t < box.top) y += box.top - t; else if (btm > box.bottom) y -= btm - box.bottom;
    }
    return (Math.abs(x - vp.x) > 0.5 || Math.abs(y - vp.y) > 0.5 || z !== vp.zoom) ? { x: x, y: y, zoom: z } : null;
  }

  // ── Rendering ─────────────────────────────────────────────────────────

  function tones(isLight) {
    return isLight
      ? { pill: '#f0f9ff', border: '#7dd3fc', text: '#0369a1', line: '#38bdf8',
          boundPill: '#ffffff', boundBorder: '#94a3b8', boundText: '#64748b',
          hiddenPill: '#f8fafc', hiddenBorder: '#a5b4fc', hiddenText: '#4338ca' }
      : { pill: '#082f49', border: 'rgba(56,189,248,.55)', text: '#7dd3fc', line: '#38bdf8',
          boundPill: '#0f172a', boundBorder: '#64748b', boundText: '#94a3b8',
          hiddenPill: '#1e1b4b', hiddenBorder: 'rgba(129,140,248,.6)', hiddenText: '#a5b4fc' };
  }

  var selectTransform = function(st) { return st.transform; };

  var GhostLayer = function(props) {
    var useStore = root.ReactFlow && root.ReactFlow.useStore;
    var t = useStore ? useStore(selectTransform) : [0, 0, 1];
    var plan = props.plan;
    if (!plan || !plan.pills.length) return null;
    var c = tones(props.isLight);
    function stroke(kind) { return kind === 'bound' ? c.boundBorder : (kind === 'hidden-edge' ? c.hiddenBorder : c.line); }
    return html`
      <div data-hg-ghosts="" style=${{ position: 'absolute', left: 0, top: 0, width: '100%', height: '100%', overflow: 'hidden', pointerEvents: 'none', zIndex: 4 }}>
        <div style=${{ position: 'absolute', left: 0, top: 0, transformOrigin: '0 0', transform: 'translate(' + t[0] + 'px,' + t[1] + 'px) scale(' + t[2] + ')' }}>
          <svg style=${{ position: 'absolute', left: 0, top: 0, overflow: 'visible' }} width="1" height="1">
            <defs>
              ${['input', 'bound', 'hidden-edge'].map(function(kind) {
                return html`<marker key=${kind} id=${'hg-ghost-arrow-' + kind} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                  <path d="M 0 0 L 10 5 L 0 10 z" fill=${stroke(kind)}></path></marker>`;
              })}
            </defs>
            ${plan.pills.map(function(p) {
              var horizontal = plan.side === 'left' || plan.side === 'right';
              var mx = (p.sx + p.tx) / 2, my = (p.sy + p.ty) / 2;
              var d = horizontal
                ? 'M ' + p.sx + ' ' + p.sy + ' C ' + mx + ' ' + p.sy + ', ' + mx + ' ' + p.ty + ', ' + p.tx + ' ' + p.ty
                : 'M ' + p.sx + ' ' + p.sy + ' C ' + p.sx + ' ' + my + ', ' + p.tx + ' ' + my + ', ' + p.tx + ' ' + p.ty;
              return html`<path key=${p.item.kind + ':' + p.item.name} data-hg-ghost-edge=${p.item.name} d=${d} fill="none"
                stroke=${stroke(p.item.kind)} strokeWidth="1.5" strokeDasharray="4 3" opacity=${p.item.kind === 'bound' ? 0.7 : 1}
                markerEnd=${'url(#hg-ghost-arrow-' + p.item.kind + ')'}></path>`;
            })}
          </svg>
          ${plan.pills.map(function(p) {
            var kind = p.item.kind, bound = kind === 'bound', hidden = kind === 'hidden-edge';
            var title = bound ? 'bound tool' : (hidden ? 'produced by ' + p.item.source + '; simplify hides the arrow' : 'passed in per run');
            return html`<div key=${kind + ':' + p.item.name} data-hg-ghost=${p.item.name} data-hg-ghost-kind=${kind} data-hg-ghost-for=${plan.stepId}
                className="font-mono" title=${title}
                style=${{ position: 'absolute', left: p.box.x0 + 'px', top: p.box.y0 + 'px', width: (p.box.x1 - p.box.x0) + 'px', height: PILL_H + 'px',
                  lineHeight: (PILL_H - 2) + 'px', boxSizing: 'border-box', borderRadius: '9999px', textAlign: 'center', fontSize: '11px',
                  fontFamily: FONT, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', padding: '0 8px',
                  borderWidth: '1px', borderStyle: bound || hidden ? 'dashed' : 'solid',
                  borderColor: bound ? c.boundBorder : (hidden ? c.hiddenBorder : c.border),
                  background: bound ? c.boundPill : (hidden ? c.hiddenPill : c.pill),
                  color: bound ? c.boundText : (hidden ? c.hiddenText : c.text),
                  opacity: bound ? 0.72 : 1,
                  boxShadow: bound ? 'none' : '0 2px 8px rgba(14,165,233,.15)' }}>
              <span style=${{ fontWeight: 600 }}>${p.parts.name}</span>${p.parts.rest ? html`<span style=${{ opacity: 0.75 }}>${p.parts.rest}</span>` : null}
            </div>`;
          })}
        </div>
      </div>`;
  };

  var Ghosts = {
    GHOST_CSS: GHOST_CSS,
    isStep: isStep,
    ghostSets: ghostSets,
    focusSets: focusSets,
    planGhosts: planGhosts,
    frameViewport: frameViewport,
    GhostLayer: GhostLayer,
  };
  root.HypergraphVizGhosts = Ghosts;
  root.HypergraphViz = root.HypergraphViz || {};
  root.HypergraphViz.Ghosts = Ghosts;
})(typeof window !== 'undefined' ? window : this);
