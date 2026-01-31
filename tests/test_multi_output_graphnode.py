"""Tests for multi-output GraphNode wiring in outer graphs.

Regression tests for a bug where NetworkX DiGraph silently overwrites edges
when multiple values flow between the same node pair (e.g., inner graph
produces 'a' and 'b', both consumed by one downstream node).
"""

import pytest

from hypergraph import Graph, node
from hypergraph.runners import AsyncRunner, SyncRunner
from hypergraph.runners._shared.types import RunStatus


# --- Inner graph nodes ---


@node(output_name="a")
def compute_a(x: int) -> int:
    return x + 1


@node(output_name="b")
def compute_b(x: int) -> int:
    return x * 10


@node(output_name="a")
async def async_compute_a(x: int) -> int:
    return x + 1


@node(output_name="b")
async def async_compute_b(x: int) -> int:
    return x * 10


# --- Outer graph consumers ---


@node(output_name="final")
def consume_both(a: int, b: int) -> dict:
    return {"a": a, "b": b}


@node(output_name="final")
def consume_both_lists(a: list, b: list) -> dict:
    return {"a": a, "b": b}


# ============================================================
# Basic multi-output GraphNode wiring
# ============================================================


class TestMultiOutputGraphNode:
    """Multi-output inner graph as_node() should wire all outputs."""

    def test_sync_two_outputs_consumed_by_one_node(self):
        """Both outputs from inner graph reach the downstream consumer."""
        inner = Graph([compute_a, compute_b], name="inner")
        outer = Graph([inner.as_node(), consume_both])
        runner = SyncRunner()
        result = runner.run(outer, {"x": 5})
        assert result.status == RunStatus.COMPLETED
        assert result["final"] == {"a": 6, "b": 50}

    @pytest.mark.asyncio
    async def test_async_two_outputs_consumed_by_one_node(self):
        inner = Graph([async_compute_a, async_compute_b], name="inner")
        outer = Graph([inner.as_node(), consume_both])
        runner = AsyncRunner()
        result = await runner.run(outer, {"x": 5})
        assert result.status == RunStatus.COMPLETED
        assert result["final"] == {"a": 6, "b": 50}

    def test_graph_edge_inference_sees_all_outputs(self):
        """The outer graph should not require 'a' or 'b' as external inputs."""
        inner = Graph([compute_a, compute_b], name="inner")
        outer = Graph([inner.as_node(), consume_both])
        # 'a' and 'b' should NOT be in required inputs — they come from inner
        assert "a" not in outer.inputs.required
        assert "b" not in outer.inputs.required
        assert "x" in outer.inputs.required


# ============================================================
# Multi-output GraphNode + map_over
# ============================================================


class TestMultiOutputGraphNodeMapOver:
    """Multi-output inner graph with map_over."""

    def test_sync_map_over_two_outputs(self):
        inner = Graph([compute_a, compute_b], name="inner")
        gn = inner.as_node().map_over("x")
        outer = Graph([gn, consume_both_lists])
        runner = SyncRunner()
        result = runner.run(outer, {"x": [1, 2, 3]})
        assert result.status == RunStatus.COMPLETED
        assert result["final"] == {"a": [2, 3, 4], "b": [10, 20, 30]}

    def test_sync_map_over_two_outputs_continue(self):
        """Continue mode with multi-output: None placeholder per output."""

        @node(output_name="a")
        def a_or_fail(x: int) -> int:
            if x < 0:
                raise ValueError("negative")
            return x + 1

        @node(output_name="b")
        def b_always(x: int) -> int:
            # This also fails when a_or_fail fails since they're in same inner graph
            if x < 0:
                raise ValueError("negative")
            return x * 10

        inner = Graph([a_or_fail, b_always], name="inner")
        gn = inner.as_node().map_over("x", error_handling="continue")
        outer = Graph([gn, consume_both_lists])
        runner = SyncRunner()
        result = runner.run(outer, {"x": [-1, 2, 3]})
        assert result.status == RunStatus.COMPLETED
        assert result["final"] == {"a": [None, 3, 4], "b": [None, 20, 30]}


# ============================================================
# Multi-output with partial consumption
# ============================================================


class TestMultiOutputPartialConsumption:
    """Not all outputs need to be consumed."""

    def test_sync_consume_only_one_of_two_outputs(self):
        """Downstream node only uses 'a', ignores 'b'."""

        @node(output_name="result")
        def use_a(a: int) -> int:
            return a * 100

        inner = Graph([compute_a, compute_b], name="inner")
        outer = Graph([inner.as_node(), use_a])
        runner = SyncRunner()
        result = runner.run(outer, {"x": 5})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == 600

    def test_sync_two_consumers_each_use_one_output(self):
        """Two downstream nodes each consume a different output."""

        @node(output_name="ra")
        def use_a(a: int) -> int:
            return a * 100

        @node(output_name="rb")
        def use_b(b: int) -> int:
            return b + 1

        inner = Graph([compute_a, compute_b], name="inner")
        outer = Graph([inner.as_node(), use_a, use_b])
        runner = SyncRunner()
        result = runner.run(outer, {"x": 5})
        assert result.status == RunStatus.COMPLETED
        assert result["ra"] == 600
        assert result["rb"] == 51
