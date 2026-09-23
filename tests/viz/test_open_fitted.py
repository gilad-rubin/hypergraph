"""visualize() opens fitted to the view, never below a readable zoom (#598, ruling D58).

One rule for notebook cells and standalone pages:

- On first render the whole graph fits inside the visible canvas, at zoom <= 1
  and never below ``MIN_READABLE_ZOOM``.
- A graph that cannot fit at that zoom opens at it, on its first steps (the
  top of the graph), centred horizontally. Never on a corner, never clipped at
  the top.
- A toolbar toggle that re-lays out the graph (inputs, types, separate
  outputs, simplify) fits again, the same fit a fresh page opens with. That
  holds until the user pans or zooms (drag, wheel, pinch, the zoom buttons).
  Fit View fits and re-arms it. A fit or a pinned step's pan (#595) is not a
  user move.

Legibility threshold: a node's text must render at 11 CSS px or more. That is
Apple's minimum text size for iOS (Human Interface Guidelines, Typography:
default 17 pt, minimum 11 pt; macOS minimum 10 pt). A width=device-width page
on a phone, and a notebook on a desktop, draw 1 CSS px per point. The smallest
text on a node is output names and types (``text-xs``, 12 px at zoom 1), so
``MIN_READABLE_ZOOM = 11 / 12``. At that zoom a node name (``text-sm``, 14 px)
renders at 12.8 px.

Every browser test renders once per engine in HYPERGRAPH_VIZ_ENGINES (the
``_browser`` fixture), so CI runs them in Chromium and in WebKit.
"""

from __future__ import annotations

import math

import pytest

from hypergraph import FunctionNode, Graph, node
from tests.viz.conftest import HAS_PLAYWRIGHT
from tests.viz.test_ghost_inputs import _EMPTY_POINT_JS, make_ghost_graph

pytestmark = pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")

LEGIBLE_PX = 11
MIN_READABLE_ZOOM = LEGIBLE_PX / 12

DESKTOP = {"viewport": {"width": 1280, "height": 900}}
PHONE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 3, "is_mobile": True, "has_touch": True}
SIZES = {"desktop": DESKTOP, "phone": PHONE}

# Every call passes both input flags explicitly, with the public defaults.
INPUTS_HIDDEN = {"show_inputs": False, "show_bounded_inputs": True}


# --- generic graphs ---------------------------------------------------------------


@node(output_name="loaded")
def load(source: str) -> str:
    return source


@node(output_name="cleaned")
def clean(loaded: str) -> str:
    return loaded


@node(output_name="summary")
def report(cleaned: str) -> str:
    return cleaned


def make_small_graph() -> Graph:
    """Three steps: fits any screen at zoom 1."""
    return Graph([load, clean, report], name="small")


def _advance(value: int) -> int:
    return value + 1


def make_tall_chain(length: int = 16) -> Graph:
    """``step_01 -> step_02 -> ... -> step_16``: too tall to fit at the readable zoom."""
    steps = [
        FunctionNode(_advance, name=f"step_{i:02d}", output_name=f"value_{i:02d}", rename_inputs={"value": f"value_{i - 1:02d}"})
        for i in range(1, length + 1)
    ]
    return Graph(steps, name="tall_chain")


@node(output_name="batch")
def split(source: str) -> str:
    return source


def make_wide_graph(width: int = 10) -> Graph:
    """``split`` fans out to ten parallel steps: too wide to fit at the readable zoom."""
    branches = [
        FunctionNode(_advance, name=f"branch_{i:02d}", output_name=f"part_{i:02d}", rename_inputs={"value": "batch"}) for i in range(1, width + 1)
    ]
    return Graph([split, *branches], name="wide")


# --- rendering --------------------------------------------------------------------


@pytest.fixture(scope="module")
def html_files(tmp_path_factory):
    out = tmp_path_factory.mktemp("fitted")
    variants = {
        "small": (make_small_graph, {}),
        "tall": (make_tall_chain, {}),
        "wide": (make_wide_graph, {}),
        "ghost": (make_ghost_graph, {}),
        "ghost_types_off": (make_ghost_graph, {"show_types": False}),
        "ghost_inputs_shown": (make_ghost_graph, {"show_inputs": True, "show_bounded_inputs": True}),
    }
    paths = {}
    for name, (make, kwargs) in variants.items():
        path = out / f"{name}.html"
        make().visualize(filepath=str(path), **{**INPUTS_HIDDEN, **kwargs})
        paths[name] = str(path)
    return paths


@pytest.fixture
def open_page(_browser, html_files):
    contexts = []

    def _open(variant: str, size: dict):
        context = _browser.new_context(**size)
        contexts.append(context)
        page = context.new_page()
        page.goto(f"file://{html_files[variant]}")
        _wait_ready(page)
        return page

    yield _open
    for context in contexts:
        context.close()


def _frames(page, n: int = 2) -> None:
    page.evaluate(f"() => new Promise(r => {{ let k = {n}; const f = () => (--k > 0 ? requestAnimationFrame(f) : r()); requestAnimationFrame(f); }})")


def _wait_ready(page) -> None:
    page.wait_for_function(
        "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0 && window.__hypergraphVizReady === true",
        timeout=15000,
    )
    _frames(page)


# --- page probes -------------------------------------------------------------------

_VIEW_JS = """() => {
  const vp = document.querySelector('.react-flow__viewport');
  const m = new DOMMatrixReadOnly(getComputedStyle(vp).transform);
  const box = el => { const r = el.getBoundingClientRect(); return {left: r.left, top: r.top, right: r.right, bottom: r.bottom}; };
  const nodes = {};
  document.querySelectorAll('.react-flow__node').forEach(el => { nodes[el.getAttribute('data-id')] = box(el); });
  return {zoom: m.a, x: m.e, y: m.f, width: window.innerWidth, height: window.innerHeight, nodes};
}"""

# Every text run inside one node, with the size it is drawn at on screen: the
# CSS font size times the scale of every transformed ancestor (the viewport's
# zoom). getBoundingClientRect() / offsetWidth is not used: offsetWidth rounds.
_NODE_TEXT_JS = """(id) => {
  const root = document.querySelector(`.react-flow__node[data-id="${id}"]`);
  const scaleOf = el => {
    let k = 1;
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      const t = getComputedStyle(e).transform;
      if (t && t !== 'none') k *= Math.hypot(new DOMMatrixReadOnly(t).a, new DOMMatrixReadOnly(t).b);
    }
    return k;
  };
  const out = [];
  root.querySelectorAll('*').forEach(el => {
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim();
    if (!own || !el.getClientRects().length) return;
    out.push({text: own, px: parseFloat(getComputedStyle(el).fontSize) * scaleOf(el)});
  });
  return out;
}"""


def _view(page) -> dict:
    return page.evaluate(_VIEW_JS)


def _union(boxes) -> dict:
    boxes = list(boxes)
    return {
        "left": min(b["left"] for b in boxes),
        "top": min(b["top"] for b in boxes),
        "right": max(b["right"] for b in boxes),
        "bottom": max(b["bottom"] for b in boxes),
    }


def _inside(box: dict, view: dict, tol: float = 0.5) -> bool:
    return box["left"] >= -tol and box["top"] >= -tol and box["right"] <= view["width"] + tol and box["bottom"] <= view["height"] + tol


def _outside(view: dict) -> list[str]:
    return [f"{nid} at { ({k: round(v, 1) for k, v in b.items()}) }" for nid, b in view["nodes"].items() if not _inside(b, view)]


def _same_view(a: dict, b: dict, tol: float = 0.5) -> bool:
    return abs(a["x"] - b["x"]) <= tol and abs(a["y"] - b["y"]) <= tol and abs(a["zoom"] - b["zoom"]) <= 1e-3


def _fmt(view: dict) -> str:
    return f"zoom={view['zoom']:.4f} x={view['x']:.1f} y={view['y']:.1f}"


def _content_height(page) -> float:
    """The graph's height at zoom 1, from the rendered node boxes."""
    view = _view(page)
    u = _union(view["nodes"].values())
    return (u["bottom"] - u["top"]) / view["zoom"]


def _click_toolbar(page, label: str, *, tap: bool = False) -> None:
    button = f'button[aria-label="{label}"]'
    if tap:
        page.tap(button, timeout=5000)
    else:
        page.click(button)
    _wait_ready(page)


# --- first render -----------------------------------------------------------------


@pytest.mark.parametrize("size", list(SIZES))
def test_small_graph_opens_whole_centred_and_unmagnified(open_page, size):
    view = _view(open_page("small", SIZES[size]))
    assert not _outside(view), f"{size}: nodes outside the canvas: {_outside(view)}"
    assert view["zoom"] == pytest.approx(1, abs=1e-3), f"{size}: a graph that fits opens at zoom 1, not magnified: {_fmt(view)}"
    u = _union(view["nodes"].values())
    assert abs((u["left"] + u["right"]) / 2 - view["width"] / 2) <= 3, f"{size}: graph centred horizontally: {u}"
    assert abs((u["top"] + u["bottom"]) / 2 - view["height"] / 2) <= 3, f"{size}: graph centred vertically: {u}"


def test_graph_that_fits_only_zoomed_out_opens_whole(open_page):
    """A standalone page a little shorter than the graph: zoom out to fit, never clip the top."""
    height = _content_height(open_page("ghost", {"viewport": {"width": 1280, "height": 2400}}))
    size = {"viewport": {"width": 1280, "height": math.ceil(0.95 * height + 60)}}
    view = _view(open_page("ghost", size))
    assert not _outside(view), f"nodes outside a {size['viewport']} canvas: {_outside(view)} ({_fmt(view)})"
    assert MIN_READABLE_ZOOM <= view["zoom"] < 1, f"zoomed out to fit, not below the readable minimum: {_fmt(view)}"


@pytest.mark.parametrize("size", list(SIZES))
def test_tall_chain_opens_at_the_readable_minimum_on_its_first_steps(open_page, size):
    view = _view(open_page("tall", SIZES[size]))
    assert view["zoom"] == pytest.approx(MIN_READABLE_ZOOM, abs=2e-3), f"{size}: a graph too tall to fit opens at the readable minimum: {_fmt(view)}"
    first = view["nodes"]["step_01"]
    assert _inside(first, view), f"{size}: the first step is on screen: {first}"
    assert first["top"] <= 40, f"{size}: the first step sits at the top: {first}"
    assert view["nodes"]["step_02"]["top"] < view["height"], f"{size}: the next step follows it on screen"
    centre = (first["left"] + first["right"]) / 2
    assert abs(centre - view["width"] / 2) <= 3, f"{size}: the chain is centred horizontally: step_01 centre {centre:.1f}"


@pytest.mark.parametrize("size", list(SIZES))
def test_wide_graph_opens_centred_at_the_readable_minimum(open_page, size):
    view = _view(open_page("wide", SIZES[size]))
    assert view["zoom"] == pytest.approx(MIN_READABLE_ZOOM, abs=2e-3), f"{size}: a graph too wide to fit opens at the readable minimum: {_fmt(view)}"
    u = _union(view["nodes"].values())
    assert u["left"] < 0 and u["right"] > view["width"], f"{size}: cropped on both sides, not parked on a corner: {u}"
    assert abs((u["left"] + u["right"]) / 2 - view["width"] / 2) <= 3, f"{size}: centred horizontally: {u}"
    assert _inside(view["nodes"]["split"], view), f"{size}: the entry step is on screen: {view['nodes']['split']}"
    assert u["top"] >= 0 and u["bottom"] <= view["height"], f"{size}: nothing clipped top or bottom: {u}"


def test_phone_shows_the_first_steps_legibly(open_page):
    page = open_page("ghost", PHONE)
    view = _view(page)
    first = view["nodes"]["fetch"]
    assert _inside(first, view), f"the first step is on screen at 390 px: {first} ({_fmt(view)})"
    assert first["top"] <= 60, f"the first step sits at the top: {first}"
    texts = page.evaluate(_NODE_TEXT_JS, "fetch")
    labels = {t["text"] for t in texts}
    assert {"fetch", "raw", ": str"} <= labels, f"the node shows its name and output type: {sorted(labels)}"
    small = [f"{t['text']!r} at {t['px']:.2f}px" for t in texts if t["px"] < LEGIBLE_PX - 0.01]
    assert not small, f"text below {LEGIBLE_PX} px: {small}"


# --- toggles and user moves -------------------------------------------------------------


def _toggle_size(open_page) -> dict:
    """A desktop page where the ghost graph fits at zoom 1, but not once its inputs are shown."""
    hidden = _content_height(open_page("ghost", {"viewport": {"width": 1280, "height": 2400}}))
    shown = _content_height(open_page("ghost_inputs_shown", {"viewport": {"width": 1280, "height": 2400}}))
    height = math.ceil(max(hidden + 60, 0.95 * shown + 60))
    assert height - 60 < shown, f"showing inputs must not fit at zoom 1 (hidden {hidden:.0f}, shown {shown:.0f})"
    return {"viewport": {"width": 1280, "height": height}}


def test_toggle_refits_like_a_fresh_open(open_page):
    size = _toggle_size(open_page)
    page = open_page("ghost", size)
    at_rest = _view(page)
    assert not _outside(at_rest), f"opens whole: {_outside(at_rest)}"

    _click_toolbar(page, "Show Inputs")
    view = _view(page)
    fresh = _view(open_page("ghost_inputs_shown", size))
    assert not _outside(view), f"after Show Inputs, nodes outside the canvas: {_outside(view)} ({_fmt(view)})"
    assert view["zoom"] < 1, f"the taller graph is zoomed out to fit: {_fmt(view)}"
    assert _same_view(view, fresh), f"Show Inputs fits like a fresh page: {_fmt(view)} vs fresh {_fmt(fresh)}"

    _click_toolbar(page, "Hide Inputs")
    assert _same_view(_view(page), at_rest), f"Hide Inputs fits back: {_fmt(_view(page))} vs {_fmt(at_rest)}"


def test_toggle_keeps_the_view_after_the_user_pans_or_zooms(open_page):
    size = _toggle_size(open_page)
    fresh_shown = _view(open_page("ghost_inputs_shown", size))

    # A drag on the empty canvas pans; a toggle then leaves the view alone.
    page = open_page("ghost", size)
    at_rest = _view(page)
    point = page.evaluate(_EMPTY_POINT_JS)
    assert point, "an empty canvas point to drag from"
    page.mouse.move(point["x"], point["y"])
    page.mouse.down()
    page.mouse.move(point["x"] + 90, point["y"] + 50, steps=8)
    page.mouse.up()
    _frames(page)
    panned = _view(page)
    assert not _same_view(panned, at_rest), "the drag panned the view"
    _click_toolbar(page, "Show Inputs")
    assert _same_view(_view(page), panned), f"a toggle after a pan keeps the view: {_fmt(_view(page))} vs {_fmt(panned)}"

    # Fit View fits, and toggles fit again from there.
    _click_toolbar(page, "Fit View")
    assert _same_view(_view(page), fresh_shown), f"Fit View fits: {_fmt(_view(page))} vs fresh {_fmt(fresh_shown)}"
    _click_toolbar(page, "Hide Inputs")
    assert _same_view(_view(page), at_rest), f"after Fit View a toggle fits again: {_fmt(_view(page))} vs {_fmt(at_rest)}"

    # The zoom buttons are a user move too.
    page = open_page("ghost", size)
    page.click('button[aria-label="Zoom Out"]')
    page.wait_for_function(
        f"new DOMMatrixReadOnly(getComputedStyle(document.querySelector('.react-flow__viewport')).transform).a < {at_rest['zoom'] - 0.01}"
    )
    _frames(page)
    zoomed = _view(page)
    _click_toolbar(page, "Hide Types")
    assert _same_view(_view(page), zoomed), f"a toggle after a zoom keeps the view: {_fmt(_view(page))} vs {_fmt(zoomed)}"


def test_pin_pans_into_view_after_the_fit_and_is_not_a_user_move(open_page):
    """#595's pin still frames a step the opening view leaves off screen.

    Its pan is not a user move, so a toggle afterwards fits again.
    """
    page = open_page("ghost", PHONE)
    view = _view(page)
    box = view["nodes"]["archive"]
    assert not _inside(box, view), f"the opening view leaves 'archive' partly off screen: {box} ({_fmt(view)})"
    # Tap its visible part, clear of the toolbar strip on the right.
    x = (max(box["left"], 4) + min(box["right"], view["width"] - 80)) / 2
    y = (max(box["top"], 4) + min(box["bottom"], view["height"] - 4)) / 2
    framed = page.evaluate("window.__hypergraphVizPinFramed || 0")
    page.touchscreen.tap(x, y)
    page.wait_for_function(f"(window.__hypergraphVizPinFramed || 0) > {framed}", timeout=15000)
    pinned = _view(page)
    assert _inside(pinned["nodes"]["archive"], pinned), f"the pin pans 'archive' on screen: {pinned['nodes']['archive']}"

    _click_toolbar(page, "Hide Types", tap=True)
    fresh = _view(open_page("ghost_types_off", PHONE))
    assert _same_view(_view(page), fresh), f"after a pin, a toggle still fits: {_fmt(_view(page))} vs fresh {_fmt(fresh)}"
