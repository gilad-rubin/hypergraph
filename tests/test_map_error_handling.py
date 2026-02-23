"""Tests for error_handling parameter in runner.map()."""

import pytest

from hypergraph import Graph, node
from hypergraph.runners import AsyncRunner, SyncRunner
from hypergraph.runners._shared.types import RunStatus


class CustomMapError(Exception):
    pass


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="doubled")
def double_or_fail(x: int) -> int:
    if x == 3:
        raise CustomMapError(f"cannot double {x}")
    return x * 2


@node(output_name="doubled")
async def async_double(x: int) -> int:
    return x * 2


@node(output_name="doubled")
async def async_double_or_fail(x: int) -> int:
    if x == 3:
        raise CustomMapError(f"cannot double {x}")
    return x * 2


class TestSyncMapErrorHandling:
    """SyncRunner.map() error_handling tests."""

    def test_raise_mode_stops_on_first_failure(self):
        graph = Graph([double_or_fail])
        runner = SyncRunner()
        with pytest.raises(CustomMapError, match="cannot double 3"):
            runner.map(graph, values={"x": [1, 2, 3, 4, 5]}, map_over="x")

    def test_raise_mode_is_default(self):
        graph = Graph([double_or_fail])
        runner = SyncRunner()
        with pytest.raises(CustomMapError):
            runner.map(graph, values={"x": [3]}, map_over="x")

    def test_continue_mode_collects_all_results(self):
        graph = Graph([double_or_fail])
        runner = SyncRunner()
        results = runner.map(
            graph,
            values={"x": [1, 2, 3, 4, 5]},
            map_over="x",
            error_handling="continue",
        )
        assert len(results) == 5
        assert results[0].status == RunStatus.COMPLETED
        assert results[0]["doubled"] == 2
        assert results[1].status == RunStatus.COMPLETED
        assert results[1]["doubled"] == 4
        assert results[2].status == RunStatus.FAILED
        assert isinstance(results[2].error, CustomMapError)
        assert results[3].status == RunStatus.COMPLETED
        assert results[3]["doubled"] == 8
        assert results[4].status == RunStatus.COMPLETED
        assert results[4]["doubled"] == 10

    def test_continue_mode_all_succeed(self):
        graph = Graph([double])
        runner = SyncRunner()
        results = runner.map(
            graph,
            values={"x": [1, 2, 3]},
            map_over="x",
            error_handling="continue",
        )
        assert all(r.status == RunStatus.COMPLETED for r in results)
        assert [r["doubled"] for r in results] == [2, 4, 6]

    def test_raise_mode_all_succeed(self):
        graph = Graph([double])
        runner = SyncRunner()
        results = runner.map(graph, values={"x": [1, 2, 3]}, map_over="x", error_handling="raise")
        assert all(r.status == RunStatus.COMPLETED for r in results)
        assert [r["doubled"] for r in results] == [2, 4, 6]


class TestAsyncMapErrorHandling:
    """AsyncRunner.map() error_handling tests."""

    @pytest.mark.asyncio
    async def test_raise_mode_stops_on_failure(self):
        graph = Graph([async_double_or_fail])
        runner = AsyncRunner()
        with pytest.raises(CustomMapError, match="cannot double 3"):
            await runner.map(graph, values={"x": [1, 2, 3, 4, 5]}, map_over="x")

    @pytest.mark.asyncio
    async def test_continue_mode_collects_all_results(self):
        graph = Graph([async_double_or_fail])
        runner = AsyncRunner()
        results = await runner.map(
            graph,
            values={"x": [1, 2, 3, 4, 5]},
            map_over="x",
            error_handling="continue",
        )
        assert len(results) == 5
        statuses = [r.status for r in results]
        assert statuses.count(RunStatus.FAILED) == 1
        assert statuses.count(RunStatus.COMPLETED) == 4
        # Find the failed one
        failed = [r for r in results if r.status == RunStatus.FAILED]
        assert len(failed) == 1
        assert isinstance(failed[0].error, CustomMapError)

    @pytest.mark.asyncio
    async def test_continue_mode_all_succeed(self):
        graph = Graph([async_double])
        runner = AsyncRunner()
        results = await runner.map(
            graph,
            values={"x": [1, 2, 3]},
            map_over="x",
            error_handling="continue",
        )
        assert all(r.status == RunStatus.COMPLETED for r in results)

    @pytest.mark.asyncio
    async def test_raise_mode_with_max_concurrency(self):
        graph = Graph([async_double_or_fail])
        runner = AsyncRunner()
        with pytest.raises(CustomMapError):
            await runner.map(
                graph,
                values={"x": [1, 2, 3, 4, 5]},
                map_over="x",
                max_concurrency=2,
            )

    @pytest.mark.asyncio
    async def test_continue_mode_with_max_concurrency(self):
        graph = Graph([async_double_or_fail])
        runner = AsyncRunner()
        results = await runner.map(
            graph,
            values={"x": [1, 2, 3, 4, 5]},
            map_over="x",
            max_concurrency=2,
            error_handling="continue",
        )
        assert len(results) == 5
        failed = [r for r in results if r.status == RunStatus.FAILED]
        assert len(failed) == 1
