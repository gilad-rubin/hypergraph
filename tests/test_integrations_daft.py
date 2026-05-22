"""Public Daft integration API tests."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("daft")

import daft

from hypergraph import Graph, node
from hypergraph.integrations.daft import DaftRunner, Options, stateful
from hypergraph.integrations.daft import node as daft_node
from hypergraph.integrations.daft.options import Options as ReExportedOptions
from hypergraph.runners.daft._options import Options as InternalOptions


def test_daft_node_batch_executes_with_typed_options() -> None:
    @daft_node(
        output_name="word_count",
        batch=True,
        return_dtype=daft.DataType.int64(),
        batch_size=2,
    )
    def count_words(text: daft.Series) -> list[int]:
        return [len(value.split()) for value in text.to_pylist()]

    graph = Graph([count_words], name="batch_words")

    result = DaftRunner().map(
        graph,
        {"text": ["one fish", "two", "red blue"]},
        map_over="text",
    )

    assert result["word_count"] == [2, 1, 2]


def test_core_node_does_not_accept_daft_batch_option() -> None:
    with pytest.raises(TypeError, match="batch"):

        @node(output_name="word_count", batch=True)
        def count_words(text: str) -> int:
            return len(text.split())


def test_stateful_batch_node_executes_with_worker_resource() -> None:
    @stateful(max_concurrency=2)
    class Prefixer:
        def __init__(self) -> None:
            self.prefix = "hg:"

        def apply(self, value: str) -> str:
            return f"{self.prefix}{value.strip().lower()}"

    @daft_node(
        output_name="normalized",
        batch=True,
        return_dtype=daft.DataType.string(),
        batch_size=2,
    )
    def normalize(text: daft.Series, prefixer: Prefixer) -> list[str]:
        return [prefixer.apply(value) for value in text.to_pylist()]

    graph = Graph([normalize], name="stateful_batch").bind(prefixer=Prefixer())

    result = DaftRunner().map(
        graph,
        {"text": [" Alpha ", "BETA", " Gamma"]},
        map_over="text",
    )

    assert result["normalized"] == ["hg:alpha", "hg:beta", "hg:gamma"]


def test_map_dataframe_rejects_output_column_collision() -> None:
    from hypergraph.graph.validation import GraphConfigError

    @node(output_name="text")
    def normalize(raw: str) -> str:
        return raw.strip().lower()

    graph = Graph([normalize], name="collision")
    frame = daft.from_pydict(
        {
            "raw": [" Alpha "],
            "text": ["already present"],
        }
    )

    with pytest.raises(GraphConfigError, match="output column.*text"):
        DaftRunner().map_dataframe(graph, frame)


def test_map_dataframe_rejects_internal_pack_column_collision() -> None:
    from hypergraph.graph.validation import GraphConfigError

    @node(output_name=("lo", "hi"))
    def bounds(x: int) -> tuple[int, int]:
        return x - 1, x + 1

    graph = Graph([bounds], name="internal_collision")
    frame = daft.from_pydict({"x": [2], "_pack_bounds": ["already present"]})

    with pytest.raises(GraphConfigError, match="internal scratch column.*_pack_bounds"):
        DaftRunner().map_dataframe(graph, frame)


def test_map_dataframe_preserves_passthrough_columns_when_columns_selects_inputs() -> None:
    @node(output_name="word_count")
    def count_words(text: str) -> int:
        return len(text.split())

    graph = Graph([count_words], name="passthrough")
    frame = daft.from_pydict(
        {
            "id": [1, 2],
            "text": ["one fish", "red blue"],
            "source": ["api", "csv"],
        }
    )

    result = DaftRunner().map_dataframe(graph, frame, columns=["text"]).collect().to_pydict()

    assert result == {
        "id": [1, 2],
        "text": ["one fish", "red blue"],
        "source": ["api", "csv"],
        "word_count": [2, 2],
    }


def test_stateful_async_node_executes_with_worker_resource() -> None:
    @stateful
    class Multiplier:
        def __init__(self) -> None:
            self.factor = 3

    @daft_node(output_name="scaled")
    async def scale(x: int, multiplier: Multiplier) -> int:
        await asyncio.sleep(0)
        return x * multiplier.factor

    graph = Graph([scale], name="stateful_async").bind(multiplier=Multiplier())

    result = DaftRunner().map(graph, {"x": [2, 4]}, map_over="x")

    assert result["scaled"] == [6, 12]


def test_async_stateless_node_uses_daft_execution_options() -> None:
    @daft_node(output_name="doubled", max_concurrency=2)
    async def double(x: int) -> int:
        await asyncio.sleep(0)
        return x * 2

    graph = Graph([double], name="async_options")

    result = DaftRunner().map(graph, {"x": [1, 2, 3]}, map_over="x")

    assert result["doubled"] == [2, 4, 6]


def test_async_stateless_node_can_ignore_daft_udf_errors() -> None:
    @daft_node(output_name="status", max_concurrency=2, max_retries=0, on_error="ignore")
    async def fail(value: str) -> str:
        await asyncio.sleep(0)
        raise ValueError(f"failed {value}")

    graph = Graph([fail], name="async_ignore")

    result = DaftRunner().map(graph, {"value": ["a", "b"]}, map_over="value")

    assert result["status"] == [None, None]


def test_map_dataframe_stateful_plan_build_does_not_eagerly_construct_resource() -> None:
    @stateful
    class LazyResource:
        constructions = 0

        def __init__(self) -> None:
            type(self).constructions += 1

        def transform(self, x: int) -> int:
            return x + 1

    @node(output_name="y")
    def transform(x: int, resource: LazyResource) -> int:
        return resource.transform(x)

    graph = Graph([transform], name="lazy_stateful").bind(resource=LazyResource())
    frame = daft.from_pydict({"x": [1, 2]})

    result = DaftRunner().map_dataframe(graph, frame)

    assert LazyResource.constructions == 1
    assert result.collect().to_pydict()["y"] == [2, 3]


def test_batch_node_requires_explicit_return_dtype() -> None:
    from hypergraph.runners._shared.validation import IncompatibleRunnerError

    @daft_node(output_name="word_count", batch=True)
    def count_words(text: daft.Series) -> list[int]:
        return [len(value.split()) for value in text.to_pylist()]

    graph = Graph([count_words], name="batch_dtype")

    with pytest.raises(IncompatibleRunnerError, match="return_dtype"):
        DaftRunner().map(graph, {"text": ["one fish"]}, map_over="text")


def test_async_batch_node_is_rejected_before_execution() -> None:
    from hypergraph.runners._shared.validation import IncompatibleRunnerError

    @daft_node(output_name="word_count", batch=True, return_dtype=daft.DataType.int64())
    async def count_words(text: daft.Series) -> list[int]:
        await asyncio.sleep(0)
        return [len(value.split()) for value in text.to_pylist()]

    graph = Graph([count_words], name="async_batch")

    with pytest.raises(IncompatibleRunnerError, match="Async batch"):
        DaftRunner().map(graph, {"text": ["one fish"]}, map_over="text")


def test_batch_size_without_batch_is_rejected_before_execution() -> None:
    from hypergraph.runners._shared.validation import IncompatibleRunnerError

    @daft_node(output_name="word_count", batch_size=2)
    def count_words(text: str) -> int:
        return len(text.split())

    graph = Graph([count_words], name="bad_batch_size")

    with pytest.raises(IncompatibleRunnerError, match="batch_size"):
        DaftRunner().map(graph, {"text": ["one fish"]}, map_over="text")


def test_sync_stateless_max_concurrency_is_rejected_before_execution() -> None:
    from hypergraph.runners._shared.validation import IncompatibleRunnerError

    @daft_node(output_name="word_count", max_concurrency=2)
    def count_words(text: str) -> int:
        return len(text.split())

    graph = Graph([count_words], name="bad_concurrency")

    with pytest.raises(IncompatibleRunnerError, match="max_concurrency"):
        DaftRunner().map(graph, {"text": ["one fish"]}, map_over="text")


def test_stateful_node_rejects_node_level_resource_options() -> None:
    from hypergraph.runners._shared.validation import IncompatibleRunnerError

    @stateful
    class Resource:
        def transform(self, value: int) -> int:
            return value + 1

    @daft_node(output_name="y", max_concurrency=2)
    def transform(x: int, resource: Resource) -> int:
        return resource.transform(x)

    graph = Graph([transform], name="bad_stateful_options").bind(resource=Resource())

    with pytest.raises(IncompatibleRunnerError, match="max_concurrency.*@stateful"):
        DaftRunner().map(graph, {"x": [1]}, map_over="x")


def test_ray_resource_options_are_rejected_at_definition_time() -> None:
    with pytest.raises(ValueError, match="num_cpus"):

        @daft_node(
            output_name="word_count",
            ray_options={"num_cpus": 1},
        )
        def count_words(text: str) -> int:
            return len(text.split())


def test_options_validate_current_daft_resource_rules_at_definition_time() -> None:
    with pytest.raises(ValueError, match="on_error"):
        Options(on_error="skip")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="max_concurrency"):
        Options(max_concurrency=0)

    with pytest.raises(ValueError, match="max_concurrency"):
        Options(max_concurrency=1.5)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="max_retries"):
        Options(max_retries=-1)

    with pytest.raises(ValueError, match="batch_size"):
        Options(batch_size=0)

    with pytest.raises(ValueError, match="batch_size"):
        Options(batch_size=1.5)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="gpus"):
        Options(gpus=1.5)


def test_options_reexports_use_one_class_identity() -> None:
    assert Options is InternalOptions is ReExportedOptions


def test_stateful_rejects_node_only_options() -> None:
    with pytest.raises(ValueError, match="stateful.*return_dtype"):

        @stateful(options=Options(return_dtype=daft.DataType.int64()))
        class Resource:
            def __init__(self) -> None:
                pass
