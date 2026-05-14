"""Tests for error_handling parameter in GraphNode.map_over()."""

import pytest

from hypergraph import Graph, node
from hypergraph.runners import AsyncRunner, SyncRunner
from hypergraph.runners._shared.types import RunStatus


class CustomMapError(Exception):
    pass


@node(output_name="doubled")
def double_or_fail(x: int) -> int:
    if x == 3:
        raise CustomMapError(f"cannot double {x}")
    return x * 2


@node(output_name="result")
def passthrough(doubled: list) -> list:
    return doubled


@node(output_name="doubled")
async def async_double_or_fail(x: int) -> int:
    if x == 3:
        raise CustomMapError(f"cannot double {x}")
    return x * 2


class TestSyncMapOverErrorHandling:
    """SyncRunner with GraphNode.map_over() error_handling."""

    def test_raise_mode_is_default(self):
        """Default error_handling='raise' raises on first inner failure."""
        inner = Graph([double_or_fail], name="inner")
        outer = Graph([inner.as_node().map_over("x"), passthrough])
        runner = SyncRunner()
        with pytest.raises(CustomMapError):
            runner.run(outer, {"x": [1, 2, 3, 4, 5]})

    def test_continue_mode_returns_none_placeholders(self):
        """error_handling='continue' uses None for failed items, preserving list length."""
        inner = Graph([double_or_fail], name="inner")
        gn = inner.as_node().map_over("x", error_handling="continue")
        outer = Graph([gn, passthrough])
        runner = SyncRunner()
        result = runner.run(outer, {"x": [1, 2, 3, 4, 5]})
        assert result.status == RunStatus.COMPLETED
        # List length preserved: 5 items in, 5 items out
        assert result["result"] == [2, 4, None, 8, 10]

    def test_continue_mode_all_succeed(self):
        """When all items succeed, continue mode behaves normally."""
        inner = Graph([double_or_fail], name="inner")
        gn = inner.as_node().map_over("x", error_handling="continue")
        outer = Graph([gn, passthrough])
        runner = SyncRunner()
        result = runner.run(outer, {"x": [1, 2, 4, 5]})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == [2, 4, 8, 10]

    def test_map_config_includes_error_handling(self):
        """map_config tuple includes error_handling as third element."""
        inner = Graph([double_or_fail], name="inner")
        gn = inner.as_node().map_over("x", error_handling="continue")
        params, mode, error_handling = gn.map_config
        assert params == ["x"]
        assert mode == "zip"
        assert error_handling == "continue"

    def test_map_config_default_error_handling(self):
        """Default map_config has error_handling='raise'."""
        inner = Graph([double_or_fail], name="inner")
        gn = inner.as_node().map_over("x")
        _, _, error_handling = gn.map_config
        assert error_handling == "raise"


class TestAsyncMapOverErrorHandling:
    """AsyncRunner with GraphNode.map_over() error_handling."""

    @pytest.mark.asyncio
    async def test_raise_mode_is_default(self):
        inner = Graph([async_double_or_fail], name="inner")
        outer = Graph([inner.as_node().map_over("x"), passthrough])
        runner = AsyncRunner()
        with pytest.raises(CustomMapError):
            await runner.run(outer, {"x": [1, 2, 3, 4, 5]})

    @pytest.mark.asyncio
    async def test_continue_mode_returns_none_placeholders(self):
        inner = Graph([async_double_or_fail], name="inner")
        gn = inner.as_node().map_over("x", error_handling="continue")
        outer = Graph([gn, passthrough])
        runner = AsyncRunner()
        result = await runner.run(outer, {"x": [1, 2, 3, 4, 5]})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == [2, 4, None, 8, 10]

    @pytest.mark.asyncio
    async def test_continue_mode_all_succeed(self):
        inner = Graph([async_double_or_fail], name="inner")
        gn = inner.as_node().map_over("x", error_handling="continue")
        outer = Graph([gn, passthrough])
        runner = AsyncRunner()
        result = await runner.run(outer, {"x": [1, 2, 4, 5]})
        assert result.status == RunStatus.COMPLETED
        assert result["result"] == [2, 4, 8, 10]
