"""Tests for the Daft-backed runner."""

from __future__ import annotations

import pytest

pytest.importorskip("daft")

from hypergraph import DaftRunner, Graph, RunStatus, SyncRunner, node


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="combined")
def combine(a: int, b: str) -> str:
    return f"{a}:{b}"


def test_daft_runner_map_matches_sync_runner_for_basic_batches():
    graph = Graph([double], name="basic_batch")

    daft_results = DaftRunner().map(graph, {"x": [1, 2, 3]}, map_over="x")
    sync_results = SyncRunner().map(graph, {"x": [1, 2, 3]}, map_over="x")

    assert daft_results["doubled"] == sync_results["doubled"] == [2, 4, 6]
    assert daft_results.status == RunStatus.COMPLETED


def test_daft_runner_supports_product_mode():
    graph = Graph([combine], name="product_batch")

    results = DaftRunner().map(
        graph,
        {"a": [1, 2], "b": ["x", "y"]},
        map_over=["a", "b"],
        map_mode="product",
    )

    assert results["combined"] == ["1:x", "1:y", "2:x", "2:y"]


def test_daft_runner_run_nested_graphnode_with_map_over():
    inner = Graph([double], name="inner")
    outer = Graph([inner.as_node(name="mapper").with_inputs(x="items").map_over("items")], name="outer")

    runner = DaftRunner()
    result = runner.run(outer, {"items": [2, 4, 6]})

    assert result["doubled"] == [4, 8, 12]


def test_daft_runner_handles_bound_inputs_inside_nested_map_over():
    @node(output_name="scored")
    def score(text: str, multiplier: int) -> int:
        return len(text) * multiplier

    scorer = Graph([score], name="scorer").bind(multiplier=3)
    batch_graph = Graph([scorer.as_node(name="score_many").with_inputs(text="texts").map_over("texts")], name="batch_graph")

    result = DaftRunner().run(batch_graph, {"texts": ["a", "tool", "hypergraph"]})

    assert result["scored"] == [3, 12, 30]


def test_daft_runner_continue_mode_preserves_failures():
    @node(output_name="safe_double")
    def maybe_fail(x: int) -> int:
        if x == 2:
            raise ValueError("boom")
        return x * 2

    graph = Graph([maybe_fail], name="failure_batch")
    results = DaftRunner().map(
        graph,
        {"x": [1, 2, 3]},
        map_over="x",
        error_handling="continue",
    )

    assert results.status == RunStatus.PARTIAL
    assert results["safe_double"] == [2, None, 6]
    assert len(results.failures) == 1
    assert isinstance(results.failures[0].error, ValueError)


def test_daft_runner_raise_mode_re_raises_first_failure():
    @node(output_name="safe_double")
    def maybe_fail(x: int) -> int:
        if x == 2:
            raise ValueError("boom")
        return x * 2

    graph = Graph([maybe_fail], name="raise_batch")

    with pytest.raises(ValueError, match="boom"):
        DaftRunner().map(graph, {"x": [1, 2, 3]}, map_over="x")


def test_daft_runner_rejects_nested_graph_with_async_nodes():
    """DaftRunner must raise at plan time for GraphNodes with async inner graphs."""
    from hypergraph.runners._shared.validation import IncompatibleRunnerError

    @node(output_name="result")
    async def async_step(x: int) -> int:
        return x + 1

    inner = Graph([async_step], name="async_inner")
    outer = Graph([inner.as_node(name="sub").with_inputs(x="val")], name="outer")

    with pytest.raises(IncompatibleRunnerError, match="async nodes"):
        DaftRunner().run(outer, {"val": 1})


def test_daft_runner_rejects_with_runner_override():
    """DaftRunner must reject GraphNodes with with_runner() set."""
    from hypergraph.runners._shared.validation import IncompatibleRunnerError

    inner = Graph([double], name="inner")
    gn = inner.as_node(name="sub").with_runner(SyncRunner())
    outer = Graph([gn], name="outer")

    with pytest.raises(IncompatibleRunnerError, match="runner overrides"):
        DaftRunner().run(outer, {"x": 1})


def test_daft_runner_map_dataframe_returns_dataframe():
    """map_dataframe should return a Daft DataFrame, not MapResult."""
    import daft

    graph = Graph([double], name="df_test")
    df = daft.from_pydict({"x": [1, 2, 3]})

    result_df = DaftRunner().map_dataframe(graph, df)

    assert isinstance(result_df, daft.DataFrame)
    collected = result_df.collect().to_pydict()
    assert collected["doubled"] == [2, 4, 6]
    assert collected["x"] == [1, 2, 3]


def test_daft_runner_map_dataframe_with_broadcast_values():
    """Broadcast values should be captured in UDF closures."""
    import daft

    @node(output_name="greeting")
    def greet(name: str, prefix: str) -> str:
        return f"{prefix}, {name}!"

    graph = Graph([greet], name="broadcast_test")
    df = daft.from_pydict({"name": ["Alice", "Bob"]})

    result_df = DaftRunner().map_dataframe(graph, df, prefix="Hi")
    collected = result_df.collect().to_pydict()
    assert collected["greeting"] == ["Hi, Alice!", "Hi, Bob!"]
