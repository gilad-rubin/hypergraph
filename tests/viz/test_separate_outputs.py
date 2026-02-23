"""Tests for separate outputs visualization mode."""

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
        sep_keys = [k for k in edges_by_state if "sep:1" in k]

        # Should have at least one key with sep:1
        assert len(sep_keys) > 0, f"No edge state keys with 'sep:1' found!\nAvailable keys: {list(edges_by_state.keys())}"

        for state_key in sep_keys:
            edges = edges_by_state[state_key]
            edges_to_data = [e for e in edges if e["target"] in data_node_ids]
            assert len(edges_to_data) > 0, (
                f"State '{state_key}' has no edges to DATA nodes!\nDATA nodes: {data_node_ids}\nEdges: {[(e['source'], e['target']) for e in edges]}"
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


class TestDeeplyNestedSeparateOutputs:
    """Test separate outputs mode with deeply nested graphs."""

    def test_deeply_nested_edges_route_through_data_nodes(self):
        """Edges should route through DATA nodes, not direct function→function."""

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

        # Render with all containers expanded and separate outputs
        result = render_graph(outer.to_flat_graph(), depth=2, separate_outputs=True)

        # Get the sep:1 edges for fully expanded state
        edges_by_state = result["meta"].get("edgesByState", {})

        # Find the key with all containers expanded (middle:1, inner:1) and sep:1
        expanded_sep1_keys = [k for k in edges_by_state if "sep:1" in k and "middle:1" in k and "inner:1" in k]

        assert len(expanded_sep1_keys) > 0, f"No fully expanded sep:1 key found. Keys: {list(edges_by_state.keys())}"

        edges = edges_by_state[expanded_sep1_keys[0]]

        # Find edge to middle/validate - should come from DATA node, not function (hierarchical IDs)
        edges_to_validate = [e for e in edges if e["target"] == "middle/validate"]

        assert len(edges_to_validate) > 0, f"No edges to 'middle/validate' found!\nAll edges: {[(e['source'], e['target']) for e in edges]}"

        # The source should be a DATA node, not a function
        for edge in edges_to_validate:
            source = edge["source"]
            assert source.startswith("data_"), (
                f"Edge to 'middle/validate' should come from DATA node, not function!\n"
                f"Got source: {source}\n"
                f"Expected: data_middle/inner/step2_step2_out or similar DATA node"
            )

    def test_deeply_nested_collapsed_then_expanded_edges(self):
        """Test edges when rendered at depth=0 then containers expanded interactively."""

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

        # Render at depth=0 (collapsed) with separate outputs
        # This is how the user sees it initially
        result = render_graph(outer.to_flat_graph(), depth=0, separate_outputs=True)

        edges_by_state = result["meta"].get("edgesByState", {})

        # Check the key for when middle AND inner are both expanded (user expands interactively)
        # Key format: "inner:1,middle:1|sep:1"
        expanded_sep1_keys = [k for k in edges_by_state if "sep:1" in k and "middle:1" in k and "inner:1" in k]

        assert len(expanded_sep1_keys) > 0, f"No fully expanded sep:1 key found. Keys: {list(edges_by_state.keys())}"

        edges = edges_by_state[expanded_sep1_keys[0]]

        # Find edge to middle/validate - should come from DATA node (hierarchical IDs)
        edges_to_validate = [e for e in edges if e["target"] == "middle/validate"]

        assert len(edges_to_validate) > 0, f"No edges to 'middle/validate' found!\nAll edges: {[(e['source'], e['target']) for e in edges]}"

        for edge in edges_to_validate:
            source = edge["source"]
            assert source.startswith("data_"), (
                f"Edge to 'middle/validate' should come from DATA node!\nGot source: {source}\nExpected: data_middle/inner/step2_step2_out"
            )

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

        result = render_graph(outer.to_flat_graph(), depth=2, separate_outputs=True)
        edges_by_state = result["meta"].get("edgesByState", {})

        # Check all sep:1 keys
        sep1_keys = [k for k in edges_by_state if "sep:1" in k]

        # Get function node IDs (not DATA, not INPUT)
        function_ids = {n["id"] for n in result["nodes"] if n["data"].get("nodeType") in ("FUNCTION", "PIPELINE")}

        for key in sep1_keys:
            edges = edges_by_state[key]
            for edge in edges:
                edge_type = edge.get("data", {}).get("edgeType", "")
                # Data edges should not go direct function→function
                if edge_type == "data":
                    is_source_function = edge["source"] in function_ids
                    is_target_function = edge["target"] in function_ids
                    assert not (is_source_function and is_target_function), (
                        f"Data edge goes direct function→function in sep:1 mode!\n"
                        f"Key: {key}\n"
                        f"Edge: {edge['source']} → {edge['target']}\n"
                        f"Data edges should route through DATA nodes"
                    )


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
        assert len(sep0_keys) == len(sep1_keys), f"Mismatch in sep:0 vs sep:1 keys.\nsep:0: {sep0_keys}\nsep:1: {sep1_keys}"

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
