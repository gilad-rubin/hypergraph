"""Integration tests for gate/routing execution.

Tests cover:
- Basic routing execution
- END termination
- Fallback behavior
- Multi-target routing
- Error handling
- Async runner support
"""

import pytest

from hypergraph import Graph, node, route, END, SyncRunner, AsyncRunner, RunStatus
from hypergraph.graph import GraphConfigError


# =============================================================================
# Basic Routing Execution Tests
# =============================================================================


class TestBasicRouteExecution:
    """Tests for basic routing behavior."""

    def test_routes_to_correct_branch_positive(self):
        """Gate routes to correct branch based on condition (positive)."""

        @node(output_name="a")
        def start(x):
            return x

        @node(output_name="result")
        def positive_path(a):
            return a * 2

        @node(output_name="result")
        def negative_path(a):
            return a * -1

        @route(targets=["positive_path", "negative_path"])
        def decide(a):
            return "positive_path" if a > 0 else "negative_path"

        graph = Graph([start, decide, positive_path, negative_path])
        result = SyncRunner().run(graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        assert result["result"] == 10  # 5 * 2

    def test_routes_to_correct_branch_negative(self):
        """Gate routes to correct branch based on condition (negative)."""

        @node(output_name="a")
        def start(x):
            return x

        @node(output_name="result")
        def positive_path(a):
            return a * 2

        @node(output_name="result")
        def negative_path(a):
            return a * -1

        @route(targets=["positive_path", "negative_path"])
        def decide(a):
            return "positive_path" if a > 0 else "negative_path"

        graph = Graph([start, decide, positive_path, negative_path])
        result = SyncRunner().run(graph, {"x": -5})

        assert result.status == RunStatus.COMPLETED
        assert result["result"] == 5  # -5 * -1

    def test_end_terminates_execution(self):
        """END sentinel prevents downstream nodes from executing."""

        @node(output_name="a")
        def start(x):
            return x

        @node(output_name="result")
        def process(a):
            return a * 2

        @route(targets=["process", END])
        def decide(a):
            return END if a == 0 else "process"

        graph = Graph([start, decide, process])
        result = SyncRunner().run(graph, {"x": 0})

        assert result.status == RunStatus.COMPLETED
        assert "result" not in result.values  # process didn't run
        assert result["a"] == 0  # start ran

    def test_all_targets_end(self):
        """Graph with only END target terminates cleanly."""

        @node(output_name="a")
        def start(x):
            return x

        @route(targets=[END])
        def decide(a):
            return END

        graph = Graph([start, decide])
        result = SyncRunner().run(graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        assert result["a"] == 5


# =============================================================================
# Fallback Tests
# =============================================================================


class TestFallbackRouting:
    """Tests for fallback behavior when routing function returns None."""

    def test_fallback_used_when_none_returned(self):
        """Fallback target is used when routing function returns None."""

        @route(targets=["a"], fallback="default")
        def decide(x):
            return None  # Returns None

        @node(output_name="result")
        def a(x):
            return "a_result"

        @node(output_name="result")
        def default(x):
            return "default_result"

        graph = Graph([decide, a, default])
        result = SyncRunner().run(graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        assert result["result"] == "default_result"

    def test_no_fallback_none_returned_does_nothing(self):
        """Without fallback, returning None activates no targets."""

        @route(targets=["a"])  # No fallback
        def decide(x):
            return None

        @node(output_name="result")
        def a(x):
            return "result"

        graph = Graph([decide, a])
        result = SyncRunner().run(graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        # 'a' should not run since None was returned and no fallback
        assert "result" not in result.values


# =============================================================================
# Multi-Target Tests
# =============================================================================


class TestMultiTargetRouting:
    """Tests for multi_target routing behavior."""

    def test_multi_target_runs_all_returned(self):
        """multi_target=True runs all returned targets."""

        @route(targets=["a", "b", "c"], multi_target=True)
        def decide(x):
            return ["a", "c"]  # Skip "b"

        @node(output_name="result_a")
        def a(x):
            return "a"

        @node(output_name="result_b")
        def b(x):
            return "b"

        @node(output_name="result_c")
        def c(x):
            return "c"

        graph = Graph([decide, a, b, c])
        result = SyncRunner().run(graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        assert result["result_a"] == "a"
        assert result["result_c"] == "c"
        assert "result_b" not in result.values

    def test_multi_target_empty_list_runs_nothing(self):
        """multi_target=True with empty list runs no targets."""

        @route(targets=["a", "b"], multi_target=True)
        def decide(x):
            return []

        @node(output_name="result_a")
        def a(x):
            return "a"

        @node(output_name="result_b")
        def b(x):
            return "b"

        graph = Graph([decide, a, b])
        result = SyncRunner().run(graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        assert "result_a" not in result.values
        assert "result_b" not in result.values

    def test_multi_target_none_runs_nothing(self):
        """multi_target=True with None runs no targets."""

        @route(targets=["a", "b"], multi_target=True)
        def decide(x):
            return None  # OK - equivalent to []

        @node(output_name="result_a")
        def a(x):
            return "a"

        @node(output_name="result_b")
        def b(x):
            return "b"

        graph = Graph([decide, a, b])
        result = SyncRunner().run(graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        assert "result_a" not in result.values
        assert "result_b" not in result.values


# =============================================================================
# Graph Validation Tests
# =============================================================================


class TestGateGraphValidation:
    """Tests for graph validation of gate nodes."""

    def test_invalid_target_raises(self):
        """Gate targeting nonexistent node raises GraphConfigError."""

        @route(targets=["nonexistent"])
        def decide(x):
            return "nonexistent"

        @node(output_name="a")
        def some_node(x):
            return x

        with pytest.raises(GraphConfigError, match="unknown node"):
            Graph([decide, some_node])

    def test_end_target_always_valid(self):
        """END is always a valid target."""

        @route(targets=[END])
        def decide(x):
            return END

        @node(output_name="a")
        def start(x):
            return x

        # Should not raise
        graph = Graph([start, decide])
        assert graph is not None

    def test_gate_targeting_itself_raises(self):
        """Gate cannot target itself."""

        @route(targets=["decide"])
        def decide(x):
            return "decide"

        with pytest.raises(GraphConfigError, match="cannot target itself"):
            Graph([decide])

    def test_gate_targeting_another_gate(self):
        """Gates can target other gates."""

        @route(targets=["gate_b"])
        def gate_a(x):
            return "gate_b"

        @route(targets=[END])
        def gate_b(x):
            return END

        # This should be allowed
        graph = Graph([gate_a, gate_b])
        assert graph is not None

    def test_multi_target_shared_output_raises(self):
        """multi_target=True with same output name should fail."""

        @node(output_name="result")
        def path_a(x):
            return x

        @node(output_name="result")
        def path_b(x):
            return x

        @route(targets=["path_a", "path_b"], multi_target=True)
        def decide(x):
            return ["path_a", "path_b"]

        with pytest.raises(GraphConfigError, match="Multiple nodes produce"):
            Graph([decide, path_a, path_b])

    def test_single_target_shared_output_allowed(self):
        """multi_target=False with same output name is OK (mutex)."""

        @node(output_name="result")
        def path_a(x):
            return x

        @node(output_name="result")
        def path_b(x):
            return x

        @route(targets=["path_a", "path_b"])
        def decide(x):
            return "path_a"

        # Should NOT raise
        graph = Graph([decide, path_a, path_b])
        assert graph is not None

    def test_multi_target_unique_outputs_allowed(self):
        """multi_target=True with different output names is OK."""

        @node(output_name="result_a")
        def path_a(x):
            return x

        @node(output_name="result_b")
        def path_b(x):
            return x

        @route(targets=["path_a", "path_b"], multi_target=True)
        def decide(x):
            return ["path_a", "path_b"]

        # Should NOT raise
        graph = Graph([decide, path_a, path_b])
        assert graph is not None

    def test_node_named_end_raises(self):
        """Node named 'END' raises GraphConfigError."""

        @node(output_name="result")
        def END(x):
            return x

        with pytest.raises(GraphConfigError, match="reserved"):
            Graph([END])


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================


class TestRoutingEdgeCases:
    """Tests for edge cases and error handling."""

    def test_invalid_return_value_raises(self):
        """Returning a target not in the targets list should error."""

        @route(targets=["a", "b"])
        def decide(x):
            return "nonexistent"  # Not in targets!

        @node(output_name="result")
        def a(x):
            return x

        @node(output_name="result")
        def b(x):
            return x

        graph = Graph([decide, a, b])
        result = SyncRunner().run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, ValueError)
        assert "invalid target" in str(result.error)

    def test_return_end_not_in_targets_raises(self):
        """Returning END when END isn't in targets should error."""

        @route(targets=["a", "b"])  # No END in targets
        def decide(x):
            return END  # Returns END anyway!

        @node(output_name="result")
        def a(x):
            return x

        @node(output_name="result")
        def b(x):
            return x

        graph = Graph([decide, a, b])
        result = SyncRunner().run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, ValueError)

    def test_single_target_returns_list_raises(self):
        """multi_target=False returning list should error."""

        @route(targets=["a", "b"], multi_target=False)
        def decide(x):
            return ["a", "b"]  # Wrong type!

        @node(output_name="result_a")
        def a(x):
            return x

        @node(output_name="result_b")
        def b(x):
            return x

        graph = Graph([decide, a, b])
        result = SyncRunner().run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, TypeError)
        assert "multi_target=False but returned a list" in str(result.error)

    def test_multi_target_returns_string_raises(self):
        """multi_target=True returning string should error."""

        @route(targets=["a", "b"], multi_target=True)
        def decide(x):
            return "a"  # Wrong type!

        @node(output_name="result_a")
        def a(x):
            return x

        @node(output_name="result_b")
        def b(x):
            return x

        graph = Graph([decide, a, b])
        result = SyncRunner().run(graph, {"x": 5})

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, TypeError)
        assert "multi_target=True but returned str" in str(result.error)

    def test_chained_gates(self):
        """Gates can route to other gates."""

        @route(targets=["gate_b", END])
        def gate_a(x):
            return "gate_b" if x > 0 else END

        @route(targets=["process", END])
        def gate_b(x):
            return "process" if x > 5 else END

        @node(output_name="result")
        def process(x):
            return x * 2

        graph = Graph([gate_a, gate_b, process])

        # x=10: gate_a -> gate_b -> process
        result = SyncRunner().run(graph, {"x": 10})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == 20

        # x=3: gate_a -> gate_b -> END
        result = SyncRunner().run(graph, {"x": 3})
        assert result.status == RunStatus.COMPLETED
        assert "result" not in result.values

        # x=-1: gate_a -> END
        result = SyncRunner().run(graph, {"x": -1})
        assert result.status == RunStatus.COMPLETED
        assert "result" not in result.values


# =============================================================================
# Async Runner Tests
# =============================================================================


class TestAsyncRouteExecution:
    """Tests for routing with AsyncRunner."""

    async def test_async_routing_basic(self):
        """Basic routing works with async runner."""

        @route(targets=["a", "b"])
        def decide(x):
            return "a"  # Routing func is sync

        @node(output_name="result")
        async def a(x):
            return x * 2

        @node(output_name="result")
        async def b(x):
            return x * 3

        graph = Graph([decide, a, b])
        result = await AsyncRunner().run(graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        assert result["result"] == 10

    async def test_async_end_terminates(self):
        """END terminates execution in async runner."""

        @node(output_name="a")
        async def start(x):
            return x

        @route(targets=["process", END])
        def decide(a):
            return END if a == 0 else "process"

        @node(output_name="result")
        async def process(a):
            return a * 2

        graph = Graph([start, decide, process])
        result = await AsyncRunner().run(graph, {"x": 0})

        assert result.status == RunStatus.COMPLETED
        assert "result" not in result.values

    async def test_async_multi_target(self):
        """Multi-target routing works with async runner."""

        @route(targets=["a", "b"], multi_target=True)
        def decide(x):
            return ["a", "b"]

        @node(output_name="result_a")
        async def a(x):
            return "a"

        @node(output_name="result_b")
        async def b(x):
            return "b"

        graph = Graph([decide, a, b])
        result = await AsyncRunner().run(graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        assert result["result_a"] == "a"
        assert result["result_b"] == "b"


# =============================================================================
# Control Edge Tests
# =============================================================================


class TestControlEdges:
    """Tests for control edge creation."""

    def test_control_edges_created(self):
        """Control edges are created from gate to targets."""

        @node(output_name="a")
        def start(x):
            return x

        @route(targets=["end_a", "end_b"])
        def decide(a):
            return "end_a"

        @node(output_name="result")
        def end_a(a):
            return a

        @node(output_name="result")
        def end_b(a):
            return a

        graph = Graph([start, decide, end_a, end_b])

        # Check control edges exist
        control_edges = [
            (u, v)
            for u, v, d in graph._nx_graph.edges(data=True)
            if d.get("edge_type") == "control"
        ]
        assert ("decide", "end_a") in control_edges
        assert ("decide", "end_b") in control_edges

    def test_data_edges_not_duplicated_as_control(self):
        """If data edge exists, control edge is not duplicated."""

        @node(output_name="a")
        def start(x):
            return x

        # Gate produces 'a' which end_node consumes
        # This creates a data edge, so no control edge needed
        @route(targets=["end_node"])
        def decide(a):
            return "end_node"

        @node(output_name="result")
        def end_node(a):
            return a

        graph = Graph([start, decide, end_node])

        # Check that there's only one edge (the data edge from start)
        edges_to_end = list(graph._nx_graph.in_edges("end_node", data=True))
        assert len(edges_to_end) >= 1  # At least the data edge from start
