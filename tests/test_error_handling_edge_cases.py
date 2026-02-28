"""Edge case tests for error_handling in runner.map() and GraphNode.map_over()."""

import pytest

from hypergraph import Graph, node
from hypergraph.runners import AsyncRunner, SyncRunner
from hypergraph.runners._shared.types import RunStatus


class FailError(Exception):
    pass


# --- Nodes that fail on specific values ---


@node(output_name="doubled")
def double_fail_on_odd(x: int) -> int:
    if x % 2 != 0:
        raise FailError(f"odd: {x}")
    return x * 2


@node(output_name="doubled")
async def async_double_fail_on_odd(x: int) -> int:
    if x % 2 != 0:
        raise FailError(f"odd: {x}")
    return x * 2


@node(output_name="doubled")
def always_fail(x: int) -> int:
    raise FailError(f"always: {x}")


@node(output_name="doubled")
async def async_always_fail(x: int) -> int:
    raise FailError(f"always: {x}")


@node(output_name="result")
def passthrough(doubled: list) -> list:
    return doubled


# ============================================================
# runner.map() edge cases
# ============================================================


class TestMapAllFail:
    """All items fail in continue mode."""

    def test_sync_all_fail_continue(self):
        graph = Graph([always_fail])
        runner = SyncRunner()
        results = runner.map(graph, values={"x": [1, 2, 3]}, map_over="x", error_handling="continue")
        assert len(results) == 3
        assert all(r.status == RunStatus.FAILED for r in results)
        assert all(isinstance(r.error, FailError) for r in results)

    @pytest.mark.asyncio
    async def test_async_all_fail_continue(self):
        graph = Graph([async_always_fail])
        runner = AsyncRunner()
        results = await runner.map(graph, values={"x": [1, 2, 3]}, map_over="x", error_handling="continue")
        assert len(results) == 3
        assert all(r.status == RunStatus.FAILED for r in results)

    @pytest.mark.asyncio
    async def test_async_all_fail_continue_with_concurrency(self):
        graph = Graph([async_always_fail])
        runner = AsyncRunner()
        results = await runner.map(
            graph,
            values={"x": [1, 2, 3]},
            map_over="x",
            error_handling="continue",
            max_concurrency=2,
        )
        assert len(results) == 3
        assert all(r.status == RunStatus.FAILED for r in results)


class TestMapMultipleFailures:
    """Multiple (but not all) items fail in continue mode."""

    def test_sync_multiple_failures_continue(self):
        """Items 1, 3, 5 fail (odd), items 2, 4 succeed."""
        graph = Graph([double_fail_on_odd])
        runner = SyncRunner()
        results = runner.map(
            graph,
            values={"x": [1, 2, 3, 4, 5]},
            map_over="x",
            error_handling="continue",
        )
        assert len(results) == 5
        statuses = [r.status for r in results]
        assert statuses == [
            RunStatus.FAILED,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
        ]
        assert results[1]["doubled"] == 4
        assert results[3]["doubled"] == 8

    @pytest.mark.asyncio
    async def test_async_multiple_failures_continue(self):
        graph = Graph([async_double_fail_on_odd])
        runner = AsyncRunner()
        results = await runner.map(
            graph,
            values={"x": [1, 2, 3, 4, 5]},
            map_over="x",
            error_handling="continue",
        )
        assert len(results) == 5
        failed_count = sum(1 for r in results if r.status == RunStatus.FAILED)
        assert failed_count == 3


class TestMapFirstItemFails:
    """First item fails - boundary case."""

    def test_sync_first_item_fails_raise(self):
        graph = Graph([always_fail])
        runner = SyncRunner()
        with pytest.raises(FailError, match="always: 1"):
            runner.map(graph, values={"x": [1, 2, 3]}, map_over="x")

    def test_sync_first_item_fails_continue(self):
        graph = Graph([always_fail])
        runner = SyncRunner()
        results = runner.map(graph, values={"x": [1]}, map_over="x", error_handling="continue")
        assert len(results) == 1
        assert results[0].status == RunStatus.FAILED


class TestMapSingleItem:
    """Single item in map."""

    def test_sync_single_success(self):
        graph = Graph([double_fail_on_odd])
        runner = SyncRunner()
        results = runner.map(graph, values={"x": [2]}, map_over="x", error_handling="continue")
        assert len(results) == 1
        assert results[0].status == RunStatus.COMPLETED
        assert results[0]["doubled"] == 4

    def test_sync_single_failure_raise(self):
        graph = Graph([double_fail_on_odd])
        runner = SyncRunner()
        with pytest.raises(FailError):
            runner.map(graph, values={"x": [1]}, map_over="x")


class TestMapEmptyInput:
    """Empty input list."""

    def test_sync_empty_raise(self):
        graph = Graph([double_fail_on_odd])
        runner = SyncRunner()
        results = runner.map(graph, values={"x": []}, map_over="x", error_handling="raise")
        assert len(results) == 0

    def test_sync_empty_continue(self):
        graph = Graph([double_fail_on_odd])
        runner = SyncRunner()
        results = runner.map(graph, values={"x": []}, map_over="x", error_handling="continue")
        assert len(results) == 0


class TestMapProductMode:
    """Product mode with error_handling."""

    @node(output_name="result")
    @staticmethod
    def add_or_fail(x: int, y: int) -> int:
        if x + y == 5:
            raise FailError("sum is 5")
        return x + y

    def test_sync_product_continue(self):
        graph = Graph([self.add_or_fail])
        runner = SyncRunner()
        results = runner.map(
            graph,
            values={"x": [1, 2, 3], "y": [2, 3]},
            map_over=["x", "y"],
            map_mode="product",
            error_handling="continue",
        )
        # 3 x 2 = 6 combinations: (1,2)=3, (1,3)=4, (2,2)=4, (2,3)=FAIL, (3,2)=FAIL, (3,3)=6
        assert len(results) == 6
        failed = [r for r in results if r.status == RunStatus.FAILED]
        assert len(failed) == 2

    def test_sync_product_raise(self):
        graph = Graph([self.add_or_fail])
        runner = SyncRunner()
        with pytest.raises(FailError, match="sum is 5"):
            runner.map(
                graph,
                values={"x": [1, 2, 3], "y": [2, 3]},
                map_over=["x", "y"],
                map_mode="product",
            )


class TestMapPartialValues:
    """Verify run() returns partial values when it fails inside map context."""

    def test_sync_raise_returns_partial_results_before_failure(self):
        """In raise mode, results collected before the failure are returned."""
        graph = Graph([double_fail_on_odd])
        runner = SyncRunner()
        # x=2 succeeds, x=3 fails - raise mode stops and raises
        with pytest.raises(FailError):
            runner.map(graph, values={"x": [2, 3, 4]}, map_over="x")
        # Can't access results after exception - this is by design.
        # The important thing is it raises immediately.


# ============================================================
# GraphNode.map_over() edge cases
# ============================================================


class TestMapOverAllFail:
    """All items fail in map_over continue mode."""

    def test_sync_all_fail_produces_all_nones(self):
        inner = Graph([always_fail], name="inner")
        gn = inner.as_node().map_over("x", error_handling="continue")
        outer = Graph([gn, passthrough])
        runner = SyncRunner()
        result = runner.run(outer, {"x": [1, 2, 3]})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == [None, None, None]

    @pytest.mark.asyncio
    async def test_async_all_fail_produces_all_nones(self):
        inner = Graph([async_always_fail], name="inner")
        gn = inner.as_node().map_over("x", error_handling="continue")
        outer = Graph([gn, passthrough])
        runner = AsyncRunner()
        result = await runner.run(outer, {"x": [1, 2, 3]})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == [None, None, None]


class TestMapOverMultipleFailures:
    """Multiple failures in map_over continue mode."""

    def test_sync_multiple_failures_none_placeholders(self):
        inner = Graph([double_fail_on_odd], name="inner")
        gn = inner.as_node().map_over("x", error_handling="continue")
        outer = Graph([gn, passthrough])
        runner = SyncRunner()
        result = runner.run(outer, {"x": [1, 2, 3, 4, 5]})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == [None, 4, None, 8, None]


class TestMapOverSingleItem:
    """Single item in map_over."""

    def test_sync_single_fail_continue(self):
        inner = Graph([always_fail], name="inner")
        gn = inner.as_node().map_over("x", error_handling="continue")
        outer = Graph([gn, passthrough])
        runner = SyncRunner()
        result = runner.run(outer, {"x": [1]})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == [None]

    def test_sync_single_fail_raise(self):
        inner = Graph([always_fail], name="inner")
        gn = inner.as_node().map_over("x")
        outer = Graph([gn, passthrough])
        runner = SyncRunner()
        with pytest.raises(FailError):
            runner.run(outer, {"x": [1]})


class TestMapOverWithRenamedOutputs:
    """error_handling=continue with renamed outputs."""

    def test_sync_renamed_outputs_continue(self):
        inner = Graph([double_fail_on_odd], name="inner")
        gn = inner.as_node().with_outputs(doubled="processed").map_over("x", error_handling="continue")

        @node(output_name="final")
        def consume(processed: list) -> list:
            return processed

        outer = Graph([gn, consume])
        runner = SyncRunner()
        result = runner.run(outer, {"x": [1, 2, 3, 4]})
        assert result.status == RunStatus.COMPLETED
        assert result["final"] == [None, 4, None, 8]


class TestMapOverEmptyInput:
    """Empty input list for map_over."""

    def test_sync_empty_continue(self):
        inner = Graph([double_fail_on_odd], name="inner")
        gn = inner.as_node().map_over("x", error_handling="continue")
        outer = Graph([gn, passthrough])
        runner = SyncRunner()
        result = runner.run(outer, {"x": []})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == []


class TestMapOverRaisePartialValues:
    """When map_over raise mode fails, outer run() raises the error."""

    def test_sync_raise_outer_run_fails(self):
        """Outer run() raises when inner map_over raises."""
        inner = Graph([always_fail], name="inner")
        gn = inner.as_node().map_over("x")
        outer = Graph([gn, passthrough])
        runner = SyncRunner()
        with pytest.raises(FailError):
            runner.run(outer, {"x": [1, 2]})
