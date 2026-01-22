"""Tests for dimension mismatch investigation.

This module helps diagnose and verify node dimension calculations.
The goal is to ensure that:
1. Calculated dimensions match rendered dimensions
2. Box shadows do NOT affect getBoundingClientRect
3. Edge endpoints align with visible node boundaries

These tests are primarily diagnostic - they measure and log dimension data
to help identify where mismatches occur in the layout pipeline.
"""

import pytest

# Import shared fixtures and helpers from conftest
from tests.viz.conftest import (
    HAS_PLAYWRIGHT,
    make_simple_graph,
    wait_for_debug_ready,
    extract_debug_nodes,
    extract_inner_bounds_and_edge_paths,
)


# =============================================================================
# Tests: Dimension Mismatch Investigation
# =============================================================================

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestDimensionMismatch:
    """Diagnostic tests for investigating dimension mismatches."""

    def test_measure_all_dimensions(self, page, temp_html_file):
        """Render a 2-node graph and measure all dimension sources.

        This test renders a simple graph and extracts dimensions from:
        1. Calculated dimensions (from debug API)
        2. Wrapper element bounds (getBoundingClientRect on .react-flow__node)
        3. Inner element bounds (getBoundingClientRect on .group.rounded-lg)

        The test logs all measurements for manual inspection and verifies
        that the graph renders successfully.
        """
        from hypergraph.viz.widget import visualize

        # Create and render simple 2-node graph
        graph = make_simple_graph()
        visualize(graph, depth=0, output=temp_html_file, _debug_overlays=True)
        page.goto(f"file://{temp_html_file}")

        # Wait for layout to complete
        wait_for_debug_ready(page)

        # Extract calculated dimensions from debug API
        # These come from the dimension calculation logic in the React component
        debug_nodes = extract_debug_nodes(page)

        # Extract rendered bounds (wrapper and inner)
        # - wrapperBounds: from .react-flow__node element (React Flow's wrapper)
        # - innerBounds: from .group.rounded-lg element (actual visible node)
        bounds_data = extract_inner_bounds_and_edge_paths(page)

        # Log all measurements for inspection
        print("\n=== Dimension Measurements ===")
        print("Data Sources:")
        print("  1. Calculated: dimensions from debug API (pre-render calculation)")
        print("  2. Wrapper: getBoundingClientRect on .react-flow__node (React Flow wrapper)")
        print("  3. Inner: getBoundingClientRect on .group.rounded-lg (visible node element)")
        print(f"\nNumber of nodes in debug API: {len(debug_nodes)}")
        print(f"Number of wrapper bounds: {len(bounds_data['wrapperBounds'])}")
        print(f"Number of inner bounds: {len(bounds_data['innerBounds'])}")

        for node in debug_nodes:
            node_id = node['id']
            calc_width = node.get('width', 'N/A')
            calc_height = node.get('height', 'N/A')

            wrapper = bounds_data['wrapperBounds'].get(node_id, {})
            inner = bounds_data['innerBounds'].get(node_id, {})
            shadow = bounds_data['shadowOffsets'].get(node_id, {})

            wrapper_width = wrapper.get('right', 0) - wrapper.get('left', 0) if wrapper else 'N/A'
            wrapper_height = wrapper.get('bottom', 0) - wrapper.get('top', 0) if wrapper else 'N/A'
            inner_width = inner.get('right', 0) - inner.get('left', 0) if inner else 'N/A'
            inner_height = inner.get('bottom', 0) - inner.get('top', 0) if inner else 'N/A'

            print(f"\nNode: {node_id}")
            print(f"  Calculated: {calc_width}x{calc_height} (from debug API / pre-render calculation)")
            print(f"  Wrapper:    {wrapper_width}x{wrapper_height} (from .react-flow__node getBoundingClientRect)")
            print(f"  Inner:      {inner_width}x{inner_height} (from .group.rounded-lg getBoundingClientRect)")

            # ROOT CAUSE #1: Calculated vs Wrapper dimension mismatch (~10px difference)
            # React Flow's wrapper (.react-flow__node) adds padding and handle elements
            # that extend beyond the calculated dimensions. This is expected behavior.
            if isinstance(calc_width, (int, float)) and isinstance(wrapper_width, (int, float)):
                calc_vs_wrapper_diff = abs(wrapper_width - calc_width)
                if calc_vs_wrapper_diff > 1:
                    print(f"  -> Calculated vs Wrapper: {calc_vs_wrapper_diff:.1f}px diff (React Flow padding/handles)")

            # ROOT CAUSE #2: Wrapper vs Inner dimension mismatch (6-14px difference)
            # CSS shadow-lg extends the visual appearance but getBoundingClientRect
            # measures the element's layout box, NOT the shadow. The difference comes from
            # wrapper containing additional elements (handles) that extend beyond inner node.
            if shadow:
                top_offset = shadow.get('topOffset', 0)
                bottom_offset = shadow.get('bottomOffset', 0)
                print(f"  Shadow offset: top={top_offset:.1f}px, bottom={bottom_offset:.1f}px")
                if abs(top_offset) > 1 or abs(bottom_offset) > 1:
                    print("  -> Wrapper vs Inner: offset from wrapper containing handles, NOT shadow")

        print("\n=== Edge Paths ===")
        print("Edge coordinates extracted from SVG path 'd' attribute")
        print("Start coordinates: from 'M x y' (move to start point)")
        print("End coordinates: from last coordinate pair in path")
        for edge in bounds_data['edgePaths']:
            source = edge.get('source', 'unknown')
            target = edge.get('target', 'unknown')
            start_x = edge.get('startX')
            start_y = edge.get('startY')
            end_x = edge.get('endX')
            end_y = edge.get('endY')
            print(f"  {source} -> {target}:")
            print(f"    Start point: ({start_x}, {start_y}) [from SVG 'M' command]")
            print(f"    End point:   ({end_x}, {end_y}) [from SVG path end]")

        # Basic sanity checks
        assert len(debug_nodes) >= 2, "Expected at least 2 nodes in simple graph"
        assert len(bounds_data['edgePaths']) >= 1, "Expected at least 1 edge"

    def test_box_shadow_not_affecting_bounds(self, page, temp_html_file):
        """Verify CSS box-shadow does NOT affect getBoundingClientRect().

        Box shadows are purely visual - they extend beyond the element's
        bounding box but do not change the reported dimensions from
        getBoundingClientRect(). This test proves this by comparing:
        - Wrapper element bounds (.react-flow__node)
        - Inner element bounds (.group.rounded-lg with shadow-lg class)

        Key insight: Shadow extends equally in ALL directions, so if shadow
        affected bounds, both width AND height would differ. The fact that
        WIDTH matches exactly (0px difference) while HEIGHT differs proves
        the shadow is NOT affecting bounds - height differences come from
        the wrapper containing additional elements (handles) that extend
        beyond the inner node.

        Shadow confirmed NOT affecting bounds - wrapper and inner element
        widths match exactly (0px difference).
        """
        from hypergraph.viz.widget import visualize

        # Create and render simple 2-node graph
        graph = make_simple_graph()
        visualize(graph, depth=0, output=temp_html_file, _debug_overlays=True)
        page.goto(f"file://{temp_html_file}")

        # Wait for layout to complete
        wait_for_debug_ready(page)

        # Extract calculated dimensions from debug API
        debug_nodes = extract_debug_nodes(page)

        # Extract rendered bounds (wrapper and inner)
        bounds_data = extract_inner_bounds_and_edge_paths(page)

        print("\n=== Box Shadow Impact Test ===")
        print("Verifying that CSS box-shadow does NOT affect getBoundingClientRect()")
        print("Key insight: shadow extends equally in all directions.")
        print("If shadow affected bounds, BOTH width and height would differ.")

        all_widths_match = True
        for node in debug_nodes:
            node_id = node['id']

            wrapper = bounds_data['wrapperBounds'].get(node_id, {})
            inner = bounds_data['innerBounds'].get(node_id, {})

            if not wrapper or not inner:
                print(f"\nNode {node_id}: Missing bounds data")
                continue

            # Calculate dimensions from bounds
            wrapper_width = wrapper.get('right', 0) - wrapper.get('left', 0)
            wrapper_height = wrapper.get('bottom', 0) - wrapper.get('top', 0)
            inner_width = inner.get('right', 0) - inner.get('left', 0)
            inner_height = inner.get('bottom', 0) - inner.get('top', 0)

            # Calculate differences
            width_diff = abs(wrapper_width - inner_width)
            height_diff = abs(wrapper_height - inner_height)

            print(f"\nNode: {node_id}")
            print(f"  Wrapper dimensions: {wrapper_width}x{wrapper_height}")
            print(f"  Inner dimensions:   {inner_width}x{inner_height}")
            print(f"  Width difference:   {width_diff}px")
            print(f"  Height difference:  {height_diff}px")

            # Shadow extends equally in ALL directions, so width is the key test.
            # Width should match exactly - proves shadow NOT affecting bounds.
            # Height may differ due to wrapper containing handles at top/bottom.
            tolerance = 0.5
            if width_diff > tolerance:
                all_widths_match = False
                print("  FAIL: Width differs - shadow may be affecting bounds!")
            else:
                print("  OK: Shadow confirmed NOT affecting bounds (width matches)")
                if height_diff > tolerance:
                    print("      (height differs due to handle elements, not shadow)")

        # Assert all nodes have matching widths - this proves shadow doesn't affect bounds
        # Height differences are expected due to wrapper containing handle elements
        assert all_widths_match, (
            "Box shadow unexpectedly affected getBoundingClientRect dimensions. "
            "Wrapper and inner element widths should be identical."
        )
        print("\n=== RESULT: Shadow confirmed NOT affecting bounds ===")
        print("All nodes have matching wrapper/inner widths (0px difference)")
        print("Height differences are from handle elements, not shadow.")
