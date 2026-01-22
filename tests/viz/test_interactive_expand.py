"""Tests for interactive expand/collapse edge routing.

These tests verify that edges after interactive expand/collapse have the
same routing as static depth rendering. This is the core bug we're tracking:

When you click to expand a nested graph, edges should route to INTERNAL
nodes (clean_text). Instead, they stay connected to the CONTAINER (preprocess).

Key test strategy:
1. Render at depth=0, click to expand, capture edge data
2. Render fresh at static depth=1, capture edge data
3. Assert: interactive edge targets == static edge targets
4. Assert: interactive edge sources == static edge sources
"""

import pytest
from hypergraph import Graph, node

try:
    import playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


# =============================================================================
# Test Graph Definitions (standard workflow for edge testing)
# =============================================================================

@node(output_name="cleaned")
def clean_text(text: str) -> str:
    """First step: clean the input text."""
    return text.strip()


@node(output_name="normalized")
def normalize_text(cleaned: str) -> str:
    """Second step: normalize the cleaned text."""
    return cleaned.lower()


@node(output_name="result")
def analyze(normalized: str) -> dict:
    """Final step: analyze the normalized text."""
    return {"length": len(normalized)}


def make_workflow():
    """Create 1-level nested graph: preprocess[clean_text, normalize_text] -> analyze."""
    preprocess = Graph(nodes=[clean_text, normalize_text], name="preprocess")
    return Graph(nodes=[preprocess.as_node(), analyze])


# =============================================================================
# Helper Functions
# =============================================================================

def extract_edge_routing(page) -> dict[str, dict]:
    """Extract edge routing data from the debug API.

    Returns dict mapping edge ID to routing info:
    {
        'edge_id': {
            'source': 'actual_source_node_id',
            'target': 'actual_target_node_id',
            'data': {...}  # includes actualSource, actualTarget if re-routed
        }
    }
    """
    page.wait_for_function(
        "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0",
        timeout=10000,
    )

    result = page.evaluate("""() => {
        const debug = window.__hypergraphVizDebug;
        const edges = {};

        // Get edges from debug data
        for (const edge of debug.edges || []) {
            // Use actual routing targets if available (for re-routed edges)
            const actualSource = (edge.data && edge.data.actualSource) || edge.source;
            const actualTarget = (edge.data && edge.data.actualTarget) || edge.target;

            edges[edge.id] = {
                source: actualSource,
                target: actualTarget,
                originalSource: edge.source,
                originalTarget: edge.target,
                data: edge.data || {},
            };
        }

        return {
            edges: edges,
            nodeIds: debug.nodes.map(n => n.id),
            summary: debug.summary,
        };
    }""")

    return result


def render_and_extract(page, graph, depth: int, temp_path: str) -> dict:
    """Render graph at given depth and extract edge routing."""
    from hypergraph.viz.widget import visualize
    import os

    visualize(graph, depth=depth, output=temp_path, _debug_overlays=True)
    page.goto(f"file://{temp_path}")
    return extract_edge_routing(page)


def click_to_expand_container(page, container_id: str) -> None:
    """Click on a collapsed container node to expand it.

    Waits for layout to settle after expansion.
    """
    # Wait for initial layout
    page.wait_for_function(
        "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0",
        timeout=10000,
    )
    initial_version = page.evaluate("window.__hypergraphVizDebug.version")

    # Find and click the container node
    # React Flow nodes have data-id attribute or id in class
    node_selector = f'[data-id="{container_id}"], .react-flow__node-custom[id*="{container_id}"]'

    # Try multiple strategies to find the node
    node_element = page.locator(node_selector).first
    if node_element.count() == 0:
        # Fallback: find by node label text
        node_element = page.locator(f'.react-flow__node:has-text("{container_id}")').first

    node_element.click()

    # Wait for layout to update (version should increment)
    page.wait_for_function(
        f"window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > {initial_version}",
        timeout=10000,
    )

    # Additional wait for layout to fully settle
    page.wait_for_timeout(500)


# =============================================================================
# Tests for Interactive Expand Edge Routing
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestInteractiveExpandEdgeRouting:
    """Tests that interactive expand produces same edge routing as static depth."""

    def test_interactive_expand_edge_targets_match_static(self):
        """After click-to-expand, edge targets should match static depth=1 targets.

        This test compares:
        - Render at depth=0, click to expand preprocess, capture edge targets
        - Render fresh at depth=1, capture edge targets
        - Assert edge targets are identical

        The bug: Interactive expand keeps edges targeting 'preprocess' container
        instead of routing them to 'clean_text' (the actual consumer inside).
        """
        from playwright.sync_api import sync_playwright
        import tempfile
        import os

        workflow = make_workflow()

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            temp_path = f.name

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()

                # === STATIC DEPTH=1: The expected/correct behavior ===
                static_data = render_and_extract(page, workflow, depth=1, temp_path=temp_path)
                static_targets = {
                    eid: info['target']
                    for eid, info in static_data['edges'].items()
                }

                # === INTERACTIVE EXPAND: Render at depth=0, click to expand ===
                render_and_extract(page, workflow, depth=0, temp_path=temp_path)
                click_to_expand_container(page, "preprocess")
                interactive_data = extract_edge_routing(page)
                interactive_targets = {
                    eid: info['target']
                    for eid, info in interactive_data['edges'].items()
                }

                browser.close()
        finally:
            os.unlink(temp_path)

        # Compare edge targets - find input edge that should route to clean_text
        # At depth=1, input edge targets clean_text (internal node)
        # The bug: After interactive expand, input edge still targets preprocess (container)

        # Find edges that enter the preprocess subgraph
        static_internal_targets = [
            target for target in static_targets.values()
            if target in ('clean_text', 'normalize_text')
        ]
        interactive_internal_targets = [
            target for target in interactive_targets.values()
            if target in ('clean_text', 'normalize_text')
        ]

        assert len(static_internal_targets) > 0, (
            "Static depth=1 should have edges targeting internal nodes.\n"
            f"Static targets: {static_targets}"
        )

        # THE KEY ASSERTION: Interactive expand should produce same internal targets
        assert set(interactive_internal_targets) == set(static_internal_targets), (
            "INTERACTIVE EXPAND BUG DETECTED!\n"
            f"\nAfter interactive expand, edges should target internal nodes.\n"
            f"Static depth=1 targets internal nodes: {static_internal_targets}\n"
            f"Interactive expand targets: {interactive_internal_targets}\n"
            f"\nFull static targets: {static_targets}\n"
            f"Full interactive targets: {interactive_targets}\n"
            f"\nThis indicates edges stay connected to the container instead of\n"
            f"routing to the actual consumer nodes inside."
        )

    def test_interactive_expand_edge_sources_match_static(self):
        """After click-to-expand, edge sources should match static depth=1 sources.

        The bug: Interactive expand keeps edges sourcing from 'preprocess' container
        instead of routing them from 'normalize_text' (the actual producer inside).
        """
        from playwright.sync_api import sync_playwright
        import tempfile
        import os

        workflow = make_workflow()

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            temp_path = f.name

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()

                # === STATIC DEPTH=1: The expected/correct behavior ===
                static_data = render_and_extract(page, workflow, depth=1, temp_path=temp_path)
                static_sources = {
                    eid: info['source']
                    for eid, info in static_data['edges'].items()
                }

                # === INTERACTIVE EXPAND: Render at depth=0, click to expand ===
                render_and_extract(page, workflow, depth=0, temp_path=temp_path)
                click_to_expand_container(page, "preprocess")
                interactive_data = extract_edge_routing(page)
                interactive_sources = {
                    eid: info['source']
                    for eid, info in interactive_data['edges'].items()
                }

                browser.close()
        finally:
            os.unlink(temp_path)

        # Find edges that exit the preprocess subgraph
        static_internal_sources = [
            source for source in static_sources.values()
            if source in ('clean_text', 'normalize_text')
            or 'data_' in source  # data nodes like data_normalize_text_normalized
        ]
        interactive_internal_sources = [
            source for source in interactive_sources.values()
            if source in ('clean_text', 'normalize_text')
            or 'data_' in source
        ]

        assert len(static_internal_sources) > 0, (
            "Static depth=1 should have edges sourcing from internal nodes.\n"
            f"Static sources: {static_sources}"
        )

        # THE KEY ASSERTION: Interactive expand should produce same internal sources
        assert set(interactive_internal_sources) == set(static_internal_sources), (
            "INTERACTIVE EXPAND BUG DETECTED!\n"
            f"\nAfter interactive expand, edges should source from internal nodes.\n"
            f"Static depth=1 sources from internal nodes: {static_internal_sources}\n"
            f"Interactive expand sources: {interactive_internal_sources}\n"
            f"\nFull static sources: {static_sources}\n"
            f"Full interactive sources: {interactive_sources}\n"
            f"\nThis indicates edges stay connected to the container instead of\n"
            f"routing from the actual producer nodes inside."
        )

    def test_input_edge_routes_to_clean_text_after_expand(self):
        """Specifically test that input_text edge targets clean_text after expand.

        This is the most direct test of the bug:
        - At depth=0: input_text -> preprocess (container) - CORRECT
        - After expand: input_text -> clean_text (internal) - EXPECTED
        - Bug: input_text -> preprocess (container) - ACTUAL BUG

        The edge should be re-routed to the actual consumer inside the container.
        """
        from playwright.sync_api import sync_playwright
        import tempfile
        import os

        workflow = make_workflow()

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            temp_path = f.name

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()

                # Render at depth=0, click to expand
                render_and_extract(page, workflow, depth=0, temp_path=temp_path)
                click_to_expand_container(page, "preprocess")
                data = extract_edge_routing(page)

                browser.close()
        finally:
            os.unlink(temp_path)

        # Find the input edge (from input_text or similar)
        input_edge = None
        for eid, info in data['edges'].items():
            if 'input' in info['source'].lower():
                input_edge = info
                break

        assert input_edge is not None, (
            f"No input edge found. Edges: {data['edges']}"
        )

        # After expand, the input edge should target clean_text, NOT preprocess
        target = input_edge['target']
        assert target == 'clean_text', (
            "INTERACTIVE EXPAND BUG: Input edge still targets container!\n"
            f"\nExpected target: 'clean_text' (the actual consumer)\n"
            f"Actual target: '{target}'\n"
            f"\nAfter expanding preprocess, the input edge should route to\n"
            f"clean_text which is the actual node that consumes the 'text' parameter.\n"
            f"Instead, the edge stays connected to the container boundary."
        )

    def test_output_edge_routes_from_normalize_text_after_expand(self):
        """Specifically test that output edge sources from normalize_text after expand.

        This is the output-side version of the bug:
        - At depth=0: preprocess -> analyze (from container) - CORRECT
        - After expand: normalize_text -> analyze (from internal) - EXPECTED
        - Bug: preprocess -> analyze (from container) - ACTUAL BUG
        """
        from playwright.sync_api import sync_playwright
        import tempfile
        import os

        workflow = make_workflow()

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            temp_path = f.name

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()

                # Render at depth=0, click to expand
                render_and_extract(page, workflow, depth=0, temp_path=temp_path)
                click_to_expand_container(page, "preprocess")
                data = extract_edge_routing(page)

                browser.close()
        finally:
            os.unlink(temp_path)

        # Find the edge to analyze (the output edge from preprocess area)
        output_edge = None
        for eid, info in data['edges'].items():
            if info['target'] == 'analyze':
                output_edge = info
                break

        assert output_edge is not None, (
            f"No edge to analyze found. Edges: {data['edges']}"
        )

        # After expand, the output edge should source from normalize_text's data node
        # or normalize_text itself, NOT preprocess
        source = output_edge['source']
        is_from_internal = (
            'normalize_text' in source
            or 'normalized' in source
            or 'data_normalize' in source
        )

        assert is_from_internal, (
            "INTERACTIVE EXPAND BUG: Output edge still sources from container!\n"
            f"\nExpected source: normalize_text or its data node\n"
            f"Actual source: '{source}'\n"
            f"\nAfter expanding preprocess, the output edge should route from\n"
            f"normalize_text (or its data node) which produces the 'normalized' output.\n"
            f"Instead, the edge stays connected to the container boundary."
        )
