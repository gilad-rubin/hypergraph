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
    is_batch,
    stateful,
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


@stateful
class MockModel:
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

    def test_stateful_decorator_sets_attribute(self):
        @stateful
        class Foo:
            pass

        assert Foo.__daft_stateful__ is True
        assert isinstance(Foo(), DaftStateful)

    def test_non_stateful_not_detected(self):

        class PlainObj:
            pass

        assert not isinstance(PlainObj(), DaftStateful)

    def test_has_stateful_values_true(self):
        assert has_stateful_values({"model": MockModel()})

    def test_has_stateful_values_false(self):
        assert not has_stateful_values({"x": 42, "y": "hello"})

    def test_false_attribute_not_detected(self):
        """Classes with __daft_stateful__ = False are NOT stateful."""

        class NotStateful:
            __daft_stateful__ = False

        assert not has_stateful_values({"obj": NotStateful()})


# ---------------------------------------------------------------------------
# Batch detection
# ---------------------------------------------------------------------------


class TestBatchDetection:
    def test_batch_true_detected(self):
        @node(output_name="out", batch=True)
        def batch_node(x: int) -> int:
            return x

        assert is_batch(batch_node)

    def test_batch_false_by_default(self):
        assert not is_batch(add_one)


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

        @node(output_name="out", batch=True)
        def batch_fn(x: int) -> int:
            return x

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


# ---------------------------------------------------------------------------
# FunctionNodeOperation async execution
# ---------------------------------------------------------------------------


class TestFunctionNodeAsyncExecution:
    def test_async_function_node(self):
        graph = Graph([async_double], name="test")
        op = create_operation(async_double, graph, bound_values={})
        df = daft.from_pydict({"x": [3, 7]})
        result = op.apply(df).collect().to_pydict()
        assert result["result"] == [6, 14]


# ---------------------------------------------------------------------------
# StatefulNodeOperation direct execution
# ---------------------------------------------------------------------------


class TestStatefulNodeOperation:
    def test_apply_produces_correct_output(self):
        graph = Graph([score_with_model], name="test")
        op = create_operation(
            score_with_model,
            graph,
            bound_values={"model": MockModel()},
        )
        df = daft.from_pydict({"text": ["hi", "hello"]})
        result = op.apply(df).collect().to_pydict()
        assert result["score"] == [2 * 42, 5 * 42]


# ---------------------------------------------------------------------------
# BatchNodeOperation direct execution
# ---------------------------------------------------------------------------


class TestBatchNodeOperation:
    def test_apply_receives_series(self):
        @node(output_name="doubled", batch=True)
        def batch_double(x: daft.Series) -> daft.Series:
            values = x.to_pylist()
            return daft.Series.from_pylist([v * 2 for v in values])

        graph = Graph([batch_double], name="test")
        op = create_operation(batch_double, graph, bound_values={})
        df = daft.from_pydict({"x": [10, 20, 30]})
        result = op.apply(df).collect().to_pydict()
        assert result["doubled"] == [20, 40, 60]


# ---------------------------------------------------------------------------
# Validation: stateful + async rejection
# ---------------------------------------------------------------------------


class TestStatefulAsyncRejection:
    def test_stateful_async_node_rejected(self):
        from hypergraph.runners._shared.validation import IncompatibleRunnerError

        @node(output_name="out")
        async def async_stateful(x: int, model: MockModel) -> int:
            return x + model.value

        graph = Graph([async_stateful], name="test")
        with pytest.raises(IncompatibleRunnerError, match="async"):
            create_operation(
                async_stateful,
                graph,
                bound_values={"model": MockModel()},
            )


# ---------------------------------------------------------------------------
# Validation: batch + multi-output rejection
# ---------------------------------------------------------------------------


class TestBatchMultiOutputRejection:
    def test_batch_multi_output_rejected(self):
        from hypergraph.runners._shared.validation import IncompatibleRunnerError

        @node(output_name=("a", "b"), batch=True)
        def bad_batch(x: int) -> tuple[int, int]:
            return (x, x)

        graph = Graph([bad_batch], name="test")
        with pytest.raises(IncompatibleRunnerError, match="multiple outputs"):
            create_operation(bad_batch, graph, bound_values={})


# ---------------------------------------------------------------------------
# Validation: _validate_stateful_constructable
# ---------------------------------------------------------------------------


class TestStatefulConstructable:
    def test_non_constructable_stateful_rejected(self):
        @stateful
        class NeedsArgs:
            def __init__(self, required_arg: str):
                self.value = required_arg

        @node(output_name="out")
        def use_it(x: int, model: NeedsArgs) -> int:
            return x

        graph = Graph([use_it], name="test")
        with pytest.raises(TypeError, match="zero-arg construction"):
            create_operation(
                use_it,
                graph,
                bound_values={"model": NeedsArgs("hello")},
            )
