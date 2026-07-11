"""Regression tests: map(error_handling="continue") must survive PRE-RUN item errors.

Bug: sync ``map()`` called ``self.run(...)`` bare, so an exception raised
before run()'s internal try block (e.g. per-item input validation) escaped
and aborted the whole batch — even with ``error_handling="continue"``.
Async ``map()`` and both ``map_iter`` variants already converted such
exceptions into FAILED item results. Sync ``map()`` was the sole outlier.

Sync/async parity: both runners must produce the same per-item statuses.
"""

import pytest

from hypergraph import AsyncRunner, Graph, MissingInputError, RunStatus, SyncRunner, node


@node(output_name="total")
def add(x: int, y: int) -> int:
    return x + y


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


def _flaky_run_wrapper(real_run, failing_index):
    """Wrap a runner's ``run`` so exactly one item raises BEFORE execution.

    Simulates a pre-run validation error (the kind run() raises before its
    internal try block) for a single item, so the batch has mixed outcomes.
    """

    def wrapper(*args, **kwargs):
        if kwargs.get("_item_index") == failing_index:
            raise MissingInputError(missing=["y"], provided=["x"])
        return real_run(*args, **kwargs)

    return wrapper


def _flaky_async_run_wrapper(real_run, failing_index):
    async def wrapper(*args, **kwargs):
        if kwargs.get("_item_index") == failing_index:
            raise MissingInputError(missing=["y"], provided=["x"])
        return await real_run(*args, **kwargs)

    return wrapper


class TestSyncMapContinuePreRunErrors:
    def test_missing_required_input_continue_returns_failed_items(self):
        """B2.1: pre-run validation error (missing input) with continue —
        the batch completes and every item carries FAILED + the error."""
        graph = Graph([add])
        runner = SyncRunner()

        # "y" is required but never provided → each item's run() raises
        # MissingInputError before its execution try block.
        result = runner.map(graph, {"x": [1, 2, 3]}, map_over="x", error_handling="continue")

        assert len(result.results) == 3
        for item in result.results:
            assert item.status == RunStatus.FAILED
            assert isinstance(item.error, MissingInputError)

    def test_single_item_pre_run_error_continue_mixed_statuses(self):
        """B2.1: item 2 raises pre-run; items 1/3 complete; batch survives."""
        graph = Graph([double])
        runner = SyncRunner()
        runner.run = _flaky_run_wrapper(runner.run, failing_index=1)

        result = runner.map(graph, {"x": [1, 2, 3]}, map_over="x", error_handling="continue")

        statuses = [r.status for r in result.results]
        assert statuses == [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.COMPLETED]
        assert isinstance(result.results[1].error, MissingInputError)
        assert result.results[0]["doubled"] == 2
        assert result.results[2]["doubled"] == 6

    def test_pre_run_error_raise_mode_still_raises(self):
        """B2.3: error_handling="raise" behavior is unchanged — still raises."""
        graph = Graph([add])
        runner = SyncRunner()

        with pytest.raises(MissingInputError):
            runner.map(graph, {"x": [1, 2, 3]}, map_over="x", error_handling="raise")


class TestAsyncMapContinuePreRunErrorsParity:
    @pytest.mark.asyncio
    async def test_missing_required_input_continue_parity(self):
        """B2.2: async map produces the same per-item statuses as sync map."""
        graph = Graph([add])

        sync_result = SyncRunner().map(graph, {"x": [1, 2, 3]}, map_over="x", error_handling="continue")
        async_result = await AsyncRunner().map(graph, {"x": [1, 2, 3]}, map_over="x", error_handling="continue")

        sync_statuses = [r.status for r in sync_result.results]
        async_statuses = [r.status for r in async_result.results]
        assert sync_statuses == async_statuses == [RunStatus.FAILED] * 3
        for item in async_result.results:
            assert isinstance(item.error, MissingInputError)

    @pytest.mark.asyncio
    async def test_single_item_pre_run_error_continue_parity(self):
        """B2.2: mixed-status scenario matches across sync and async."""
        graph = Graph([double])

        sync_runner = SyncRunner()
        sync_runner.run = _flaky_run_wrapper(sync_runner.run, failing_index=1)
        sync_result = sync_runner.map(graph, {"x": [1, 2, 3]}, map_over="x", error_handling="continue")

        async_runner = AsyncRunner()
        async_runner.run = _flaky_async_run_wrapper(async_runner.run, failing_index=1)
        async_result = await async_runner.map(graph, {"x": [1, 2, 3]}, map_over="x", error_handling="continue")

        sync_statuses = [r.status for r in sync_result.results]
        async_statuses = [r.status for r in async_result.results]
        assert sync_statuses == async_statuses == [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.COMPLETED]

    @pytest.mark.asyncio
    async def test_pre_run_error_raise_mode_parity(self):
        """B2.3 parity: raise mode raises on both runners."""
        graph = Graph([add])

        with pytest.raises(MissingInputError):
            SyncRunner().map(graph, {"x": [1, 2, 3]}, map_over="x", error_handling="raise")

        with pytest.raises(MissingInputError):
            await AsyncRunner().map(graph, {"x": [1, 2, 3]}, map_over="x", error_handling="raise")
