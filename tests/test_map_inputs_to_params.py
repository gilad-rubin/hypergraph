"""Tests for map_inputs_to_params method on nodes.

This tests the polymorphic approach to mapping renamed inputs back to
original function parameters, replacing isinstance checks with proper OOP.
"""

import pytest

from hypergraph.nodes.base import HyperNode
from hypergraph.nodes.function import FunctionNode, node
from hypergraph.nodes.gate import RouteNode, route, END


class TestHyperNodeMapInputsToParams:
    """Tests for HyperNode.map_inputs_to_params default behavior."""

    def test_default_returns_inputs_unchanged(self):
        """Base class default implementation returns inputs unchanged."""
        # Use FunctionNode but test the base behavior before any renames
        def foo(a, b):
            return a + b

        fn = FunctionNode(foo, output_name="result")
        inputs = {"a": 1, "b": 2}

        # With no renames, map_inputs_to_params should return inputs as-is
        result = fn.map_inputs_to_params(inputs)
        assert result == {"a": 1, "b": 2}

    def test_empty_inputs_returns_empty(self):
        """Empty inputs dict returns empty dict."""
        def foo():
            pass

        fn = FunctionNode(foo)
        result = fn.map_inputs_to_params({})
        assert result == {}


class TestFunctionNodeMapInputsToParams:
    """Tests for FunctionNode.map_inputs_to_params with renames."""

    def test_single_rename(self):
        """Single renamed input is mapped back to original."""
        def foo(x, y):
            return x + y

        fn = FunctionNode(foo, output_name="result", rename_inputs={"x": "input_x"})
        # External world uses "input_x", but function expects "x"
        inputs = {"input_x": 10, "y": 20}

        result = fn.map_inputs_to_params(inputs)

        assert result == {"x": 10, "y": 20}

    def test_multiple_renames(self):
        """Multiple renamed inputs are all mapped back."""
        def foo(a, b, c):
            return a + b + c

        fn = FunctionNode(foo, output_name="result", rename_inputs={"a": "x", "b": "y"})
        inputs = {"x": 1, "y": 2, "c": 3}

        result = fn.map_inputs_to_params(inputs)

        assert result == {"a": 1, "b": 2, "c": 3}

    def test_chained_renames(self):
        """Chained renames (a->x then x->z) map z back to a."""
        def foo(a, b):
            return a + b

        fn = FunctionNode(foo, output_name="result")
        renamed_once = fn.with_inputs(a="x")
        renamed_twice = renamed_once.with_inputs(x="z")

        inputs = {"z": 100, "b": 200}

        result = renamed_twice.map_inputs_to_params(inputs)

        assert result == {"a": 100, "b": 200}

    def test_parallel_renames_same_batch(self):
        """Parallel renames in same call don't chain incorrectly.

        If we rename x->y and y->z in the same with_inputs call,
        they should NOT chain (x shouldn't map to z).
        """
        def foo(x, y):
            return x + y

        fn = FunctionNode(foo, output_name="result")
        # Rename both in same call: x->a, y->b
        renamed = fn.with_inputs(x="a", y="b")

        inputs = {"a": 10, "b": 20}

        result = renamed.map_inputs_to_params(inputs)

        # x->a and y->b, so a maps to x, b maps to y
        assert result == {"x": 10, "y": 20}

    def test_swap_renames_same_batch(self):
        """Swapping names in same call works correctly.

        If we rename x->y and y->x in the same call, they swap.
        """
        def foo(x, y):
            return x + y

        fn = FunctionNode(foo, output_name="result")
        # Swap: x->y, y->x in same call
        renamed = fn.with_inputs(x="y", y="x")

        inputs = {"y": 10, "x": 20}  # Note: reversed from original

        result = renamed.map_inputs_to_params(inputs)

        # After swap: current "y" was originally "x", current "x" was originally "y"
        assert result == {"x": 10, "y": 20}

    def test_no_renames_passthrough(self):
        """Without renames, inputs pass through unchanged."""
        def foo(a, b, c):
            return a + b + c

        fn = FunctionNode(foo, output_name="result")
        inputs = {"a": 1, "b": 2, "c": 3}

        result = fn.map_inputs_to_params(inputs)

        assert result == {"a": 1, "b": 2, "c": 3}

    def test_extra_inputs_preserved(self):
        """Extra inputs not in function signature are preserved.

        This handles cases where inputs dict may have extra keys
        that the node doesn't care about.
        """
        def foo(x):
            return x

        fn = FunctionNode(foo, output_name="result", rename_inputs={"x": "input"})
        inputs = {"input": 42, "extra": "ignored"}

        result = fn.map_inputs_to_params(inputs)

        # "input" -> "x", "extra" stays as "extra"
        assert result == {"x": 42, "extra": "ignored"}


class TestRouteNodeMapInputsToParams:
    """Tests for RouteNode.map_inputs_to_params with renames."""

    def test_single_rename(self):
        """Single renamed input is mapped back to original."""
        def decide(x):
            return "target_a" if x > 0 else "target_b"

        rn = RouteNode(decide, targets=["target_a", "target_b"], rename_inputs={"x": "value"})
        inputs = {"value": 10}

        result = rn.map_inputs_to_params(inputs)

        assert result == {"x": 10}

    def test_multiple_renames(self):
        """Multiple renamed inputs are all mapped back."""
        def decide(a, b):
            return "target_a" if a > b else "target_b"

        rn = RouteNode(decide, targets=["target_a", "target_b"], rename_inputs={"a": "x", "b": "y"})
        inputs = {"x": 10, "y": 5}

        result = rn.map_inputs_to_params(inputs)

        assert result == {"a": 10, "b": 5}

    def test_chained_renames_via_with_inputs(self):
        """Chained renames work for RouteNode too."""
        def decide(a):
            return "target_a"

        rn = RouteNode(decide, targets=["target_a"])
        renamed_once = rn.with_inputs(a="x")
        renamed_twice = renamed_once.with_inputs(x="z")

        inputs = {"z": 100}

        result = renamed_twice.map_inputs_to_params(inputs)

        assert result == {"a": 100}

    def test_no_renames_passthrough(self):
        """Without renames, inputs pass through unchanged."""
        def decide(flag):
            return "target_a" if flag else "target_b"

        rn = RouteNode(decide, targets=["target_a", "target_b"])
        inputs = {"flag": True}

        result = rn.map_inputs_to_params(inputs)

        assert result == {"flag": True}

    def test_with_route_decorator(self):
        """map_inputs_to_params works with @route decorator."""
        @route(targets=["process", END], rename_inputs={"x": "input_value"})
        def decide(x):
            return "process" if x > 0 else END

        inputs = {"input_value": 42}

        result = decide.map_inputs_to_params(inputs)

        assert result == {"x": 42}


class TestMapInputsToParamsIntegration:
    """Integration tests for map_inputs_to_params with graph execution."""

    def test_function_node_renamed_inputs_execute_correctly(self):
        """FunctionNode with renamed inputs executes correctly in graph."""
        from hypergraph import Graph
        from hypergraph.runners.sync import SyncRunner

        @node(output_name="result", rename_inputs={"x": "input_value"})
        def double(x):
            return x * 2

        graph = Graph([double])
        runner = SyncRunner()

        # Pass value using renamed input name
        result = runner.run(graph, {"input_value": 21})

        assert result["result"] == 42

    def test_route_node_renamed_inputs_execute_correctly(self):
        """RouteNode with renamed inputs executes correctly in graph."""
        from hypergraph import Graph
        from hypergraph.runners.sync import SyncRunner

        @route(targets=["double", END], rename_inputs={"x": "check_value"})
        def decide(x):
            return "double" if x > 0 else END

        @node(output_name="result")
        def double(check_value):
            return check_value * 2

        graph = Graph([decide, double])
        runner = SyncRunner()

        result = runner.run(graph, {"check_value": 10})

        assert result["result"] == 20

    def test_chained_renames_execute_correctly(self):
        """Chained renames work correctly during execution."""
        from hypergraph import Graph
        from hypergraph.runners.sync import SyncRunner

        def add(a, b):
            return a + b

        fn = FunctionNode(add, output_name="sum")
        # Chain: a -> x -> z
        renamed = fn.with_inputs(a="x").with_inputs(x="z")

        graph = Graph([renamed])
        runner = SyncRunner()

        result = runner.run(graph, {"z": 10, "b": 20})

        assert result["sum"] == 30
