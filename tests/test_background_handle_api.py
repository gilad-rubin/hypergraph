"""Public API contract for process-local background execution handles."""

from __future__ import annotations

import asyncio
import inspect
from concurrent.futures import Future

import pytest

import hypergraph
import hypergraph.runners as runner_namespace
from hypergraph import (
    AsyncHandle,
    AsyncRunner,
    DaftRunner,
    Graph,
    IncompatibleRunnerError,
    SyncHandle,
    SyncRunner,
    node,
)


@pytest.mark.parametrize(
    ("runner_type", "start_name", "blocking_name", "excluded", "return_type"),
    [
        (
            SyncRunner,
            "start_run",
            "run",
            {"error_handling", "override_workflow", "fork_from", "retry_from"},
            "SyncHandle[RunResult]",
        ),
        (
            AsyncRunner,
            "start_run",
            "run",
            {"error_handling", "override_workflow", "fork_from", "retry_from"},
            "AsyncHandle[RunResult]",
        ),
        (
            SyncRunner,
            "start_map",
            "map",
            {"error_handling"},
            "SyncHandle[MapResult]",
        ),
        (
            AsyncRunner,
            "start_map",
            "map",
            {"error_handling"},
            "AsyncHandle[MapResult]",
        ),
    ],
)
def test_start_methods_mirror_only_public_blocking_options(
    runner_type,
    start_name: str,
    blocking_name: str,
    excluded: set[str],
    return_type: str,
) -> None:
    start_method = getattr(runner_type, start_name)
    blocking_method = getattr(runner_type, blocking_name)
    start_parameters = inspect.signature(start_method).parameters
    blocking_parameters = inspect.signature(blocking_method).parameters
    expected_names = tuple(name for name in blocking_parameters if not name.startswith("_") and name not in excluded)

    assert tuple(start_parameters) == expected_names
    for name in expected_names:
        start_parameter = start_parameters[name]
        blocking_parameter = blocking_parameters[name]
        assert start_parameter.kind is blocking_parameter.kind
        assert start_parameter.default == blocking_parameter.default
        assert start_parameter.annotation == blocking_parameter.annotation
    assert inspect.signature(start_method).return_annotation == return_type
    assert inspect.iscoroutinefunction(start_method) is False


def test_handle_exports_and_minimal_surface_are_exact() -> None:
    forbidden = {
        "status",
        "wait",
        "failure",
        "failures",
        "failed_item_indexes",
        "view",
        "inspect",
        "cancel",
        "cancelled",
        "exception",
        "add_done_callback",
        "running",
        "__await__",
    }

    assert hypergraph.SyncHandle is runner_namespace.SyncHandle is SyncHandle
    assert hypergraph.AsyncHandle is runner_namespace.AsyncHandle is AsyncHandle
    assert {name for name in vars(SyncHandle) if not name.startswith("_")} == {
        "done",
        "stop",
        "result",
    }
    assert {name for name in vars(AsyncHandle) if not name.startswith("_")} == {
        "done",
        "stop",
        "result",
    }
    assert forbidden.isdisjoint(dir(SyncHandle))
    assert forbidden.isdisjoint(dir(AsyncHandle))
    assert issubclass(SyncHandle, Future) is False
    assert issubclass(AsyncHandle, asyncio.Task) is False
    assert inspect.iscoroutinefunction(SyncHandle.result) is False
    assert inspect.iscoroutinefunction(AsyncHandle.result) is True
    assert "start_run" not in dir(DaftRunner)
    assert "start_map" not in dir(DaftRunner)


@pytest.mark.parametrize("method_name", ["start_run", "start_map"])
def test_sync_start_methods_reject_error_handling_as_an_option(
    method_name: str,
) -> None:
    @node(output_name="seen")
    def echo_error_handling(error_handling: str) -> str:
        return error_handling

    runner = SyncRunner()
    graph = Graph([echo_error_handling])
    values = {"error_handling": "graph input"} if method_name == "start_run" else {"error_handling": ["graph input"]}
    options = {} if method_name == "start_run" else {"map_over": "error_handling"}

    accepted = getattr(runner, method_name)(graph, values, **options)
    accepted_result = accepted.result()
    assert accepted_result["seen"] == ("graph input" if method_name == "start_run" else ["graph input"])

    direct_error = None
    unexpected_handle = None
    try:
        unexpected_handle = getattr(runner, method_name)(
            graph,
            error_handling="raise",
            **options,
        )
    except TypeError as error:
        direct_error = error
    finally:
        if unexpected_handle is not None:
            with pytest.raises(TypeError):
                unexpected_handle.result(raise_on_failure=False)

    assert direct_error is not None
    assert "unexpected keyword argument 'error_handling'" in str(direct_error)


@pytest.mark.parametrize("method_name", ["start_run", "start_map"])
async def test_async_start_methods_reject_error_handling_as_an_option(
    method_name: str,
) -> None:
    @node(output_name="seen")
    async def echo_error_handling(error_handling: str) -> str:
        return error_handling

    runner = AsyncRunner()
    graph = Graph([echo_error_handling])
    values = {"error_handling": "graph input"} if method_name == "start_run" else {"error_handling": ["graph input"]}
    options = {} if method_name == "start_run" else {"map_over": "error_handling"}

    accepted = getattr(runner, method_name)(graph, values, **options)
    accepted_result = await accepted.result()
    assert accepted_result["seen"] == ("graph input" if method_name == "start_run" else ["graph input"])

    direct_error = None
    unexpected_handle = None
    try:
        unexpected_handle = getattr(runner, method_name)(
            graph,
            error_handling="raise",
            **options,
        )
    except TypeError as error:
        direct_error = error
    finally:
        if unexpected_handle is not None:
            with pytest.raises(TypeError):
                await unexpected_handle.result(raise_on_failure=False)

    assert direct_error is not None
    assert "unexpected keyword argument 'error_handling'" in str(direct_error)


def test_sync_start_run_rejects_lineage_options_but_accepts_same_named_inputs_in_values() -> None:
    @node(output_name="seen")
    def echo_lineage_names(
        override_workflow: bool,
        fork_from: str,
        retry_from: str,
    ) -> tuple[bool, str, str]:
        return override_workflow, fork_from, retry_from

    runner = SyncRunner()
    graph = Graph([echo_lineage_names])
    values = {
        "override_workflow": True,
        "fork_from": "source",
        "retry_from": "failed",
    }

    accepted = runner.start_run(graph, values).result()
    assert accepted["seen"] == (True, "source", "failed")

    for option, direct_value in values.items():
        remaining_values = {name: value for name, value in values.items() if name != option}
        with pytest.raises(TypeError, match=rf"unexpected keyword argument '{option}'") as caught:
            runner.start_run(
                graph,
                remaining_values,
                **{option: direct_value},
            )

        assert "How to fix:" in str(caught.value)
        assert "values={" in str(caught.value)


async def test_async_start_run_rejects_lineage_options_but_accepts_same_named_inputs_in_values() -> None:
    @node(output_name="seen")
    async def echo_lineage_names(
        override_workflow: bool,
        fork_from: str,
        retry_from: str,
    ) -> tuple[bool, str, str]:
        return override_workflow, fork_from, retry_from

    runner = AsyncRunner()
    graph = Graph([echo_lineage_names])
    values = {
        "override_workflow": True,
        "fork_from": "source",
        "retry_from": "failed",
    }

    accepted = await runner.start_run(graph, values).result()
    assert accepted["seen"] == (True, "source", "failed")

    for option, direct_value in values.items():
        remaining_values = {name: value for name, value in values.items() if name != option}
        with pytest.raises(TypeError, match=rf"unexpected keyword argument '{option}'") as caught:
            runner.start_run(
                graph,
                remaining_values,
                **{option: direct_value},
            )

        assert "How to fix:" in str(caught.value)
        assert "values={" in str(caught.value)


def test_sync_result_propagates_failure_when_no_run_result_exists() -> None:
    @node(output_name="never")
    async def async_only() -> int:
        return 1

    handle = SyncRunner().start_run(Graph([async_only]))
    errors = []
    for raise_on_failure in (False, True):
        with pytest.raises(IncompatibleRunnerError) as caught:
            handle.result(raise_on_failure=raise_on_failure)
        errors.append(caught.value)

    assert errors[0] is errors[1]


async def test_async_result_propagates_failure_when_no_map_result_exists() -> None:
    @node(output_name="copied")
    async def identity(value: int) -> int:
        return value

    handle = AsyncRunner().start_map(
        Graph([identity]),
        {"value": [1]},
        map_over="value",
        map_mode="unknown",
    )
    errors = []
    for raise_on_failure in (False, True):
        with pytest.raises(ValueError, match="Unknown map_mode") as caught:
            await handle.result(raise_on_failure=raise_on_failure)
        errors.append(caught.value)

    assert errors[0] is errors[1]


def test_sync_map_repeated_retrieval_returns_same_object() -> None:
    @node(output_name="doubled")
    def double(value: int) -> int:
        return value * 2

    handle = SyncRunner().start_map(
        Graph([double]),
        {"value": [1, 2]},
        map_over="value",
    )
    first = handle.result(raise_on_failure=False)
    second = handle.result(raise_on_failure=False)

    assert second is first
    assert first["doubled"] == [2, 4]


async def test_async_map_repeated_retrieval_returns_same_object() -> None:
    @node(output_name="doubled")
    async def double(value: int) -> int:
        return value * 2

    handle = AsyncRunner().start_map(
        Graph([double]),
        {"value": [1, 2]},
        map_over="value",
    )
    first = await handle.result(raise_on_failure=False)
    second = await handle.result(raise_on_failure=False)

    assert second is first
    assert first["doubled"] == [2, 4]


@pytest.mark.parametrize("operation", ["run", "map"])
async def test_async_blocking_operations_reject_zero_max_concurrency(
    operation: str,
) -> None:
    @node(output_name="doubled")
    async def double(value: int) -> int:
        return value * 2

    runner = AsyncRunner()
    graph = Graph([double])

    with pytest.raises(ValueError, match="max_concurrency must be >= 1"):
        if operation == "run":
            await asyncio.wait_for(
                runner.run(graph, value=1, max_concurrency=0),
                timeout=0.25,
            )
        else:
            await runner.map(
                graph,
                {"value": [1, 2]},
                map_over="value",
                max_concurrency=0,
            )


async def test_async_background_map_rejects_zero_max_concurrency_without_result() -> None:
    @node(output_name="doubled")
    async def double(value: int) -> int:
        return value * 2

    handle = AsyncRunner().start_map(
        Graph([double]),
        {"value": [1, 2]},
        map_over="value",
        max_concurrency=0,
    )

    errors = []
    for raise_on_failure in (False, True):
        with pytest.raises(ValueError, match="max_concurrency must be >= 1") as caught:
            await handle.result(raise_on_failure=raise_on_failure)
        errors.append(caught.value)

    assert errors[0] is errors[1]


async def test_async_background_run_rejects_zero_max_concurrency_without_hanging() -> None:
    @node(output_name="doubled")
    async def double(value: int) -> int:
        return value * 2

    handle = AsyncRunner().start_run(
        Graph([double]),
        value=1,
        max_concurrency=0,
    )

    with pytest.raises(ValueError, match="max_concurrency must be >= 1"):
        await asyncio.wait_for(handle.result(raise_on_failure=False), timeout=1)

    assert handle.done is True
