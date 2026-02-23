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

        # THE KEY ASSERTION: Target should be prompt_building/build_context, NOT prompt_building
        target = fdp_edges[0]["target"]
        assert target == "prompt_building/build_context", (
            f"EDGE ROUTING BUG!\n"
            f"Expected: filter_document_pages -> prompt_building/build_context\n"
            f"Actual: filter_document_pages -> {target}\n"
            f"\nWhen prompt_building is expanded, edges should route to\n"
            f"the actual internal consumer (prompt_building/build_context), not the container."
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
            f"Expected: filter_document_pages -> prompt_building (collapsed)\nActual: filter_document_pages -> {target}"
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
        for item in result["nodes"]:
            if item["id"] == "input_system_instructions":
                input_node = item
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
        for item in result["nodes"]:
            if item["id"] == "input_query":
                input_node = item
                break

        assert input_node is not None, "input_query node not found"

        owner = input_node["data"].get("ownerContainer")
        assert owner is None, f"Expected: ownerContainer=None (query has external consumers)\nActual: ownerContainer={owner}"


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
        for item in data.nodes:
            if item.get("id") == "prompt_building":
                container = item
                break

        assert container is not None, "prompt_building container not found"

        container_left = container.get("x", 0)
        container_right = container_left + container.get("width", 0)
        container_top = container.get("y", 0)
        container_bottom = container_top + container.get("height", 0)

        # Find system_instructions INPUT position
        input_node = None
        for item in data.nodes:
            if item.get("id") == "input_system_instructions":
                input_node = item
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
        for item in data.nodes:
            if item.get("id") == "prompt_building":
                container = item
                break

        assert container is not None, "prompt_building container not found"

        container_left = container.get("x", 0)
        container_right = container_left + container.get("width", 0)
        container_top = container.get("y", 0)
        container_bottom = container_top + container.get("height", 0)

        # Find images INPUT position
        input_node = None
        for item in data.nodes:
            if item.get("id") == "input_images":
                input_node = item
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
        for item in data.nodes:
            if item.get("id") == "prompt_building":
                container = item
                break

        assert container is not None, "prompt_building container not found"

        container_top = container.get("y", 0)
        container_bottom = container_top + container.get("height", 0)

        # Find query INPUT position
        input_node = None
        for item in data.nodes:
            if item.get("id") == "input_query":
                input_node = item
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

        # Find the context_text DATA node (from prompt_building/build_context)
        data_node = None
        for item in result["nodes"]:
            if item["id"] == "data_prompt_building/build_context_context_text":
                data_node = item
                break

        assert data_node is not None, "data_prompt_building/build_context_context_text node not found"

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

        # Find the chat_messages DATA node (from prompt_building/build_prompt)
        data_node = None
        for item in result["nodes"]:
            if item["id"] == "data_prompt_building/build_prompt_chat_messages":
                data_node = item
                break

        assert data_node is not None, "data_prompt_building/build_prompt_chat_messages node not found"

        internal_only = data_node["data"].get("internalOnly")
        assert internal_only is False, f"Expected: internalOnly=False (chat_messages has external consumer)\nActual: internalOnly={internal_only}"


# =============================================================================
# Test: Control Edge Routing (Route/IfElse Nodes)
# =============================================================================


class TestControlEdgeRouting:
    """Test that control edges from route/ifelse nodes route correctly."""

    def test_control_edge_routes_to_container_when_collapsed(self):
        """Control edge should go to container when collapsed.

        When a route targets a container that is collapsed,
        the edge should go to the container boundary.
        """
        from hypergraph import END, Graph, node, route
        from hypergraph.viz.renderer import render_graph

        @node(output_name="result")
        def inner_step(x: int) -> int:
            return x * 2

        @route(targets=["inner_graph", END])
        def decide(x: int) -> str:
            return "inner_graph" if x > 0 else END

        inner = Graph(nodes=[inner_step], name="inner_graph")
        outer = Graph(nodes=[decide, inner.as_node()], name="outer")
        flat_graph = outer.to_flat_graph()

        result = render_graph(flat_graph, depth=0)
        edges_by_state = result["meta"]["edgesByState"]
        collapsed_key = "inner_graph:0|sep:0"

        assert collapsed_key in edges_by_state
        edges = edges_by_state[collapsed_key]

        # Find control edges from decide (excluding END edges)
        control_edges = [e for e in edges if e["source"] == "decide" and e["target"] != "__end__"]
        assert len(control_edges) == 1

        # When collapsed, target should be the container
        target = control_edges[0]["target"]
        assert target == "inner_graph", f"Expected: decide -> inner_graph (collapsed container)\nActual: decide -> {target}"

    def test_control_edge_routes_to_internal_node_when_expanded(self):
        """Control edge should go to internal node when container is expanded.

        When a route targets a container that is expanded,
        the edge should go to the entry point node inside the container.
        """
        from hypergraph import END, Graph, node, route
        from hypergraph.viz.renderer import render_graph

        @node(output_name="result")
        def inner_step(x: int) -> int:
            return x * 2

        @route(targets=["inner_graph", END])
        def decide(x: int) -> str:
            return "inner_graph" if x > 0 else END

        inner = Graph(nodes=[inner_step], name="inner_graph")
        outer = Graph(nodes=[decide, inner.as_node()], name="outer")
        flat_graph = outer.to_flat_graph()

        result = render_graph(flat_graph, depth=1)
        edges_by_state = result["meta"]["edgesByState"]
        expanded_key = "inner_graph:1|sep:0"

        assert expanded_key in edges_by_state
        edges = edges_by_state[expanded_key]

        # Find control edges from decide (excluding END edges)
        control_edges = [e for e in edges if e["source"] == "decide" and e["target"] != "__end__"]
        assert len(control_edges) == 1

        # When expanded, target should be the internal entry point (hierarchical ID)
        target = control_edges[0]["target"]
        assert target == "inner_graph/inner_step", (
            f"CONTROL EDGE ROUTING BUG!\n"
            f"Expected: decide -> inner_graph/inner_step (entry point inside container)\n"
            f"Actual: decide -> {target}\n"
            f"\nWhen inner_graph is expanded, control edges should route to\n"
            f"the entry point node inside, not the container boundary."
        )


# =============================================================================
# Test Graph: Batch Evaluation with Nested Mapped Graph
# =============================================================================
# Models the retrieval_recall_batch structure:
# build_pairs → batch_eval (nested, mapped) → compute_metrics


@node(output_name="eval_pairs")
def build_pairs(queries: list[str]) -> list[dict]:
    """Build evaluation pairs from queries."""
    return [{"query": q} for q in queries]


@node(output_name="eval_result")
def run_single_eval(eval_pair: dict) -> dict:
    """Run a single evaluation (inside nested graph)."""
    return {"result": eval_pair["query"]}


@node(output_name="metrics")
def compute_metrics(eval_results: list[dict]) -> dict:
    """Aggregate evaluation results into metrics."""
    return {"count": len(eval_results)}


def make_batch_eval_graph() -> Graph:
    """Create a batch evaluation graph with nested mapped subgraph.

    Structure:
        build_pairs → batch_eval (nested, mapped over eval_pairs) → compute_metrics

    This models the retrieval_recall_batch_config pattern where:
    - build_pairs outputs eval_pairs
    - batch_eval consumes eval_pairs (mapped), outputs eval_results
    - compute_metrics consumes eval_results
    """
    batch_eval = Graph(nodes=[run_single_eval], name="batch_eval")
    mapped_eval = batch_eval.as_node().with_inputs(eval_pair="eval_pairs").with_outputs(eval_result="eval_results").map_over("eval_pairs")

    return Graph(
        nodes=[build_pairs, mapped_eval, compute_metrics],
        name="batch_evaluation",
    )


# =============================================================================
# Test: Edge Routing INTO Expanded Nested Graph
# =============================================================================


class TestEdgeRoutingIntoExpandedContainer:
    """Test that data edges route INTO internal consumers when container is expanded.

    Bug: When batch_eval is expanded, the edge from eval_pairs still goes to
    the container boundary instead of routing to the internal consumer (run_single_eval).
    """

    def test_edge_routes_to_internal_consumer_when_expanded(self):
        """Edge from build_pairs should go to run_single_eval when expanded.

        When batch_eval is expanded, the edge carrying eval_pairs should route
        to the actual internal consumer (run_single_eval), not the container.
        """
        from hypergraph.viz.renderer import render_graph

        graph = make_batch_eval_graph()
        flat_graph = graph.to_flat_graph()
        result = render_graph(flat_graph, depth=1)

        # Get pre-computed edges for expanded state
        edges_by_state = result["meta"]["edgesByState"]
        expanded_key = "batch_eval:1|sep:0"  # expanded, merged outputs

        assert expanded_key in edges_by_state, f"Key {expanded_key} not found"
        edges = edges_by_state[expanded_key]

        # Find the edge from build_pairs
        bp_edges = [e for e in edges if e["source"] == "build_pairs"]

        assert len(bp_edges) == 1, f"Expected 1 edge from build_pairs, got {len(bp_edges)}: {bp_edges}"

        # THE KEY ASSERTION: Target should be batch_eval/run_single_eval, NOT batch_eval
        target = bp_edges[0]["target"]
        assert target == "batch_eval/run_single_eval", (
            f"EDGE ROUTING BUG!\n"
            f"Expected: build_pairs -> batch_eval/run_single_eval\n"
            f"Actual: build_pairs -> {target}\n"
            f"\nWhen batch_eval is expanded, the edge carrying eval_pairs should\n"
            f"route to the actual internal consumer (batch_eval/run_single_eval), not the container."
        )

    def test_edge_routes_to_container_when_collapsed(self):
        """Edge from build_pairs should go to batch_eval when collapsed."""
        from hypergraph.viz.renderer import render_graph

        graph = make_batch_eval_graph()
        flat_graph = graph.to_flat_graph()
        result = render_graph(flat_graph, depth=0)

        # Get pre-computed edges for collapsed state
        edges_by_state = result["meta"]["edgesByState"]
        collapsed_key = "batch_eval:0|sep:0"

        assert collapsed_key in edges_by_state, f"Key {collapsed_key} not found"
        edges = edges_by_state[collapsed_key]

        # Find the edge from build_pairs
        bp_edges = [e for e in edges if e["source"] == "build_pairs"]

        assert len(bp_edges) == 1, f"Expected 1 edge from build_pairs, got {len(bp_edges)}"

        # When collapsed, target should be the container
        target = bp_edges[0]["target"]
        assert target == "batch_eval", f"Expected: build_pairs -> batch_eval (collapsed)\nActual: build_pairs -> {target}"


# =============================================================================
# Test: Edge Routing OUT OF Expanded Nested Graph
# =============================================================================


class TestEdgeRoutingFromExpandedContainer:
    """Test that data edges route FROM internal producers when container is expanded.

    Bug: When batch_eval is expanded, compute_metrics appears to receive an edge
    from nowhere because the source is shown as the container, not the internal producer.
    """

    def test_edge_from_internal_producer_when_expanded(self):
        """Edge to compute_metrics should come from run_single_eval when expanded.

        When batch_eval is expanded, the edge carrying eval_results should show
        it comes from the actual internal producer (run_single_eval).
        """
        from hypergraph.viz.renderer import render_graph

        graph = make_batch_eval_graph()
        flat_graph = graph.to_flat_graph()
        result = render_graph(flat_graph, depth=1)

        # Get pre-computed edges for expanded state
        edges_by_state = result["meta"]["edgesByState"]
        expanded_key = "batch_eval:1|sep:0"  # expanded, merged outputs

        assert expanded_key in edges_by_state, f"Key {expanded_key} not found"
        edges = edges_by_state[expanded_key]

        # Find the edge to compute_metrics
        cm_edges = [e for e in edges if e["target"] == "compute_metrics"]

        assert len(cm_edges) == 1, (
            f"Expected 1 edge to compute_metrics, got {len(cm_edges)}.\n"
            f"All edges: {[(e['source'], e['target']) for e in edges]}\n"
            f"\nBUG: Edge may be missing or malformed when container is expanded."
        )

        # THE KEY ASSERTION: Source should be batch_eval/run_single_eval, NOT batch_eval
        source = cm_edges[0]["source"]
        assert source == "batch_eval/run_single_eval", (
            f"EDGE ROUTING BUG!\n"
            f"Expected: batch_eval/run_single_eval -> compute_metrics\n"
            f"Actual: {source} -> compute_metrics\n"
            f"\nWhen batch_eval is expanded, the edge carrying eval_results should\n"
            f"show it comes from the actual internal producer (batch_eval/run_single_eval)."
        )

    def test_edge_from_container_when_collapsed(self):
        """Edge to compute_metrics should come from batch_eval when collapsed."""
        from hypergraph.viz.renderer import render_graph

        graph = make_batch_eval_graph()
        flat_graph = graph.to_flat_graph()
        result = render_graph(flat_graph, depth=0)

        # Get pre-computed edges for collapsed state
        edges_by_state = result["meta"]["edgesByState"]
        collapsed_key = "batch_eval:0|sep:0"

        assert collapsed_key in edges_by_state, f"Key {collapsed_key} not found"
        edges = edges_by_state[collapsed_key]

        # Find the edge to compute_metrics
        cm_edges = [e for e in edges if e["target"] == "compute_metrics"]

        assert len(cm_edges) == 1, f"Expected 1 edge to compute_metrics, got {len(cm_edges)}"

        # When collapsed, source should be the container
        source = cm_edges[0]["source"]
        assert source == "batch_eval", f"Expected: batch_eval -> compute_metrics (collapsed)\nActual: {source} -> compute_metrics"


# =============================================================================
# Test Graph: Input Groups inside Collapsed Containers
# =============================================================================


@node(output_name="alpha_out")
def alpha_step(alpha: int) -> int:
    return alpha


@node(output_name="beta_out")
def beta_step(beta: int) -> int:
    return beta


def make_input_group_container_graph() -> Graph:
    """Graph where two inputs live only inside a nested container."""
    inner = Graph(nodes=[alpha_step, beta_step], name="inner")
    return Graph(nodes=[inner.as_node()], name="outer")


# =============================================================================
# Test: INPUT/INPUT_GROUP visibility when container is collapsed
# =============================================================================


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestInputVisibilityWhenCollapsed:
    """Inputs owned by a collapsed container should be hidden."""

    def test_internal_input_hidden_when_collapsed(self):
        """Inputs scoped to a collapsed container should not be visible."""
        from hypergraph.viz import extract_debug_data

        graph = make_generation_graph()
        data = extract_debug_data(graph, depth=0)

        node_ids = {n["id"] for n in data.nodes}

        # system_instructions is only consumed inside prompt_building
        assert "input_system_instructions" not in node_ids, "input_system_instructions should be hidden when prompt_building is collapsed."
        # query has external consumers; it should remain visible at root
        assert "input_query" in node_ids, "input_query should stay visible at root."

    def test_input_group_hidden_when_collapsed(self):
        """INPUT_GROUP owned by a collapsed container should not be visible."""
        from hypergraph.viz import extract_debug_data

        graph = make_input_group_container_graph()
        data = extract_debug_data(graph, depth=0)

        node_ids = {n["id"] for n in data.nodes}
        assert "input_group_alpha_beta" not in node_ids, "input_group_alpha_beta should be hidden when inner is collapsed."


# =============================================================================
# Test: Stable INPUT_GROUP edges across expansion states
# =============================================================================


class TestInputGroupEdgesAcrossExpansion:
    """INPUT_GROUP edge sources should match visible group IDs when expanded."""

    def test_input_group_edges_exist_when_expanded(self):
        """Expanded edge set should NOT reference INPUT_GROUP node; individual inputs take over."""
        from hypergraph.viz.renderer import render_graph

        graph = make_input_group_container_graph()
        flat_graph = graph.to_flat_graph()
        result = render_graph(flat_graph, depth=0)

        group_node = next(n for n in result["nodes"] if n["data"]["nodeType"] == "INPUT_GROUP")

        edges_by_state = result["meta"]["edgesByState"]
        expandable = result["meta"]["expandableNodes"]
        expanded_key = ",".join(f"{node_id}:1" for node_id in expandable) + "|sep:0"

        assert expanded_key in edges_by_state, f"Key {expanded_key} not found"
        edges = edges_by_state[expanded_key]

        group_edges = [e for e in edges if e["source"] == group_node["id"]]

        assert not group_edges, (
            f"Expected NO edges from INPUT_GROUP {group_node['id']} in expanded state.\nEdges: {[(e['source'], e['target']) for e in edges]}"
        )


# =============================================================================
# Test Graph: Container output visibility
# =============================================================================


@node(output_name="internal_only")
def produce_internal(seed: int) -> int:
    return seed + 1


@node(output_name="external")
def produce_external(internal_only: int) -> int:
    return internal_only * 2


@node(output_name="used")
def consume_external(external: int) -> int:
    return external


def make_container_output_graph() -> Graph:
    """Nested container exposing one external and one internal-only output."""
    inner = Graph(nodes=[produce_internal, produce_external], name="inner")
    return Graph(nodes=[inner.as_node(), consume_external], name="outer")


# =============================================================================
# Test: Container outputs only shown when externally consumed
# =============================================================================


class TestContainerOutputVisibility:
    """Container outputs without external consumers should be hidden."""

    def test_internal_container_output_hidden_in_merged_mode(self):
        """Merged outputs should omit internal-only container outputs."""
        from hypergraph.viz.renderer import render_graph

        graph = make_container_output_graph()
        result = render_graph(graph.to_flat_graph(), depth=0, separate_outputs=False)

        container_node = next(n for n in result["nodes"] if n["id"] == "inner")
        output_names = {o["name"] for o in container_node["data"].get("outputs", [])}

        assert "external" in output_names
        assert "internal_only" not in output_names

    def test_internal_container_output_hidden_in_separate_mode(self):
        """Separate outputs should omit DATA nodes for internal-only container outputs."""
        from hypergraph.viz.renderer import render_graph

        graph = make_container_output_graph()
        result = render_graph(graph.to_flat_graph(), depth=0, separate_outputs=True)

        data_node_ids = {n["id"] for n in result["nodes"] if n["data"]["nodeType"] == "DATA"}

        assert "data_inner_external" in data_node_ids
        assert "data_inner_internal_only" not in data_node_ids
