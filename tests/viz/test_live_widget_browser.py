"""Browser-level tests for the live-widget message protocol.

These exercise the viz.js side of the round-trip without a real
Python kernel. viz.js posts `hypergraph-request-state` to
`window.parent`; when the iframe HTML is loaded at the top level
(directly, not embedded), `window.parent === window`, so the same
window can listen for the request and post back
`hypergraph-apply-state` as an anywidget host would.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hypergraph import Graph, node
from hypergraph.viz.html import generate_widget_html
from hypergraph.viz.renderer import render_graph_single_state
from tests.viz.conftest import HAS_PLAYWRIGHT


pytestmark = pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")


@node(output_name="inner_a_out")
def _inner_a(ia_in: int) -> int:
    return ia_in


@node(output_name="inner_b_out")
def _inner_b(ib_in: int) -> int:
    return ib_in


def _nested_graph() -> Graph:
    inner = Graph(nodes=[_inner_a, _inner_b], name="inner")
    return Graph(nodes=[inner.as_node()], name="outer")


def _wait_debug_ready(page) -> None:
    page.wait_for_function(
        "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0",
        timeout=10000,
    )


def test_initial_live_payload_has_no_by_state_maps(page, temp_html_file):
    graph = _nested_graph()
    initial = render_graph_single_state(graph.to_flat_graph(), depth=0)
    assert initial["meta"]["liveMode"] is True
    assert "nodesByState" not in initial["meta"]

    Path(temp_html_file).write_text(generate_widget_html(initial), encoding="utf-8")
    page.goto(f"file://{temp_html_file}")
    _wait_debug_ready(page)

    nodes = page.evaluate("window.__hypergraphVizDebug.nodes")
    assert nodes, "Expected nodes in the single-state payload"


def test_expansion_click_posts_request_and_applies_response(page, temp_html_file):
    graph = _nested_graph()
    flat = graph.to_flat_graph()

    collapsed = render_graph_single_state(flat, depth=0)
    expanded = render_graph_single_state(flat, depth=1)

    Path(temp_html_file).write_text(generate_widget_html(collapsed), encoding="utf-8")
    page.goto(f"file://{temp_html_file}")
    _wait_debug_ready(page)

    # Install a mock host that records requests and echoes a staged response.
    page.evaluate(
        """(expandedPayload) => {
            window.__hgRequests = [];
            window.__hgNextResponse = expandedPayload;
            window.addEventListener('message', (ev) => {
                if (!ev.data || ev.data.type !== 'hypergraph-request-state') return;
                window.__hgRequests.push(ev.data);
                if (window.__hgNextResponse) {
                    window.postMessage({
                        type: 'hypergraph-apply-state',
                        requestId: ev.data.requestId,
                        graphData: window.__hgNextResponse,
                    }, '*');
                }
            });
        }""",
        expanded,
    )

    page.locator('[data-id="inner"]').first.click()

    page.wait_for_function(
        "window.__hgRequests && window.__hgRequests.length > 0",
        timeout=5000,
    )
    display_state = page.evaluate("window.__hgRequests[window.__hgRequests.length - 1].displayState")
    assert display_state["expansion"]["inner"] is True

    # The response payload swaps in expanded state — inner nodes visible.
    page.wait_for_function(
        "window.__hypergraphVizDebug && window.__hypergraphVizDebug.nodes.some(n => n.id && n.id.startsWith('inner/'))",
        timeout=5000,
    )


def test_kernel_hint_appears_when_no_response(page, temp_html_file):
    graph = _nested_graph()
    initial = render_graph_single_state(graph.to_flat_graph(), depth=0)

    Path(temp_html_file).write_text(generate_widget_html(initial), encoding="utf-8")
    page.goto(f"file://{temp_html_file}")
    _wait_debug_ready(page)

    # Install a silent host: records requests but never replies.
    page.evaluate(
        """() => {
            window.__hgRequests = [];
            window.addEventListener('message', (ev) => {
                if (ev.data && ev.data.type === 'hypergraph-request-state') {
                    window.__hgRequests.push(ev.data);
                }
            });
        }"""
    )

    page.locator('[data-id="inner"]').first.click()

    # Banner should appear after the request timeout (~2.5s).
    page.get_by_text("Start a Python kernel").wait_for(timeout=8000)


def test_sep_and_ext_toggles_post_request(page, temp_html_file):
    """Separate-outputs and show-inputs toggles must also route through
    the live-widget protocol in live mode."""
    graph = _nested_graph()
    initial = render_graph_single_state(graph.to_flat_graph(), depth=0, separate_outputs=False, show_inputs=True)

    Path(temp_html_file).write_text(generate_widget_html(initial), encoding="utf-8")
    page.goto(f"file://{temp_html_file}")
    _wait_debug_ready(page)

    page.evaluate(
        """() => {
            window.__hgRequests = [];
            window.addEventListener('message', (ev) => {
                if (ev.data && ev.data.type === 'hypergraph-request-state') {
                    window.__hgRequests.push(ev.data);
                }
            });
            // Flip separate_outputs via the render-options hook that the
            // gallery harness uses (same path the toolbar buttons take).
            window.__hypergraphVizSetRenderOptions({ separateOutputs: true });
            window.__hypergraphVizSetRenderOptions({ showInputs: false });
        }"""
    )

    page.wait_for_function(
        "window.__hgRequests && window.__hgRequests.length >= 2",
        timeout=5000,
    )
    requests = page.evaluate("window.__hgRequests")
    sep_values = [r["displayState"].get("separate_outputs") for r in requests]
    ext_values = [r["displayState"].get("show_inputs") for r in requests]
    assert True in sep_values
    assert False in ext_values
