"""Tests for deep nesting issues (red-team #10, #31).

Investigates scenarios that reportedly cause InfiniteLoopError or hangs:
- Scenario A: 4+ level nesting (sync) — PASSES
- Scenario B: Same inner graph used twice with renames — PASSES
- Scenario C: Node output name == input name — BUG CONFIRMED (InfiniteLoopError)
- Scenario D: GraphNode name collision with outer node (#31) — PASSES (validated)
"""

import pytest

from hypergraph import Graph, SyncRunner, node
from hypergraph.exceptions import InfiniteLoopError


# -- Shared fixtures --

@node(output_name="v1")
def add_one(x: int) -> int:
    return x + 1


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="x")
def transform_x(x: int) -> int:
    """Output name intentionally matches input name."""
    return x + 1


@node(output_name="result")
def identity(x: int) -> int:
    return x


# -- Scenario A: 4-level sync nesting --

class TestFourLevelNestingSync:
    """4-level nesting works in async (test_async_runner.py:616).
    Verify it also works in sync."""

    def test_four_level_chain(self):
        @node(output_name="l4")
        def level4(x: int) -> int:
            return x + 1

        @node(output_name="l3")
        def level3(l4: int) -> int:
            return l4 + 1

        @node(output_name="l2")
        def level2(l3: int) -> int:
            return l3 + 1

        @node(output_name="l1")
        def level1(l2: int) -> int:
            return l2 + 1

        g4 = Graph(nodes=[level4], name="g4")
        g3 = Graph(nodes=[g4.as_node(), level3], name="g3")
        g2 = Graph(nodes=[g3.as_node(), level2], name="g2")
        g1 = Graph(nodes=[g2.as_node(), level1], name="g1")

        runner = SyncRunner()
        result = runner.run(g1, {"x": 0})

        assert result["l4"] == 1
        assert result["l3"] == 2
        assert result["l2"] == 3
        assert result["l1"] == 4


# -- Scenario B: Same inner graph used twice --

class TestSameInnerGraphTwice:
    """Two GraphNode instances from the same inner graph in one outer graph."""

    def test_same_graph_different_renames(self):
        inner = Graph(nodes=[double], name="inner")

        node_a = (
            inner.as_node(name="inner_a")
            .with_inputs(x="a")
            .with_outputs(doubled="res_a")
        )
        node_b = (
            inner.as_node(name="inner_b")
            .with_inputs(x="b")
            .with_outputs(doubled="res_b")
        )

        outer = Graph(nodes=[node_a, node_b])
        runner = SyncRunner()
        result = runner.run(outer, {"a": 3, "b": 5})

        assert result["res_a"] == 6
        assert result["res_b"] == 10

    def test_same_graph_no_renames_conflict(self):
        """Without renames, two instances of same graph produce same output name.
        This should raise a validation error, not hang."""
        inner = Graph(nodes=[double], name="inner")

        with pytest.raises(Exception):
            # Both produce "doubled" — should be caught at build time
            Graph(nodes=[
                inner.as_node(name="inner_a"),
                inner.as_node(name="inner_b"),
            ])


# -- Scenario C: Output name == input name (BUG CONFIRMED) --

class TestOutputMatchesInput:
    """A node whose output has the same name as its input (SM-007).
    The staleness detector loops forever because it can't distinguish
    'new value produced by this node' from 'old input value'.

    Root cause: the runner sees output "x" already in state (from the input),
    considers the node's output stale, and re-executes it forever."""

    @pytest.mark.xfail(
        raises=AssertionError,
        reason="BUG: InfiniteLoopError when output name == input name (SM-007)",
        strict=True,
    )
    def test_single_node_output_equals_input(self):
        """transform_x: x -> x. Should run once and return x+1."""
        graph = Graph(nodes=[transform_x])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 0})
        assert result.status.name != "FAILED", f"Failed with: {result.error}"
        assert result["x"] == 1

    @pytest.mark.xfail(
        raises=AssertionError,
        reason="BUG: InfiniteLoopError when node output shadows its input (SM-007)",
        strict=True,
    )
    def test_chained_output_equals_input(self):
        """step1 takes val, produces val. step2 takes val, produces result.
        step1 triggers infinite loop because its output shadows its input."""

        @node(output_name="val")
        def step1(val: int) -> int:
            return val + 1

        @node(output_name="result")
        def step2(val: int) -> int:
            return val * 10

        graph = Graph(nodes=[step1, step2])
        runner = SyncRunner()
        result = runner.run(graph, {"val": 5})
        assert result.status.name != "FAILED", f"Failed with: {result.error}"
        assert result["result"] == 60  # (5+1) * 10


# -- Scenario D: GraphNode name collision (#31) --

class TestGraphNodeNameCollision:
    """A GraphNode with the same name as another node in the outer graph.
    Should raise a validation error at build time, not infinite loop."""

    def test_graphnode_name_matches_function_node(self):
        """Inner graph named same as an outer function node."""

        @node(output_name="inner_out")
        def inner_fn(x: int) -> int:
            return x + 1

        @node(output_name="outer_out")
        def compute(inner_out: int) -> int:
            return inner_out * 2

        inner = Graph(nodes=[inner_fn], name="compute")
        gn = inner.as_node()  # name="compute", same as the function node

        with pytest.raises(Exception):
            Graph(nodes=[gn, compute])

    def test_two_graphnodes_same_name(self):
        """Two GraphNodes with the same name should be caught."""

        @node(output_name="a")
        def fn_a(x: int) -> int:
            return x

        @node(output_name="b")
        def fn_b(x: int) -> int:
            return x

        g1 = Graph(nodes=[fn_a], name="shared")
        g2 = Graph(nodes=[fn_b], name="shared")

        with pytest.raises(Exception):
            Graph(nodes=[g1.as_node(), g2.as_node()])
