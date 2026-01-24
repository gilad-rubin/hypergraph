"""Tests for scope-aware visibility in nested graph visualization.

These tests verify:
1. Edges route to internal nodes (not containers) when expanded
2. INPUT nodes are positioned inside containers when their consumers are internal
3. Internal-only DATA nodes are hidden when containers are collapsed
"""

import pytest
from hypergraph import Graph, node


# =============================================================================
# Test Graph: Generation with nested prompt_building
# =============================================================================

@node(output_name="filtered_document")
def filter_document_pages(document: str, selected_pages: list[int]) -> str:
    return f"filtered({document})"


@node(output_name="raw_answer")
def generate_answer(chat_messages: list[dict]) -> str:
    return f"generated({chat_messages})"


@node(output_name="response")
def format_response(raw_answer: str, query: str) -> dict:
    return {"answer": raw_answer, "query": query}


@node(output_name="system_prompt")
def get_system_prompt(system_instructions: str) -> str:
    return f"system: {system_instructions}"


@node(output_name="context_text")
def build_context(filtered_document: str) -> str:
    return f"context: {filtered_document}"


@node(output_name="chat_messages")
def build_prompt(
    system_prompt: str,
    context_text: str,
    query: str,
    images: list[bytes] | None = None,
) -> list[dict]:
    return [{"role": "user", "content": query}]


def make_generation_graph() -> Graph:
    """Create generation graph with nested prompt_building."""
    prompt_building = Graph(
        nodes=[get_system_prompt, build_context, build_prompt],
        name="prompt_building",
    )
    return Graph(
        nodes=[
            filter_document_pages,
            prompt_building.as_node(),
            generate_answer,
            format_response,
        ],
        name="generation",
    )


# =============================================================================
# Test: Edge Routing to Internal Nodes
# =============================================================================

class TestEdgeRoutingToInternalNodes:
    """Test that edges route to actual internal nodes, not containers."""

    def test_filtered_document_routes_to_build_context_when_expanded(self):
        """When prompt_building is expanded, filtered_document should go to build_context.

        The flat graph has: filter_document_pages -> prompt_building
        But when expanded, it should route to: filter_document_pages -> build_context

        This is because build_context is the actual consumer of filtered_document.
        """
        from hypergraph.viz.renderer import render_graph

        graph = make_generation_graph()
        flat_graph = graph.to_flat_graph()
        result = render_graph(flat_graph, depth=1)

        # Get pre-computed edges for expanded state
        edges_by_state = result["meta"]["edgesByState"]
        expanded_key = "prompt_building:1|sep:0"  # expanded, merged outputs

        assert expanded_key in edges_by_state, f"Key {expanded_key} not found"
        edges = edges_by_state[expanded_key]

        # Find the edge from filter_document_pages
        fdp_edges = [e for e in edges if e["source"] == "filter_document_pages"]

        assert len(fdp_edges) == 1, f"Expected 1 edge from filter_document_pages, got {len(fdp_edges)}"

        # THE KEY ASSERTION: Target should be build_context, NOT prompt_building
        target = fdp_edges[0]["target"]
        assert target == "build_context", (
            f"EDGE ROUTING BUG!\n"
            f"Expected: filter_document_pages -> build_context\n"
            f"Actual: filter_document_pages -> {target}\n"
            f"\nWhen prompt_building is expanded, edges should route to\n"
            f"the actual internal consumer (build_context), not the container."
        )

    def test_collapsed_routes_to_container(self):
        """When prompt_building is collapsed, filtered_document should go to container.

        The flat graph has: filter_document_pages -> prompt_building
        When collapsed, this should remain as-is since internal nodes aren't visible.
        """
        from hypergraph.viz.renderer import render_graph

        graph = make_generation_graph()
        flat_graph = graph.to_flat_graph()
        result = render_graph(flat_graph, depth=0)

        # Get pre-computed edges for collapsed state
        edges_by_state = result["meta"]["edgesByState"]
        collapsed_key = "prompt_building:0|sep:0"  # collapsed, merged outputs

        assert collapsed_key in edges_by_state, f"Key {collapsed_key} not found"
        edges = edges_by_state[collapsed_key]

        # Find the edge from filter_document_pages
        fdp_edges = [e for e in edges if e["source"] == "filter_document_pages"]

        assert len(fdp_edges) == 1, f"Expected 1 edge from filter_document_pages, got {len(fdp_edges)}"

        # When collapsed, target should be the container
        target = fdp_edges[0]["target"]
        assert target == "prompt_building", (
            f"Expected: filter_document_pages -> prompt_building (collapsed)\n"
            f"Actual: filter_document_pages -> {target}"
        )


# =============================================================================
# Test: INPUT Node Positioning
# =============================================================================

class TestInputNodePositioning:
    """Test that INPUT nodes are positioned correctly relative to containers."""

    def test_internal_only_input_has_owner_container(self):
        """system_instructions should have ownerContainer=prompt_building when expanded.

        system_instructions is only consumed by get_system_prompt (inside prompt_building).
        When the container is expanded, the INPUT should be scoped to that container.
        """
        from hypergraph.viz.renderer import render_graph

        graph = make_generation_graph()
        flat_graph = graph.to_flat_graph()
        result = render_graph(flat_graph, depth=1)

        # Find the system_instructions INPUT node
        input_node = None
        for node in result["nodes"]:
            if node["id"] == "input_system_instructions":
                input_node = node
                break

        assert input_node is not None, "input_system_instructions node not found"

        owner = input_node["data"].get("ownerContainer")
        assert owner == "prompt_building", (
            f"SCOPE BUG!\n"
            f"Expected: ownerContainer='prompt_building'\n"
            f"Actual: ownerContainer={owner}\n"
            f"\nsystem_instructions is only consumed by get_system_prompt\n"
            f"which is inside prompt_building. It should be scoped to that container."
        )

    def test_external_input_has_no_owner(self):
        """query should have no ownerContainer (consumed by both inner and outer).

        query is consumed by:
        - build_prompt (inside prompt_building)
        - format_response (outside, at root level)

        Since it has consumers at multiple levels, it should stay at root.
        """
        from hypergraph.viz.renderer import render_graph

        graph = make_generation_graph()
        flat_graph = graph.to_flat_graph()
        result = render_graph(flat_graph, depth=1)

        # Find the query INPUT node
        input_node = None
        for node in result["nodes"]:
            if node["id"] == "input_query":
                input_node = node
                break

        assert input_node is not None, "input_query node not found"

        owner = input_node["data"].get("ownerContainer")
        assert owner is None, (
            f"Expected: ownerContainer=None (query has external consumers)\n"
            f"Actual: ownerContainer={owner}"
        )


# =============================================================================
# Test: INPUT Node Visual Positioning Inside Containers
# =============================================================================

try:
    from tests.viz.conftest import HAS_PLAYWRIGHT
except ImportError:
    HAS_PLAYWRIGHT = False


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestInputPositioningInsideContainers:
    """Test that INPUT nodes are visually positioned inside their ownerContainer."""

    def test_system_instructions_inside_container_bounds(self):
        """system_instructions should be positioned inside prompt_building bounds.

        When prompt_building is expanded and system_instructions has
        ownerContainer=prompt_building, it should be visually inside the container.
        """
        from hypergraph.viz import extract_debug_data

        graph = make_generation_graph()
        data = extract_debug_data(graph, depth=1)

        # Find prompt_building container bounds
        container = None
        for node in data.nodes:
            if node.get("id") == "prompt_building":
                container = node
                break

        assert container is not None, "prompt_building container not found"

        container_left = container.get("x", 0)
        container_right = container_left + container.get("width", 0)
        container_top = container.get("y", 0)
        container_bottom = container_top + container.get("height", 0)

        # Find system_instructions INPUT position
        input_node = None
        for node in data.nodes:
            if node.get("id") == "input_system_instructions":
                input_node = node
                break

        assert input_node is not None, "input_system_instructions not found"

        input_x = input_node.get("x", 0)
        input_y = input_node.get("y", 0)
        input_width = input_node.get("width", 100)
        input_height = input_node.get("height", 36)

        # Check if INPUT is inside container bounds
        is_inside_x = container_left <= input_x and (input_x + input_width) <= container_right
        is_inside_y = container_top <= input_y and (input_y + input_height) <= container_bottom

        assert is_inside_x and is_inside_y, (
            f"INPUT POSITIONING BUG!\n"
            f"input_system_instructions should be INSIDE prompt_building bounds.\n"
            f"\nContainer bounds:\n"
            f"  left={container_left:.0f}, right={container_right:.0f}\n"
            f"  top={container_top:.0f}, bottom={container_bottom:.0f}\n"
            f"\nINPUT position:\n"
            f"  x={input_x:.0f}, y={input_y:.0f}\n"
            f"  (right edge: {input_x + input_width:.0f}, bottom edge: {input_y + input_height:.0f})\n"
            f"\nINPUT is {'inside' if is_inside_x else 'OUTSIDE'} X bounds\n"
            f"INPUT is {'inside' if is_inside_y else 'OUTSIDE'} Y bounds"
        )

    def test_images_inside_container_bounds(self):
        """images INPUT should be positioned inside prompt_building bounds."""
        from hypergraph.viz import extract_debug_data

        graph = make_generation_graph()
        data = extract_debug_data(graph, depth=1)

        # Find prompt_building container bounds
        container = None
        for node in data.nodes:
            if node.get("id") == "prompt_building":
                container = node
                break

        assert container is not None, "prompt_building container not found"

        container_left = container.get("x", 0)
        container_right = container_left + container.get("width", 0)
        container_top = container.get("y", 0)
        container_bottom = container_top + container.get("height", 0)

        # Find images INPUT position
        input_node = None
        for node in data.nodes:
            if node.get("id") == "input_images":
                input_node = node
                break

        assert input_node is not None, "input_images not found"

        input_x = input_node.get("x", 0)
        input_y = input_node.get("y", 0)
        input_width = input_node.get("width", 100)
        input_height = input_node.get("height", 36)

        # Check if INPUT is inside container bounds
        is_inside_x = container_left <= input_x and (input_x + input_width) <= container_right
        is_inside_y = container_top <= input_y and (input_y + input_height) <= container_bottom

        assert is_inside_x and is_inside_y, (
            f"INPUT POSITIONING BUG!\n"
            f"input_images should be INSIDE prompt_building bounds.\n"
            f"\nContainer bounds:\n"
            f"  left={container_left:.0f}, right={container_right:.0f}\n"
            f"  top={container_top:.0f}, bottom={container_bottom:.0f}\n"
            f"\nINPUT position:\n"
            f"  x={input_x:.0f}, y={input_y:.0f}\n"
            f"\nINPUT is {'inside' if is_inside_x else 'OUTSIDE'} X bounds\n"
            f"INPUT is {'inside' if is_inside_y else 'OUTSIDE'} Y bounds"
        )

    def test_query_outside_container_bounds(self):
        """query INPUT should NOT be inside prompt_building (has external consumers)."""
        from hypergraph.viz import extract_debug_data

        graph = make_generation_graph()
        data = extract_debug_data(graph, depth=1)

        # Find prompt_building container bounds
        container = None
        for node in data.nodes:
            if node.get("id") == "prompt_building":
                container = node
                break

        assert container is not None, "prompt_building container not found"

        container_top = container.get("y", 0)
        container_bottom = container_top + container.get("height", 0)

        # Find query INPUT position
        input_node = None
        for node in data.nodes:
            if node.get("id") == "input_query":
                input_node = node
                break

        assert input_node is not None, "input_query not found"

        input_y = input_node.get("y", 0)
        input_height = input_node.get("height", 36)

        # query should NOT be fully inside the container (it has external consumers)
        # It's OK if it's above or partially overlapping, but should not be contained
        is_fully_inside_y = container_top <= input_y and (input_y + input_height) <= container_bottom

        # This is a sanity check - query should stay at root level
        assert not is_fully_inside_y or input_y < container_top, (
            f"query INPUT should be at ROOT level (outside container), not inside.\n"
            f"Container top={container_top:.0f}, bottom={container_bottom:.0f}\n"
            f"INPUT y={input_y:.0f}"
        )


# =============================================================================
# Test: Internal-Only DATA Node Visibility
# =============================================================================

class TestInternalOnlyDataNodes:
    """Test that internal-only DATA nodes have the correct flag."""

    def test_context_text_is_internal_only(self):
        """context_text should be marked as internalOnly.

        context_text is produced by build_context and consumed by build_prompt.
        Both are inside prompt_building, so it's internal-only.
        """
        from hypergraph.viz.renderer import render_graph

        graph = make_generation_graph()
        flat_graph = graph.to_flat_graph()
        result = render_graph(flat_graph, depth=1)

        # Find the context_text DATA node (from build_context)
        data_node = None
        for node in result["nodes"]:
            if node["id"] == "data_build_context_context_text":
                data_node = node
                break

        assert data_node is not None, "data_build_context_context_text node not found"

        internal_only = data_node["data"].get("internalOnly")
        assert internal_only is True, (
            f"INTERNAL-ONLY BUG!\n"
            f"Expected: internalOnly=True\n"
            f"Actual: internalOnly={internal_only}\n"
            f"\ncontext_text is produced and consumed entirely within prompt_building."
        )

    def test_chat_messages_is_not_internal_only(self):
        """chat_messages should NOT be marked as internalOnly.

        chat_messages is produced by build_prompt (inside) but consumed by
        generate_answer (outside). It has external consumers.
        """
        from hypergraph.viz.renderer import render_graph

        graph = make_generation_graph()
        flat_graph = graph.to_flat_graph()
        result = render_graph(flat_graph, depth=1)

        # Find the chat_messages DATA node (from build_prompt)
        data_node = None
        for node in result["nodes"]:
            if node["id"] == "data_build_prompt_chat_messages":
                data_node = node
                break

        assert data_node is not None, "data_build_prompt_chat_messages node not found"

        internal_only = data_node["data"].get("internalOnly")
        assert internal_only is False, (
            f"Expected: internalOnly=False (chat_messages has external consumer)\n"
            f"Actual: internalOnly={internal_only}"
        )
