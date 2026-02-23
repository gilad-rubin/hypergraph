"""Tests for value source detection and resolution order."""

import threading

from hypergraph import Graph, SyncRunner, node
from hypergraph.graph.validation import GraphConfigError


class TestValueSourceDetection:
    """Test get_value_source() correctly identifies parameter sources."""

    def test_edge_value_source(self):
        """Edge values (upstream outputs) have highest priority."""

        @node(output_name="x")
        def produce() -> int:
            return 42

        @node(output_name="result")
        def consume(x: int = 0) -> int:
            return x

        graph = Graph([produce, consume])
        runner = SyncRunner()

        # x comes from edge (produce → consume), not from default
        result = runner.run(graph, {})
        assert result["result"] == 42

    def test_provided_value_source(self):
        """Provided values (from run() call) override defaults and bindings."""

        @node(output_name="result")
        def process(x: int = 10) -> int:
            return x

        graph = Graph([process]).bind(x=99)
        runner = SyncRunner()

        # Provided value should override both binding and default
        result = runner.run(graph, {"x": 42})
        assert result["result"] == 42

    def test_bound_value_source(self):
        """Bound values (from .bind()) override defaults."""

        @node(output_name="result")
        def process(x: int = 10) -> int:
            return x

        graph = Graph([process]).bind(x=99)
        runner = SyncRunner()

        # Bound value should be used (not default)
        result = runner.run(graph, {})
        assert result["result"] == 99

    def test_default_value_source(self):
        """Signature defaults are lowest priority."""

        @node(output_name="result")
        def process(x: int = 10) -> int:
            return x

        graph = Graph([process])
        runner = SyncRunner()

        # Should use signature default
        result = runner.run(graph, {})
        assert result["result"] == 10


class TestNonCopyableBoundValues:
    """Test that bound values with non-copyable objects work correctly."""

    def test_nested_graph_bound_values_not_deep_copied(self):
        """Bound values in nested graphs should not be deep-copied at runtime.

        This is the exact bug from the user's notebook: when a graph with
        bound non-copyable objects (like Embedder with RLock) is used as
        a node, the runner should NOT attempt to deep-copy those values.
        """

        class NonCopyableObject:
            """Object with thread lock - cannot be pickled/deep-copied."""

            def __init__(self):
                self._lock = threading.RLock()

        @node(output_name="result")
        def use_object(query: str, obj: NonCopyableObject) -> str:
            return f"Used {query}"

        # Create non-copyable object
        obj = NonCopyableObject()

        # Bind it in inner graph
        inner_graph = Graph([use_object], name="inner").bind(obj=obj)

        # Use as node in outer graph
        outer_graph = Graph([inner_graph.as_node()], name="outer")

        # Should NOT raise error about deep-copy failure
        runner = SyncRunner()
        result = runner.run(outer_graph, {"query": "test"})

        assert result["result"] == "Used test"

    def test_bound_values_are_shared_not_copied(self):
        """Verify bound values are intentionally shared, not copied per run."""
        state_tracker = {"calls": 0}

        @node(output_name="result")
        def increment_counter(tracker: dict) -> int:
            tracker["calls"] += 1
            return tracker["calls"]

        graph = Graph([increment_counter]).bind(tracker=state_tracker)
        runner = SyncRunner()

        # First run
        res1 = runner.run(graph, {})
        assert res1["result"] == 1

        # Second run - should see shared state (NOT copied)
        res2 = runner.run(graph, {})
        assert res2["result"] == 2  # Shared, not copied!

    def test_non_copyable_default_raises_clear_error(self):
        """Non-copyable signature defaults should raise helpful error."""

        class NonCopyableObject:
            def __init__(self):
                self._lock = threading.RLock()

        # Create a default instance once (not in decorator)
        default_obj = NonCopyableObject()

        @node(output_name="x")
        def produce() -> int:
            return 1

        @node(output_name="result")
        def bad_default(x: int, obj: NonCopyableObject = default_obj) -> str:
            return "bad"

        # Graph with edge to ensure bad_default executes
        graph = Graph([produce, bad_default])
        runner = SyncRunner()

        # Should fail with GraphConfigError in RunResult
        result = runner.run(graph, {})

        # Check that execution failed
        from hypergraph.runners._shared.types import RunStatus

        assert result.status == RunStatus.FAILED
        assert result.error is not None
        assert isinstance(result.error, GraphConfigError)

        # Check error message content
        error_msg = str(result.error)
        assert "cannot be safely copied" in error_msg
        assert ".bind()" in error_msg  # Suggests solution
        assert "Why copying is needed" in error_msg  # Explains problem
