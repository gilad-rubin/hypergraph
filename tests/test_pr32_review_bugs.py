"""Tests targeting bugs identified in PR #32 code review.

Bug 1 (collect_as_lists): When a successful RunResult is missing an expected
output name after rename translation, nothing is appended to that output's list,
causing misaligned list lengths across collected outputs.

Bug 2 (partial state): _execute_graph sets _partial_state from the pre-superstep
state, so outputs produced by earlier nodes in the same superstep are lost.
"""

import pytest

from hypergraph import Graph, node
from hypergraph.runners import AsyncRunner, SyncRunner
from hypergraph.runners._shared.types import RunStatus


# ============================================================
# Bug 1: collect_as_lists missing output → list length mismatch
# ============================================================


class MissingOutputError(Exception):
    pass


@node(output_name="a")
def produce_a(x: int) -> int:
    return x + 1


@node(output_name="b")
def produce_b(x: int) -> int:
    return x * 10


@node(output_name="a")
def produce_a_or_fail(x: int) -> int:
    if x == 3:
        raise MissingOutputError(f"fail on {x}")
    return x + 1


@node(output_name="b")
def produce_b_or_fail(x: int) -> int:
    if x == 3:
        raise MissingOutputError(f"fail on {x}")
    return x * 10


@node(output_name="combined")
def combine(a: list, b: list) -> dict:
    return {"a": a, "b": b}


class TestCollectAsListsLengthConsistency:
    """All output lists from map_over must have equal lengths."""

    def test_multi_output_map_over_all_succeed(self):
        """When all items succeed, both output lists should have same length."""
        inner = Graph([produce_a, produce_b], name="inner")
        gn = inner.as_node().map_over("x")
        outer = Graph([gn, combine])
        runner = SyncRunner()
        result = runner.run(outer, {"x": [1, 2, 3]})
        assert result["combined"]["a"] == [2, 3, 4]
        assert result["combined"]["b"] == [10, 20, 30]

    def test_multi_output_map_over_continue_mode_lengths_match(self):
        """In continue mode, failed items should produce None for ALL outputs.

        This is the core bug: if an output name is missing from renamed_values
        for a successful result, that list gets shorter than the others.
        """
        inner = Graph([produce_a_or_fail, produce_b_or_fail], name="inner")
        gn = inner.as_node().map_over("x", error_handling="continue")
        outer = Graph([gn, combine])
        runner = SyncRunner()
        result = runner.run(outer, {"x": [1, 2, 3, 4]})
        a_list = result["combined"]["a"]
        b_list = result["combined"]["b"]
        # Both lists must be same length (4 items)
        assert len(a_list) == len(b_list) == 4
        # Item at index 2 (x=3) should be None for both
        assert a_list[2] is None
        assert b_list[2] is None

    def test_renamed_multi_output_map_over_lengths_match(self):
        """Renamed outputs should still produce equal-length lists."""
        inner = Graph([produce_a, produce_b], name="inner")
        gn = (
            inner.as_node().map_over("x")
            .with_outputs(a="alpha", b="beta")
        )

        @node(output_name="result")
        def use_renamed(alpha: list, beta: list) -> dict:
            return {"alpha": alpha, "beta": beta}

        outer = Graph([gn, use_renamed])
        runner = SyncRunner()
        result = runner.run(outer, {"x": [1, 2, 3]})
        assert len(result["result"]["alpha"]) == len(result["result"]["beta"]) == 3

    def test_runner_map_continue_multi_output_graph(self):
        """runner.map() with continue mode on a multi-output graph."""
        inner = Graph([produce_a_or_fail, produce_b_or_fail], name="inner")
        runner = SyncRunner()
        results = runner.map(
            inner,
            values={"x": [1, 2, 3, 4]},
            map_over="x",
            error_handling="continue",
        )
        assert len(results) == 4
        # x=3 fails
        assert results[2].status == RunStatus.FAILED
        # Other items succeed with both outputs
        for i in [0, 1, 3]:
            assert results[i].status == RunStatus.COMPLETED
            assert "a" in results[i].values
            assert "b" in results[i].values


# ============================================================
# Bug 2: partial state loses superstep outputs
# ============================================================


class SuperstepError(Exception):
    pass


@node(output_name="first_out")
def first_node(x: int) -> int:
    """Runs first in the superstep, succeeds."""
    return x + 100


@node(output_name="second_out")
def second_node_fails(x: int) -> int:
    """Runs second in same superstep, fails."""
    raise SuperstepError("boom")


class TestPartialStateFromSuperstep:
    """Partial values should include outputs from nodes that ran before the
    failure within the same superstep."""

    def test_partial_values_include_completed_node_in_same_superstep(self):
        """If node A succeeds and node B fails in the same superstep,
        partial_values should contain A's output.

        Both first_node and second_node_fails depend only on 'x',
        so they're in the same superstep.
        """
        graph = Graph([first_node, second_node_fails])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 1})
        assert result.status == RunStatus.FAILED
        # The key test: first_node ran before second_node_fails in the superstep.
        # Its output should be in partial_values.
        assert "first_out" in result.values, (
            "Partial state should include outputs from nodes that completed "
            "before the failure in the same superstep"
        )
        assert result.values["first_out"] == 101

    def test_partial_values_from_prior_superstep(self):
        """Outputs from completed supersteps should always be in partial values."""

        @node(output_name="step1_out")
        def step1(x: int) -> int:
            return x * 2

        @node(output_name="step2_out")
        def step2_fails(step1_out: int) -> int:
            raise SuperstepError("step2 boom")

        graph = Graph([step1, step2_fails])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5})
        assert result.status == RunStatus.FAILED
        # step1 ran in a prior superstep, so its output must be preserved
        assert "step1_out" in result.values
        assert result.values["step1_out"] == 10

    def test_partial_values_multi_superstep_chain(self):
        """In a 3-node chain A→B→C where C fails, both A and B outputs should
        be in partial values."""

        @node(output_name="a_out")
        def node_a(x: int) -> int:
            return x + 1

        @node(output_name="b_out")
        def node_b(a_out: int) -> int:
            return a_out * 2

        @node(output_name="c_out")
        def node_c(b_out: int) -> int:
            raise SuperstepError("node_c failed")

        graph = Graph([node_a, node_b, node_c])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5})
        assert result.status == RunStatus.FAILED
        assert result.values.get("a_out") == 6
        assert result.values.get("b_out") == 12

    @pytest.mark.asyncio
    async def test_async_partial_values_include_completed_in_superstep(self):
        """Same superstep partial-state bug should be tested for async runner."""

        @node(output_name="first_out")
        async def async_first(x: int) -> int:
            return x + 100

        @node(output_name="second_out")
        async def async_second_fails(x: int) -> int:
            raise SuperstepError("async boom")

        graph = Graph([async_first, async_second_fails])
        runner = AsyncRunner()
        result = await runner.run(graph, {"x": 1})
        assert result.status == RunStatus.FAILED
        assert "first_out" in result.values, (
            "Async partial state should include outputs from nodes that "
            "completed before the failure in the same superstep"
        )


# ============================================================
# Combined: partial values + continue mode interactions
# ============================================================


class TestPartialValuesInContinueMode:
    """Test that partial values work correctly with error_handling="continue"."""

    def test_map_continue_partial_values_present(self):
        """Each failed RunResult in continue mode should have partial values."""

        @node(output_name="step1_out")
        def step1(x: int) -> int:
            return x * 2

        @node(output_name="step2_out")
        def step2_maybe_fail(step1_out: int) -> int:
            if step1_out == 6:  # x=3
                raise SuperstepError("fail at step2")
            return step1_out + 1

        graph = Graph([step1, step2_maybe_fail])
        runner = SyncRunner()
        results = runner.map(
            graph,
            values={"x": [1, 2, 3, 4]},
            map_over="x",
            error_handling="continue",
        )
        assert len(results) == 4
        # x=3 fails at step2, but step1 completed
        failed = results[2]
        assert failed.status == RunStatus.FAILED
        # step1 output should be in partial values
        assert failed.values.get("step1_out") == 6
