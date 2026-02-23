"""Tests for emit/wait_for ordering feature."""

import pytest

from hypergraph import (
    END,
    AsyncRunner,
    FunctionNode,
    Graph,
    GraphConfigError,
    SyncRunner,
    ifelse,
    node,
    route,
)
from hypergraph.nodes.base import _EMIT_SENTINEL

# =============================================================================
# Validation tests
# =============================================================================


class TestValidation:
    """Build-time validation for wait_for references."""

    def test_wait_for_nonexistent_output_raises(self):
        """wait_for referencing an output that no node produces raises error."""

        @node(output_name="result")
        def step(x: int) -> int:
            return x + 1

        @node(output_name="final", wait_for="nonexistent")
        def consumer(result: int) -> int:
            return result * 2

        with pytest.raises(GraphConfigError, match="wait_for='nonexistent'"):
            Graph(nodes=[step, consumer])

    def test_wait_for_emit_output_is_valid(self):
        """wait_for referencing an emit output is accepted."""

        @node(output_name="result", emit="done")
        def producer(x: int) -> int:
            return x + 1

        @node(output_name="final", wait_for="done")
        def consumer(result: int) -> int:
            return result * 2

        # Should not raise
        graph = Graph(nodes=[producer, consumer])
        assert graph is not None

    def test_wait_for_data_output_is_valid(self):
        """wait_for referencing a regular data output is accepted."""

        @node(output_name="result")
        def producer(x: int) -> int:
            return x + 1

        @node(output_name="final", wait_for="result")
        def consumer(x: int) -> int:
            return x * 2

        graph = Graph(nodes=[producer, consumer])
        assert graph is not None

    def test_wait_for_typo_suggestion(self):
        """Error message suggests correct name on typo."""

        @node(output_name="result", emit="done_signal")
        def producer(x: int) -> int:
            return x + 1

        @node(output_name="final", wait_for="done_signl")
        def consumer(result: int) -> int:
            return result * 2

        with pytest.raises(GraphConfigError, match="Did you mean 'done_signal'"):
            Graph(nodes=[producer, consumer])


# =============================================================================
# Node attribute tests
# =============================================================================


class TestNodeAttributes:
    """Test emit/wait_for attribute behavior on node classes."""

    def test_emit_appears_in_outputs(self):
        """Emit names appear in node.outputs."""

        @node(output_name="result", emit="done")
        def producer(x: int) -> int:
            return x + 1

        assert "done" in producer.outputs
        assert "result" in producer.outputs

    def test_emit_not_in_data_outputs(self):
        """Emit names do not appear in node.data_outputs."""

        @node(output_name="result", emit="done")
        def producer(x: int) -> int:
            return x + 1

        assert "done" not in producer.data_outputs
        assert "result" in producer.data_outputs

    def test_emit_only_node(self):
        """Node with only emit and no data output."""

        @node(emit="done")
        def side_effect(x: int) -> None:
            pass

        assert side_effect.outputs == ("done",)
        assert side_effect.data_outputs == ()

    def test_wait_for_property(self):
        """wait_for is accessible as a tuple property."""

        @node(output_name="result", wait_for="signal")
        def consumer(x: int) -> int:
            return x

        assert consumer.wait_for == ("signal",)

    def test_wait_for_tuple_form(self):
        """wait_for accepts a tuple of names."""

        @node(output_name="result", wait_for=("a", "b"))
        def consumer(x: int) -> int:
            return x

        assert consumer.wait_for == ("a", "b")

    def test_route_node_with_emit(self):
        """RouteNode supports emit."""

        @route(targets=["a", "b"], emit="decided")
        def decide(x: int) -> str:
            return "a"

        assert "decided" in decide.outputs
        assert decide.data_outputs == ()

    def test_ifelse_node_with_emit(self):
        """IfElseNode supports emit."""

        @ifelse(when_true="a", when_false="b", emit="checked")
        def check(x: int) -> bool:
            return x > 0

        assert "checked" in check.outputs
        assert check.data_outputs == ()

    def test_route_node_with_wait_for(self):
        """RouteNode supports wait_for."""

        @route(targets=["a", END], wait_for="turn_done")
        def decide(messages: list) -> str:
            return "a"

        assert decide.wait_for == ("turn_done",)

    def test_emit_sentinel_is_singleton(self):
        """_EMIT_SENTINEL is a unique object."""
        assert _EMIT_SENTINEL is _EMIT_SENTINEL
        assert _EMIT_SENTINEL is not None
        assert _EMIT_SENTINEL is not True


# =============================================================================
# DAG ordering tests
# =============================================================================


class TestDAGOrdering:
    """wait_for enforces ordering in DAGs."""

    def test_wait_for_ordering_in_dag(self):
        """Consumer waits for producer's emit before running."""
        execution_order = []

        @node(output_name="a", emit="a_done")
        def step_a(x: int) -> int:
            execution_order.append("a")
            return x + 1

        @node(output_name="b", wait_for="a_done")
        def step_b(x: int) -> int:
            execution_order.append("b")
            return x * 2

        graph = Graph(nodes=[step_a, step_b])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5})

        assert result["a"] == 6
        assert result["b"] == 10
        # step_b should run after step_a due to wait_for
        assert execution_order == ["a", "b"]

    def test_emit_sentinels_not_in_result(self):
        """Emit sentinel values are filtered from the final output."""

        @node(output_name="result", emit="done")
        def producer(x: int) -> int:
            return x + 1

        graph = Graph(nodes=[producer])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5})

        assert result["result"] == 6
        assert "done" not in result.values

    def test_multiple_wait_for(self):
        """Node can wait for multiple emits."""
        execution_order = []

        @node(output_name="a", emit="a_done")
        def step_a(x: int) -> int:
            execution_order.append("a")
            return x + 1

        @node(output_name="b", emit="b_done")
        def step_b(x: int) -> int:
            execution_order.append("b")
            return x + 2

        @node(output_name="c", wait_for=("a_done", "b_done"))
        def step_c(a: int, b: int) -> int:
            execution_order.append("c")
            return a + b

        graph = Graph(nodes=[step_a, step_b, step_c])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5})

        assert result["c"] == 13  # (5+1) + (5+2)
        # step_c runs after both a and b
        assert execution_order[-1] == "c"


# =============================================================================
# Cyclic graph tests
# =============================================================================


class TestCyclicOrdering:
    """wait_for enforces ordering within cycles."""

    def test_chat_loop_ordering(self):
        """should_continue sees complete turn via emit/wait_for.

        The emit goes on `accumulate` (the last step of the turn), so
        `should_continue` waits until messages are fully updated.
        """
        turns = []

        @node(output_name="response")
        def generate(messages: list) -> str:
            turns.append(f"gen:{len(messages)}")
            return f"response_{len(messages)}"

        @node(output_name="messages", emit="turn_done")
        def accumulate(messages: list, response: str) -> list:
            turns.append(f"acc:{len(messages)}")
            return messages + [response]

        @route(targets=["generate", END], wait_for="turn_done")
        def should_continue(messages: list) -> str:
            turns.append(f"sc:{len(messages)}")
            if len(messages) >= 3:
                return END
            return "generate"

        graph = Graph(nodes=[generate, accumulate, should_continue])
        runner = SyncRunner()
        result = runner.run(graph, {"messages": ["hello"]})

        # should_continue should always see the updated messages
        assert result["messages"][-1].startswith("response_")
        assert len(result["messages"]) >= 3


# =============================================================================
# Async compatibility
# =============================================================================


class TestAsyncCompat:
    """emit/wait_for works with async runner."""

    @pytest.mark.asyncio
    async def test_async_emit_wait_for(self):
        """emit/wait_for works with AsyncRunner."""
        execution_order = []

        @node(output_name="a", emit="a_done")
        async def async_step_a(x: int) -> int:
            execution_order.append("a")
            return x + 1

        @node(output_name="b", wait_for="a_done")
        async def async_step_b(x: int) -> int:
            execution_order.append("b")
            return x * 2

        graph = Graph(nodes=[async_step_a, async_step_b])
        runner = AsyncRunner()
        result = await runner.run(graph, {"x": 5})

        assert result["a"] == 6
        assert result["b"] == 10
        assert execution_order == ["a", "b"]
        assert "a_done" not in result.values


# =============================================================================
# Edge cases
# =============================================================================


class TestEdgeCases:
    """Edge cases for emit/wait_for."""

    def test_no_emit_no_wait_for_unchanged(self):
        """Nodes without emit/wait_for behave exactly as before."""

        @node(output_name="result")
        def plain(x: int) -> int:
            return x + 1

        assert plain.outputs == ("result",)
        assert plain.data_outputs == ("result",)
        assert plain.wait_for == ()

    def test_emit_string_form(self):
        """emit accepts a single string."""

        @node(output_name="result", emit="done")
        def producer(x: int) -> int:
            return x

        assert producer.outputs == ("result", "done")

    def test_emit_tuple_form(self):
        """emit accepts a tuple of strings."""

        @node(output_name="result", emit=("done", "logged"))
        def producer(x: int) -> int:
            return x

        assert producer.outputs == ("result", "done", "logged")
        assert producer.data_outputs == ("result",)

    def test_function_node_constructor_with_emit(self):
        """FunctionNode constructor accepts emit/wait_for."""

        def my_func(x: int) -> int:
            return x + 1

        fn = FunctionNode(my_func, output_name="result", emit="done", wait_for="signal")
        assert "done" in fn.outputs
        assert fn.wait_for == ("signal",)

    def test_with_outputs_preserves_emit(self):
        """Renaming data outputs doesn't affect emit outputs."""

        @node(output_name="result", emit="done")
        def producer(x: int) -> int:
            return x

        renamed = producer.with_outputs(result="renamed_result")
        assert renamed.outputs == ("renamed_result", "done")
        assert renamed.data_outputs == ("renamed_result",)

    def test_with_outputs_can_rename_emit(self):
        """Emit outputs can also be renamed via with_outputs."""

        @node(output_name="result", emit="done")
        def producer(x: int) -> int:
            return x

        renamed = producer.with_outputs(done="finished")
        assert "finished" in renamed.outputs
        assert renamed.data_outputs == ("result",)


# =============================================================================
# Overlap validation tests
# =============================================================================


class TestOverlapValidation:
    """Emit/wait_for must not overlap with outputs/inputs."""

    def test_emit_overlaps_output_name_raises(self):
        """emit name same as output_name raises ValueError."""
        with pytest.raises(ValueError, match="emit names overlap with output names"):

            @node(output_name="result", emit="result")
            def bad(x: int) -> int:
                return x

    def test_wait_for_overlaps_param_raises(self):
        """wait_for name same as function parameter raises ValueError."""
        with pytest.raises(ValueError, match="wait_for names overlap with input parameters"):

            @node(output_name="result", wait_for="x")
            def bad(x: int) -> int:
                return x

    def test_emit_and_wait_for_share_name_raises(self):
        """emit and wait_for sharing a name raises ValueError."""
        with pytest.raises(ValueError, match="emit and wait_for share names"):

            @node(output_name="result", emit="signal", wait_for="signal")
            def bad(x: int) -> int:
                return x

    def test_route_emit_overlaps_raises(self):
        """RouteNode emit overlapping is caught."""
        # RouteNode has no data_outputs, but emit still validated
        with pytest.raises(ValueError, match="emit and wait_for share names"):

            @route(targets=["a", END], emit="sig", wait_for="sig")
            def bad(x: int) -> str:
                return "a"

    def test_ifelse_emit_overlaps_raises(self):
        """IfElseNode emit overlapping is caught."""
        with pytest.raises(ValueError, match="emit and wait_for share names"):

            @ifelse(when_true="a", when_false="b", emit="sig", wait_for="sig")
            def bad(x: int) -> bool:
                return True

    def test_multiple_emit_partial_overlap_raises(self):
        """One of multiple emit names overlapping output raises."""
        with pytest.raises(ValueError, match="emit names overlap with output names"):

            @node(output_name="result", emit=("done", "result"))
            def bad(x: int) -> int:
                return x


# =============================================================================
# Viz ordering edge tests
# =============================================================================


class TestVizOrderingEdges:
    """Ordering edges appear in visualization with correct style."""

    def test_ordering_edges_in_merged_mode(self):
        """Ordering edges appear in merged output mode."""
        from hypergraph.viz.renderer import render_graph

        @node(output_name="a", emit="a_done")
        def step_a(x: int) -> int:
            return x + 1

        @node(output_name="b", wait_for="a_done")
        def step_b(x: int) -> int:
            return x * 2

        graph = Graph(nodes=[step_a, step_b])
        result = render_graph(graph.to_flat_graph(), depth=0, separate_outputs=False)

        ordering_edges = [e for e in result["edges"] if e.get("data", {}).get("edgeType") == "ordering"]
        assert len(ordering_edges) >= 1
        edge = ordering_edges[0]
        assert edge["source"] == "step_a"
        assert edge["target"] == "step_b"
        assert edge["style"]["stroke"] == "#8b5cf6"
        assert "strokeDasharray" in edge["style"]

    def test_ordering_edges_in_separate_mode(self):
        """Ordering edges appear in separate output mode."""
        from hypergraph.viz.renderer import render_graph

        @node(output_name="a", emit="a_done")
        def step_a(x: int) -> int:
            return x + 1

        @node(output_name="b", wait_for="a_done")
        def step_b(x: int) -> int:
            return x * 2

        graph = Graph(nodes=[step_a, step_b])
        result = render_graph(graph.to_flat_graph(), depth=0, separate_outputs=True)

        ordering_edges = [e for e in result["edges"] if e.get("data", {}).get("edgeType") == "ordering"]
        assert len(ordering_edges) >= 1
        edge = ordering_edges[0]
        assert edge["source"] == "step_a"
        assert edge["target"] == "step_b"
        assert edge["style"]["stroke"] == "#8b5cf6"

    def test_emit_sentinels_not_in_data_outputs_nx(self):
        """NetworkX graph stores data_outputs separate from outputs."""

        @node(output_name="result", emit="done")
        def producer(x: int) -> int:
            return x

        graph = Graph(nodes=[producer])
        flat = graph.to_flat_graph()
        attrs = flat.nodes["producer"]
        assert "done" in attrs["outputs"]
        assert "done" not in attrs["data_outputs"]
        assert "result" in attrs["data_outputs"]

    def test_wait_for_in_nx_attrs(self):
        """NetworkX graph includes wait_for in node attributes."""

        @node(output_name="result", emit="signal")
        def producer(x: int) -> int:
            return x

        @node(output_name="final", wait_for="signal")
        def consumer(result: int) -> int:
            return result

        graph = Graph(nodes=[producer, consumer])
        flat = graph.to_flat_graph()
        attrs = flat.nodes["consumer"]
        assert attrs["wait_for"] == ("signal",)

    def test_ordering_edge_in_flat_graph(self):
        """Flat graph contains ordering edges for wait_for dependencies."""

        @node(output_name="a", emit="a_done")
        def step_a(x: int) -> int:
            return x + 1

        @node(output_name="b", wait_for="a_done")
        def step_b(x: int) -> int:
            return x * 2

        graph = Graph(nodes=[step_a, step_b])
        flat = graph.to_flat_graph()

        ordering_edges = [(u, v, d) for u, v, d in flat.edges(data=True) if d.get("edge_type") == "ordering"]
        assert len(ordering_edges) == 1
        u, v, d = ordering_edges[0]
        assert u == "step_a"
        assert v == "step_b"
        assert d["value_names"] == ["a_done"]
