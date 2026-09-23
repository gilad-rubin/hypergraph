"""Arrowheads sit on the end of their edge line and point along it.

React Flow's ``ArrowClosed`` marker (``orient="auto-start-reverse"``) takes its
direction from the path's tangent at the end. ``curveBasis`` used to finish
every edge with a cubic whose control points both coincide with the end point:
a zero-length end tangent. Chromium falls back to an earlier point and draws
the head correctly, but WebKit (Safari, iOS web views) orients such a marker
at 0 degrees. Every head then points right, sits beside the line, and half of
it hides under the target node.

CI runs Chromium only, so the path-shape test below checks what every engine
agrees on: the direction the final path command gives by itself. The pixel
test measures the rendered head in the browser the suite runs.
"""

from __future__ import annotations

import base64
import math

import pytest

from hypergraph import END, Graph, ifelse, node
from tests.viz.conftest import HAS_PLAYWRIGHT, _cached_html_path, wait_for_debug_ready

pytestmark = pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")


@node(output_name="doc")
def load(seed: int) -> str:
    return str(seed)


@node(output_name="parsed")
def parse(doc: str) -> str:
    return doc


@node(output_name="receipt", emit="stored")
def store(parsed: str) -> str:
    return parsed


@ifelse(when_true="extract", when_false=END, wait_for="stored")
def ready() -> bool:
    return True


@node(output_name="table")
def extract(parsed: str) -> str:
    return parsed


@node(output_name="revision")
def save(table: str) -> str:
    return table


def make_arrow_graph() -> Graph:
    """A chain, a gate, an ordering edge, and a long edge that skips two ranks.

    input_seed -> load -> parse -> store -(stored)-> ready =True=> extract -> save
                                parse ----------------------------> extract
                                                     ready =False=> END
    """
    return Graph([load, parse, store, ready, extract, save])


EXPECTED_EDGES = {
    ("input_seed", "load"),
    ("load", "parse"),
    ("parse", "store"),
    ("parse", "extract"),  # long edge: skips store and ready
    ("store", "ready"),  # ordering (emit -> wait_for)
    ("ready", "extract"),  # control, True branch
    ("ready", "__end__"),  # control, False branch
    ("extract", "save"),
}

# Node borders are 1px wide and dagre positions are fractional: the same
# tolerance test_visual_layout_issues uses for the edge-meets-diamond check.
BORDER_TOLERANCE_PX = 1.5
# The final command's own direction and the path's arrival tangent must agree.
# On a straight final segment they are the same vector; 2 degrees absorbs
# float rounding in the SVG path string.
ORIENTATION_TOLERANCE_DEG = 2.0
# Below this the final command gives no direction of its own and each engine
# falls back differently (WebKit: 0 degrees).
MIN_DIRECTION_PX = 0.5

_PROBE_JS = r"""() => {
  const debug = window.__hypergraphVizDebug;
  const metaById = new Map((debug.layoutedEdges || []).map(e => [e.id, e]));
  const visibleBox = (id) => {
    const w = document.querySelector('.react-flow__node[data-id="' + CSS.escape(id) + '"]');
    if (!w) return null;
    // BRANCH draws a rotated square; every other node's box is the first
    // element that draws a border.
    const el = w.querySelector('div[style*="rotate(45deg)"]') ||
      Array.from(w.querySelectorAll('div')).find(d => parseFloat(getComputedStyle(d).borderTopWidth) > 0);
    const r = el.getBoundingClientRect();
    return { top: r.top, left: r.left, right: r.right };
  };
  const parseD = (d) => {
    const segs = [];
    (d.match(/[A-Za-z]|-?\d*\.?\d+(?:e[-+]?\d+)?/g) || []).forEach(t => {
      if (/[A-Za-z]/.test(t)) segs.push({ cmd: t, nums: [] });
      else segs[segs.length - 1].nums.push(parseFloat(t));
    });
    return segs;
  };
  const vp = document.querySelector('.react-flow__viewport');
  const zoom = parseFloat((vp.style.transform.match(/scale\(([\d.]+)\)/) || [0, 1])[1]);
  return Array.from(document.querySelectorAll('.react-flow__edge')).map(g => {
    const path = g.querySelector('path.react-flow__edge-path');
    const tid = (g.getAttribute('data-testid') || '').replace('rf__edge-', '');
    const meta = metaById.get(tid.replace(/_exp_.*$/, '')) || {};
    const source = (meta.data && meta.data.actualSource) || meta.source;
    const target = (meta.data && meta.data.actualTarget) || meta.target;
    const m = path.getScreenCTM();
    const client = p => ({ x: m.a * p.x + m.c * p.y + m.e, y: m.b * p.x + m.d * p.y + m.f });
    const L = path.getTotalLength();
    const end = client(path.getPointAtLength(L));
    const before = client(path.getPointAtLength(Math.max(0, L - 0.5)));
    const segs = parseD(path.getAttribute('d'));
    const last = segs[segs.length - 1], n = last.nums;
    const lastEnd = { x: n[n.length - 2], y: n[n.length - 1] };
    let from = null;
    if (last.cmd === 'C') from = { x: n[2], y: n[3] };
    else if (last.cmd === 'Q') from = { x: n[0], y: n[1] };
    else if (last.cmd === 'L' && segs.length > 1) {
      const pn = segs[segs.length - 2].nums;
      from = { x: pn[pn.length - 2], y: pn[pn.length - 1] };
    }
    const a = from ? client(from) : null, b = client(lastEnd);
    return {
      source, target, zoom,
      edgeType: meta.data && meta.data.edgeType,
      d: path.getAttribute('d'),
      end,
      arrive: { x: end.x - before.x, y: end.y - before.y },
      lastCmd: last.cmd,
      ownDir: a ? { x: b.x - a.x, y: b.y - a.y } : null,
      box: visibleBox(target),
    };
  });
}"""

# Leave only the arrowheads on screen: nodes hidden (a head tucked under a node
# must still be measured), edge strokes transparent, labels and chrome gone.
_HEADS_ONLY_CSS = (
    ".react-flow__node{visibility:hidden!important}"
    ".react-flow__edge-path{stroke:transparent!important}"
    ".react-flow__background,.react-flow__panel,.react-flow__edgelabel-renderer{display:none!important}"
)

_HEAD_PIXELS_JS = r"""async ({ png, ends, radius }) => {
  const img = new Image();
  img.src = 'data:image/png;base64,' + png;
  await img.decode();
  const dpr = img.naturalWidth / window.innerWidth;
  const c = document.createElement('canvas');
  c.width = img.naturalWidth; c.height = img.naturalHeight;
  const ctx = c.getContext('2d');
  ctx.drawImage(img, 0, 0);
  const px = ctx.getImageData(0, 0, c.width, c.height).data;
  const rgb = (x, y) => { const i = (y * c.width + x) * 4; return [px[i], px[i + 1], px[i + 2]]; };
  const bg = rgb(2, 2);
  return ends.map(e => {
    const pts = [];
    for (let y = Math.floor((e.y - radius) * dpr); y < Math.ceil((e.y + radius) * dpr); y++) {
      for (let x = Math.floor((e.x - radius) * dpr); x < Math.ceil((e.x + radius) * dpr); x++) {
        if (x < 0 || y < 0 || x >= c.width || y >= c.height) continue;
        const p = rgb(x, y);
        if (Math.abs(p[0] - bg[0]) + Math.abs(p[1] - bg[1]) + Math.abs(p[2] - bg[2]) > 60) {
          pts.push([(x + 0.5) / dpr, (y + 0.5) / dpr]);
        }
      }
    }
    return pts;
  });
}"""


@pytest.fixture(scope="module")
def arrow_edges(_browser):
    """Rendered geometry of every edge, plus the pixels of every arrowhead."""
    html_path = _cached_html_path(make_arrow_graph(), depth=0)
    page = _browser.new_page(viewport={"width": 900, "height": 1400}, device_scale_factor=2)
    try:
        page.goto(f"file://{html_path}")
        wait_for_debug_ready(page)
        edges = page.evaluate(_PROBE_JS)
        page.add_style_tag(content=_HEADS_ONLY_CSS)
        page.wait_for_timeout(100)
        png = base64.b64encode(page.screenshot()).decode()
        zoom = edges[0]["zoom"] if edges else 1.0
        # ArrowClosed spans ~6 marker units at 12.5/20 px per unit per stroke
        # px (<= 2): under 8px of head, so 13px (at zoom 1) holds the whole head
        # and stays clear of the neighbouring head 40px away.
        radius = 10 * zoom + 3
        pixels = page.evaluate(
            _HEAD_PIXELS_JS,
            {"png": png, "ends": [e["end"] for e in edges], "radius": radius},
        )
    finally:
        page.close()
    for edge, pts in zip(edges, pixels, strict=True):
        edge["headPixels"] = pts
    return edges


def _angle_between(u: dict, v: dict) -> float:
    a = math.degrees(math.atan2(u["y"], u["x"]) - math.atan2(v["y"], v["x"]))
    return abs((a + 180) % 360 - 180)


def _label(edge: dict) -> str:
    return f"{edge['source']} -> {edge['target']} ({edge['edgeType']})"


def test_every_edge_kind_is_rendered(arrow_edges):
    pairs = {(e["source"], e["target"]) for e in arrow_edges}
    assert pairs == EXPECTED_EDGES
    kinds = {e["edgeType"] for e in arrow_edges}
    assert {"data", "ordering", "control"} <= kinds, kinds


def test_arrow_tip_meets_target_border(arrow_edges):
    problems = []
    for edge in arrow_edges:
        end, box = edge["end"], edge["box"]
        if abs(end["y"] - box["top"]) > BORDER_TOLERANCE_PX:
            problems.append(f"{_label(edge)}: path ends at y={end['y']:.2f}, target border at y={box['top']:.2f}")
        if not box["left"] <= end["x"] <= box["right"]:
            problems.append(f"{_label(edge)}: path ends at x={end['x']:.2f}, outside the target [{box['left']:.2f}, {box['right']:.2f}]")
        if edge["arrive"]["y"] <= 0:
            problems.append(f"{_label(edge)}: path arrives moving up, not into the node")
    assert not problems, "\n".join(problems)


def test_final_path_segment_orients_the_head_along_the_line(arrow_edges):
    """The head's direction must come from the final command itself.

    A final cubic whose control points sit on its end point has no end
    direction of its own; WebKit then draws the head at 0 degrees (pointing
    right, beside the line). Require every engine to get the direction from the
    last command, and require that direction to match where the line arrives.
    """
    problems = []
    for edge in arrow_edges:
        own = edge["ownDir"]
        own_len = math.hypot(own["x"], own["y"]) if own else 0.0
        if own_len < MIN_DIRECTION_PX:
            problems.append(
                f"{_label(edge)}: final '{edge['lastCmd']}' has no end direction of its own ({own_len:.3f}px), path ends '...{edge['d'][-60:]}'"
            )
            continue
        err = _angle_between(own, edge["arrive"])
        if err > ORIENTATION_TOLERANCE_DEG:
            problems.append(f"{_label(edge)}: final command points {err:.1f} degrees away from where the line arrives")
    assert not problems, "WebKit would draw these arrowheads at 0 degrees, beside the line:\n" + "\n".join(problems)


def test_rendered_head_is_centred_on_the_line_end(arrow_edges):
    """Measure each rendered arrowhead's pixels against the path's end."""
    problems = []
    for edge in arrow_edges:
        pts = edge["headPixels"]
        if len(pts) < 20:
            problems.append(f"{_label(edge)}: no arrowhead drawn near the path end ({len(pts)} px)")
            continue
        ex, ey = edge["end"]["x"], edge["end"]["y"]
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        a = edge["arrive"]
        norm = math.hypot(a["x"], a["y"])
        ux, uy = a["x"] / norm, a["y"] / norm
        # The head's centroid lies on the line through the end point...
        off_line = abs((cx - ex) * -uy + (cy - ey) * ux)
        if off_line > 1.0:
            problems.append(f"{_label(edge)}: arrowhead centroid is {off_line:.2f}px off the line")
        # ...behind the end point, so the head points the way the line arrives...
        axis_err = _angle_between({"x": ex - cx, "y": ey - cy}, a)
        if axis_err > 10.0:
            problems.append(f"{_label(edge)}: arrowhead points {axis_err:.1f} degrees off the line")
        # ...and its tip is the end point. The marker's own 1-unit stroke with
        # round joins overshoots the geometric tip by ~0.6px at stroke 2; one
        # more device pixel absorbs anti-aliasing.
        tip = max((p[0] - ex) * ux + (p[1] - ey) * uy for p in pts)
        if abs(tip) > BORDER_TOLERANCE_PX:
            problems.append(f"{_label(edge)}: arrowhead tip is {tip:.2f}px from the path end")
    assert not problems, "\n".join(problems)
