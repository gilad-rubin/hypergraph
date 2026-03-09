"""Tests for DaftRunner — Daft DataFrame pipeline execution.

Covers:
- Basic run() with single and multi-node DAGs
- map() with zip and product modes
- Validation: rejects cycles, interrupts, unsupported node types
- Error handling modes (raise vs continue)
- Delegation: DaftRunner used via GraphNode.with_runner()
"""

import pytest

from hypergraph import Graph, node
from hypergraph.runners._shared.types import RunStatus

daft = pytest.importorskip("daft", reason="daft not installed")

from hypergraph.integrations.daft import DaftRunner  # noqa: E402

# === Test nodes ===


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="incremented")
def increment(x: int) -> int:
    return x + 1


@node(output_name="sum")
def add(a: int, b: int) -> int:
    return a + b


@node(output_name="product")
def multiply(a: int, b: int) -> int:
    return a * b


@node(output_name="greeting")
def greet(name: str) -> str:
    return f"hello {name}"


@node(output_name="upper")
def upper(greeting: str) -> str:
    return greeting.upper()


@node(output_name="boom")
def explode(x: int) -> int:
    raise ValueError("kaboom")


# === Basic run() ===


class TestDaftRunnerRun:
    def test_single_node(self):
        graph = Graph([double])
        runner = DaftRunner()
        result = runner.run(graph, x=5)
        assert result.values["doubled"] == 10
        assert result.status == RunStatus.COMPLETED

    def test_chain_two_nodes(self):
        """x -> doubled, then add(a=doubled, b) -> sum."""
        graph = Graph([double, add.with_inputs(a="doubled")])
        runner = DaftRunner()
        result = runner.run(graph, x=3, b=100)
        assert result.values["doubled"] == 6
        assert result.values["sum"] == 106

    def test_three_node_chain(self):
        """x -> doubled -> incremented (takes doubled as x)."""
        inc_from_doubled = increment.with_inputs(x="doubled")
        graph = Graph([double, inc_from_doubled])
        runner = DaftRunner()
        result = runner.run(graph, x=4)
        assert result.values["doubled"] == 8
        assert result.values["incremented"] == 9

    def test_diamond_dag(self):
        """Diamond: x -> double, x -> increment, then product(doubled, incremented)."""
        prod = multiply.with_inputs(a="doubled", b="incremented")
        graph = Graph([double, increment, prod])
        runner = DaftRunner()
        result = runner.run(graph, x=5)
        assert result.values["doubled"] == 10
        assert result.values["incremented"] == 6
        assert result.values["product"] == 60

    def test_string_values(self):
        """Non-numeric types work through daft.DataType.python()."""
        graph = Graph([greet, upper])
        runner = DaftRunner()
        result = runner.run(graph, name="world")
        assert result.values["upper"] == "HELLO WORLD"

    def test_values_dict_kwarg(self):
        """Passing inputs via values= dict instead of **kwargs."""
        graph = Graph([double])
        runner = DaftRunner()
        result = runner.run(graph, values={"x": 7})
        assert result.values["doubled"] == 14

    def test_run_id_present(self):
        graph = Graph([double])
        result = DaftRunner().run(graph, x=1)
        assert result.run_id is not None


# === map() ===


class TestDaftRunnerMap:
    def test_zip_mode(self):
        graph = Graph([double])
        runner = DaftRunner()
        result = runner.map(graph, map_over="x", x=[1, 2, 3])
        values = [r.values["doubled"] for r in result.results]
        assert values == [2, 4, 6]
        assert result.map_mode == "zip"

    def test_zip_with_broadcast(self):
        """Broadcast param repeated for each mapped row."""
        graph = Graph([add])
        runner = DaftRunner()
        result = runner.map(graph, map_over="a", a=[10, 20, 30], b=5)
        values = [r.values["sum"] for r in result.results]
        assert values == [15, 25, 35]

    def test_product_mode(self):
        """Cartesian product of two map_over params."""
        graph = Graph([add])
        runner = DaftRunner()
        result = runner.map(
            graph,
            map_over=["a", "b"],
            map_mode="product",
            a=[1, 2],
            b=[10, 20],
        )
        values = [r.values["sum"] for r in result.results]
        # product: (1,10), (1,20), (2,10), (2,20)
        assert sorted(values) == [11, 12, 21, 22]

    def test_zip_unequal_lengths_raises(self):
        graph = Graph([add])
        runner = DaftRunner()
        with pytest.raises(ValueError, match="equal-length"):
            runner.map(graph, map_over=["a", "b"], a=[1, 2], b=[10])

    def test_map_metadata(self):
        graph = Graph([double], name="test_graph")
        result = DaftRunner().map(graph, map_over="x", x=[1])
        assert result.map_over == ("x",)
        assert result.graph_name == "test_graph"
        assert result.run_id is not None


# === Validation ===


class TestDaftRunnerValidation:
    def test_rejects_cyclic_graph(self):
        from hypergraph.exceptions import IncompatibleRunnerError

        @node(output_name="b")
        def step_a(a: int) -> int:
            return a + 1

        @node(output_name="a")
        def step_b(b: int) -> int:
            return b + 1

        graph = Graph([step_a, step_b], entrypoint="step_a")
        assert graph.has_cycles

        runner = DaftRunner()
        with pytest.raises(IncompatibleRunnerError, match="cyclic"):
            runner.run(graph, a=1)

    def test_rejects_interrupt_node(self):
        from hypergraph.exceptions import IncompatibleRunnerError
        from hypergraph.nodes.interrupt import InterruptNode

        def handler(draft: str):
            return None

        interrupt = InterruptNode(handler, output_name="response")
        graph = Graph([interrupt])
        assert graph.has_interrupts

        runner = DaftRunner()
        with pytest.raises(IncompatibleRunnerError, match="InterruptNode"):
            runner.run(graph)

    def test_rejects_gate_node(self):
        from hypergraph.exceptions import IncompatibleRunnerError
        from hypergraph.nodes.gate import IfElseNode

        @node(output_name="other")
        def other_node(x: int) -> int:
            return x

        def check(x: int) -> bool:
            return x > 0

        gate = IfElseNode(
            func=check,
            when_true="double",
            when_false="other_node",
        )
        graph = Graph([gate, double, other_node])

        runner = DaftRunner()
        with pytest.raises(IncompatibleRunnerError, match="FunctionNode"):
            runner.run(graph, x=1)


# === Error handling ===


class TestDaftRunnerErrorHandling:
    def test_raise_mode_propagates(self):
        graph = Graph([explode])
        runner = DaftRunner()
        with pytest.raises(ValueError, match="kaboom"):
            runner.run(graph, x=1)

    def test_continue_mode_returns_failed(self):
        graph = Graph([explode])
        runner = DaftRunner()
        result = runner.run(graph, x=1, error_handling="continue")
        assert result.status == RunStatus.FAILED
        assert result.error is not None

    def test_map_raise_mode_propagates(self):
        graph = Graph([explode])
        runner = DaftRunner()
        with pytest.raises(ValueError, match="kaboom"):
            runner.map(graph, map_over="x", x=[1, 2])

    def test_map_continue_mode_returns_failed(self):
        graph = Graph([explode])
        runner = DaftRunner()
        result = runner.map(graph, map_over="x", x=[1, 2], error_handling="continue")
        assert all(r.status == RunStatus.FAILED for r in result.results)


# === Delegation: DaftRunner via with_runner() ===


class TestDaftDelegation:
    """DaftRunner used as a delegated runner for a GraphNode."""

    def test_sync_delegates_to_daft(self):
        """SyncRunner parent delegates a subgraph to DaftRunner."""
        from hypergraph.runners import SyncRunner

        inner = Graph([double, add.with_inputs(a="doubled")], name="inner")
        gn = inner.as_node(runner=DaftRunner())
        outer = Graph([gn])

        result = SyncRunner().run(outer, x=5, b=100)
        assert result.values["doubled"] == 10
        assert result.values["sum"] == 110

    def test_sync_delegates_to_daft_with_map_over(self):
        """SyncRunner parent, GraphNode mapped over x, delegated to DaftRunner."""
        from hypergraph.runners import SyncRunner

        inner = Graph([double], name="inner")
        gn = inner.as_node(runner=DaftRunner()).map_over("x")
        outer = Graph([gn])

        result = SyncRunner().run(outer, x=[1, 2, 3])
        assert result.values["doubled"] == [2, 4, 6]

    @pytest.mark.asyncio
    async def test_async_delegates_to_daft(self):
        """AsyncRunner parent delegates to sync DaftRunner."""
        from hypergraph.runners import AsyncRunner

        inner = Graph([double], name="inner")
        gn = inner.as_node(runner=DaftRunner())
        outer = Graph([gn])

        result = await AsyncRunner().run(outer, x=5)
        assert result.values["doubled"] == 10
