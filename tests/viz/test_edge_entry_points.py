"""Every arrowhead points into its node, and heads on one node never overlap.

Ruling D53, the entry-side mirror of the sibling-exit rule:

1. An edge entering a node's top border arrives going downward. Its final
   segment is within 30 degrees of the border normal, so the head points into
   the node. A near-horizontal approach bends down before it lands.
2. Edges into the same node never land on the same point. Entry points are
   spread along the border in the left-to-right order of each edge's previous
   point, so no two heads overlap.

Dagre ends each edge at the point where the line from its last bend meets the
node's box, and the layout then clamps that point into the padded part of the
top border. A bend far to one side therefore lands nearly horizontally. Two
such edges can be clamped onto the same point: one head lies flat along the
border and the other hides it.

Three shapes, rendered in a real browser (the first two also inside an
expanded container, which the compound layout handles):
- repro: a gate's True edge and an edge from a node to its side converge on one
  target.
- fanin: three sources on the left, in the middle and on the right feed one
  node.
- side: a source far to the side, one rank above its target.

The graphs pin these assertions:
- the direction the final path command gives by itself points down;
- the rendered head's own pixels point down too;
- each head sits centred on its own line;
- each tip lies on its target's border;
- no two tips on one target are closer than a rendered head is wide.
"""

from __future__ import annotations

import base64
import math

import pytest

from hypergraph import END, Graph, ifelse, node
from tests.viz.conftest import HAS_PLAYWRIGHT, _cached_html_path, wait_for_debug_ready

pytestmark = pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")


# --- repro: a gate's True edge and a side edge converge on one target ---------


@node(output_name="receipt", emit="fetched")
def fetch(url: str, key: str) -> str:
    return url


@node(output_name="parts", wait_for="fetched")
def split(key: str) -> list:
    return [key]


@node(output_name="archived_at", emit="archived")
def archive(parts: list) -> str:
    return "archived"


@ifelse(when_true="build", when_false=END, wait_for="archived")
def gate(flags: list) -> bool:
    return bool(flags)


@node(output_name="table")
def build(parts: list, flags: list, limit: int) -> list:
    return parts


@node(output_name="revision")
def publish(table: list, owner: str, note: str) -> str:
    return "revision"


def make_repro_graph() -> Graph:
    """``gate`` (far left) sends True to ``build`` (far right) one rank below."""
    return Graph([fetch, split, archive, gate, build, publish])


# --- fanin: three sources left, middle and right into one node ----------------


@node(output_name="left_part")
def west(a: int) -> int:
    return a


@node(output_name="mid_part")
def centre(b: int) -> int:
    return b


@node(output_name="right_part")
def east(c: int) -> int:
    return c


@node(output_name="joined")
def join(left_part: int, mid_part: int, right_part: int) -> int:
    return left_part + mid_part + right_part


def make_fanin_graph() -> Graph:
    return Graph([west, centre, east, join])


# --- side: a source far to the side, one rank above its target ----------------


@node(output_name="base_left")
def origin_left(alpha: int) -> int:
    return alpha


@node(output_name="base_right")
def origin_right(beta: int) -> int:
    return beta


@node(output_name="refined")
def refine(base_right: int) -> int:
    return base_right


@node(output_name="side")
def origin_side(gamma: int) -> int:
    return gamma


@node(output_name="combo")
def combine(base_right: int, refined: int, side: int) -> int:
    return base_right + refined + side


@node(output_name="lifted")
def lift(base_left: int) -> int:
    return base_left


@node(output_name="result")
def finish(base_right: int, side: int, lifted: int) -> int:
    return base_right + side + lifted


def make_side_graph() -> Graph:
    """``lift`` sits ~400px right of ``finish``, one rank above it."""
    return Graph([origin_left, origin_right, refine, origin_side, combine, lift, finish])


def _nested(make) -> Graph:
    """The same shape one level down, laid out by the compound (expanded-container) path."""
    return Graph([make().as_node(name="box")])


# name -> (graph factory, show_inputs, depth, the converging edges this case exists for)
CASES = {
    "repro": (make_repro_graph, True, 0, {"build": {"gate", "input_limit"}}),
    "fanin": (make_fanin_graph, False, 0, {"join": {"west", "centre", "east"}}),
    "side": (make_side_graph, False, 0, {"finish": {"lift"}}),
    "repro_nested": (lambda: _nested(make_repro_graph), True, 1, {"box/build": {"box/gate", "input_limit"}}),
    "fanin_nested": (lambda: _nested(make_fanin_graph), False, 1, {"box/join": {"box/west", "box/centre", "box/east"}}),
}

# D53: the final segment within 30 degrees of the top border's normal.
MAX_ENTRY_ANGLE_DEG = 30.0
# Same tolerance as the ARROWS and edge-meets-diamond checks: 1px borders at
# fractional layout positions.
BORDER_TOLERANCE_PX = 1.5
# The centred-head checks of test_edge_arrowheads.
MAX_OFF_LINE_PX = 1.0
MAX_AXIS_ERR_DEG = 10.0

_PROBE_JS = r"""() => {
  const debug = window.__hypergraphVizDebug;
  const metaById = new Map((debug.layoutedEdges || []).map(e => [e.id, e]));
  const box = (id) => {
    const w = document.querySelector('.react-flow__node[data-id="' + CSS.escape(id) + '"]');
    if (!w) return null;
    const diamond = w.querySelector('div[style*="rotate(45deg)"]');
    const el = diamond ||
      Array.from(w.querySelectorAll('div')).find(d => parseFloat(getComputedStyle(d).borderTopWidth) > 0);
    const r = el.getBoundingClientRect();
    return { top: r.top, left: r.left, right: r.right, diamond: !!diamond };
  };
  const parseD = (d) => {
    const segs = [];
    (d.match(/[A-Za-z]|-?\d*\.?\d+(?:e[-+]?\d+)?/g) || []).forEach(t => {
      if (/[A-Za-z]/.test(t)) segs.push({ cmd: t, nums: [] });
      else segs[segs.length - 1].nums.push(parseFloat(t));
    });
    return segs;
  };
  return Array.from(document.querySelectorAll('.react-flow__edge')).map((g, index) => {
    const path = g.querySelector('path.react-flow__edge-path');
    const tid = (g.getAttribute('data-testid') || '').replace('rf__edge-', '');
    const meta = metaById.get(tid.replace(/_exp_.*$/, '')) || {};
    const data = meta.data || {};
    const m = path.getScreenCTM();
    const client = p => ({ x: m.a * p.x + m.c * p.y + m.e, y: m.b * p.x + m.d * p.y + m.f });
    const L = path.getTotalLength();
    const end = client(path.getPointAtLength(L));
    const before = client(path.getPointAtLength(Math.max(0, L - 0.5)));
    const segs = parseD(path.getAttribute('d'));
    const last = segs[segs.length - 1], n = last.nums;
    let from = null;
    if (last.cmd === 'C') from = { x: n[2], y: n[3] };
    else if (last.cmd === 'Q') from = { x: n[0], y: n[1] };
    else if (last.cmd === 'L' && segs.length > 1) {
      const pn = segs[segs.length - 2].nums;
      from = { x: pn[pn.length - 2], y: pn[pn.length - 1] };
    }
    const a = from ? client(from) : null, b = client({ x: n[n.length - 2], y: n[n.length - 1] });
    const source = data.actualSource || meta.source, target = data.actualTarget || meta.target;
    return {
      index, source, target, edgeType: data.edgeType, feedback: !!data.isFeedbackEdge,
      d: path.getAttribute('d'), end,
      arrive: { x: end.x - before.x, y: end.y - before.y },
      ownDir: a ? { x: b.x - a.x, y: b.y - a.y } : null,
      box: box(target),
    };
  });
}"""

# Leave only arrowheads on screen: nodes hidden (a head tucked under a node is
# still measured), strokes transparent, labels and chrome gone.
_HEADS_ONLY_CSS = (
    ".react-flow__node{visibility:hidden!important}"
    ".react-flow__edge-path{stroke:transparent!important}"
    ".react-flow__background,.react-flow__panel,.react-flow__edgelabel-renderer{display:none!important}"
)

# Show exactly one edge's arrowhead, so heads that overlap are still measured
# one at a time.
_ONLY_HEAD_JS = r"""(keep) => {
  document.querySelectorAll('.react-flow__edge').forEach((g, i) => {
    const p = g.querySelector('path.react-flow__edge-path');
    if (!p.dataset.markerEnd) p.dataset.markerEnd = p.getAttribute('marker-end') || '';
    if (i === keep) p.setAttribute('marker-end', p.dataset.markerEnd);
    else p.removeAttribute('marker-end');
  });
}"""

_PIXELS_JS = r"""async ({ png, x0, y0, scale }) => {
  const img = new Image();
  img.src = 'data:image/png;base64,' + png;
  await img.decode();
  const c = document.createElement('canvas');
  c.width = img.naturalWidth; c.height = img.naturalHeight;
  const ctx = c.getContext('2d');
  ctx.drawImage(img, 0, 0);
  const px = ctx.getImageData(0, 0, c.width, c.height).data;
  const at = (x, y) => { const i = (y * c.width + x) * 4; return [px[i], px[i + 1], px[i + 2]]; };
  const bg = at(0, 0);
  const pts = [];
  for (let y = 0; y < c.height; y++) {
    for (let x = 0; x < c.width; x++) {
      const p = at(x, y);
      if (Math.abs(p[0] - bg[0]) + Math.abs(p[1] - bg[1]) + Math.abs(p[2] - bg[2]) > 60) {
        pts.push([x0 + (x + 0.5) / scale, y0 + (y + 0.5) / scale]);
      }
    }
  }
  return pts;
}"""


def _render(browser, make, show_inputs: bool, depth: int) -> list[dict]:
    page = browser.new_page(viewport={"width": 1200, "height": 1400}, device_scale_factor=2)
    try:
        page.goto(f"file://{_cached_html_path(make(), depth=depth, show_inputs=show_inputs, show_bounded_inputs=False)}")
        wait_for_debug_ready(page)
        edges = [e for e in page.evaluate(_PROBE_JS) if not e["feedback"]]
        zoom = page.evaluate(
            "parseFloat((document.querySelector('.react-flow__viewport').style.transform.match(/scale\\(([\\d.]+)\\)/) || [0, 1])[1])"
        )
        # An ArrowClosed head is 9 marker units across and 6 long, with its stroke,
        # at 12.5/20 px per unit per stroke px (<= 2): under 12px. A 16px box
        # around the tip holds all of it.
        radius = 16 * zoom
        page.add_style_tag(content=_HEADS_ONLY_CSS)
        for edge in edges:
            page.evaluate(_ONLY_HEAD_JS, edge["index"])
            # Clip on whole CSS pixels. Engines round a fractional clip origin to
            # different device pixels (WebKit lands one device pixel from
            # Chromium), which shifts every measured pixel and tilts a straight
            # head by up to 13 degrees in WebKit, 2 in Chromium.
            x0, y0 = math.floor(edge["end"]["x"] - radius), math.floor(edge["end"]["y"] - radius)
            size = math.ceil(2 * radius) + 1
            clip = {"x": x0, "y": y0, "width": size, "height": size}
            png = base64.b64encode(page.screenshot(clip=clip)).decode()
            edge["headPixels"] = page.evaluate(_PIXELS_JS, {"png": png, "x0": x0, "y0": y0, "scale": 2})
    finally:
        page.close()
    return edges


@pytest.fixture(scope="module")
def rendered(_browser):
    return {name: _render(_browser, make, show_inputs, depth) for name, (make, show_inputs, depth, _) in CASES.items()}


def _angle_from_down(v: dict) -> float:
    """Signed degrees between ``v`` and straight down (+y)."""
    return math.degrees(math.atan2(v["x"], v["y"]))


def _angle_between(u: dict, v: dict) -> float:
    a = math.degrees(math.atan2(u["y"], u["x"]) - math.atan2(v["y"], v["x"]))
    return abs((a + 180) % 360 - 180)


def _head(edge: dict) -> dict | None:
    """The rendered head measured from its own pixels, in client px."""
    pts = edge["headPixels"]
    if len(pts) < 20:
        return None
    ex, ey = edge["end"]["x"], edge["end"]["y"]
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    axis = {"x": ex - cx, "y": ey - cy}
    norm = math.hypot(axis["x"], axis["y"]) or 1.0
    px, py = -axis["y"] / norm, axis["x"] / norm
    across = [(p[0] - cx) * px + (p[1] - cy) * py for p in pts]
    a = edge["arrive"]
    an = math.hypot(a["x"], a["y"]) or 1.0
    return {
        "axis": axis,
        "width": max(across) - min(across),
        "off_line": abs((cx - ex) * -a["y"] / an + (cy - ey) * a["x"] / an),
    }


def _label(edge: dict) -> str:
    return f"{edge['source']} -> {edge['target']} ({edge['edgeType']})"


def _by_target(edges: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for edge in edges:
        out.setdefault(edge["target"], []).append(edge)
    return out


@pytest.mark.parametrize("case", sorted(CASES))
def test_case_renders_the_shape_under_test(rendered, case):
    by_target = _by_target(rendered[case])
    for target, sources in CASES[case][3].items():
        assert sources <= {e["source"] for e in by_target.get(target, [])}, (
            f"{case}: expected {sorted(sources)} into {target}, got {[_label(e) for e in by_target.get(target, [])]}"
        )


@pytest.mark.parametrize("case", sorted(CASES))
def test_final_segment_points_into_the_node(rendered, case):
    problems = []
    for edge in rendered[case]:
        own = edge["ownDir"]
        if own is None or math.hypot(own["x"], own["y"]) < 0.5:
            problems.append(f"{_label(edge)}: final path command has no direction of its own")
            continue
        angle = _angle_from_down(own)
        if abs(angle) > MAX_ENTRY_ANGLE_DEG:
            problems.append(f"{_label(edge)}: lands {angle:+.1f} deg from straight down (limit {MAX_ENTRY_ANGLE_DEG:.0f})")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("case", sorted(CASES))
def test_rendered_head_points_into_the_node_and_sits_on_its_line(rendered, case):
    problems = []
    for edge in rendered[case]:
        head = _head(edge)
        if head is None:
            problems.append(f"{_label(edge)}: no arrowhead drawn at the path end")
            continue
        angle = _angle_from_down(head["axis"])
        if abs(angle) > MAX_ENTRY_ANGLE_DEG:
            problems.append(f"{_label(edge)}: rendered head points {angle:+.1f} deg from straight down")
        err = _angle_between(head["axis"], edge["arrive"])
        if err > MAX_AXIS_ERR_DEG or head["off_line"] > MAX_OFF_LINE_PX:
            problems.append(f"{_label(edge)}: head is {err:.1f} deg / {head['off_line']:.2f}px off its own line")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("case", sorted(CASES))
def test_tip_lies_on_the_target_border(rendered, case):
    problems = []
    for edge in rendered[case]:
        tip, box = edge["end"], edge["box"]
        border_y = box["top"]
        if box["diamond"]:
            # The rotated square's top edges fall 1px per px from its top vertex.
            border_y += abs(tip["x"] - (box["left"] + box["right"]) / 2)
        if abs(tip["y"] - border_y) > BORDER_TOLERANCE_PX or not box["left"] <= tip["x"] <= box["right"]:
            problems.append(f"{_label(edge)}: tip ({tip['x']:.1f}, {tip['y']:.1f}) is off the border y={border_y:.1f}")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("case", sorted(CASES))
def test_heads_on_one_node_never_overlap(rendered, case):
    """Two tips on one target sit at least one rendered head-width apart."""
    problems = []
    for target, group in _by_target(rendered[case]).items():
        heads = {id(e): _head(e) for e in group}
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                ha, hb = heads[id(a)], heads[id(b)]
                if ha is None or hb is None:
                    continue
                width = max(ha["width"], hb["width"])
                gap = math.hypot(a["end"]["x"] - b["end"]["x"], a["end"]["y"] - b["end"]["y"])
                if gap < width:
                    problems.append(
                        f"{target}: tips of {a['source']} and {b['source']} are {gap:.1f}px apart, closer than a head is wide ({width:.1f}px)"
                    )
    assert not problems, "\n".join(problems)
