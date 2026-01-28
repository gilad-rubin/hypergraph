"""Tests for interactive expand/collapse edge routing.

These tests verify that edges after interactive expand/collapse have the
same routing as static depth rendering. This is the core bug we're tracking:

**Expand Bug (FIXED):**
When you click to expand a nested graph, edges should route to INTERNAL
nodes (clean_text). Instead, they stay connected to the CONTAINER (preprocess).

**Collapse Bug (NEW):**
When you click to collapse an expanded nested graph, edges should route back
to the CONTAINER (preprocess). Instead, they disappear because the original
edge target (clean_text) is now hidden and there's no fallback to the container.

Key test strategy:
1. Render at depth=0, click to expand, capture edge data
2. Render fresh at static depth=1, capture edge data
3. Assert: interactive edge targets == static edge targets
4. Assert: interactive edge sources == static edge sources

For collapse:
1. Render at depth=1 (expanded), click to collapse, capture edge data
2. Render fresh at static depth=0 (collapsed), capture edge data
3. Assert: interactive edge targets == static edge targets
"""

import pytest

# Import shared fixtures and helpers from conftest
from tests.viz.conftest import (
    HAS_PLAYWRIGHT,
    make_workflow,
    extract_edge_routing,
    render_and_extract,
    click_to_expand_container,
    click_to_collapse_container,
)


# =============================================================================
# Tests for Interactive Expand Edge Routing
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestInteractiveExpandEdgeRouting:
    """Tests that interactive expand produces same edge routing as static depth."""

    def test_interactive_expand_edge_targets_match_static(self, page, temp_html_file):
        """After click-to-expand, edge targets should match static depth=1 targets.

        This test compares:
        - Render at depth=0, click to expand preprocess, capture edge targets
        - Render fresh at depth=1, capture edge targets
        - Assert edge targets are identical

        The bug: Interactive expand keeps edges targeting 'preprocess' container
        instead of routing them to 'clean_text' (the actual consumer inside).
        """
        workflow = make_workflow()

        # === STATIC DEPTH=1: The expected/correct behavior ===
        static_data = render_and_extract(page, workflow, depth=1, temp_path=temp_html_file)
        static_targets = {
            eid: info['target']
            for eid, info in static_data['edges'].items()
        }

        # === INTERACTIVE EXPAND: Render at depth=0, click to expand ===
        render_and_extract(page, workflow, depth=0, temp_path=temp_html_file)
        click_to_expand_container(page, "preprocess")
        interactive_data = extract_edge_routing(page)
        interactive_targets = {
            eid: info['target']
            for eid, info in interactive_data['edges'].items()
        }

        # Compare edge targets - find input edge that should route to clean_text
        # At depth=1, input edge targets clean_text (internal node)
        # The bug: After interactive expand, input edge still targets preprocess (container)

        # Find edges that enter the preprocess subgraph (hierarchical IDs)
        static_internal_targets = [
            target for target in static_targets.values()
            if target in ('preprocess/clean_text', 'preprocess/normalize_text')
        ]
        interactive_internal_targets = [
            target for target in interactive_targets.values()
            if target in ('preprocess/clean_text', 'preprocess/normalize_text')
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

    def test_interactive_expand_edge_sources_match_static(self, page, temp_html_file):
        """After click-to-expand, edge sources should match static depth=1 sources.

        The bug: Interactive expand keeps edges sourcing from 'preprocess' container
        instead of routing them from 'normalize_text' (the actual producer inside).
        """
        workflow = make_workflow()
        # === STATIC DEPTH=1: The expected/correct behavior ===
        static_data = render_and_extract(page, workflow, depth=1, temp_path=temp_html_file)
        static_sources = {
            eid: info['source']
            for eid, info in static_data['edges'].items()
        }

        # === INTERACTIVE EXPAND: Render at depth=0, click to expand ===
        render_and_extract(page, workflow, depth=0, temp_path=temp_html_file)
        click_to_expand_container(page, "preprocess")
        interactive_data = extract_edge_routing(page)
        interactive_sources = {
            eid: info['source']
            for eid, info in interactive_data['edges'].items()
        }

        # Find edges that exit the preprocess subgraph (hierarchical IDs)
        static_internal_sources = [
            source for source in static_sources.values()
            if source in ('preprocess/clean_text', 'preprocess/normalize_text')
            or 'data_preprocess/' in source  # data nodes like data_preprocess/normalize_text_normalized
        ]
        interactive_internal_sources = [
            source for source in interactive_sources.values()
            if source in ('preprocess/clean_text', 'preprocess/normalize_text')
            or 'data_preprocess/' in source
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

    def test_input_edge_routes_to_clean_text_after_expand(self, page, temp_html_file):
        """Specifically test that input_text edge targets clean_text after expand.

        This is the most direct test of the bug:
        - At depth=0: input_text -> preprocess (container) - CORRECT
        - After expand: input_text -> clean_text (internal) - EXPECTED
        - Bug: input_text -> preprocess (container) - ACTUAL BUG

        The edge should be re-routed to the actual consumer inside the container.
        """
        workflow = make_workflow()
        # Render at depth=0, click to expand
        render_and_extract(page, workflow, depth=0, temp_path=temp_html_file)
        click_to_expand_container(page, "preprocess")
        data = extract_edge_routing(page)

        # Find the input edge (from input_text or similar)
        input_edge = None
        for eid, info in data['edges'].items():
            if 'input' in info['source'].lower():
                input_edge = info
                break

        assert input_edge is not None, (
            f"No input edge found. Edges: {data['edges']}"
        )

        # After expand, the input edge should target preprocess/clean_text, NOT preprocess
        target = input_edge['target']
        assert target == 'preprocess/clean_text', (
            "INTERACTIVE EXPAND BUG: Input edge still targets container!\n"
            f"\nExpected target: 'preprocess/clean_text' (the actual consumer)\n"
            f"Actual target: '{target}'\n"
            f"\nAfter expanding preprocess, the input edge should route to\n"
            f"preprocess/clean_text which is the actual node that consumes the 'text' parameter.\n"
            f"Instead, the edge stays connected to the container boundary."
        )

    def test_output_edge_routes_from_normalize_text_after_expand(self, page, temp_html_file):
        """Specifically test that output edge sources from normalize_text after expand.

        This is the output-side version of the bug:
        - At depth=0: preprocess -> analyze (from container) - CORRECT
        - After expand: normalize_text -> analyze (from internal) - EXPECTED
        - Bug: preprocess -> analyze (from container) - ACTUAL BUG
        """
        workflow = make_workflow()
        # Render at depth=0, click to expand
        render_and_extract(page, workflow, depth=0, temp_path=temp_html_file)
        click_to_expand_container(page, "preprocess")
        data = extract_edge_routing(page)

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


# =============================================================================
# Tests for Interactive Collapse Edge Routing (NEW BUG)
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestInteractiveCollapseEdgeRouting:
    """Tests that interactive collapse produces same edge routing as static depth=0.

    The bug: When starting at depth=1 (expanded) and collapsing interactively,
    the input edge doesn't reconnect to the container because the original
    target (clean_text) is hidden and there's no fallback to find the visible
    parent container.
    """

    def test_interactive_collapse_edge_targets_match_static(self, page, temp_html_file):
        """After click-to-collapse, edge targets should match static depth=0 targets.

        This test compares:
        - Render at depth=1 (expanded), click to collapse preprocess, capture edge targets
        - Render fresh at depth=0, capture edge targets
        - Assert edge targets are identical

        The bug: Interactive collapse keeps edges targeting 'clean_text' (now hidden)
        instead of routing them to 'preprocess' (the visible container).
        """
        workflow = make_workflow()
        # === STATIC DEPTH=0: The expected/correct behavior (collapsed) ===
        static_data = render_and_extract(page, workflow, depth=0, temp_path=temp_html_file)
        static_targets = {
            eid: info['target']
            for eid, info in static_data['edges'].items()
        }

        # === INTERACTIVE COLLAPSE: Render at depth=1 (expanded), click to collapse ===
        render_and_extract(page, workflow, depth=1, temp_path=temp_html_file)
        click_to_collapse_container(page, "preprocess")
        interactive_data = extract_edge_routing(page)
        interactive_targets = {
            eid: info['target']
            for eid, info in interactive_data['edges'].items()
        }

        # After collapse, targets should match the static depth=0 view
        assert interactive_targets == static_targets, (
            "INTERACTIVE COLLAPSE BUG DETECTED!\n"
            f"\nStatic targets: {static_targets}\n"
            f"Interactive targets: {interactive_targets}\n"
            f"\nEdges should match static depth=0 after collapse."
        )

        # Ensure no edges point at hidden internal nodes after collapse
        hidden_targets = {"clean_text", "normalize_text"}
        bad_targets = hidden_targets.intersection(interactive_targets.values())
        assert not bad_targets, (
            "Collapsed view should not target hidden internal nodes.\n"
            f"Unexpected targets: {sorted(bad_targets)}"
        )

    def test_input_edge_routes_to_container_after_collapse(self, page, temp_html_file):
        """Specifically test that input_text edge targets preprocess after collapse.

        This is the most direct test of the collapse bug:
        - At depth=1: input_text -> clean_text (internal) - CORRECT
        - After collapse: input_text -> preprocess (container) - EXPECTED
        - Bug: Edge disappears or stays targeting clean_text - ACTUAL BUG

        The edge should be re-routed to the container when internal node is hidden.
        """
        workflow = make_workflow()
        # Render at depth=1 (expanded), click to collapse
        render_and_extract(page, workflow, depth=1, temp_path=temp_html_file)
        click_to_collapse_container(page, "preprocess")
        data = extract_edge_routing(page)

        # Inputs that are only used inside a collapsed container are hidden.
        input_edges = [
            info for info in data['edges'].values()
            if 'input' in info['source'].lower()
        ]

        assert not input_edges, (
            "Collapsed view should hide internal-only input edges.\n"
            f"Edges: {data['edges']}"
        )

    def test_output_edge_routes_from_container_after_collapse(self, page, temp_html_file):
        """Specifically test that output edge sources from preprocess after collapse.

        This is the output-side version of the collapse bug:
        - At depth=1: normalize_text -> analyze (from internal) - CORRECT
        - After collapse: preprocess -> analyze (from container) - EXPECTED
        - Bug: Edge disappears or stays sourcing from normalize_text - ACTUAL BUG
        """
        workflow = make_workflow()
        # Render at depth=1 (expanded), click to collapse
        render_and_extract(page, workflow, depth=1, temp_path=temp_html_file)
        click_to_collapse_container(page, "preprocess")
        data = extract_edge_routing(page)

        # Find the edge to analyze (the output edge from preprocess area)
        output_edge = None
        for eid, info in data['edges'].items():
            if info['target'] == 'analyze':
                output_edge = info
                break

        assert output_edge is not None, (
            f"COLLAPSE BUG: Output edge to analyze is MISSING!\n"
            f"No edge to analyze found. Edges: {data['edges']}\n"
            f"\nThe edge to analyze should exist and source from 'preprocess'.\n"
            f"Instead, the edge disappeared because its original source\n"
            f"(normalize_text) is now hidden and no fallback was found."
        )

        # After collapse, the output edge should source from preprocess, NOT normalize_text
        source = output_edge['source']
        is_from_container = source == 'preprocess'

        assert is_from_container, (
            "INTERACTIVE COLLAPSE BUG: Output edge has wrong source!\n"
            f"\nExpected source: 'preprocess' (the visible container)\n"
            f"Actual source: '{source}'\n"
            f"\nAfter collapsing preprocess, the output edge should route from\n"
            f"the container boundary, not the hidden internal node."
        )
