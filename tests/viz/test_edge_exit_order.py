"""Edges that leave one node never cross near it, and leave in target order.

Dagre sorts the first bend of every edge by the node above it. Edges leaving
the same node all have that node above them, so they tie, and the tie-break
can put them in the opposite order to where each edge goes next. The edges
then swap sides just under the source. A gate's True edge heads left before
curving right to its target, while False heads right before curving left to
END. A function node's consumers tangle the same way.

These tests render real graphs in the browser. For every node with two or more
outgoing edges they check that no two of those edges intersect before
reaching the rank below the node, and that the edges leave in the left-to-right
order of their targets.
"""

from __future__ import annotations

import pytest

from hypergraph import END, Graph, ifelse, node
from tests.viz.conftest import HAS_PLAYWRIGHT, _cached_html_path, wait_for_debug_ready

pytestmark = pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")


# --- A gate whose False branch lands left and True branch lands right --------


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


def make_gate_graph() -> Graph:
    """``gate`` routes True to ``build`` (placed right) and False to END (placed left)."""
    return Graph([fetch, split, archive, gate, build, publish])


# --- A function node whose three consumers land left, middle and right --------


@node(output_name="base")
def origin() -> int:
    return 1


@node(output_name="item")
def spread(factor: int, base: int) -> int:
    return base * factor


@node(output_name="idx")
def index(base: int) -> int:
    return base


@node(output_name="merged")
def merge(mode: str, item: int, idx: int) -> int:
    return item + idx


@node(output_name="scored")
def score(weight: float, idx: int) -> float:
    return weight * idx


@node(output_name="a")
def first(base: int, item: int) -> int:
    return item


@node(output_name="b")
def second(base: int, item: int) -> int:
    return item


@node(output_name="summary")
def report(scored: float) -> str:
    return str(scored)


def make_fan_graph() -> Graph:
    """``spread`` feeds ``first``, ``second`` and ``merge``; ``merge`` is pulled right by ``index``."""
    return Graph([origin, spread, index, merge, score, first, second, report])


def _nested(make) -> Graph:
    """The same shape one level down, laid out by the compound (expanded-container) path."""
    return Graph([make().as_node(name="box")])


# name -> (graph, depth, the fan-out under test: source -> targets)
GRAPHS = {
    "gate": (make_gate_graph, 0, {"gate": {"build", "__end__"}}),
    "fan": (make_fan_graph, 0, {"spread": {"first", "second", "merge"}}),
    "gate_nested": (lambda: _nested(make_gate_graph), 1, {"box/gate": {"box/build", "__end__"}}),
    "fan_nested": (lambda: _nested(make_fan_graph), 1, {"box/spread": {"box/first", "box/second", "box/merge"}}),
}

# Depth below the source at which the exit direction is read. A gate's edges
# share the diamond's bottom vertex, so its exit order is the side each edge
# heads to, not a point on the border.
EXIT_PROBE_DEPTHS_PX = (4, 8, 12, 16, 24, 32, 40)
# Two targets (or two exits) closer than this are the same place, not an order.
SAME_PLACE_PX = 1.0

_PROBE_JS = r"""() => {
  const debug = window.__hypergraphVizDebug;
  const metaById = new Map((debug.layoutedEdges || []).map(e => [e.id, e]));
  const box = (id) => {
    const w = document.querySelector('.react-flow__node[data-id="' + CSS.escape(id) + '"]');
    if (!w) return null;
    const el = w.querySelector('div[style*="rotate(45deg)"]') ||
      Array.from(w.querySelectorAll('div')).find(d => parseFloat(getComputedStyle(d).borderTopWidth) > 0);
    const r = el.getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, left: r.left, right: r.right };
  };
  return Array.from(document.querySelectorAll('.react-flow__edge')).map(g => {
    const path = g.querySelector('path.react-flow__edge-path');
    const tid = (g.getAttribute('data-testid') || '').replace('rf__edge-', '');
    const meta = metaById.get(tid.replace(/_exp_.*$/, '')) || {};
    const data = meta.data || {};
    const m = path.getScreenCTM();
    const client = p => ({ x: m.a * p.x + m.c * p.y + m.e, y: m.b * p.x + m.d * p.y + m.f });
    const L = path.getTotalLength();
    const samples = [];
    for (let s = 0; s < L; s += 1) samples.push(client(path.getPointAtLength(s)));
    samples.push(client(path.getPointAtLength(L)));
    const source = data.actualSource || meta.source;
    const target = data.actualTarget || meta.target;
    return {
      source, target, edgeType: data.edgeType, feedback: !!data.isFeedbackEdge,
      samples, srcBox: box(source), tgtBox: box(target),
    };
  });
}"""


@pytest.fixture(scope="module")
def rendered_edges(_browser):
    """Every rendered edge of every graph, sampled along its path, keyed by graph name."""
    page = _browser.new_page(viewport={"width": 1200, "height": 1400})
    out = {}
    try:
        for name, (make, depth, _) in GRAPHS.items():
            page.goto(f"file://{_cached_html_path(make(), depth=depth)}")
            wait_for_debug_ready(page)
            out[name] = page.evaluate(_PROBE_JS)
    finally:
        page.close()
    return out


def _siblings(edges: list[dict]) -> dict[str, list[dict]]:
    by_source: dict[str, list[dict]] = {}
    for edge in edges:
        if not edge["feedback"]:
            by_source.setdefault(edge["source"], []).append(edge)
    return {source: group for source, group in by_source.items() if len(group) >= 2}


def _x_at_depth(edge: dict, y: float) -> float | None:
    for a, b in zip(edge["samples"], edge["samples"][1:], strict=False):
        if a["y"] <= y <= b["y"] and b["y"] > a["y"]:
            return a["x"] + (b["x"] - a["x"]) * (y - a["y"]) / (b["y"] - a["y"])
    return None


def _exit_side(a: dict, b: dict) -> int:
    """-1 when ``a`` leaves left of ``b``, +1 when right, 0 when they never separate."""
    bottom = a["srcBox"]["bottom"]
    for depth in EXIT_PROBE_DEPTHS_PX:
        xa, xb = _x_at_depth(a, bottom + depth), _x_at_depth(b, bottom + depth)
        if xa is None or xb is None:
            break
        if abs(xa - xb) > SAME_PLACE_PX / 2:
            return -1 if xa < xb else 1
    return 0


def _crosses(p: dict, q: dict, r: dict, s: dict) -> bool:
    def orient(a: dict, b: dict, c: dict) -> float:
        return (b["x"] - a["x"]) * (c["y"] - a["y"]) - (b["y"] - a["y"]) * (c["x"] - a["x"])

    return orient(p, q, r) * orient(p, q, s) < 0 and orient(r, s, p) * orient(r, s, q) < 0


def _first_crossing(a: dict, b: dict, below: float) -> dict | None:
    """The first point where the two paths cross above ``below`` (client y)."""
    sa = [pt for pt in a["samples"][2:] if pt["y"] < below]
    sb = [pt for pt in b["samples"][2:] if pt["y"] < below]
    for p, q in zip(sa, sa[1:], strict=False):
        for r, s in zip(sb, sb[1:], strict=False):
            if _crosses(p, q, r, s):
                return p
    return None


def _label(edge: dict) -> str:
    return f"{edge['source']} -> {edge['target']} ({edge['edgeType']})"


@pytest.mark.parametrize("graph_name", sorted(GRAPHS))
def test_graph_has_the_fan_out_under_test(rendered_edges, graph_name):
    groups = _siblings(rendered_edges[graph_name])
    for source, targets in GRAPHS[graph_name][2].items():
        assert source in groups, f"{source} has fewer than two outgoing edges: {sorted(groups)}"
        assert {e["target"] for e in groups[source]} == targets


@pytest.mark.parametrize("graph_name", sorted(GRAPHS))
def test_edges_leaving_one_node_do_not_cross_before_the_next_rank(rendered_edges, graph_name):
    problems = []
    for source, group in _siblings(rendered_edges[graph_name]).items():
        next_rank = min(e["tgtBox"]["top"] for e in group)
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                hit = _first_crossing(a, b, next_rank)
                if hit is not None:
                    problems.append(
                        f"{_label(a)} crosses {_label(b)} at ({hit['x']:.1f}, {hit['y']:.1f}), "
                        f"{hit['y'] - a['srcBox']['bottom']:.1f}px below {source}"
                    )
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("graph_name", sorted(GRAPHS))
def test_edges_leave_in_the_left_to_right_order_of_their_targets(rendered_edges, graph_name):
    problems = []
    for source, group in _siblings(rendered_edges[graph_name]).items():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                ta, tb = a["samples"][-1]["x"], b["samples"][-1]["x"]
                if abs(ta - tb) <= SAME_PLACE_PX:
                    continue
                want = -1 if ta < tb else 1
                got = _exit_side(a, b)
                if got != want:
                    left, right = (a, b) if want < 0 else (b, a)
                    problems.append(
                        f"{source}: {left['target']} lands left of {right['target']} "
                        f"({min(ta, tb):.1f} < {max(ta, tb):.1f}) but its edge leaves "
                        f"{'on the right' if got else 'on the same line'}"
                    )
    assert not problems, "\n".join(problems)
