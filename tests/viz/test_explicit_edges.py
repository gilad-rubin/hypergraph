"""Tests for explicit edge routing (new architecture).

These tests define the EXPECTED behavior after the refactor:
- Python generates explicit edges to actual nodes
- No `__inputs__` artificial node - individual input nodes instead
- Edges connect directly to consumers/producers, not containers

These tests will FAIL until the refactor is complete.
"""

import pytest
from hypergraph import Graph, node


# =============================================================================
# Test Graph Definitions
# =============================================================================

# --- 1-level nesting: workflow ---
@node(output_name="cleaned")
def clean_text(text: str) -> str:
    return text.strip()


@node(output_name="normalized")
def normalize_text(cleaned: str) -> str:
    return cleaned.lower()


@node(output_name="result")
def analyze(normalized: str) -> dict:
    return {"length": len(normalized)}


def make_workflow():
    """Create 1-level nested graph: preprocess -> analyze."""
    preprocess = Graph(nodes=[clean_text, normalize_text], name="preprocess")
    return Graph(nodes=[preprocess.as_node(), analyze])


# --- 2-level nesting: outer ---
@node(output_name="step1_out")
def step1(x: int) -> int:
    return x + 1


@node(output_name="step2_out")
def step2(step1_out: int) -> int:
    return step1_out * 2


@node(output_name="validated")
def validate(step2_out: int) -> int:
    return step2_out


@node(output_name="logged")
def log_result(validated: int) -> int:
    return validated


def make_outer():
    """Create 2-level nested graph: middle -> log_result."""
    inner = Graph(nodes=[step1, step2], name="inner")
    middle = Graph(nodes=[inner.as_node(), validate], name="middle")
    return Graph(nodes=[middle.as_node(), log_result])


# =============================================================================
# Helper to extract edge pairs from instructions
# =============================================================================

def get_edge_pairs(graph, depth: int) -> set[tuple[str, str]]:
    """Extract (source, target) pairs from visualization instructions."""
    from hypergraph.viz.renderer.instructions import build_instructions

    flat_graph = graph.to_flat_graph()
    instructions = build_instructions(flat_graph, depth=depth)
    return {(e.source, e.target) for e in instructions.edges}


def get_node_ids(graph, depth: int) -> set[str]:
    """Extract node IDs from visualization instructions."""
    from hypergraph.viz.renderer.instructions import build_instructions

    flat_graph = graph.to_flat_graph()
    instructions = build_instructions(flat_graph, depth=depth)
    return {n.id for n in instructions.nodes}


# =============================================================================
# Tests for explicit edge routing
# =============================================================================

class TestWorkflowExplicitEdges:
    """Test that workflow graph has explicit edges at each depth."""

    def test_depth0_edges_to_container(self):
        """At depth=0, edges connect to collapsed preprocess container.

        Expected edges:
        - input_text → preprocess (collapsed container)
        - preprocess → analyze
        """
        workflow = make_workflow()
        edges = get_edge_pairs(workflow, depth=0)

        # Should have edge TO preprocess (container)
        has_edge_to_preprocess = any(tgt == "preprocess" for _, tgt in edges)
        assert has_edge_to_preprocess, (
            f"Expected edge to 'preprocess' container at depth=0.\n"
            f"Actual edges: {edges}"
        )

        # Should have edge FROM preprocess to analyze
        assert ("preprocess", "analyze") in edges or any(
            src == "preprocess" and "analyze" in tgt for src, tgt in edges
        ), f"Expected edge preprocess→analyze at depth=0.\nActual edges: {edges}"

    def test_depth1_edges_to_internal_nodes(self):
        """At depth=1, edges connect to actual internal nodes.

        Expected edges:
        - input_text → clean_text (actual consumer, not preprocess)
        - clean_text → normalize_text (internal edge)
        - normalize_text → analyze (actual producer, not preprocess)
        """
        workflow = make_workflow()
        edges = get_edge_pairs(workflow, depth=1)

        # Should NOT have edge to preprocess container
        edges_to_preprocess = [e for e in edges if e[1] == "preprocess"]
        assert len(edges_to_preprocess) == 0, (
            f"At depth=1, edges should go to internal nodes, not container.\n"
            f"Found edges TO preprocess: {edges_to_preprocess}\n"
            f"All edges: {edges}"
        )

        # Should have edge to clean_text (actual consumer)
        has_edge_to_clean_text = any("clean_text" in tgt for _, tgt in edges)
        assert has_edge_to_clean_text, (
            f"Expected edge to 'clean_text' at depth=1.\n"
            f"Actual edges: {edges}"
        )

    def test_depth1_no_inputs_artificial_node(self):
        """At depth=1, should not have __inputs__ artificial node.

        Instead, should have individual input nodes like 'input_text'.
        """
        workflow = make_workflow()
        nodes = get_node_ids(workflow, depth=1)

        # Should NOT have __inputs__ artificial node
        assert "__inputs__" not in nodes, (
            f"Should not have '__inputs__' artificial node.\n"
            f"Found nodes: {nodes}"
        )

        # Should have individual input node
        has_input_node = any("input" in n.lower() and "text" in n.lower() for n in nodes)
        assert has_input_node, (
            f"Expected individual input node like 'input_text'.\n"
            f"Found nodes: {nodes}"
        )


class TestOuterExplicitEdges:
    """Test that outer (2-level nested) graph has explicit edges at each depth."""

    def test_depth0_edges_to_container(self):
        """At depth=0, edges connect to collapsed middle container.

        Expected edges:
        - input_x → middle (collapsed container)
        - middle → log_result
        """
        outer = make_outer()
        edges = get_edge_pairs(outer, depth=0)

        # Should have edge TO middle (container)
        has_edge_to_middle = any(tgt == "middle" for _, tgt in edges)
        assert has_edge_to_middle, (
            f"Expected edge to 'middle' container at depth=0.\n"
            f"Actual edges: {edges}"
        )

        # Should have edge FROM middle to log_result
        has_edge_from_middle = any(
            src == "middle" and "log_result" in tgt for src, tgt in edges
        )
        assert has_edge_from_middle, (
            f"Expected edge middle→log_result at depth=0.\n"
            f"Actual edges: {edges}"
        )

    def test_depth1_edges_to_inner_container(self):
        """At depth=1, edges connect to inner container (not middle boundary).

        Expected edges:
        - input_x → inner (inner container, not middle boundary)
        - inner → validate (internal to middle)
        - validate → log_result
        """
        outer = make_outer()
        edges = get_edge_pairs(outer, depth=1)

        # Should have edge TO inner (not to middle boundary)
        has_edge_to_inner = any("inner" in tgt for _, tgt in edges)
        assert has_edge_to_inner, (
            f"Expected edge to 'inner' container at depth=1.\n"
            f"Actual edges: {edges}"
        )

        # Input edge should NOT go to middle (the outer container)
        # It should go to inner (the first visible child)
        input_edges = [(s, t) for s, t in edges if "input" in s.lower()]
        for src, tgt in input_edges:
            assert tgt != "middle", (
                f"Input edge should go to 'inner', not 'middle' at depth=1.\n"
                f"Found: {src} → {tgt}\n"
                f"All edges: {edges}"
            )

    def test_depth2_edges_to_actual_nodes(self):
        """At depth=2, edges connect to actual internal nodes.

        Expected edges:
        - input_x → step1 (actual consumer)
        - step1 → step2 (internal to inner)
        - step2 → validate (cross-boundary)
        - validate → log_result
        """
        outer = make_outer()
        edges = get_edge_pairs(outer, depth=2)

        # Should have edge directly to step1
        has_edge_to_step1 = any("step1" in tgt for _, tgt in edges)
        assert has_edge_to_step1, (
            f"Expected edge to 'step1' at depth=2.\n"
            f"Actual edges: {edges}"
        )

        # Should have internal edge step1 → step2
        has_step1_to_step2 = any(
            "step1" in src and "step2" in tgt for src, tgt in edges
        )
        assert has_step1_to_step2, (
            f"Expected edge step1→step2 at depth=2.\n"
            f"Actual edges: {edges}"
        )

        # Input edge should NOT go to container
        input_edges = [(s, t) for s, t in edges if "input" in s.lower()]
        for src, tgt in input_edges:
            assert tgt not in ("middle", "inner"), (
                f"Input edge should go to 'step1', not container at depth=2.\n"
                f"Found: {src} → {tgt}\n"
                f"All edges: {edges}"
            )

    def test_depth2_no_inputs_artificial_node(self):
        """At depth=2, should not have __inputs__ artificial node."""
        outer = make_outer()
        nodes = get_node_ids(outer, depth=2)

        assert "__inputs__" not in nodes, (
            f"Should not have '__inputs__' artificial node.\n"
            f"Found nodes: {nodes}"
        )


class TestIndividualInputNodes:
    """Test that individual input nodes are created instead of INPUT_GROUP."""

    def test_workflow_has_individual_input(self):
        """Workflow should have input_text node, not __inputs__."""
        workflow = make_workflow()
        nodes = get_node_ids(workflow, depth=1)

        # Should have individual input node
        input_nodes = [n for n in nodes if n.startswith("input_")]
        assert len(input_nodes) >= 1, (
            f"Expected at least one 'input_*' node.\n"
            f"Found nodes: {nodes}"
        )

        # Should NOT have __inputs__
        assert "__inputs__" not in nodes

    def test_outer_has_individual_input(self):
        """Outer should have input_x node, not __inputs__."""
        outer = make_outer()
        nodes = get_node_ids(outer, depth=2)

        # Should have individual input node for 'x'
        has_input_x = any("input" in n.lower() and "x" in n.lower() for n in nodes)
        assert has_input_x, (
            f"Expected 'input_x' node.\n"
            f"Found nodes: {nodes}"
        )

        # Should NOT have __inputs__
        assert "__inputs__" not in nodes


# =============================================================================
# Parametrized tests for edge expectations
# =============================================================================

class TestEdgeExpectations:
    """Parametrized tests matching the design document expectations."""

    @pytest.mark.parametrize("depth,must_have_target", [
        (0, "preprocess"),  # depth=0: edges to container
        (1, "clean_text"),  # depth=1: edges to internal node
    ])
    def test_workflow_input_edge_target(self, depth, must_have_target):
        """Test that workflow input edge goes to expected target."""
        workflow = make_workflow()
        edges = get_edge_pairs(workflow, depth=depth)

        # Find input edge
        input_edges = [(s, t) for s, t in edges if "input" in s.lower()]
        assert len(input_edges) > 0, f"No input edge found. Edges: {edges}"

        # Check target
        targets = [t for _, t in input_edges]
        has_expected_target = any(must_have_target in t for t in targets)
        assert has_expected_target, (
            f"At depth={depth}, input edge should target '{must_have_target}'.\n"
            f"Actual targets: {targets}\n"
            f"All edges: {edges}"
        )

    @pytest.mark.parametrize("depth,must_have_target", [
        (0, "middle"),   # depth=0: edges to outer container
        (1, "inner"),    # depth=1: edges to inner container
        (2, "step1"),    # depth=2: edges to actual node
    ])
    def test_outer_input_edge_target(self, depth, must_have_target):
        """Test that outer input edge goes to expected target."""
        outer = make_outer()
        edges = get_edge_pairs(outer, depth=depth)

        # Find input edge
        input_edges = [(s, t) for s, t in edges if "input" in s.lower()]
        assert len(input_edges) > 0, f"No input edge found at depth={depth}. Edges: {edges}"

        # Check target
        targets = [t for _, t in input_edges]
        has_expected_target = any(must_have_target in t for t in targets)
        assert has_expected_target, (
            f"At depth={depth}, input edge should target '{must_have_target}'.\n"
            f"Actual targets: {targets}\n"
            f"All edges: {edges}"
        )
