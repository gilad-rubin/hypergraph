"""Tests for separate outputs visualization mode."""

import pytest

from hypergraph import END, Graph, ifelse, node, route
from hypergraph.viz.renderer import render_graph
from tests.viz.conftest import HAS_PLAYWRIGHT, scene_for_state


@node(output_name="cleaned")
def clean_text(text: str) -> str:
    return text.strip()


@node(output_name="normalized")
def normalize(cleaned: str) -> str:
    return cleaned.lower()


@node(output_name="result")
def analyze(normalized: str) -> dict:
    return {"length": len(normalized)}


class TestSeparateOutputsEdges:
    """Test that edges exist in separate outputs mode."""

    def test_output_edges_exist_in_base_edges(self):
        """Base edges should include function->DATA edges for separate outputs mode."""
        preprocess = Graph(nodes=[clean_text, normalize], name="preprocess")
        workflow = Graph(nodes=[preprocess.as_node(), analyze])

        result = render_graph(workflow.to_flat_graph(), depth=0, separate_outputs=True)
        edges = result["edges"]

        # Find DATA nodes
        data_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "DATA"]
        data_node_ids = {n["id"] for n in data_nodes}

        # Edges TO DATA nodes should exist (function -> DATA)
        edges_to_data = [e for e in edges if e["target"] in data_node_ids]
        assert len(edges_to_data) > 0, (
            f"No edges to DATA nodes found!\nDATA nodes: {data_node_ids}\nAll edges: {[(e['source'], e['target']) for e in edges]}"
        )

    def test_data_edges_exist_from_data_nodes(self):
        """Edges FROM DATA nodes to consumers should exist."""
        preprocess = Graph(nodes=[clean_text, normalize], name="preprocess")
        workflow = Graph(nodes=[preprocess.as_node(), analyze])

        result = render_graph(workflow.to_flat_graph(), depth=1, separate_outputs=True)
        edges = result["edges"]

        # Find DATA nodes
        data_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "DATA"]
        data_node_ids = {n["id"] for n in data_nodes}

        # At depth=1, internal nodes are visible, so edges FROM DATA nodes should exist
        edges_from_data = [e for e in edges if e["source"] in data_node_ids]
        assert len(edges_from_data) > 0, (
            f"No edges from DATA nodes found!\nDATA nodes: {data_node_ids}\nAll edges: {[(e['source'], e['target']) for e in edges]}"
        )

    def test_separate_mode_emits_edges_to_data_nodes_in_every_state(self):
        """Every separate-outputs scene should include edges into DATA nodes."""
        preprocess = Graph(nodes=[clean_text, normalize], name="preprocess")
        workflow = Graph(nodes=[preprocess.as_node(), analyze])

        for expand_all in (False, True):
            scene = scene_for_state(workflow, expand_all=expand_all, separate_outputs=True)
            data_node_ids = {n["id"] for n in scene["nodes"] if n["data"]["nodeType"] == "DATA"}
            edges_to_data = [e for e in scene["edges"] if e["target"] in data_node_ids]
            assert edges_to_data, (
                f"No edges to DATA nodes (expand_all={expand_all}). "
                f"DATA: {data_node_ids}, Edges: {[(e['source'], e['target']) for e in scene['edges']]}"
            )


class TestSeparateOutputsNodeVisibility:
    """Test that DATA nodes are visible in separate outputs mode."""

    def test_data_nodes_created(self):
        """DATA nodes should be created for all outputs."""
        preprocess = Graph(nodes=[clean_text, normalize], name="preprocess")
        workflow = Graph(nodes=[preprocess.as_node(), analyze])

        result = render_graph(workflow.to_flat_graph(), depth=0, separate_outputs=True)

        data_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "DATA"]
        data_labels = {n["data"]["label"] for n in data_nodes}

        # All outputs should have DATA nodes
        assert "cleaned" in data_labels
        assert "normalized" in data_labels
        assert "result" in data_labels

    def test_internal_gate_output_not_rendered_as_data_node(self):
        """Gate internal output (_{gate_name}) should not appear as a DATA node."""

        @node(output_name="value")
        def source(seed: int) -> int:
            return seed

        @ifelse(when_true="accept", when_false="reject")
        def gate_decision(value: int) -> bool:
            return value > 0

        @node(output_name="accepted")
        def accept(value: int) -> int:
            return value

        @node(output_name="rejected")
        def reject(value: int) -> int:
            return value

        graph = Graph(nodes=[source, gate_decision, accept, reject])
        result = render_graph(graph.to_flat_graph(), depth=0, separate_outputs=True)

        data_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "DATA"]
        data_labels = {n["data"]["label"] for n in data_nodes}
        data_ids = {n["id"] for n in data_nodes}

        assert "_gate_decision" not in data_labels
        assert "data_gate_decision__gate_decision" not in data_ids
        assert "value" in data_labels

    def test_gate_emit_output_still_rendered(self):
        """Filtering should remove only internal gate output, not gate emit outputs."""

        @node(output_name="value")
        def source(seed: int) -> int:
            return seed

        @ifelse(when_true="accept", when_false="reject", emit="decision_made")
        def gate_decision(value: int) -> bool:
            return value > 0

        @node(output_name="accepted")
        def accept(value: int) -> int:
            return value

        @node(output_name="rejected")
        def reject(value: int) -> int:
            return value

        graph = Graph(nodes=[source, gate_decision, accept, reject])
        result = render_graph(graph.to_flat_graph(), depth=0, separate_outputs=True)

        data_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "DATA"]
        data_labels = {n["data"]["label"] for n in data_nodes}

        assert "_gate_decision" not in data_labels
        assert "decision_made" in data_labels

    def test_route_internal_output_not_rendered_as_data_node(self):
        """Route internal output (_{route_name}) should not appear as DATA."""

        @node(output_name="value")
        def source(seed: int) -> int:
            return seed

        @route(targets=["accept", END], emit="route_done")
        def choose(value: int):
            return "accept" if value > 0 else END

        @node(output_name="accepted")
        def accept(value: int) -> int:
            return value

        graph = Graph(nodes=[source, choose, accept])
        result = render_graph(graph.to_flat_graph(), depth=0, separate_outputs=True)

        data_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "DATA"]
        data_labels = {n["data"]["label"] for n in data_nodes}

        assert "_choose" not in data_labels
        assert "route_done" in data_labels

    def test_nested_route_internal_output_not_rendered_as_data_node(self):
        """Nested route should not leak local internal output in separate mode."""

        @node(output_name="value")
        def source(seed: int) -> int:
            return seed

        @route(targets=["accept", END], emit="route_done")
        def choose(value: int):
            return "accept" if value > 0 else END

        @node(output_name="accepted")
        def accept(value: int) -> int:
            return value

        inner = Graph(nodes=[source, choose, accept], name="inner")
        outer = Graph(nodes=[inner.as_node(name="inner_node")])

        result = render_graph(outer.to_flat_graph(), depth=2, separate_outputs=True)
        data_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "DATA"]
        data_labels = {n["data"]["label"] for n in data_nodes}

        assert "_choose" not in data_labels
        assert "route_done" in data_labels


class TestDeeplyNestedSeparateOutputs:
    """Test separate outputs mode with deeply nested graphs."""

    def test_deeply_nested_edges_route_through_data_nodes(self):
        """Edges should route through DATA nodes, not direct function→function."""

        @node(output_name="step1_out")
        def step1(x: int) -> int:
            return x + 1

        @node(output_name="step2_out")
        def step2(step1_out: int) -> int:
            return step1_out * 2

        inner = Graph(nodes=[step1, step2], name="inner")

        @node(output_name="validated")
        def validate(step2_out: int) -> int:
            return step2_out

        middle = Graph(nodes=[inner.as_node(), validate], name="middle")

        @node(output_name="logged")
        def log_result(validated: int) -> int:
            return validated

        outer = Graph(nodes=[middle.as_node(), log_result])

        scene = scene_for_state(outer, expand_all=True, separate_outputs=True)
        edges_to_validate = [e for e in scene["edges"] if e["target"] == "middle/validate" and not e.get("hidden")]

        assert edges_to_validate, f"No edges to 'middle/validate' found!\nAll edges: {[(e['source'], e['target']) for e in scene['edges']]}"
        for edge in edges_to_validate:
            assert edge["source"].startswith("data_"), f"Edge to 'middle/validate' should come from a DATA node; got {edge['source']}"

    def test_deeply_nested_collapsed_then_expanded_edges(self):
        """Same routing should hold when starting collapsed and expanding interactively."""

        @node(output_name="step1_out")
        def step1(x: int) -> int:
            return x + 1

        @node(output_name="step2_out")
        def step2(step1_out: int) -> int:
            return step1_out * 2

        inner = Graph(nodes=[step1, step2], name="inner")

        @node(output_name="validated")
        def validate(step2_out: int) -> int:
            return step2_out

        middle = Graph(nodes=[inner.as_node(), validate], name="middle")

        @node(output_name="logged")
        def log_result(validated: int) -> int:
            return validated

        outer = Graph(nodes=[middle.as_node(), log_result])

        # Start collapsed — IR is the same. Expanding interactively must
        # produce the same DATA-node-routed edge set as the all-expanded
        # render.
        scene = scene_for_state(
            outer,
            expansion_state={"middle": True, "middle/inner": True},
            separate_outputs=True,
        )
        edges_to_validate = [e for e in scene["edges"] if e["target"] == "middle/validate" and not e.get("hidden")]
        assert edges_to_validate
        for edge in edges_to_validate:
            assert edge["source"].startswith("data_"), f"Edge to 'middle/validate' should come from a DATA node; got {edge['source']}"

    def test_deeply_nested_no_function_to_function_data_edges(self):
        """In separate outputs mode, data edges should never go direct function→function."""

        # Level 1
        @node(output_name="step1_out")
        def step1(x: int) -> int:
            return x + 1

        @node(output_name="step2_out")
        def step2(step1_out: int) -> int:
            return step1_out * 2

        inner = Graph(nodes=[step1, step2], name="inner")

        # Level 2
        @node(output_name="validated")
        def validate(step2_out: int) -> int:
            return step2_out

        middle = Graph(nodes=[inner.as_node(), validate], name="middle")

        # Level 3
        @node(output_name="logged")
        def log_result(validated: int) -> int:
            return validated

        outer = Graph(nodes=[middle.as_node(), log_result])

        # Sweep every expansion state for the 3-level fixture: in
        # separate_outputs mode no visible data edge should connect two
        # function/container nodes directly — they must route through a
        # DATA node.
        from itertools import product

        expandables = ["middle", "middle/inner"]
        for bits in product([False, True], repeat=len(expandables)):
            state = dict(zip(expandables, bits, strict=True))
            scene = scene_for_state(outer, expansion_state=state, separate_outputs=True)
            function_ids = {n["id"] for n in scene["nodes"] if n["data"].get("nodeType") in ("FUNCTION", "PIPELINE")}
            for edge in scene["edges"]:
                if edge.get("hidden"):
                    continue
                if edge.get("data", {}).get("edgeType") != "data":
                    continue
                assert not (edge["source"] in function_ids and edge["target"] in function_ids), (
                    f"Direct function→function data edge in separate_outputs mode!\nExpansion: {state}\nEdge: {edge['source']} → {edge['target']}"
                )


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestSeparateOutputsLayout:
    """Test that layout positions sources above targets in separate outputs mode."""

    def test_simple_graph_sources_above_targets(self):
        """In a simple graph with separate outputs, sources should be above targets."""
        from hypergraph.viz import extract_debug_data

        simple = Graph(nodes=[clean_text, normalize, analyze])
        data = extract_debug_data(simple, depth=0, separate_outputs=True)

        # All edges should have positive vertical distance (source above target)
        for edge in data.edges:
            if edge.vert_dist is not None:
                assert edge.vert_dist >= 0, (
                    f"Edge {edge.source} -> {edge.target} has target above source!\n"
                    f"srcBottom={edge.src_bottom}, tgtTop={edge.tgt_top}, vDist={edge.vert_dist}"
                )

    def test_nested_graph_sources_above_targets(self):
        """In a nested graph with separate outputs, sources should be above targets."""
        from hypergraph.viz import extract_debug_data

        preprocess = Graph(nodes=[clean_text, normalize], name="preprocess")
        workflow = Graph(nodes=[preprocess.as_node(), analyze])

        # Test at depth=1 (expanded)
        data = extract_debug_data(workflow, depth=1, separate_outputs=True)

        for edge in data.edges:
            if edge.vert_dist is not None:
                assert edge.vert_dist >= 0, (
                    f"Edge {edge.source} -> {edge.target} has target above source!\n"
                    f"srcBottom={edge.src_bottom}, tgtTop={edge.tgt_top}, vDist={edge.vert_dist}"
                )

    def test_deeply_nested_sources_above_targets(self):
        """In deeply nested graph with separate outputs, sources should be above targets."""
        from hypergraph.viz import extract_debug_data

        # Level 1: Simple transform
        @node(output_name="step1_out")
        def step1(x: int) -> int:
            return x + 1

        @node(output_name="step2_out")
        def step2(step1_out: int) -> int:
            return step1_out * 2

        inner = Graph(nodes=[step1, step2], name="inner")

        # Level 2: Wrap inner + add validation
        @node(output_name="validated")
        def validate(step2_out: int) -> int:
            return step2_out

        middle = Graph(nodes=[inner.as_node(), validate], name="middle")

        # Level 3: Wrap middle + add logging
        @node(output_name="logged")
        def log_result(validated: int) -> int:
            return validated

        outer = Graph(nodes=[middle.as_node(), log_result])

        # Test at depth=2 (all expanded)
        data = extract_debug_data(outer, depth=2, separate_outputs=True)

        # Collect any edges where target is above source
        issues = [edge for edge in data.edges if edge.vert_dist is not None and edge.vert_dist < 0]

        assert len(issues) == 0, f"Found {len(issues)} edges with target above source:\n" + "\n".join(
            f"  {e.source} -> {e.target}: vDist={e.vert_dist}" for e in issues
        )

    def test_no_edge_issues_in_separate_outputs_mode(self):
        """No edge issues should exist in separate outputs mode."""
        from hypergraph.viz import extract_debug_data

        preprocess = Graph(nodes=[clean_text, normalize], name="preprocess")
        workflow = Graph(nodes=[preprocess.as_node(), analyze])

        data = extract_debug_data(workflow, depth=1, separate_outputs=True)

        assert data.summary["edgeIssues"] == 0, f"Found {data.summary['edgeIssues']} edge issues:\n" + "\n".join(
            f"  {e.source} -> {e.target}: {e.issue}" for e in data.edge_issues
        )
