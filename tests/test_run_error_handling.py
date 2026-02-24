"""Tests for error_handling parameter on runner.run()."""

import pytest

from hypergraph import Graph, node
from hypergraph.runners import AsyncRunner, RunStatus, SyncRunner


class CustomRunError(Exception):
    """Custom exception for testing."""

    pass


@node(output_name="result")
def succeeding_node(x: int) -> int:
    return x * 2


@node(output_name="result")
def failing_node(x: int) -> int:
    raise CustomRunError("intentional failure")


@node(output_name="step_a")
def step_a(x: int) -> int:
    return x + 100


@node(output_name="step_b")
def step_b(step_a: int) -> int:
    raise CustomRunError("step_b failed")


# === Sync Tests ===


class TestSyncRunRaiseMode:
    """Test error_handling='raise' (new default) for SyncRunner."""

    def test_run_raises_on_failure_by_default(self):
        graph = Graph([failing_node])
        runner = SyncRunner()
        with pytest.raises(CustomRunError, match="intentional failure"):
            runner.run(graph, {"x": 5})

    def test_run_raises_original_exception_type(self):
        graph = Graph([failing_node])
        runner = SyncRunner()
        with pytest.raises(CustomRunError):
            runner.run(graph, {"x": 5})

    def test_run_raises_with_clean_str(self):
        """str(e) should be the original message, no wrapper noise."""
        graph = Graph([failing_node])
        runner = SyncRunner()
        with pytest.raises(CustomRunError) as exc_info:
            runner.run(graph, {"x": 5})
        assert str(exc_info.value) == "intentional failure"

    def test_run_success_returns_result(self):
        """Successful runs still return RunResult."""
        graph = Graph([succeeding_node])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == 10

    def test_run_explicit_raise_same_as_default(self):
        graph = Graph([failing_node])
        runner = SyncRunner()
        with pytest.raises(CustomRunError, match="intentional failure"):
            runner.run(graph, {"x": 5}, error_handling="raise")


class TestSyncRunContinueMode:
    """Test error_handling='continue' for SyncRunner."""

    def test_continue_returns_failed_result(self):
        graph = Graph([failing_node])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5}, error_handling="continue")
        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomRunError)

    def test_continue_has_partial_values(self):
        """Partial values from nodes that succeeded before the failure."""
        graph = Graph([step_a, step_b])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5}, error_handling="continue")
        assert result.status == RunStatus.FAILED
        assert "step_a" in result.values
        assert result.values["step_a"] == 105

    def test_continue_preserves_error(self):
        graph = Graph([failing_node])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5}, error_handling="continue")
        assert isinstance(result.error, CustomRunError)
        assert str(result.error) == "intentional failure"


class TestSyncRunValidation:
    """Test error_handling parameter validation."""

    def test_invalid_error_handling_raises_value_error(self):
        graph = Graph([succeeding_node])
        runner = SyncRunner()
        with pytest.raises(ValueError, match="Invalid error_handling"):
            runner.run(graph, {"x": 5}, error_handling="invalid")

    def test_invalid_error_handling_includes_how_to_fix(self):
        graph = Graph([succeeding_node])
        runner = SyncRunner()
        with pytest.raises(ValueError, match="How to fix"):
            runner.run(graph, {"x": 5}, error_handling="invalid")


# === Async Tests ===


class TestAsyncRunRaiseMode:
    """Test error_handling='raise' (new default) for AsyncRunner."""

    @pytest.mark.asyncio
    async def test_run_raises_on_failure_by_default(self):
        graph = Graph([failing_node])
        runner = AsyncRunner()
        with pytest.raises(CustomRunError, match="intentional failure"):
            await runner.run(graph, {"x": 5})

    @pytest.mark.asyncio
    async def test_run_raises_original_exception_type(self):
        graph = Graph([failing_node])
        runner = AsyncRunner()
        with pytest.raises(CustomRunError):
            await runner.run(graph, {"x": 5})

    @pytest.mark.asyncio
    async def test_run_raises_with_clean_str(self):
        graph = Graph([failing_node])
        runner = AsyncRunner()
        with pytest.raises(CustomRunError) as exc_info:
            await runner.run(graph, {"x": 5})
        assert str(exc_info.value) == "intentional failure"

    @pytest.mark.asyncio
    async def test_run_success_returns_result(self):
        graph = Graph([succeeding_node])
        runner = AsyncRunner()
        result = await runner.run(graph, {"x": 5})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == 10


class TestAsyncRunContinueMode:
    """Test error_handling='continue' for AsyncRunner."""

    @pytest.mark.asyncio
    async def test_continue_returns_failed_result(self):
        graph = Graph([failing_node])
        runner = AsyncRunner()
        result = await runner.run(graph, {"x": 5}, error_handling="continue")
        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, CustomRunError)

    @pytest.mark.asyncio
    async def test_continue_has_partial_values(self):
        graph = Graph([step_a, step_b])
        runner = AsyncRunner()
        result = await runner.run(graph, {"x": 5}, error_handling="continue")
        assert result.status == RunStatus.FAILED
        assert "step_a" in result.values
        assert result.values["step_a"] == 105


class TestMapStillWorks:
    """Verify map() is unaffected by run() default change."""

    def test_map_raise_mode_still_works(self):
        graph = Graph([failing_node])
        runner = SyncRunner()
        with pytest.raises(CustomRunError):
            runner.map(graph, values={"x": [1, 2, 3]}, map_over="x")

    def test_map_continue_mode_still_works(self):
        graph = Graph([failing_node])
        runner = SyncRunner()
        results = runner.map(
            graph,
            values={"x": [1, 2, 3]},
            map_over="x",
            error_handling="continue",
        )
        assert len(results) == 3
        assert all(r.status == RunStatus.FAILED for r in results)
