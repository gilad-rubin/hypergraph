"""Tests for the visualization renderer."""

import itertools
import re

import pytest

from hypergraph import END, Graph, ifelse, interrupt, node, route
from hypergraph.viz import visualize
from hypergraph.viz._common import get_expandable_nodes
from hypergraph.viz.renderer import render_graph
from tests.viz.conftest import scene_for_state


def _enumerate_scenes(graph, *, show_inputs: bool):
    """Yield ``(state_key, visible_nodes, visible_edges)`` for every
    (expansion × separate_outputs) combination. Hidden nodes/edges are
    filtered out so callers see the visible-only contract that the legacy
    ``edgesByState`` precompute used to expose.
    """
    flat = graph.to_flat_graph()
    expandable = sorted(get_expandable_nodes(flat))
    ext_key = "ext:1" if show_inputs else "ext:0"
    for bits in itertools.product([False, True], repeat=len(expandable)):
        exp_state = dict(zip(expandable, bits, strict=True))
        for separate in (False, True):
            scene = scene_for_state(
                flat,
                expansion_state=exp_state,
                separate_outputs=separate,
                show_inputs=show_inputs,
            )
            visible_nodes = [n for n in scene["nodes"] if not n.get("hidden")]
            visible_edges = [e for e in scene["edges"] if not e.get("hidden")]
            sep_key = "sep:1" if separate else "sep:0"
            parts = [f"{n}:{1 if exp_state[n] else 0}" for n in expandable]
            key = (",".join(parts) + f"|{sep_key}|{ext_key}") if parts else f"{sep_key}|{ext_key}"
            yield key, visible_nodes, visible_edges


@node(output_name="doubled")
def double(x: int) -> int:
    """Double a number."""
    return x * 2


@node(output_name="result")
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@node(output_name="tripled")
def triple(x: int) -> int:
    """Triple a number."""
    return x * 3


@node(output_name="a_val")
def step_a(x: int) -> int:
    return x + 1


@node(output_name="b_val")
def step_b(a_val: int) -> int:
    return a_val + 1


@node(output_name="c_val")
def step_c(b_val: int) -> int:
    return b_val + 1


@node(output_name="result_val")
def step_d(b_val: int, c_val: int) -> int:
    return b_val + c_val


def _build_interrupt_cycle_graph() -> Graph:
    """Notebook demo graph: ask_user <-> llm with should_continue gate."""

    @interrupt(output_name="user_input")
    def ask_slack(messages: list[str], slack: object) -> None:
        return None

    @node(output_name="assistant_text")
    def llm_step(messages: list[str]) -> str:
        return "assistant draft"

    @node(output_name="messages")
    def add_user_message(messages: list[str], user_input: str) -> list[str]:
        return [*messages, f"user: {user_input}"]

    @node(output_name="messages")
    def add_assistant_message(messages: list[str], assistant_text: str) -> list[str]:
        return [*messages, f"assistant: {assistant_text}"]

    @route(targets=["ask_user", END])
    def should_continue(messages: list[str], max_turns: int) -> str:
        return "ask_user"

    ask_user_node = Graph(
        [ask_slack, add_user_message],
        edges=[(ask_slack, add_user_message)],
        name="ask_user",
        entrypoint="ask_slack",
    ).as_node()
    llm_node = Graph(
        [llm_step, add_assistant_message],
        edges=[(llm_step, add_assistant_message)],
        name="llm",
        entrypoint="llm_step",
    ).as_node()
    return Graph(
        [ask_user_node, llm_node, should_continue],
        edges=[
            (ask_user_node, llm_node),
            (llm_node, should_continue),
            (llm_node, ask_user_node),
        ],
        name="slack_cycle",
        entrypoint="ask_user",
    )


def _edge_signature(edge: dict) -> tuple[str, str, str]:
    data = edge.get("data", {})
    return (str(edge["source"]), str(edge["target"]), str(data.get("edgeType")))


class TestRenderGraph:
    """Tests for render_graph function."""

    def test_render_single_node(self):
        """Test rendering a graph with a single node."""
        graph = Graph(nodes=[double])
        result = render_graph(graph.to_flat_graph())

        assert "nodes" in result
        assert "edges" in result
        assert "meta" in result

        node_types = {n["data"]["nodeType"] for n in result["nodes"]}
        assert "FUNCTION" in node_types
        assert "INPUT" in node_types  # For external input 'x' (individual, not grouped)

        fn_node = next(n for n in result["nodes"] if n["data"]["nodeType"] == "FUNCTION")
        assert fn_node["id"] == "double"
        assert fn_node["data"]["label"] == "double"

    def test_render_node_outputs(self):
        """In separate-outputs mode each function output becomes a DATA node."""
        graph = Graph(nodes=[double])
        result = render_graph(graph.to_flat_graph(), separate_outputs=True)

        data_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "DATA"]
        assert len(data_nodes) == 1
        assert data_nodes[0]["data"]["label"] == "doubled"
        assert data_nodes[0]["data"]["typeHint"] == "int"
        assert data_nodes[0]["data"]["sourceId"] == "double"

    def test_merged_mode_omits_data_nodes(self):
        """Merged mode (default) does not materialize DATA scene nodes."""
        graph = Graph(nodes=[double])
        result = render_graph(graph.to_flat_graph())

        data_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "DATA"]
        assert data_nodes == []

    def test_render_node_inputs(self):
        """Test that node inputs are captured correctly."""
        graph = Graph(nodes=[add])
        result = render_graph(graph.to_flat_graph())

        fn_node = next(n for n in result["nodes"] if n["data"]["nodeType"] == "FUNCTION")
        inputs = fn_node["data"]["inputs"]

        assert len(inputs) == 2
        input_names = {inp["name"] for inp in inputs}
        assert input_names == {"a", "b"}

    def test_render_multiple_nodes(self):
        """Test rendering a graph with multiple nodes."""
        graph = Graph(nodes=[double, add])
        result = render_graph(graph.to_flat_graph())

        # Check FUNCTION nodes specifically
        fn_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "FUNCTION"]
        assert len(fn_nodes) == 2
        node_ids = {n["id"] for n in fn_nodes}
        assert node_ids == {"double", "add"}

    def test_render_edges(self):
        """Test that edges are created from output->input connections."""

        @node(output_name="doubled")
        def double_fn(x: int) -> int:
            return x * 2

        @node(output_name="result")
        def use_doubled(doubled: int) -> int:
            return doubled + 1

        graph = Graph(nodes=[double_fn, use_doubled])
        result = render_graph(graph.to_flat_graph())

        # Default mode (separate_outputs=False) uses merged output format:
        # Data edges go directly from producer function to consumer function
        data_edges = [e for e in result["edges"] if e.get("data", {}).get("edgeType") == "data"]
        assert len(data_edges) == 1
        # Data edge goes from producer function to consumer function (not via DATA node)
        assert data_edges[0]["source"] == "double_fn"
        assert data_edges[0]["target"] == "use_doubled"

        # In merged mode, no output edges (function → DATA) are created
        output_edges = [e for e in result["edges"] if e.get("data", {}).get("edgeType") == "output"]
        assert len(output_edges) == 0  # No output edges in merged mode

    def test_render_with_bound_inputs(self):
        """Test that bound inputs are marked correctly."""
        graph = Graph(nodes=[add]).bind(a=5)
        result = render_graph(graph.to_flat_graph())

        fn_node = next(n for n in result["nodes"] if n["data"]["nodeType"] == "FUNCTION")
        inputs = fn_node["data"]["inputs"]

        a_input = next(inp for inp in inputs if inp["name"] == "a")
        b_input = next(inp for inp in inputs if inp["name"] == "b")

        assert a_input["is_bound"] is True
        assert b_input["is_bound"] is False

    def test_render_options_passthrough(self):
        """Test that options are included in the result."""
        graph = Graph(nodes=[double])
        result = render_graph(
            graph.to_flat_graph(),
            theme="dark",
            show_types=True,
            depth=2,
            show_inputs=True,
            show_bounded_inputs=True,
        )

        assert result["meta"]["theme_preference"] == "dark"
        assert result["meta"]["show_types"] is True
        assert result["meta"]["initial_depth"] == 2
        assert result["meta"]["show_inputs"] is True
        assert result["meta"]["show_bounded_inputs"] is True

    def test_render_options_inputs_visible_by_default(self):
        """Renderer defaults to including INPUT/INPUT_GROUP nodes."""
        graph = Graph(nodes=[double])
        result = render_graph(graph.to_flat_graph())
        assert result["meta"]["show_inputs"] is True
        assert result["meta"]["show_bounded_inputs"] is False

    def test_graph_visualize_accepts_show_external_inputs_alias(self, tmp_path):
        """Graph.visualize keeps the old flag as a deprecated alias."""
        graph = Graph(nodes=[double])
        output = tmp_path / "graph.html"

        with pytest.warns(DeprecationWarning, match="show_external_inputs is deprecated"):
            graph.visualize(show_external_inputs=False, filepath=str(output))

        assert output.exists()
        html = output.read_text()
        assert re.search(r'"show_inputs"\s*:\s*false', html)

    def test_visualize_accepts_show_external_inputs_alias(self, tmp_path):
        """Top-level visualize keeps the old flag as a deprecated alias."""
        graph = Graph(nodes=[double])
        output = tmp_path / "widget.html"

        with pytest.warns(DeprecationWarning, match="show_external_inputs is deprecated"):
            visualize(graph, show_external_inputs=False, filepath=str(output))

        assert output.exists()
        html = output.read_text()
        assert re.search(r'"show_inputs"\s*:\s*false', html)

    def test_visualize_rejects_conflicting_input_flags(self):
        """Conflicting new/old input flags should fail loudly."""
        graph = Graph(nodes=[double])

        with pytest.raises(TypeError, match="Pass either show_inputs or show_external_inputs"):
            visualize(graph, show_inputs=True, show_external_inputs=False)

    def test_render_hides_bound_input_nodes_by_default(self):
        """Bound inputs stay hidden unless show_bounded_inputs=True."""
        graph = Graph(nodes=[add]).bind(a=5)

        default_result = render_graph(graph.to_flat_graph())
        default_input_ids = {n["id"] for n in default_result["nodes"] if n["data"]["nodeType"] in {"INPUT", "INPUT_GROUP"}}
        assert default_input_ids == {"input_b"}

        expanded_result = render_graph(graph.to_flat_graph(), show_bounded_inputs=True)
        expanded_input_ids = {n["id"] for n in expanded_result["nodes"] if n["data"]["nodeType"] in {"INPUT", "INPUT_GROUP"}}
        assert expanded_input_ids == {"input_a", "input_b"}

    def test_render_never_shows_shared_inputs(self):
        """Shared params should not create INPUT nodes or input edges."""

        @node(output_name="messages")
        def add_message(messages: list[str], user_input: str) -> list[str]:
            return [*messages, user_input]

        @node(output_name="result")
        def summarize(messages: list[str]) -> str:
            return "\n".join(messages)

        graph = Graph(
            nodes=[add_message, summarize],
            edges=[(add_message, summarize)],
            shared="messages",
            entrypoint="add_message",
        )

        result = render_graph(graph.to_flat_graph(), show_bounded_inputs=True)
        input_ids = {n["id"] for n in result["nodes"] if n["data"]["nodeType"] in {"INPUT", "INPUT_GROUP"}}
        input_edges = {(e["source"], e["target"]) for e in result["edges"] if e.get("data", {}).get("edgeType") == "input"}

        assert "input_messages" not in input_ids
        assert input_ids == {"input_user_input"}
        assert all(source != "input_messages" for source, _ in input_edges)

    def test_keeps_dag_dependencies_without_transitive_pruning(self):
        """DAG visualization should keep all declared/inferred dependencies."""
        graph = Graph(nodes=[step_a, step_b, step_c, step_d])
        result = render_graph(graph.to_flat_graph())

        edge_pairs = {(edge["source"], edge["target"]) for edge in result["edges"]}

        assert ("step_a", "step_b") in edge_pairs
        assert ("step_b", "step_c") in edge_pairs
        assert ("step_c", "step_d") in edge_pairs
        assert ("step_b", "step_d") in edge_pairs

    def test_start_node_for_explicit_entrypoint(self):
        """Configured entrypoints get a synthetic START node and edge."""
        graph = Graph(nodes=[double, add]).with_entrypoint("add")
        result = render_graph(graph.to_flat_graph())

        start_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] == "START"]
        assert len(start_nodes) == 1
        assert start_nodes[0]["id"] == "__start__"

        start_edges = [e for e in result["edges"] if e["source"] == "__start__"]
        assert len(start_edges) == 1
        assert start_edges[0]["target"] == "add"

    def test_no_start_node_without_explicit_entrypoint(self):
        """No synthetic START node is rendered by default."""
        graph = Graph(nodes=[double, add])
        result = render_graph(graph.to_flat_graph())

        assert all(n["data"]["nodeType"] != "START" for n in result["nodes"])

    def test_start_edge_targets_internal_node_when_entrypoint_container_expanded(self):
        """When an entrypoint container is expanded, START should target an inner node.

        This avoids rendering START->container edges that visually appear to attach
        to container chrome instead of executable nodes.
        """

        @node(output_name="messages")
        def first(messages: list[str], seed: str) -> list[str]:
            return [*messages, seed]

        @node(output_name="messages")
        def second(messages: list[str]) -> list[str]:
            return messages

        inner = Graph(
            nodes=[first, second],
            edges=[(first, second), (second, first)],
            name="inner",
            entrypoint="first",
        )
        outer = Graph(nodes=[inner.as_node()], entrypoint="inner")

        result = render_graph(outer.to_flat_graph(), depth=1)

        start_edges = [e for e in result["edges"] if e["source"] == "__start__"]
        assert len(start_edges) == 1
        assert start_edges[0]["target"] != "inner"
        assert start_edges[0]["target"].startswith("inner/")

    def test_render_nested_graph(self):
        """Test rendering a nested graph."""
        inner = Graph(nodes=[double], name="inner")
        outer = Graph(nodes=[inner.as_node(), add])

        result = render_graph(outer.to_flat_graph(), depth=1)

        # Should have FUNCTION/PIPELINE nodes from both outer and inner
        fn_and_pipeline_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] in ("FUNCTION", "PIPELINE")]
        node_ids = {n["id"] for n in fn_and_pipeline_nodes}
        assert "inner" in node_ids  # The pipeline node
        assert "inner/double" in node_ids  # Inner node (expanded, hierarchical ID)
        assert "add" in node_ids  # Outer node

        # Inner nodes should have parentNode set
        double_node = next(n for n in result["nodes"] if n["id"] == "inner/double")
        assert double_node["parentNode"] == "inner"

        # Pipeline node should be expanded
        inner_node = next(n for n in result["nodes"] if n["id"] == "inner")
        assert inner_node["data"]["nodeType"] == "PIPELINE"
        assert inner_node["data"]["isExpanded"] is True

    def test_render_nested_graph_collapsed(self):
        """Test that depth=0 keeps nested graphs collapsed."""
        inner = Graph(nodes=[double], name="inner")
        outer = Graph(nodes=[inner.as_node(), add])

        result = render_graph(outer.to_flat_graph(), depth=0)

        # All nodes should be present (children included for click-to-expand)
        # Visibility is controlled by JS based on expansion state
        fn_and_pipeline_nodes = [n for n in result["nodes"] if n["data"]["nodeType"] in ("FUNCTION", "PIPELINE")]
        node_ids = {n["id"] for n in fn_and_pipeline_nodes}
        assert "inner" in node_ids
        assert "add" in node_ids
        # double is now always included (visibility controlled by JS)
        assert "inner/double" in node_ids  # Hierarchical ID

        # Inner graph should be marked as collapsed
        inner_node = next(n for n in result["nodes"] if n["id"] == "inner")
        assert inner_node["data"]["isExpanded"] is False

        # Child node should have parentNode reference
        double_node = next(n for n in result["nodes"] if n["id"] == "inner/double")
        assert double_node.get("parentNode") == "inner"

    def test_render_collapsed_nested_graph_shows_leaf_outputs(self):
        """Collapsed containers should advertise terminal inner outputs."""

        @node(output_name="step1_out")
        def step1(x: int) -> int:
            return x + 1

        @node(output_name="step2_out")
        def step2(step1_out: int) -> int:
            return step1_out * 2

        @node(output_name="validated")
        def validate(step2_out: int) -> int:
            return step2_out

        inner = Graph(nodes=[step1, step2], name="inner")
        middle = Graph(nodes=[inner.as_node(), validate], name="middle")
        outer = Graph(nodes=[middle.as_node()])

        result = render_graph(outer.to_flat_graph(), depth=0)

        middle_node = next(n for n in result["nodes"] if n["id"] == "middle")
        output_names = {output["name"] for output in middle_node["data"].get("outputs", [])}

        assert output_names == {"validated"}

    def test_render_collapsed_nested_graph_keeps_terminal_output_from_mixed_output_node(self):
        """Collapsed containers should keep terminal sibling outputs."""

        @node(output_name=("out1", "out2"))
        def split(x: int) -> tuple[int, int]:
            return x + 1, x + 2

        @node(output_name="used")
        def consume(out1: int) -> int:
            return out1 * 2

        inner = Graph(nodes=[split, consume], name="inner")
        outer = Graph(nodes=[inner.as_node()])

        result = render_graph(outer.to_flat_graph(), depth=0)

        inner_node = next(n for n in result["nodes"] if n["id"] == "inner")
        output_names = {output["name"] for output in inner_node["data"].get("outputs", [])}

        assert output_names == {"out2", "used"}

    def test_render_collapsed_nested_graph_ignores_ordering_edges_for_leaf_outputs(self):
        """Collapsed containers should not let ordering edges hide outputs."""

        @node(output_name="produced")
        def produce(x: int) -> int:
            return x + 1

        @node(output_name="done")
        def wait_only(flag: int) -> int:
            return flag

        inner = Graph(nodes=[produce, wait_only], name="inner", edges=[(produce, wait_only)])
        outer = Graph(nodes=[inner.as_node()])

        result = render_graph(outer.to_flat_graph(), depth=0)

        inner_node = next(n for n in result["nodes"] if n["id"] == "inner")
        output_names = {output["name"] for output in inner_node["data"].get("outputs", [])}

        assert output_names == {"done", "produced"}

    def test_render_collapsed_nested_graph_shows_leaf_data_nodes_in_separate_mode(self):
        """Separate output mode should create DATA nodes for collapsed leaf outputs."""

        @node(output_name="step1_out")
        def step1(x: int) -> int:
            return x + 1

        @node(output_name="step2_out")
        def step2(step1_out: int) -> int:
            return step1_out * 2

        @node(output_name="validated")
        def validate(step2_out: int) -> int:
            return step2_out

        inner = Graph(nodes=[step1, step2], name="inner")
        middle = Graph(nodes=[inner.as_node(), validate], name="middle")
        outer = Graph(nodes=[middle.as_node()])

        result = render_graph(outer.to_flat_graph(), depth=0, separate_outputs=True)

        data_node_ids = {n["id"] for n in result["nodes"] if n["data"]["nodeType"] == "DATA"}

        assert "data_middle_validated" in data_node_ids
        assert "data_middle_step1_out" not in data_node_ids
        assert "data_middle_step2_out" not in data_node_ids

    def test_interrupt_cycle_hides_all_inputs_when_inputs_disabled(self):
        """Notebook regression: no INPUT nodes/edges should appear with show_inputs=False."""
        graph = _build_interrupt_cycle_graph()

        result = render_graph(graph.to_flat_graph(), depth=0, show_inputs=False)

        # Initial state should hide input nodes and input edges.
        assert all(n["data"]["nodeType"] not in {"INPUT", "INPUT_GROUP"} for n in result["nodes"])
        assert all(e.get("data", {}).get("edgeType") != "input" for e in result["edges"])

        # Every (expansion × separate) state with show_inputs=False must do the same.
        scenes = list(_enumerate_scenes(graph, show_inputs=False))
        assert scenes, "Expected at least one expansion-state scene"
        for key, nodes, edges in scenes:
            assert all(n["data"]["nodeType"] not in {"INPUT", "INPUT_GROUP"} for n in nodes), f"INPUT node leaked in state {key}"
            assert all(e.get("data", {}).get("edgeType") != "input" for e in edges), f"INPUT edge leaked in state {key}"

    def test_interrupt_cycle_control_edge_persists_across_expansion_states(self):
        """Notebook regression: should_continue -> ask_user control edge must not disappear."""
        graph = _build_interrupt_cycle_graph()

        scenes = list(_enumerate_scenes(graph, show_inputs=False))
        assert scenes, "Expected at least one expansion-state scene"

        for key, _nodes, edges in scenes:
            control_edges = [e for e in edges if e.get("data", {}).get("edgeType") == "control" and e.get("source") == "should_continue"]
            assert control_edges, f"Missing should_continue control edge in state {key}"

            ask_user_targets = [e for e in control_edges if str(e.get("target", "")).startswith("ask_user")]
            assert ask_user_targets, f"Missing should_continue -> ask_user* control edge in state {key}"
            # Semantic invariant: edge type marks it as control. The CSS
            # dashed style is derived from this flag in viz.js (Stage 5
            # pixel parity), not asserted here.

    def test_interrupt_cycle_expected_edges_for_0_1_2_expanded_graph_nodes(self):
        """Notebook demo: edge routing should be correct for collapsed/1-expanded/2-expanded states."""
        graph = _build_interrupt_cycle_graph()

        expected_by_state = {
            "ask_user:0,llm:0|sep:0|ext:0": {
                ("__start__", "ask_user", "start"),
                ("ask_user", "llm", "data"),
                ("llm", "should_continue", "data"),
                ("llm", "ask_user", "data"),
                ("should_continue", "ask_user", "control"),
                ("should_continue", "__end__", "end"),
            },
            "ask_user:1,llm:0|sep:0|ext:0": {
                ("__start__", "ask_user/ask_slack", "start"),
                ("ask_user/ask_slack", "ask_user/add_user_message", "data"),
                ("ask_user/add_user_message", "llm", "data"),
                ("llm", "should_continue", "data"),
                ("llm", "ask_user/ask_slack", "data"),
                ("should_continue", "ask_user/ask_slack", "control"),
                ("should_continue", "__end__", "end"),
            },
            "ask_user:0,llm:1|sep:0|ext:0": {
                ("__start__", "ask_user", "start"),
                ("ask_user", "llm/llm_step", "data"),
                ("llm/llm_step", "llm/add_assistant_message", "data"),
                ("llm/add_assistant_message", "should_continue", "data"),
                ("llm/add_assistant_message", "ask_user", "data"),
                ("should_continue", "ask_user", "control"),
                ("should_continue", "__end__", "end"),
            },
            "ask_user:1,llm:1|sep:0|ext:0": {
                ("__start__", "ask_user/ask_slack", "start"),
                ("ask_user/ask_slack", "ask_user/add_user_message", "data"),
                ("ask_user/add_user_message", "llm/llm_step", "data"),
                ("llm/llm_step", "llm/add_assistant_message", "data"),
                ("llm/add_assistant_message", "should_continue", "data"),
                ("llm/add_assistant_message", "ask_user/ask_slack", "data"),
                ("should_continue", "ask_user/ask_slack", "control"),
                ("should_continue", "__end__", "end"),
            },
        }

        edges_by_key = {key: edges for key, _nodes, edges in _enumerate_scenes(graph, show_inputs=False)}

        for state_key, expected in expected_by_state.items():
            assert state_key in edges_by_key, f"Missing scene for state: {state_key}"
            actual = {_edge_signature(edge) for edge in edges_by_key[state_key]}
            assert actual == expected, f"Unexpected edges for state {state_key}"

            control_edges = [e for e in edges_by_key[state_key] if e.get("data", {}).get("edgeType") == "control"]
            assert control_edges, f"Expected control edge in state {state_key}"

    def test_interrupt_cycle_messages_feedback_edge_present_in_separate_outputs_mode(self):
        """Notebook regression: llm messages edge to ask_user should not disappear in sep mode."""
        graph = _build_interrupt_cycle_graph()
        edges_by_key = {key: edges for key, _nodes, edges in _enumerate_scenes(graph, show_inputs=False)}

        state_collapsed_target = "ask_user:0,llm:1|sep:1|ext:0"
        state_expanded_target = "ask_user:1,llm:1|sep:1|ext:0"
        assert state_collapsed_target in edges_by_key
        assert state_expanded_target in edges_by_key

        collapsed_signatures = {_edge_signature(edge) for edge in edges_by_key[state_collapsed_target]}
        expanded_signatures = {_edge_signature(edge) for edge in edges_by_key[state_expanded_target]}

        assert ("data_llm/add_assistant_message_messages", "ask_user", "data") in collapsed_signatures
        assert ("data_llm/add_assistant_message_messages", "ask_user/ask_slack", "data") in expanded_signatures


class TestNodeType:
    """Tests for node_type property on HyperNode subclasses."""

    def test_function_node_type(self):
        """Test that FunctionNode has node_type='FUNCTION'."""
        from hypergraph.nodes.function import FunctionNode

        fn = FunctionNode(lambda x: x, output_name="y")
        assert fn.node_type == "FUNCTION"

    def test_graph_node_type(self):
        """Test that GraphNode has node_type='GRAPH'."""
        inner = Graph(nodes=[double], name="inner")
        gn = inner.as_node()
        assert gn.node_type == "GRAPH"


class TestNodeToParentMap:
    """Tests for node_to_parent map in render output."""

    def test_render_graph_includes_node_to_parent_map(self):
        """Test that render_graph includes node_to_parent map in meta for nested graphs."""
        # Create a nested graph: inner contains double, outer contains inner and add
        inner = Graph(nodes=[double], name="inner")
        outer = Graph(nodes=[inner.as_node(), add])

        result = render_graph(outer.to_flat_graph())

        # Assert node_to_parent exists in meta
        assert "node_to_parent" in result["meta"]
        node_to_parent = result["meta"]["node_to_parent"]

        # The 'inner/double' node should have 'inner' as its parent (hierarchical ID)
        assert "inner/double" in node_to_parent
        assert node_to_parent["inner/double"] == "inner"

        # The 'inner' node should NOT be in the map (it's at root level, no parent)
        assert "inner" not in node_to_parent

        # The 'add' node should NOT be in the map (it's at root level, no parent)
        assert "add" not in node_to_parent

    def test_node_to_parent_map_deeply_nested(self):
        """Test node_to_parent map with multiple nesting levels."""
        # Create deeply nested: level1 contains level2 contains triple
        level2 = Graph(nodes=[triple], name="level2")
        level1 = Graph(nodes=[level2.as_node()], name="level1")
        outer = Graph(nodes=[level1.as_node()])

        result = render_graph(outer.to_flat_graph())
        node_to_parent = result["meta"]["node_to_parent"]

        # triple's parent is level2 (hierarchical IDs: level1/level2/triple)
        assert node_to_parent.get("level1/level2/triple") == "level1/level2"

        # level2's parent is level1 (hierarchical ID: level1/level2)
        assert node_to_parent.get("level1/level2") == "level1"

        # level1 has no parent (root level)
        assert "level1" not in node_to_parent

    def test_node_to_parent_map_empty_for_flat_graph(self):
        """Test node_to_parent map is empty when graph has no nesting."""
        graph = Graph(nodes=[double, add])
        result = render_graph(graph.to_flat_graph())

        node_to_parent = result["meta"]["node_to_parent"]

        # Flat graph has no parent relationships
        assert node_to_parent == {}


class TestExclusiveBranchEdges:
    """Edges from mutex producers feeding the same consumer get marked exclusive."""

    @staticmethod
    def _build_ifelse_merge_graph() -> Graph:
        @ifelse(when_true="branch_a", when_false="branch_b")
        def decide(x: int) -> bool:
            return x > 0

        @node(output_name="result")
        def branch_a(x: int) -> str:
            return "a"

        @node(output_name="result")
        def branch_b(x: int) -> str:
            return "b"

        @node(output_name="final")
        def consumer(result: str) -> str:
            return result

        return Graph([decide, branch_a, branch_b, consumer])

    def test_merged_mode_marks_exclusive(self):
        graph = self._build_ifelse_merge_graph()
        result = render_graph(graph.to_flat_graph(), separate_outputs=False)

        data_edges = [e for e in result["edges"] if e.get("data", {}).get("edgeType") == "data"]
        assert len(data_edges) == 2
        assert all(e["target"] == "consumer" for e in data_edges)
        assert all(e["data"]["exclusive"] is True for e in data_edges)

    def test_separate_mode_marks_exclusive(self):
        graph = self._build_ifelse_merge_graph()
        result = render_graph(graph.to_flat_graph(), separate_outputs=True)

        consumer_edges = [e for e in result["edges"] if e.get("data", {}).get("edgeType") == "data" and e["target"] == "consumer"]
        assert len(consumer_edges) == 2
        assert {e["source"] for e in consumer_edges} == {"data_branch_a_result", "data_branch_b_result"}
        assert all(e["data"]["exclusive"] is True for e in consumer_edges)

    def test_linear_data_edges_not_marked(self):
        @node(output_name="x")
        def first() -> int:
            return 1

        @node(output_name="y")
        def second(x: int) -> int:
            return x + 1

        graph = Graph([first, second])
        result = render_graph(graph.to_flat_graph())

        data_edges = [e for e in result["edges"] if e.get("data", {}).get("edgeType") == "data"]
        assert len(data_edges) == 1
        assert data_edges[0]["data"]["exclusive"] is False

    def test_route_branches_marked_exclusive(self):
        """N-ary @route with default multi_target=False is also exclusive."""

        @route(targets=["path_a", "path_b"])
        def decide(x: int) -> str:
            return "path_a"

        @node(output_name="value")
        def path_a(x: int) -> int:
            return 1

        @node(output_name="value")
        def path_b(x: int) -> int:
            return 2

        @node(output_name="final")
        def consumer(value: int) -> int:
            return value

        graph = Graph([decide, path_a, path_b, consumer])
        result = render_graph(graph.to_flat_graph())

        data_edges = [e for e in result["edges"] if e.get("data", {}).get("edgeType") == "data" and e["target"] == "consumer"]
        assert len(data_edges) == 2
        assert all(e["data"]["exclusive"] is True for e in data_edges)

    def test_multi_target_route_not_marked(self):
        """multi_target=True routes run targets concurrently, not exclusively."""

        @route(targets=["path_a", "path_b"], multi_target=True)
        def fan_out(x: int) -> list:
            return ["path_a", "path_b"]

        @node(output_name="value")
        def path_a(x: int) -> int:
            return 1

        @node(output_name="value_b")
        def path_b(x: int) -> int:
            return 2

        @node(output_name="final")
        def consumer(value: int, value_b: int) -> int:
            return value + value_b

        graph = Graph([fan_out, path_a, path_b, consumer])
        result = render_graph(graph.to_flat_graph())

        data_edges = [e for e in result["edges"] if e.get("data", {}).get("edgeType") == "data" and e["target"] == "consumer"]
        assert all(e["data"]["exclusive"] is False for e in data_edges)

    def test_multi_target_route_excluded_from_mutex_groups(self):
        """Unit-level: parallel routes never claim their branches as mutex."""
        from hypergraph.viz._common import _compute_mutex_groups

        @route(targets=["path_a", "path_b"], multi_target=True)
        def fan_out(x: int) -> list:
            return ["path_a", "path_b"]

        @node(output_name="va")
        def path_a(x: int) -> int:
            return 1

        @node(output_name="vb")
        def path_b(x: int) -> int:
            return 2

        graph = Graph([fan_out, path_a, path_b])
        groups = _compute_mutex_groups(graph.to_flat_graph())
        assert groups == [], "multi_target route must not produce mutex groups"
