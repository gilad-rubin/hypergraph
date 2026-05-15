"""Tests for GraphNode.with_runner() delegation.

Covers:
- with_runner() immutability and API
- as_node(runner=...) sugar
- Delegation dispatch in sync and async executors
- runner_override not affecting definition_hash
"""

import pytest

from hypergraph import Graph, node
from hypergraph.runners import AsyncRunner, SyncRunner

# === Test nodes ===


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(x: int) -> int:
    return x * 3


@node(output_name="sum")
def add(a: int, b: int) -> int:
    return a + b


# === Unit tests for with_runner() API ===


class TestWithRunnerAPI:
    """GraphNode.with_runner() returns immutable copies."""

    def test_with_runner_returns_new_instance(self):
        inner = Graph([double], name="inner")
        gn = inner.as_node()
        runner = SyncRunner()
        delegated = gn.with_runner(runner)

        assert delegated is not gn
        assert delegated.runner_override is runner
        assert gn.runner_override is None

    def test_as_node_runner_kwarg(self):
        inner = Graph([double], name="inner")
        runner = SyncRunner()
        gn = inner.as_node(runner=runner)

        assert gn.runner_override is runner

    def test_as_node_without_runner(self):
        inner = Graph([double], name="inner")
        gn = inner.as_node()
        assert gn.runner_override is None

    def test_runner_override_not_in_definition_hash(self):
        """runner_override is a runtime config, not structural."""
        inner = Graph([double], name="inner")
        gn_plain = inner.as_node()
        gn_delegated = inner.as_node(runner=SyncRunner())

        assert gn_plain.definition_hash == gn_delegated.definition_hash

    def test_with_runner_preserves_map_over(self):
        inner = Graph([double], name="inner")
        mapped = inner.as_node().map_over("x")
        delegated = mapped.with_runner(SyncRunner())

        assert delegated.map_config is not None
        assert delegated.runner_override is not None

    def test_with_runner_preserves_rename(self):
        inner = Graph([double], name="inner")
        renamed = inner.as_node().rename_inputs(x="input_val")
        delegated = renamed.with_runner(SyncRunner())

        assert "input_val" in delegated.inputs
        assert delegated.runner_override is not None

    def test_chaining_with_runner_replaces(self):
        """Second with_runner replaces the first."""
        inner = Graph([double], name="inner")
        runner1 = SyncRunner()
        runner2 = SyncRunner()
        gn = inner.as_node().with_runner(runner1).with_runner(runner2)

        assert gn.runner_override is runner2


# === Functional delegation tests ===


class TestDelegationExecution:
    """Verify that runner_override is actually used at execution time."""

    def test_sync_delegation_to_another_sync_runner(self):
        """A SyncRunner delegates a subgraph to another SyncRunner."""
        inner = Graph([double], name="inner")
        child_runner = SyncRunner()
        gn = inner.as_node(runner=child_runner)
        outer = Graph([gn])

        parent_runner = SyncRunner()
        # x is owned by inner GraphNode → addressed by its parent-facing key (use dict form)
        result = parent_runner.run(outer, {"x": 5})
        assert result.values["doubled"] == 10

    def test_sync_delegation_with_map_over(self):
        """Delegated runner handles map_over correctly."""
        inner = Graph([double], name="inner")
        child_runner = SyncRunner()
        mapped = inner.as_node(runner=child_runner).map_over("x")
        outer = Graph([mapped])

        parent_runner = SyncRunner()
        # x is owned by inner GraphNode → addressed by its parent-facing key
        result = parent_runner.run(outer, {"x": [1, 2, 3]})
        assert result.values["doubled"] == [2, 4, 6]

    @pytest.mark.asyncio
    async def test_async_delegation_to_another_async_runner(self):
        """An AsyncRunner delegates a subgraph to another AsyncRunner."""
        inner = Graph([double], name="inner")
        child_runner = AsyncRunner()
        gn = inner.as_node(runner=child_runner)
        outer = Graph([gn])

        parent_runner = AsyncRunner()
        # x is owned by inner GraphNode → addressed by its parent-facing key
        result = await parent_runner.run(outer, {"x": 5})
        assert result.values["doubled"] == 10

    def test_delegation_with_renamed_inputs(self):
        """Delegated runner respects input renaming."""
        inner = Graph([double], name="inner")
        gn = inner.as_node(runner=SyncRunner()).rename_inputs(x="val")
        outer = Graph([gn])

        # val (renamed from x) is the parent-facing input address.
        result = SyncRunner().run(outer, {"val": 7})
        assert result.values["doubled"] == 14

    def test_delegation_in_multi_node_graph(self):
        """Only the GraphNode with runner_override is delegated."""
        inner = Graph([double], name="inner")
        gn = inner.as_node(runner=SyncRunner())
        outer = Graph([gn, add.rename_inputs(a="doubled")])

        # x is owned by inner GraphNode; b is declared at outer (consumed by add)
        result = SyncRunner().run(outer, {"x": 3, "b": 10})
        assert result.values["doubled"] == 6
        assert result.values["sum"] == 16
