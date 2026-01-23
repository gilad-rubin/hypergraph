"""Tests for separate outputs visualization mode."""

import pytest
from hypergraph import Graph, node
from hypergraph.viz.renderer import render_graph


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
            f"No edges to DATA nodes found!\n"
            f"DATA nodes: {data_node_ids}\n"
            f"All edges: {[(e['source'], e['target']) for e in edges]}"
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
            f"No edges from DATA nodes found!\n"
            f"DATA nodes: {data_node_ids}\n"
            f"All edges: {[(e['source'], e['target']) for e in edges]}"
        )

    def test_precomputed_edges_include_output_edges_when_separate(self):
        """Pre-computed edges should include output edges for separate outputs mode."""
        preprocess = Graph(nodes=[clean_text, normalize], name="preprocess")
        workflow = Graph(nodes=[preprocess.as_node(), analyze])

        result = render_graph(workflow.to_flat_graph(), depth=0, separate_outputs=True)

        # Pre-computed edges are in meta.edgesByState
        edges_by_state = result["meta"].get("edgesByState", {})

        # Find DATA nodes
        data_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "DATA"]
        data_node_ids = {n["id"] for n in data_nodes}

        # Look for keys with sep:1 (separate outputs mode)
        sep_keys = [k for k in edges_by_state.keys() if "sep:1" in k]

        # Should have at least one key with sep:1
        assert len(sep_keys) > 0, (
            f"No edge state keys with 'sep:1' found!\n"
            f"Available keys: {list(edges_by_state.keys())}"
        )

        for state_key in sep_keys:
            edges = edges_by_state[state_key]
            edges_to_data = [e for e in edges if e["target"] in data_node_ids]
            assert len(edges_to_data) > 0, (
                f"State '{state_key}' has no edges to DATA nodes!\n"
                f"DATA nodes: {data_node_ids}\n"
                f"Edges: {[(e['source'], e['target']) for e in edges]}"
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


class TestSeparateOutputsEdgeKeys:
    """Test that edge state keys properly encode separateOutputs flag."""

    def test_edge_state_keys_include_sep_flag(self):
        """Edge state keys should include sep:0 and sep:1 variants."""
        preprocess = Graph(nodes=[clean_text, normalize], name="preprocess")
        workflow = Graph(nodes=[preprocess.as_node(), analyze])

        result = render_graph(workflow.to_flat_graph(), depth=0, separate_outputs=True)
        edges_by_state = result["meta"].get("edgesByState", {})

        keys = list(edges_by_state.keys())

        # Should have both sep:0 and sep:1 variants for each expansion state
        sep0_keys = [k for k in keys if "sep:0" in k]
        sep1_keys = [k for k in keys if "sep:1" in k]

        assert len(sep0_keys) > 0, f"No sep:0 keys found. Keys: {keys}"
        assert len(sep1_keys) > 0, f"No sep:1 keys found. Keys: {keys}"
        assert len(sep0_keys) == len(sep1_keys), (
            f"Mismatch in sep:0 vs sep:1 keys.\n"
            f"sep:0: {sep0_keys}\n"
            f"sep:1: {sep1_keys}"
        )

    def test_empty_graph_edge_key_format(self):
        """For graphs with no containers, keys should be just 'sep:0' or 'sep:1'."""
        # Simple graph with no containers
        simple = Graph(nodes=[clean_text, normalize, analyze])

        result = render_graph(simple.to_flat_graph(), depth=0, separate_outputs=True)
        edges_by_state = result["meta"].get("edgesByState", {})

        keys = list(edges_by_state.keys())

        # Should have "sep:0" and "sep:1" as the only keys (no expansion part)
        assert "sep:0" in keys, f"'sep:0' not found. Keys: {keys}"
        assert "sep:1" in keys, f"'sep:1' not found. Keys: {keys}"
