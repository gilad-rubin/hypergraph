"""Tests for code review bug fixes.

TDD tests for issues identified in the code review:
- Bug 1: Python 3.10 Self import compatibility
- Bug 2: Stale state in async superstep (concurrent nodes read from same state)
- Bug 3: Rename chaining breaks execution
- Bug 4: FunctionNode.defaults ignores renames
- Bug 5: Value comparison fails for numpy-like arrays
- Edge 1: GraphNode input type returns arbitrary match
- Edge 2: Invalid select parameter silently ignored
- Edge 3: GraphNode name bypass separator validation
- Edge 4: Input validation allows overriding internal values
"""

import warnings

import pytest

from hypergraph import END, Graph
from hypergraph.nodes.function import node
from hypergraph.nodes.gate import route
from hypergraph.runners.sync import SyncRunner


class TestPython310Compatibility:
    """Bug 1: Python 3.10 Self import compatibility.

    graph_node.py imports Self from typing, which is Python 3.11+ only.
    pyproject.toml declares requires-python >= 3.10.
    """

    def test_graphnode_import_succeeds(self):
        """GraphNode should import on Python 3.10+."""
        # This test verifies the fix - import should not fail
        from hypergraph.nodes.graph_node import GraphNode
        assert GraphNode is not None

    def test_graphnode_map_over_returns_graphnode(self):
        """GraphNode.map_over() should return a GraphNode instance."""
        @node(output_name="y")
        def double(x: int) -> int:
            return x * 2

        inner = Graph([double], name="inner")
        gn = inner.as_node()
        mapped = gn.map_over("x")

        # Verify it's still a GraphNode
        from hypergraph.nodes.graph_node import GraphNode
        assert isinstance(mapped, GraphNode)

    def test_graphnode_with_inputs_returns_graphnode(self):
        """GraphNode.with_inputs() should return a GraphNode instance."""
        @node(output_name="y")
        def double(x: int) -> int:
            return x * 2

        inner = Graph([double], name="inner")
        gn = inner.as_node()
        renamed = gn.with_inputs(x="z")

        from hypergraph.nodes.graph_node import GraphNode
        assert isinstance(renamed, GraphNode)


class TestAsyncSuperstepStateSafety:
    """Bug 2: Stale state in async superstep.

    All concurrent nodes in a superstep read from the same state object.
    If they share inputs, they can see stale data.
    """

    @pytest.mark.asyncio
    async def test_concurrent_nodes_see_consistent_input_versions(self):
        """All nodes in superstep should see same input version snapshot."""
        from hypergraph.runners.async_ import AsyncRunner

        call_order = []

        @node(output_name="a")
        async def node_a(x: int) -> int:
            call_order.append("a")
            return x + 1

        @node(output_name="b")
        async def node_b(x: int) -> int:
            call_order.append("b")
            return x + 2

        # Both nodes consume x, run concurrently
        graph = Graph([node_a, node_b])
        runner = AsyncRunner()
        result = await runner.run(graph, {"x": 10})

        # Both should see x=10
        assert result["a"] == 11  # 10 + 1
        assert result["b"] == 12  # 10 + 2


class TestRenameChaining:
    """Bug 3: Rename chaining breaks execution.

    Multiple with_inputs() calls don't compose correctly:
    node.with_inputs(a="x").with_inputs(x="z") should map z -> a
    but it maps z -> x instead.
    """

    def test_rename_chaining_execution(self):
        """Chained renames should compose transitively."""
        @node(output_name="result")
        def add_one(a: int) -> int:
            return a + 1

        # Chain renames: a -> x -> z
        chained = add_one.with_inputs(a="x").with_inputs(x="z")

        # Should have input "z" that maps to original "a"
        assert chained.inputs == ("z",)

        # Execute in graph
        graph = Graph([chained])
        runner = SyncRunner()
        result = runner.run(graph, {"z": 10})

        # Should call add_one(a=10)
        assert result["result"] == 11

    def test_triple_rename_chaining(self):
        """Three-level rename chaining should work."""
        @node(output_name="result")
        def double(x: int) -> int:
            return x * 2

        # Chain: x -> a -> b -> c
        tripled = double.with_inputs(x="a").with_inputs(a="b").with_inputs(b="c")

        assert tripled.inputs == ("c",)

        graph = Graph([tripled])
        runner = SyncRunner()
        result = runner.run(graph, {"c": 5})

        assert result["result"] == 10  # 5 * 2


class TestFunctionNodeDefaultsWithRenames:
    """Bug 4: FunctionNode.defaults ignores renames.

    FunctionNode.defaults returns original param names,
    but has_default_for/get_default_for expect renamed names.
    """

    def test_defaults_uses_renamed_names(self):
        """FunctionNode.defaults should use renamed parameter names."""
        @node(output_name="result", rename_inputs={"x": "input_value"})
        def func_with_default(x: int = 10) -> int:
            return x

        # defaults should use renamed name
        assert "input_value" in func_with_default.defaults
        assert func_with_default.defaults["input_value"] == 10

    def test_has_default_for_renamed_param(self):
        """has_default_for should work with renamed parameter names."""
        @node(output_name="result", rename_inputs={"x": "input_value"})
        def func_with_default(x: int = 10) -> int:
            return x

        # Should find default using renamed name
        assert func_with_default.has_default_for("input_value") is True
        # Original name should NOT work (it's been renamed)
        assert func_with_default.has_default_for("x") is False

    def test_get_default_for_renamed_param(self):
        """get_default_for should work with renamed parameter names."""
        @node(output_name="result", rename_inputs={"x": "my_param"})
        def func_with_default(x: int = 42) -> int:
            return x

        # Should get default using renamed name
        assert func_with_default.get_default_for("my_param") == 42

    def test_defaults_after_with_inputs(self):
        """defaults should reflect names after with_inputs() chaining."""
        @node(output_name="result")
        def func_with_default(x: int = 5) -> int:
            return x

        renamed = func_with_default.with_inputs(x="z")

        # z should have the default, not x
        assert renamed.has_default_for("z") is True
        assert renamed.has_default_for("x") is False
        assert renamed.get_default_for("z") == 5


class TestValueComparisonSafety:
    """Bug 5: Value comparison fails for numpy-like arrays.

    GraphState.update_value uses `!=` which raises for numpy arrays.
    """

    def test_update_value_with_array_like(self):
        """update_value should handle array-like values without raising."""
        from hypergraph.runners._shared.types import GraphState

        class ArrayLike:
            """Mock array-like that raises on truth test."""
            def __init__(self, data):
                self.data = data

            def __ne__(self, other):
                # Return array-like result (ambiguous truth value)
                return [a != b for a, b in zip(self.data, other.data, strict=False)]

            def __bool__(self):
                raise ValueError("Ambiguous truth value")

        state = GraphState()
        arr1 = ArrayLike([1, 2, 3])
        arr2 = ArrayLike([1, 2, 3])

        # Should not raise
        state.update_value("arr", arr1)
        state.update_value("arr", arr2)

    def test_update_value_with_same_object(self):
        """update_value with same object should not increment version."""
        from hypergraph.runners._shared.types import GraphState

        state = GraphState()
        obj = {"key": "value"}

        state.update_value("x", obj)
        v1 = state.get_version("x")

        # Same object reference
        state.update_value("x", obj)
        v2 = state.get_version("x")

        assert v1 == v2  # Version should not change for same object


class TestGraphNodeInputTypeConsistency:
    """Edge 1: GraphNode input type returns arbitrary match.

    If multiple inner nodes share an input with different types,
    get_input_type returns first found (dict ordering dependent).
    """

    def test_input_type_consistency_warning(self):
        """Graph should warn/error if shared inputs have inconsistent types."""
        # This tests the design choice: we should validate at graph construction
        @node(output_name="out1")
        def node_int(x: int) -> int:
            return x

        @node(output_name="out2")
        def node_str(x: str) -> str:
            return x

        # Both consume 'x' but with different types
        # This should either raise or warn during validation
        # For now, test the current behavior
        inner = Graph([node_int, node_str], name="inner")
        gn = inner.as_node()

        # get_input_type should return a consistent result
        # (currently returns first match - test documents behavior)
        t = gn.get_input_type("x")
        assert t in (int, str)  # Documents current behavior


class TestSelectParameterValidation:
    """Edge 2: Invalid select parameter handling.

    Runner.run() with select=["typo_output"] silently returns empty by default,
    warns with on_missing="warn", errors with on_missing="error".
    """

    def test_invalid_select_ignored_by_default(self):
        """Invalid select parameter silently omitted with default on_missing='ignore'."""
        @node(output_name="result")
        def identity(x: int) -> int:
            return x

        graph = Graph([identity])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 10}, select=["typo_result"])
        assert result.values == {}

    def test_invalid_select_warns_with_on_missing(self):
        """Invalid select parameter warns with on_missing='warn'."""
        @node(output_name="result")
        def identity(x: int) -> int:
            return x

        graph = Graph([identity])
        runner = SyncRunner()

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            runner.run(graph, {"x": 10}, select=["typo_result"], on_missing="warn")

            assert len(w) >= 1
            assert any("typo_result" in str(warning.message) for warning in w)

    def test_valid_select_no_warning(self):
        """Valid select parameter should not warn."""
        @node(output_name="result")
        def identity(x: int) -> int:
            return x

        graph = Graph([identity])
        runner = SyncRunner()

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = runner.run(graph, {"x": 10}, select=["result"])

            # No warnings for valid select
            assert len(w) == 0
            assert result["result"] == 10


class TestGraphNodeNameValidation:
    """Edge 3: GraphNode name can bypass separator validation.

    Graph validates no '.' or '/' in name, but as_node(name="foo.bar")
    can create GraphNode with reserved characters.
    """

    def test_graphnode_name_cannot_contain_dot(self):
        """GraphNode name should not contain '.'."""
        @node(output_name="y")
        def double(x: int) -> int:
            return x * 2

        inner = Graph([double], name="inner")

        with pytest.raises(ValueError) as exc_info:
            inner.as_node(name="foo.bar")

        assert "." in str(exc_info.value) or "reserved" in str(exc_info.value).lower()

    def test_graphnode_name_cannot_contain_slash(self):
        """GraphNode name should not contain '/'."""
        @node(output_name="y")
        def double(x: int) -> int:
            return x * 2

        inner = Graph([double], name="inner")

        with pytest.raises(ValueError) as exc_info:
            inner.as_node(name="foo/bar")

        assert "/" in str(exc_info.value) or "reserved" in str(exc_info.value).lower()


class TestRenameErrorMessages:
    """Test that rename error messages show full rename chain."""

    def test_error_shows_single_rename(self):
        """Error message shows single rename."""
        from hypergraph.nodes._rename import RenameError

        @node(output_name="result")
        def func(x: int) -> int:
            return x

        renamed = func.with_inputs(x="y")

        with pytest.raises(RenameError) as exc_info:
            renamed.with_inputs(x="z")  # x doesn't exist anymore

        error_msg = str(exc_info.value)
        assert "x" in error_msg
        assert "y" in error_msg
        assert "renamed" in error_msg.lower()

    def test_error_shows_full_rename_chain(self):
        """Error message shows full rename chain (a→x→z)."""
        from hypergraph.nodes._rename import RenameError

        @node(output_name="result")
        def func(a: int) -> int:
            return a

        # Chain: a -> x -> z
        chained = func.with_inputs(a="x").with_inputs(x="z")

        with pytest.raises(RenameError) as exc_info:
            chained.with_inputs(a="w")  # a doesn't exist anymore

        error_msg = str(exc_info.value)
        # Should show full chain a→x→z
        assert "a" in error_msg
        assert "x" in error_msg
        assert "z" in error_msg
        assert "→" in error_msg  # Shows chain notation


class TestInputValidationOverrides:
    """Edge 4: Input validation allows overriding internal edge values.

    Runner accepts any values dict without checking for edge-produced names.
    This bypasses graph.bind() which explicitly forbids this.
    """

    def test_cannot_override_edge_produced_values(self):
        """Cannot provide values for edge-produced outputs in runner.run()."""
        @node(output_name="intermediate")
        def step1(x: int) -> int:
            return x + 1

        @node(output_name="result")
        def step2(intermediate: int) -> int:
            return intermediate * 2

        graph = Graph([step1, step2])
        runner = SyncRunner()

        # Trying to override 'intermediate' which is produced by step1
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            runner.run(graph, {"x": 10, "intermediate": 999})

            # Should at least warn about overriding internal value
            assert len(w) >= 1
            assert any("intermediate" in str(warning.message) for warning in w)


class TestMultiCycleEntryPointValidation:
    """Explicit entrypoint must still validate other independent cycles."""

    def test_explicit_entrypoint_validates_other_cycles(self):
        """Providing entrypoint for cycle A must still check cycle B."""

        @node(output_name="a")
        def node_a(b: int) -> int:
            return b + 1

        @node(output_name="b")
        def node_b(a: int) -> int:
            return a + 1

        @route(targets=["node_a", END])
        def gate_ab(a: int) -> str:
            return END if a > 3 else "node_a"

        @node(output_name="x")
        def node_x(y: int) -> int:
            return y + 1

        @node(output_name="y")
        def node_y(x: int) -> int:
            return x + 1

        @route(targets=["node_x", END])
        def gate_xy(x: int) -> str:
            return END if x > 3 else "node_x"

        graph = Graph([node_a, node_b, gate_ab, node_x, node_y, gate_xy])
        runner = SyncRunner()

        # Cycle A entrypoint satisfied (b=1 for node_a), but cycle B has no entry
        from hypergraph.exceptions import MissingInputError
        with pytest.raises((ValueError, MissingInputError)):
            runner.run(graph, {"b": 1}, entrypoint="node_a")

    def test_explicit_entrypoint_passes_with_both_cycles_satisfied(self):
        """Both cycles satisfied: explicit on A, implicit on B."""

        @node(output_name="a")
        def node_a(b: int) -> int:
            return b + 1

        @node(output_name="b")
        def node_b(a: int) -> int:
            return a + 1

        @route(targets=["node_a", END])
        def gate_ab(a: int) -> str:
            return END if a > 3 else "node_a"

        @node(output_name="x")
        def node_x(y: int) -> int:
            return y + 1

        @node(output_name="y")
        def node_y(x: int) -> int:
            return x + 1

        @route(targets=["node_x", END])
        def gate_xy(x: int) -> str:
            return END if x > 3 else "node_x"

        graph = Graph([node_a, node_b, gate_ab, node_x, node_y, gate_xy])
        runner = SyncRunner()

        # Both cycles satisfied (node_a needs b, node_x needs y)
        result = runner.run(graph, {"b": 1, "y": 1}, entrypoint="node_a")
        assert result.status.value == "completed"


class TestDefaultedCycleParams:
    """Cycle params with defaults should not require entrypoint values."""

    def test_defaulted_cycle_param_excluded_from_entrypoint(self):
        """If a cycle param has a default, it's not required for entry."""

        @node(output_name="b")
        def node_a(a: int) -> int:
            return a + 1

        @node(output_name="a")
        def node_b(b: int, extra: int = 0) -> int:
            return b + extra

        @route(targets=["node_a", END])
        def gate(a: int) -> str:
            return END if a > 3 else "node_a"

        graph = Graph([node_a, node_b, gate])

        # extra has a default, so node_b's entrypoint should only need 'b'
        # (not 'b' AND 'extra')
        ep = graph.inputs.entrypoints
        if "node_b" in ep:
            assert "extra" not in ep["node_b"]


class TestEarlyOnMissingValidation:
    """on_missing should be validated eagerly, even when no outputs are missing."""

    def test_invalid_on_missing_fails_even_when_all_outputs_present(self):
        """Invalid on_missing raises immediately, not only when outputs miss."""

        @node(output_name="result")
        def identity(x: int) -> int:
            return x

        graph = Graph([identity])
        runner = SyncRunner()

        with pytest.raises(ValueError, match="Invalid on_missing"):
            runner.run(graph, {"x": 10}, select=["result"], on_missing="raise")
