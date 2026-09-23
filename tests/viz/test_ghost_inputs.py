"""visualize() hides inputs by default and reveals each step's inputs as ghost pills (#595).

Rulings D56/D57. With inputs hidden (the default everywhere), the graph shows
steps only. Hovering a step on a desktop, or tapping it on a touch screen,
draws that step's inputs as ghost pills beside it:

(a) inputs no step produces, as ``name : Type``;
(b) bound tools, faded and dashed (left out when ``show_bounded_inputs=False``);
(c) an input another step produces whose arrow ``simplify`` hides, labelled
    with its source, ``raw ← fetch``.

The step's upstream and downstream path stays lit and everything else dims,
the gate's True/False labels included, through the edge component's own
state. A click or tap pins the ghosts and pans them on screen; an empty-canvas
click or Escape clears them, and so does the Show/Hide Inputs toggle. With
inputs shown there are no ghosts, and bound tools render as faded, dashed
input boxes unless ``show_bounded_inputs=False``.

DOM contract the page exposes for these tests:
- ghost pill: ``[data-hg-ghost=<value name>]`` with ``data-hg-ghost-kind``
  (``input`` | ``bound`` | ``hidden-edge``) and ``data-hg-ghost-for=<step id>``;
  its text is the pill label;
- node focus: the React Flow node wrapper carries ``hg-focus`` (the step) or
  ``hg-dim`` (off the path);
- edge: ``[data-hg-edge]`` (the edge's path group) and ``[data-hg-edge-label]``
  (its label), both with ``data-hg-source``, ``data-hg-target`` and
  ``data-hg-dimmed``, set by the edge component itself;
- pin framed: ``window.__hypergraphVizPinFramed`` counts pins whose pan has
  reached its target on screen (see ``_pin``).

Every browser test renders once per engine in HYPERGRAPH_VIZ_ENGINES (the
``_browser`` fixture), so CI runs them in Chromium and in WebKit.
"""

from __future__ import annotations

import inspect
import re
import tempfile
import warnings

import pytest

from hypergraph import Graph, ifelse, node

# --- the synthetic graph -----------------------------------------------------


class Parser:
    """A tool bound at startup."""


@node(output_name="raw")
def fetch(url: str, timeout: int) -> str:
    return url


@node(output_name="parsed")
def parse(raw: str, parser: Parser) -> str:
    return raw


@node(output_name="summary")
def summarize(parsed: str, raw: str) -> str:
    return parsed + raw


@ifelse(when_true="publish", when_false="archive")
def is_ready(summary: str) -> bool:
    return bool(summary)


@node(output_name="receipt")
def publish(summary: str, channel: str) -> str:
    return summary


@node(output_name="archived")
def archive(summary: str) -> str:
    return summary


@node(output_name="tags")
def tag(summary: str, locale: str) -> list:
    return [summary, locale]


@node(output_name="score")
def score(tags: list) -> float:
    return float(len(tags))


@node(output_name="batch")
def split(source: str) -> list:
    return [source]


@node(output_name="left_part")
def left(batch: list) -> int:
    return len(batch)


@node(output_name="mid_part")
def middle(batch: list, region: str, window_size: int, retry_policy: dict[str, int], threshold: float) -> int:
    return len(batch)


@node(output_name="right_part")
def right(batch: list) -> int:
    return len(batch)


@node(output_name="merged")
def merge(left_part: int, mid_part: int, right_part: int) -> int:
    return left_part + mid_part + right_part


def make_crowded_graph() -> Graph:
    """``middle`` has four typed inputs and a sibling close on each side.

    Neither side has room for the pills, so placement falls back to a row
    above or below the step.
    """
    return Graph([split, left, middle, right, merge], name="crowded")


CROWDED_GHOSTS = [
    ("region", "input", "region : str"),
    ("window_size", "input", "window_size : int"),
    ("retry_policy", "input", "retry_policy : dict[str, int]"),
    ("threshold", "input", "threshold : float"),
]


@node(output_name="seed")
def start(src: str) -> str:
    return src


@ifelse(when_true="wide", when_false="alt")
def gate(seed: str) -> bool:
    return bool(seed)


@node(output_name="w")
def wide(seed: str, p1: int, p2: int, p3: int, p4: int, p5: int, p6: int, p7: int, p8: int, p9: int, p10: int) -> int:
    return p1


@node(output_name="w")
def alt(seed: str, q1: dict[str, list[int]], q2: dict[str, list[int]], q3: str) -> int:
    return len(q3)


@node(output_name="l1")
def side_l(seed: str) -> int:
    return 1


@node(output_name="r1")
def side_r(seed: str) -> int:
    return 1


@node(output_name="z")
def join(w: int, l1: int, r1: int, seed: str, zz: str) -> int:
    return w


def make_gate_labels_graph() -> Graph:
    """A gate's True/False labels sit where ``alt``'s and ``wide``'s pills would go.

    ``gate`` sends True to ``wide`` (ten inputs) and False to ``alt`` (three,
    two with long types), and both targets have siblings close beside them.
    Placement that ignored edge labels would put ``alt``'s row on the labels.
    """
    return Graph([start, gate, wide, alt, side_l, side_r, join], name="gated")


GATE_LABEL_GHOSTS = {
    "alt": [
        ("q1", "input", "q1 : dict[str, list[int]]"),
        ("q2", "input", "q2 : dict[str, list[int]]"),
        ("q3", "input", "q3 : str"),
    ],
    "wide": [(f"p{i}", "input", f"p{i} : int") for i in range(1, 11)],
}


def make_ghost_graph() -> Graph:
    """Every ghost kind, a gate's True/False labels and a nested container.

    - ``fetch`` takes two outside inputs (``url``, ``timeout``);
    - ``parse`` takes a bound tool (``parser``);
    - ``summarize`` reads ``raw`` from ``fetch``; ``fetch → parse → summarize``
      implies that arrow, so ``simplify`` hides it;
    - ``is_ready`` sends True to ``publish`` (which takes ``channel``) and
      False to ``archive``;
    - the ``enrich`` container's ``tag`` step takes an outside ``locale``.
    """
    enrich = Graph([tag, score], name="enrich")
    return Graph([fetch, parse, summarize, is_ready, publish, archive, enrich.as_node()], name="pipeline").bind(parser=Parser())


# step -> [(value name, kind, pill text)], in pill order
EXPECTED_GHOSTS: dict[str, list[tuple[str, str, str]]] = {
    "fetch": [("url", "input", "url : str"), ("timeout", "input", "timeout : int")],
    "parse": [("parser", "bound", "parser : Parser")],
    "summarize": [("raw", "hidden-edge", "raw ← fetch")],
    "is_ready": [],
    "publish": [("channel", "input", "channel : str")],
    "archive": [],
    "enrich": [("locale", "input", "locale : str")],
}
EXPECTED_GHOSTS_EXPANDED = {
    "enrich/tag": [("locale", "input", "locale : str")],
    "enrich/score": [],
}

DESKTOP = {"width": 1400, "height": 1000}
PHONE = {"width": 390, "height": 844}


# --- rendering ----------------------------------------------------------------


@pytest.fixture(scope="module")
def html_files(tmp_path_factory):
    """Render each variant once; the default variant passes no kwargs at all."""
    out = tmp_path_factory.mktemp("ghosts")
    graph = make_ghost_graph()
    variants = {
        "default": {},
        "expanded": {"depth": 1},
        "inputs_shown": {"show_inputs": True},
        "inputs_shown_no_bound": {"show_inputs": True, "show_bounded_inputs": False},
        "no_bound": {"show_bounded_inputs": False},
        "separate": {"separate_outputs": True},
    }
    paths = {}
    for name, kwargs in variants.items():
        path = out / f"{name}.html"
        graph.visualize(filepath=str(path), **kwargs)
        paths[name] = str(path)
    for name, make in (("crowded", make_crowded_graph), ("gate_labels", make_gate_labels_graph)):
        path = out / f"{name}.html"
        make().visualize(filepath=str(path))
        paths[name] = str(path)
    return paths


def _open(browser, path: str, *, touch: bool = False):
    options = {"viewport": PHONE, "has_touch": True, "is_mobile": True} if touch else {"viewport": DESKTOP}
    context = browser.new_context(**options)
    page = context.new_page()
    page.goto(f"file://{path}")
    page.wait_for_function(
        "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0 && window.__hypergraphVizReady === true",
        timeout=15000,
    )
    _settle(page)
    return context, page


def _settle(page) -> None:
    """Let React commit and the .15s fades end."""
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
    page.wait_for_timeout(400)


def _pin(page, step: str, *, tap: bool = False) -> None:
    """Tap or click ``step`` to pin it, and wait until its pan has ended on screen.

    A fixed wait is not enough: a slow engine can deliver the tap's click
    after ``tap()`` returns, and the view is then read mid-glide.
    """
    framed = page.evaluate("window.__hypergraphVizPinFramed || 0")
    if tap:
        page.tap(f'[data-id="{step}"]', timeout=5000)
    else:
        page.click(f'[data-id="{step}"]')
    page.wait_for_function(f"(window.__hypergraphVizPinFramed || 0) > {framed}", timeout=15000)
    _settle(page)


@pytest.fixture
def desktop(_browser, html_files):
    contexts = []

    def open_page(variant: str = "default"):
        context, page = _open(_browser, html_files[variant])
        contexts.append(context)
        return page

    yield open_page
    for context in contexts:
        context.close()


@pytest.fixture
def phone(_browser, html_files):
    contexts = []

    def open_page(variant: str = "default"):
        context, page = _open(_browser, html_files[variant], touch=True)
        contexts.append(context)
        return page

    yield open_page
    for context in contexts:
        context.close()


# --- page probes ----------------------------------------------------------------

_STATE_JS = """() => {
  const box = el => { const r = el.getBoundingClientRect(); return {left: r.left, top: r.top, right: r.right, bottom: r.bottom}; };
  const shown = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden'; };
  const opacity = el => { let o = 1; for (let e = el; e && e.nodeType === 1; e = e.parentElement) o *= parseFloat(getComputedStyle(e).opacity); return o; };
  const text = el => (el.textContent || '').replace(/\\s+/g, ' ').trim();
  return {
    ghosts: [...document.querySelectorAll('[data-hg-ghost]')].map(el => ({
      name: el.getAttribute('data-hg-ghost'), kind: el.getAttribute('data-hg-ghost-kind'),
      step: el.getAttribute('data-hg-ghost-for'), text: text(el), box: box(el), opacity: opacity(el),
      borderStyle: getComputedStyle(el).borderTopStyle })),
    nodes: [...document.querySelectorAll('.react-flow__node')].filter(shown).map(el => ({
      id: el.getAttribute('data-id'), box: box(el), dim: el.classList.contains('hg-dim'),
      focus: el.classList.contains('hg-focus'), opacity: opacity(el) })),
    edges: [...document.querySelectorAll('[data-hg-edge]')].map(el => ({
      source: el.getAttribute('data-hg-source'), target: el.getAttribute('data-hg-target'),
      dimmed: el.getAttribute('data-hg-dimmed'), opacity: opacity(el) })),
    labels: [...document.querySelectorAll('.react-flow__edgelabel-renderer > div')].map(el => ({
      text: text(el), box: box(el), opacity: opacity(el),
      source: el.getAttribute('data-hg-source'), target: el.getAttribute('data-hg-target'),
      dimmed: el.getAttribute('data-hg-dimmed'), isEdgeLabel: el.hasAttribute('data-hg-edge-label') })),
    tooltips: [...document.querySelectorAll('[role="tooltip"]')].filter(shown).map(text),
    viewport: {width: window.innerWidth, height: window.innerHeight},
    layoutVersion: window.__hypergraphVizDebug ? window.__hypergraphVizDebug.version : null,
  };
}"""

# A canvas point with nothing on it or near it: the pane itself answers
# elementFromPoint within 32px all round (mobile browsers snap a tap onto a
# clickable element close by).
_EMPTY_POINT_JS = """() => {
  const R = 32;
  const pane = (x, y) => { const el = document.elementFromPoint(x, y); return !!el && el.classList.contains('react-flow__pane'); };
  for (let y = R + 4; y < window.innerHeight - R - 4; y += 12) {
    for (let x = R + 4; x < window.innerWidth - R - 4; x += 12) {
      let clear = true;
      for (const [dx, dy] of [[0, 0], [-R, 0], [R, 0], [0, -R], [0, R], [-R, -R], [R, -R], [-R, R], [R, R]]) {
        if (!pane(x + dx, y + dy)) { clear = false; break; }
      }
      if (clear) return {x, y};
    }
  }
  return null;
}"""


def _state(page) -> dict:
    return page.evaluate(_STATE_JS)


def _ghost_set(state: dict) -> list[tuple[str, str, str]]:
    return [(g["name"], g["kind"], g["text"]) for g in state["ghosts"]]


def _overlaps(a: dict, b: dict, tol: float = 0.5) -> bool:
    return a["left"] < b["right"] - tol and b["left"] < a["right"] - tol and a["top"] < b["bottom"] - tol and b["top"] < a["bottom"] - tol


def _assert_no_overlap(state: dict, step: str, where: str) -> None:
    """No ghost overlaps a node (its own step included), an edge label, or another ghost.

    The step's expanded ancestor containers are the one exception: a step
    inside a container has its ghosts inside that container's box.
    """
    ancestors = {step.rsplit("/", k)[0] for k in range(1, step.count("/") + 1)}
    problems = []
    ghosts = state["ghosts"]
    for g in ghosts:
        for n in state["nodes"]:
            if n["id"] in ancestors:
                continue
            if _overlaps(g["box"], n["box"]):
                problems.append(f"ghost {g['name']!r} overlaps node {n['id']!r}")
        for lab in state["labels"]:
            if _overlaps(g["box"], lab["box"]):
                problems.append(f"ghost {g['name']!r} overlaps edge label {lab['text']!r}")
    for i, a in enumerate(ghosts):
        for b in ghosts[i + 1 :]:
            if _overlaps(a["box"], b["box"]):
                problems.append(f"ghost {a['name']!r} overlaps ghost {b['name']!r}")
    assert not problems, f"{where}: " + "; ".join(problems)


def _assert_on_screen(state: dict, step: str, where: str) -> None:
    vw, vh = state["viewport"]["width"], state["viewport"]["height"]
    boxes = [(f"ghost {g['name']!r}", g["box"]) for g in state["ghosts"]]
    boxes += [(f"step {n['id']!r}", n["box"]) for n in state["nodes"] if n["id"] == step]
    off = [f"{label} at {b}" for label, b in boxes if b["left"] < -0.5 or b["top"] < -0.5 or b["right"] > vw + 0.5 or b["bottom"] > vh + 0.5]
    assert not off, f"{where}: off screen ({vw}x{vh}): " + "; ".join(off)


def _assert_clear(state: dict, where: str) -> None:
    dimmed_nodes = [n["id"] for n in state["nodes"] if n["dim"] or n["focus"]]
    dimmed_edges = [f"{e['source']}->{e['target']}" for e in state["edges"] if e["dimmed"] == "true"]
    dimmed_labels = [lab["text"] for lab in state["labels"] if lab["dimmed"] == "true" or lab["opacity"] < 0.99]
    assert not state["ghosts"], f"{where}: ghosts still drawn: {_ghost_set(state)}"
    assert not dimmed_nodes, f"{where}: nodes still focused or dimmed: {dimmed_nodes}"
    assert not dimmed_edges, f"{where}: edges still dimmed: {dimmed_edges}"
    assert not dimmed_labels, f"{where}: edge labels still dimmed: {dimmed_labels}"


def _node_boxes(state: dict) -> dict[str, dict]:
    return {n["id"]: n["box"] for n in state["nodes"]}


# --- at rest ----------------------------------------------------------------------


def test_inputs_hidden_by_default_and_nothing_ghosted_at_rest(desktop):
    page = desktop("default")
    state = _state(page)
    input_nodes = [n["id"] for n in state["nodes"] if n["id"].startswith("input_")]
    assert not input_nodes, f"inputs are hidden by default, but the page draws {input_nodes}"
    _assert_clear(state, "at rest")
    assert state["edges"], "every drawn edge carries the edge component's data-hg-edge group"


# --- hover and tap show exactly the step's ghost set -----------------------------


@pytest.mark.parametrize("step", list(EXPECTED_GHOSTS))
def test_hover_shows_exactly_the_steps_ghosts(desktop, step):
    page = desktop("default")
    before = _state(page)
    page.hover(f'[data-id="{step}"]')
    _settle(page)
    state = _state(page)
    assert _ghost_set(state) == EXPECTED_GHOSTS[step], f"hover {step}"
    assert all(g["step"] == step for g in state["ghosts"]), f"hover {step}: ghosts belong to {step}"
    focused = [n["id"] for n in state["nodes"] if n["focus"]]
    assert focused == [step], f"hover {step}: focused nodes {focused}"
    # Nothing is re-laid out: no layout pass, no node moves.
    assert state["layoutVersion"] == before["layoutVersion"], f"hover {step} re-ran the layout"
    moved = [
        nid
        for nid, b in _node_boxes(state).items()
        if nid in _node_boxes(before) and any(abs(b[k] - _node_boxes(before)[nid][k]) > 0.5 for k in ("left", "top", "right", "bottom"))
    ]
    assert not moved, f"hover {step} moved {moved}"
    _assert_no_overlap(state, step, f"hover {step}")
    for g in state["ghosts"]:
        if g["kind"] == "bound":
            assert g["borderStyle"] == "dashed", f"bound ghost {g['name']} is dashed"
            assert g["opacity"] < 0.9, f"bound ghost {g['name']} is faded (opacity {g['opacity']:.2f})"
    # Leaving an unpinned step clears everything.
    point = page.evaluate(_EMPTY_POINT_JS)
    page.mouse.move(point["x"], point["y"])
    _settle(page)
    _assert_clear(_state(page), f"mouse left {step}")


@pytest.mark.parametrize("step", ["enrich/tag", "enrich/score"])
def test_hover_inside_an_expanded_container(desktop, step):
    page = desktop("expanded")
    page.hover(f'[data-id="{step}"]')
    _settle(page)
    state = _state(page)
    assert _ghost_set(state) == EXPECTED_GHOSTS_EXPANDED[step], f"hover {step}"
    _assert_no_overlap(state, step, f"hover {step} (expanded)")


@pytest.mark.parametrize("step", ["fetch", "parse", "summarize"])
def test_tap_shows_pins_and_frames_the_steps_ghosts(phone, step):
    page = phone("default")
    _pin(page, step, tap=True)
    state = _state(page)
    assert _ghost_set(state) == EXPECTED_GHOSTS[step], f"tap {step}"
    _assert_no_overlap(state, step, f"tap {step}")
    _assert_on_screen(state, step, f"tap {step}")
    # A tap pins: the ghosts stay after the finger lifts, and an empty-canvas tap clears them.
    point = page.evaluate(_EMPTY_POINT_JS)
    assert point, "no empty canvas point on the phone screen"
    page.touchscreen.tap(point["x"], point["y"])
    _settle(page)
    _assert_clear(_state(page), f"empty-canvas tap after {step}")


def test_crowded_step_falls_back_above_or_below_with_types_kept(desktop, phone):
    page = desktop("crowded")
    page.hover('[data-id="middle"]')
    _settle(page)
    state = _state(page)
    assert _ghost_set(state) == CROWDED_GHOSTS, "hover middle"
    _assert_no_overlap(state, "middle", "hover middle (crowded)")

    page = phone("crowded")
    _pin(page, "middle", tap=True)
    state = _state(page)
    assert _ghost_set(state) == CROWDED_GHOSTS, "tap middle"
    _assert_no_overlap(state, "middle", "tap middle (crowded)")
    # At the readable zoom the four pills do not fit a phone, so only the step
    # must be on screen (test_open_fitted.py pins which pills it shows).
    _assert_on_screen({**state, "ghosts": []}, "middle", "tap middle (crowded)")


@pytest.mark.parametrize("step", list(GATE_LABEL_GHOSTS))
def test_ghosts_never_cover_a_gates_true_false_labels(desktop, step):
    page = desktop("gate_labels")
    labels = {lab["text"] for lab in _state(page)["labels"]}
    assert {"True", "False"} <= labels, f"the gate's labels are drawn: {sorted(labels)}"
    page.hover(f'[data-id="{step}"]')
    _settle(page)
    state = _state(page)
    assert _ghost_set(state) == GATE_LABEL_GHOSTS[step], f"hover {step}"
    _assert_no_overlap(state, step, f"hover {step} (gate labels)")
    _pin(page, step)
    state = _state(page)
    assert _ghost_set(state) == GATE_LABEL_GHOSTS[step], f"pinned {step}"
    _assert_no_overlap(state, step, f"pinned {step} (gate labels)")


def test_separate_outputs_still_ghosts_the_edge_simplify_hides(desktop):
    page = desktop("separate")
    page.hover('[data-id="summarize"]')
    _settle(page)
    state = _state(page)
    assert _ghost_set(state) == EXPECTED_GHOSTS["summarize"], "hover summarize, separate outputs"
    _assert_no_overlap(state, "summarize", "hover summarize (separate outputs)")


def test_hidden_types_show_names_only(desktop):
    page = desktop("default")
    page.click('button[aria-label="Hide Types"]')
    page.wait_for_function("window.__hypergraphVizReady === true", timeout=15000)
    _settle(page)
    page.hover('[data-id="fetch"]')
    _settle(page)
    assert _ghost_set(_state(page)) == [("url", "input", "url"), ("timeout", "input", "timeout")]


# --- focus: the step's path stays lit, the rest dims -----------------------------

LIT = {
    "publish": {"fetch", "parse", "summarize", "is_ready", "publish"},
    "archive": {"fetch", "parse", "summarize", "is_ready", "archive"},
}


@pytest.mark.parametrize("step", list(LIT))
def test_focus_lights_the_path_and_dims_the_rest_including_gate_labels(desktop, step):
    page = desktop("default")
    page.hover(f'[data-id="{step}"]')
    _settle(page)
    state = _state(page)
    lit = LIT[step]
    wrong = [n["id"] for n in state["nodes"] if (n["id"] in lit) == n["dim"]]
    assert not wrong, f"hover {step}: lit/dim wrong for {wrong} (lit = {sorted(lit)})"
    for n in state["nodes"]:
        if n["dim"]:
            assert n["opacity"] < 0.5, f"dimmed node {n['id']} still at opacity {n['opacity']:.2f}"
    for e in state["edges"]:
        should_dim = not (e["source"] in lit and e["target"] in lit)
        assert e["dimmed"] == ("true" if should_dim else "false"), f"hover {step}: edge {e['source']}->{e['target']} dimmed={e['dimmed']}"
        if should_dim:
            assert e["opacity"] < 0.5, f"edge {e['source']}->{e['target']} dimmed but at opacity {e['opacity']:.2f}"
    gate_labels = {lab["text"]: lab for lab in state["labels"] if lab["text"] in ("True", "False")}
    assert set(gate_labels) == {"True", "False"}, f"gate labels drawn: {sorted(gate_labels)}"
    for text, lab in gate_labels.items():
        # The edge component labels its own edge; nothing matches label text.
        assert lab["isEdgeLabel"] and lab["source"] == "is_ready", f"{text} label is not tied to its edge: {lab}"
        should_dim = lab["target"] not in lit
        assert lab["dimmed"] == ("true" if should_dim else "false"), f"hover {step}: {text} label dimmed={lab['dimmed']}"
        assert (lab["opacity"] < 0.5) == should_dim, f"hover {step}: {text} label opacity {lab['opacity']:.2f}"


# --- pin, clear, toggle -------------------------------------------------------------


def test_click_pins_and_empty_canvas_click_or_escape_clears(desktop):
    page = desktop("default")
    _pin(page, "fetch")
    point = page.evaluate(_EMPTY_POINT_JS)
    page.mouse.move(point["x"], point["y"])
    _settle(page)
    state = _state(page)
    assert _ghost_set(state) == EXPECTED_GHOSTS["fetch"], "a click pins the ghosts past mouse-leave"
    _assert_no_overlap(state, "fetch", "pinned fetch")
    _assert_on_screen(state, "fetch", "pinned fetch")

    page.mouse.click(point["x"], point["y"])
    _settle(page)
    _assert_clear(_state(page), "empty-canvas click")

    _pin(page, "publish")
    assert _ghost_set(_state(page)) == EXPECTED_GHOSTS["publish"], "pinned publish"
    page.keyboard.press("Escape")
    _settle(page)
    _assert_clear(_state(page), "Escape")


def test_toggling_inputs_clears_every_ghost_and_dim_state(desktop):
    page = desktop("default")
    _pin(page, "publish")
    assert _ghost_set(_state(page)) == EXPECTED_GHOSTS["publish"], "pinned publish"

    page.click('button[aria-label="Show Inputs"]')
    page.wait_for_function("window.__hypergraphVizReady === true", timeout=15000)
    _settle(page)
    state = _state(page)
    _assert_clear(state, "after Show Inputs")
    assert any(n["id"].startswith("input_") for n in state["nodes"]), "Show Inputs draws the input boxes"

    page.click('button[aria-label="Hide Inputs"]')
    page.wait_for_function("window.__hypergraphVizReady === true", timeout=15000)
    _settle(page)
    _assert_clear(_state(page), "after Hide Inputs")


# --- inputs shown -------------------------------------------------------------------


def test_inputs_shown_means_no_ghosts_and_bound_tools_faded_and_dashed(desktop):
    page = desktop("inputs_shown")
    page.hover('[data-id="fetch"]')
    _settle(page)
    state = _state(page)
    _assert_clear(state, "hover with inputs shown")
    bound = page.evaluate("""() => {
      const el = document.querySelector('[data-id="input_parser"]');
      if (!el) return null;
      const pill = el.querySelector('.rounded-full');
      let o = 1; for (let e = pill; e && e.nodeType === 1; e = e.parentElement) o *= parseFloat(getComputedStyle(e).opacity);
      return {borderStyle: getComputedStyle(pill).borderTopStyle, opacity: o};
    }""")
    assert bound is not None, "bound tools are shown by default when inputs are shown"
    assert bound["borderStyle"] == "dashed", f"bound tool box is dashed, got {bound['borderStyle']}"
    assert bound["opacity"] < 0.9, f"bound tool box is faded, got opacity {bound['opacity']:.2f}"


def test_show_bounded_inputs_false_hides_bound_tools_and_their_ghosts(desktop):
    page = desktop("inputs_shown_no_bound")
    ids = [n["id"] for n in _state(page)["nodes"]]
    assert "input_parser" not in ids, "show_bounded_inputs=False hides the bound tool box"
    assert "input_group_timeout_url" in ids or "input_url" in ids, f"unbound inputs still drawn: {ids}"

    page = desktop("no_bound")
    page.hover('[data-id="parse"]')
    _settle(page)
    assert _ghost_set(_state(page)) == [], "show_bounded_inputs=False leaves bound tools out of the ghosts"


# --- toolbar on touch ------------------------------------------------------------------


def test_tapped_toolbar_button_does_not_leave_its_tooltip_up(phone):
    page = phone("default")
    page.tap('button[aria-label="Zoom In"]', timeout=5000)
    _settle(page)
    assert _state(page)["tooltips"] == [], "a tapped toolbar button's tooltip stays up"


# --- the Python resolver: one place decides the defaults ------------------------------


def _meta_flags(path: str) -> tuple[bool, bool]:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    inputs = re.search(r'"show_inputs"\s*:\s*(true|false)', text)
    bounded = re.search(r'"show_bounded_inputs"\s*:\s*(true|false)', text)
    assert inputs and bounded, "the page payload carries both flags"
    return inputs.group(1) == "true", bounded.group(1) == "true"


def test_resolver_defaults_and_passthrough():
    from hypergraph.viz.widget import _resolve_input_visibility

    assert _resolve_input_visibility(None, None, None) == (False, True)
    assert _resolve_input_visibility(True, None, None) == (True, True)
    assert _resolve_input_visibility(None, False, None) == (False, False)
    assert _resolve_input_visibility(True, False, None) == (True, False)


def test_resolver_owns_the_deprecated_alias():
    from hypergraph.viz.widget import _resolve_input_visibility

    with pytest.warns(DeprecationWarning, match="show_external_inputs is deprecated"):
        assert _resolve_input_visibility(None, None, True) == (True, True)
    with pytest.warns(DeprecationWarning, match="show_external_inputs is deprecated"):
        assert _resolve_input_visibility(False, None, False) == (False, True)
    with pytest.raises(TypeError, match="Pass either show_inputs or show_external_inputs"):
        _resolve_input_visibility(True, None, False)


def _internal_helpers() -> dict:
    """Each internal viz helper that takes the input flags, given everything else."""
    from hypergraph.viz.html import LayoutEstimator, estimate_layout
    from hypergraph.viz.renderer import render_graph
    from hypergraph.viz.renderer.ir_builder import build_graph_ir
    from hypergraph.viz.scene_builder import build_initial_scene

    graph = make_ghost_graph()
    flat = graph.to_flat_graph()
    return {
        "build_initial_scene": lambda **flags: build_initial_scene(build_graph_ir(flat), **flags),
        "render_graph": lambda **flags: render_graph(flat, **flags),
        "estimate_layout": lambda **flags: estimate_layout(graph, **flags),
        "LayoutEstimator": lambda **flags: LayoutEstimator(graph, **flags),
    }


@pytest.mark.parametrize("missing", ["show_inputs", "show_bounded_inputs"])
@pytest.mark.parametrize("helper", ["build_initial_scene", "render_graph", "estimate_layout", "LayoutEstimator"])
def test_internal_helpers_refuse_a_missing_input_flag(helper, missing):
    """The resolver is the only default (D59): an internal helper takes both flags, already resolved."""
    flags = {"show_inputs": True, "show_bounded_inputs": False}
    del flags[missing]
    with pytest.raises(TypeError, match=f"keyword-only argument: '{missing}'"):
        _internal_helpers()[helper](**flags)


def _make_table():
    from hypergraph.materialization._lancedb_store import LanceDBStore

    @node(output_name="items")
    def produce_items(source: str) -> list[dict]:
        return [{"item_id": "i0", "text": source}]

    @node(output_name="clean_text")
    def clean(text: str) -> str:
        return text.strip()

    inner = Graph([clean], name="proc").as_node(name="items_node").map_over("items", identity="item_id")
    return Graph([produce_items, inner]).as_table(identity="doc_id", store=LanceDBStore(tempfile.mkdtemp() + "/store"))


def _entry_points(tmp_path, monkeypatch):
    """Each public entry point, called with kwargs, returning the flags it rendered with."""
    from hypergraph.viz import debug, visualize

    graph = make_ghost_graph()
    table = _make_table()
    captured: dict = {}

    def fake_extract(graph, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(debug, "_extract_debug_data_sync", fake_extract)
    monkeypatch.setattr(debug, "_is_in_async_context", lambda: False)

    def via_file(call):
        def run(**kwargs):
            path = str(tmp_path / f"out_{len(list(tmp_path.iterdir()))}.html")
            call(filepath=path, **kwargs)
            return _meta_flags(path)

        return run

    def via_debug(**kwargs):
        captured.clear()
        debug.extract_debug_data(graph, **kwargs)
        return captured["show_inputs"], captured["show_bounded_inputs"]

    return {
        "Graph.visualize": via_file(graph.visualize),
        "viz.visualize": via_file(lambda **kw: visualize(graph, **kw)),
        "HyperTable.visualize": via_file(table.visualize),
        "HyperTable.visualize(include_children=False)": via_file(lambda **kw: table.visualize(include_children=False, **kw)),
        "extract_debug_data": via_debug,
    }


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"show_inputs": True}, {"show_bounded_inputs": False}, {"show_inputs": True, "show_bounded_inputs": False}],
    ids=["defaults", "inputs_on", "bound_off", "inputs_on_bound_off"],
)
def test_every_entry_point_resolves_through_the_one_resolver(tmp_path, monkeypatch, kwargs):
    from hypergraph.viz import widget

    calls = []
    real = widget._resolve_input_visibility

    def spy(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(widget, "_resolve_input_visibility", spy)
    expected = real(kwargs.get("show_inputs"), kwargs.get("show_bounded_inputs"), None)
    for name, entry in _entry_points(tmp_path, monkeypatch).items():
        calls.clear()
        assert entry(**kwargs) == expected, f"{name}(**{kwargs}) rendered with a different default"
        assert calls, f"{name} decides its defaults without the resolver"


def test_every_entry_point_defaults_to_none_and_warns_at_the_caller(tmp_path, monkeypatch):
    from hypergraph.materialization import HyperTable
    from hypergraph.viz import extract_debug_data, visualize

    for fn in (Graph.visualize, visualize, extract_debug_data):
        params = inspect.signature(fn).parameters
        assert params["show_inputs"].default is None, f"{fn.__qualname__} show_inputs default"
        assert params["show_bounded_inputs"].default is None, f"{fn.__qualname__} show_bounded_inputs default"
    assert "kwargs" in inspect.signature(HyperTable.visualize).parameters

    for name, entry in _entry_points(tmp_path, monkeypatch).items():
        with warnings.catch_warnings(record=True) as record:
            warnings.simplefilter("always")
            assert entry(show_external_inputs=True) == (True, True), name
        deprecations = [w for w in record if issubclass(w.category, DeprecationWarning) and "show_external_inputs" in str(w.message)]
        assert len(deprecations) == 1, f"{name}: {[str(w.message) for w in record]}"
        assert deprecations[0].filename == __file__, f"{name} warns from {deprecations[0].filename}, not its caller"
