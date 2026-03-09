"""Tests for Daft UDF operations."""

from __future__ import annotations

import pytest

pytest.importorskip("daft")

import daft

from hypergraph import Graph, node
from hypergraph.runners.daft.operations import (
    DaftStateful,
    create_operation,
    has_stateful_values,
    is_batch_marked,
    mark_batch,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@node(output_name="y")
def add_one(x: int) -> int:
    return x + 1


@node(output_name="greeting")
def greet(name: str, prefix: str = "Hello") -> str:
    return f"{prefix}, {name}!"


@node(output_name="result")
async def async_double(x: int) -> int:
    return x * 2


class MockModel:
    __daft_stateful__ = True
    init_count = 0

    def __init__(self):
        MockModel.init_count += 1
        self.value = 42


@node(output_name="score")
def score_with_model(text: str, model: MockModel) -> int:
    return len(text) * model.value


# ---------------------------------------------------------------------------
# DaftStateful protocol
# ---------------------------------------------------------------------------


class TestDaftStatefulProtocol:
    def test_protocol_detection(self):
        assert isinstance(MockModel(), DaftStateful)

    def test_non_stateful_not_detected(self):

        class PlainObj:
            pass

        assert not isinstance(PlainObj(), DaftStateful)

    def test_has_stateful_values_true(self):
        assert has_stateful_values({"model": MockModel()})

    def test_has_stateful_values_false(self):
        assert not has_stateful_values({"x": 42, "y": "hello"})


# ---------------------------------------------------------------------------
# Batch marker
# ---------------------------------------------------------------------------


class TestBatchMarker:
    def test_mark_batch_sets_attribute(self):

        def my_func():
            pass

        mark_batch(my_func)
        assert my_func.__daft_batch__ is True

    def test_is_batch_marked_detects(self):

        @node(output_name="out")
        def batch_node(x: int) -> int:
            return x

        mark_batch(batch_node.func)
        assert is_batch_marked(batch_node)

    def test_is_batch_marked_false_by_default(self):
        assert not is_batch_marked(add_one)


# ---------------------------------------------------------------------------
# create_operation routing
# ---------------------------------------------------------------------------


class TestCreateOperation:
    def test_routes_function_node(self):
        from hypergraph.runners.daft.operations import FunctionNodeOperation

        graph = Graph([add_one], name="test")
        op = create_operation(add_one, graph, bound_values={})
        assert isinstance(op, FunctionNodeOperation)

    def test_routes_stateful_node(self):
        from hypergraph.runners.daft.operations import StatefulNodeOperation

        graph = Graph([score_with_model], name="test")
        op = create_operation(
            score_with_model,
            graph,
            bound_values={"model": MockModel()},
        )
        assert isinstance(op, StatefulNodeOperation)

    def test_routes_batch_node(self):
        from hypergraph.runners.daft.operations import BatchNodeOperation

        @node(output_name="out")
        def batch_fn(x: int) -> int:
            return x

        mark_batch(batch_fn.func)
        graph = Graph([batch_fn], name="test")
        op = create_operation(batch_fn, graph, bound_values={})
        assert isinstance(op, BatchNodeOperation)

    def test_routes_graph_node(self):
        from hypergraph.runners.daft.operations import GraphNodeOperation

        inner = Graph([add_one], name="inner")
        gn = inner.as_node(name="nested")
        outer = Graph([gn], name="outer")
        op = create_operation(gn, outer, bound_values={})
        assert isinstance(op, GraphNodeOperation)


# ---------------------------------------------------------------------------
# FunctionNodeOperation execution
# ---------------------------------------------------------------------------


class TestFunctionNodeOperation:
    def test_sync_execution(self):
        graph = Graph([add_one], name="test")
        op = create_operation(add_one, graph, bound_values={})
        df = daft.from_pydict({"x": [1, 2, 3]})
        result = op.apply(df).collect().to_pydict()
        assert result["y"] == [2, 3, 4]

    def test_with_defaults(self):
        graph = Graph([greet], name="test")
        op = create_operation(greet, graph, bound_values={})
        df = daft.from_pydict({"name": ["World"], "prefix": ["Hi"]})
        result = op.apply(df).collect().to_pydict()
        assert result["greeting"] == ["Hi, World!"]

    def test_with_bound_values(self):
        graph = Graph([greet], name="test")
        op = create_operation(greet, graph, bound_values={"prefix": "Yo"})
        df = daft.from_pydict({"name": ["World"]})
        result = op.apply(df).collect().to_pydict()
        assert result["greeting"] == ["Yo, World!"]


# ---------------------------------------------------------------------------
# GraphNodeOperation execution
# ---------------------------------------------------------------------------


class TestGraphNodeOperation:
    def test_simple_nested_graph(self):
        inner = Graph([add_one], name="inner")
        gn = inner.as_node(name="nested")
        outer = Graph([gn], name="outer")
        op = create_operation(gn, outer, bound_values={})
        df = daft.from_pydict({"x": [10, 20]})
        result = op.apply(df).collect().to_pydict()
        assert result["y"] == [11, 21]

    def test_nested_graph_with_map_over(self):
        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        inner = Graph([double], name="inner")
        gn = inner.as_node(name="mapper").with_inputs(x="items").map_over("items")
        outer = Graph([gn], name="outer")
        op = create_operation(gn, outer, bound_values={})
        df = daft.from_pydict({"items": [[1, 2, 3]]})
        result = op.apply(df).collect().to_pydict()
        assert result["doubled"] == [[2, 4, 6]]

    def test_multi_output_function_node(self):
        @node(output_name=("lo", "hi"))
        def split(x: int) -> tuple[int, int]:
            return (x - 1, x + 1)

        graph = Graph([split], name="multi")
        op = create_operation(split, graph, bound_values={})
        df = daft.from_pydict({"x": [5, 10]})
        result = op.apply(df).collect().to_pydict()
        assert result["lo"] == [4, 9]
        assert result["hi"] == [6, 11]
        assert "_pack_split" not in result
