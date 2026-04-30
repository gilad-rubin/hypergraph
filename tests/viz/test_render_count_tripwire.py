"""Render-count tripwire (PR #88, Stage 5).

Pre-IR, a hook-deps bug in the live widget caused every click to trigger
~10,000 App renders and freeze the browser for ~6 s. Issue #88 calls
this out as user story 16: a click that causes a hidden render storm
must fail CI even when the rendered output looks fine.

Strategy: instrument App with a `__hypergraphAppRenderCount` window
counter. Reset before a click, click an expandable container, wait for
layout to settle, read the delta. Cap with a generous-but-finite
ceiling — anything four-digits is the regression class we're fencing.
"""

from __future__ import annotations

import pytest

from hypergraph import Graph, node
from tests.viz.conftest import (
    HAS_PLAYWRIGHT,
    click_to_collapse_container,
    click_to_expand_container,
    render_to_page,
    wait_for_debug_ready,
)

pytestmark = pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")

# An honest click triggers a small handful of renders for state +
# layout + viewport-fit. A hundred is generous; 10,000 is the bug class.
RENDER_DELTA_PER_CLICK_CEILING = 100


def _make_nested_graph() -> Graph:
    @node(output_name="cleaned")
    def clean_text(text: str) -> str:
        return text.strip()

    @node(output_name="tokens")
    def tokenize(cleaned: str) -> list[str]:
        return cleaned.split()

    inner = Graph(nodes=[clean_text, tokenize], name="preprocess")

    @node(output_name="result")
    def consume(tokens: list[str]) -> int:
        return len(tokens)

    return Graph(nodes=[inner.as_node(), consume], name="pipeline")


def _read_render_count(page) -> int:
    return page.evaluate("window.__hypergraphAppRenderCount || 0")


def test_expand_click_does_not_trigger_render_storm(page, temp_html_file):
    """Expanding a collapsed container must not cause >100 App renders."""
    graph = _make_nested_graph()
    render_to_page(page, graph, depth=0, temp_path=temp_html_file)
    wait_for_debug_ready(page)

    before = _read_render_count(page)
    click_to_expand_container(page, "preprocess")
    after = _read_render_count(page)

    delta = after - before
    assert 0 < delta < RENDER_DELTA_PER_CLICK_CEILING, (
        f"Expand click triggered {delta} App renders (before={before}, after={after}); "
        f"ceiling is {RENDER_DELTA_PER_CLICK_CEILING}. "
        "Suspect: hook-deps bug, fresh literal in deps array, or memo-cache miss on every render."
    )


def test_collapse_click_does_not_trigger_render_storm(page, temp_html_file):
    """Collapsing an expanded container must not cause >100 App renders."""
    graph = _make_nested_graph()
    render_to_page(page, graph, depth=1, temp_path=temp_html_file)
    wait_for_debug_ready(page)

    before = _read_render_count(page)
    click_to_collapse_container(page, "preprocess")
    after = _read_render_count(page)

    delta = after - before
    assert 0 < delta < RENDER_DELTA_PER_CLICK_CEILING, (
        f"Collapse click triggered {delta} App renders (before={before}, after={after}); ceiling is {RENDER_DELTA_PER_CLICK_CEILING}."
    )


def test_initial_render_count_is_bounded(page, temp_html_file):
    """A fresh viz page should reach steady-state in well under 100 renders.

    This catches an even subtler regression: a re-render loop that
    settles eventually but burns a thousand renders before doing so.
    Steady-state happens once the layout commits + the viewport fits;
    `wait_for_debug_ready` blocks until both are done."""
    graph = _make_nested_graph()
    render_to_page(page, graph, depth=0, temp_path=temp_html_file)
    wait_for_debug_ready(page)

    initial = _read_render_count(page)
    assert initial < RENDER_DELTA_PER_CLICK_CEILING, (
        f"Steady-state render count is {initial}; ceiling is {RENDER_DELTA_PER_CLICK_CEILING}. "
        "Suspect: a re-render loop that converges but burns renders along the way."
    )
