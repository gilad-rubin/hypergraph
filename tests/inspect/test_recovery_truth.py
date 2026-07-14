"""Executable truth checks for inspect recovery code and nested map identity."""

from __future__ import annotations

import asyncio
import copy
import html
import pickle
import re
import textwrap
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, node
from hypergraph.runners._shared._inspect import MapInspection, MapItemInspection, RunInspection
from hypergraph.runners._shared._inspect_html import build_inspection_payload
from hypergraph.runners._shared._inspect_transport import _native_failure_markup


def _nested_failure_graph() -> Graph:
    @node(output_name="reviewed")
    def review_customer(customer_id: str) -> str:
        if customer_id.startswith("reject-"):
            raise ValueError(f"manual review: {customer_id}")
        return f"approved:{customer_id}"

    inner = Graph([review_customer], name="inner-review")
    return Graph(
        [inner.as_node(name="review_group").map_over("customer_id")],
        name="outer-review",
    )


def _nested_values() -> dict[str, list[list[str]]]:
    return {
        "customer_id": [
            ["approve-outer-0", "reject-outer-0"],
            ["approve-outer-1", "reject-outer-1"],
        ]
    }


def _assert_nested_outer_and_inner_indexes(batch: Any) -> None:
    assert [failed.failure.item_index for failed in batch.failures] == [0, 1]
    artifact = batch.inspect()._artifact
    assert [item.item_index for item in artifact.items] == [0, 1]
    for outer_index, item in enumerate(artifact.items):
        assert item.run is not None
        leaf = next(node for node in item.run.nodes if node.qualified_name == "review_group/review_customer" and node.status == "failed")
        assert leaf.item_index == 1
        assert leaf.failure is not None
        assert leaf.failure.item_index == 1
        assert item.run.failures[0].item_index == outer_index


def test_sync_nested_map_public_failures_use_outer_indexes_while_leaf_keeps_inner_index() -> None:
    batch = SyncRunner().map(
        _nested_failure_graph(),
        _nested_values(),
        map_over="customer_id",
        inspect=True,
        error_handling="continue",
    )

    _assert_nested_outer_and_inner_indexes(batch)


@pytest.mark.asyncio
async def test_async_nested_map_public_failures_use_outer_indexes_while_leaf_keeps_inner_index() -> None:
    batch = await AsyncRunner().map(
        _nested_failure_graph(),
        _nested_values(),
        map_over="customer_id",
        inspect=True,
        error_handling="continue",
    )

    _assert_nested_outer_and_inner_indexes(batch)


def _failure_markup(artifact: RunInspection | MapInspection) -> str:
    payload = build_inspection_payload(
        artifact,
        delivery_state="saved",
        delivery_label="Saved snapshot",
    )
    kind = payload["kind"]
    assert kind in {"run", "map"}
    data = payload[kind]
    assert isinstance(data, dict)
    return _native_failure_markup(kind=kind, data=data, message={})


def _recovery_code(markup: str) -> str:
    match = re.search(
        r"Smallest useful (?:result evidence|recovery code):</p><pre><code>(.*?)</code></pre>",
        markup,
        flags=re.DOTALL,
    )
    assert match is not None
    return html.unescape(match.group(1).replace("<wbr>", ""))


def _failed_graph() -> Graph:
    @node(output_name="reviewed")
    def review(customer_id: str) -> str:
        raise ValueError(f"manual review: {customer_id}")

    return Graph([review], name="recovery-review")


def _execute_sync(code: str, *, runner: Any, graph: Graph, values: dict[str, Any]) -> dict[str, Any]:
    namespace = {"runner": runner, "graph": graph, "values": values}
    exec(code, namespace)
    return namespace


async def _execute_async(code: str, *, runner: Any, graph: Graph, values: dict[str, Any]) -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    source = "async def __snippet(runner, graph, values):\n" + textwrap.indent(code, "    ") + "\n    return locals()\n"
    exec(source, namespace)
    return await namespace["__snippet"](runner, graph, values)


def test_sync_run_and_map_recovery_code_executes_and_private_origin_survives_copy_pickle() -> None:
    graph = _failed_graph()
    runner = SyncRunner()
    run = runner.run(
        graph,
        {"customer_id": "maya-23"},
        inspect=True,
        error_handling="continue",
    )
    batch = runner.map(
        graph,
        {"customer_id": ["maya-23"]},
        map_over="customer_id",
        inspect=True,
        error_handling="continue",
    )
    started_run = runner.start_run(
        graph,
        {"customer_id": "maya-23"},
        inspect=True,
    ).result(raise_on_failure=False)
    started_batch = runner.start_map(
        graph,
        {"customer_id": ["maya-23"]},
        map_over="customer_id",
        inspect=True,
    ).result(raise_on_failure=False)

    for result in (run, batch, started_run, started_batch):
        assert result.inspect()._artifact._runner_kind == "sync"
    for result in (copy.deepcopy(run), copy.deepcopy(batch), pickle.loads(pickle.dumps(run)), pickle.loads(pickle.dumps(batch))):
        assert result.inspect()._artifact._runner_kind == "sync"

    for inspected_run in (run, started_run):
        run_code = _recovery_code(_failure_markup(inspected_run.inspect()._artifact))
        assert "result = runner.run(" in run_code
        assert "await runner.run(" not in run_code
        assert (
            _execute_sync(
                run_code,
                runner=runner,
                graph=graph,
                values={"customer_id": "maya-23"},
            )["failure"].item_index
            is None
        )
    for inspected_batch in (batch, started_batch):
        map_code = _recovery_code(_failure_markup(inspected_batch.inspect()._artifact))
        assert "batch = runner.map(" in map_code
        assert "await runner.map(" not in map_code
        assert (
            _execute_sync(
                map_code,
                runner=runner,
                graph=graph,
                values={"customer_id": ["maya-23"]},
            )["failure"].item_index
            == 0
        )


@pytest.mark.asyncio
async def test_async_run_and_map_recovery_code_executes_and_private_origin_survives_copy_pickle() -> None:
    graph = _failed_graph()
    runner = AsyncRunner()
    run = await runner.run(
        graph,
        {"customer_id": "maya-23"},
        inspect=True,
        error_handling="continue",
    )
    batch = await runner.map(
        graph,
        {"customer_id": ["maya-23"]},
        map_over="customer_id",
        inspect=True,
        error_handling="continue",
    )
    started_run = await runner.start_run(
        graph,
        {"customer_id": "maya-23"},
        inspect=True,
    ).result(raise_on_failure=False)
    started_batch = await runner.start_map(
        graph,
        {"customer_id": ["maya-23"]},
        map_over="customer_id",
        inspect=True,
    ).result(raise_on_failure=False)

    for result in (run, batch, started_run, started_batch):
        assert result.inspect()._artifact._runner_kind == "async"
    for result in (copy.deepcopy(run), copy.deepcopy(batch), pickle.loads(pickle.dumps(run)), pickle.loads(pickle.dumps(batch))):
        assert result.inspect()._artifact._runner_kind == "async"

    for inspected_run in (run, started_run):
        run_code = _recovery_code(_failure_markup(inspected_run.inspect()._artifact))
        assert "result = await runner.run(" in run_code
        run_locals = await _execute_async(
            run_code,
            runner=runner,
            graph=graph,
            values={"customer_id": "maya-23"},
        )
        assert run_locals["failure"].item_index is None
    for inspected_batch in (batch, started_batch):
        map_code = _recovery_code(_failure_markup(inspected_batch.inspect()._artifact))
        assert "batch = await runner.map(" in map_code
        map_locals = await _execute_async(
            map_code,
            runner=runner,
            graph=graph,
            values={"customer_id": ["maya-23"]},
        )
        assert map_locals["failure"].item_index == 0


@pytest.mark.parametrize("runner_kind", ["sync", "async"])
@pytest.mark.parametrize(
    ("source", "artifact_kind"),
    [
        ("start", "run"),
        ("start", "map"),
        ("run", "run"),
        ("run", "map"),
        ("batch", "map"),
    ],
)
def test_boundary_recovery_code_uses_origin_and_executes_without_unbound_result(
    runner_kind: str,
    source: str,
    artifact_kind: str,
) -> None:
    class BoundaryRunner:
        def run(self, *_args: Any, **kwargs: Any) -> Any:
            if source == "start":
                raise RuntimeError("start unavailable")
            return SimpleNamespace(error=RuntimeError("run boundary"), failure=None, summary=lambda: "failed")

        def map(self, *_args: Any, **kwargs: Any) -> Any:
            if source in {"start", "batch"}:
                raise RuntimeError(f"{source} unavailable")
            return SimpleNamespace(failures=[SimpleNamespace(error=RuntimeError("run boundary"), failure=None)])

    class AsyncBoundaryRunner:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            return BoundaryRunner().run(*args, **kwargs)

        async def map(self, *args: Any, **kwargs: Any) -> Any:
            return BoundaryRunner().map(*args, **kwargs)

    if artifact_kind == "map":
        boundary_run = (
            RunInspection(
                run_id="run-boundary",
                graph_name="boundary",
                workflow_id=None,
                item_index=0,
                status="failed",
                nodes=(),
                failures=(),
                total_duration_ms=1.0,
                captured=True,
                terminal=True,
                error=RuntimeError("run unavailable"),
                _runner_kind=runner_kind,
            )
            if source == "run"
            else None
        )
        artifact: RunInspection | MapInspection = MapInspection(
            run_id="pending" if source == "start" else f"{source}-boundary",
            graph_name="boundary",
            workflow_id=None,
            status="running" if source == "start" else "failed",
            map_over=("customer_id",),
            map_mode="zip",
            requested_count=1,
            items=(MapItemInspection(0, "failed", {"customer_id": "x"}, boundary_run),) if boundary_run else (),
            unstarted_item_indexes=(0,) if boundary_run is None else (),
            total_duration_ms=1.0,
            captured=True,
            terminal=source != "start",
            error=RuntimeError("batch unavailable") if source == "batch" else None,
            _runner_kind=runner_kind,
        )
    else:
        artifact = RunInspection(
            run_id="pending" if source == "start" else "run-boundary",
            graph_name="boundary",
            workflow_id=None,
            item_index=None,
            status="running" if source == "start" else "failed",
            nodes=(),
            failures=(),
            total_duration_ms=1.0,
            captured=True,
            terminal=source != "start",
            error=RuntimeError("run unavailable") if source == "run" else None,
            _runner_kind=runner_kind,
        )
    message: dict[str, object] = {"kind": "exception", "type_name": "RuntimeError", "text": "start unavailable"} if source == "start" else {}

    payload = build_inspection_payload(artifact, delivery_state="saved", delivery_label="Saved snapshot")
    kind = payload["kind"]
    data = payload[kind]
    assert isinstance(kind, str) and isinstance(data, dict)
    code = _recovery_code(_native_failure_markup(kind=kind, data=data, message=message))
    if runner_kind == "async":
        assert "await runner." in code
        asyncio.run(
            _execute_async(
                code,
                runner=AsyncBoundaryRunner(),
                graph=_failed_graph(),
                values={"customer_id": ["x"]},
            )
        )
    else:
        assert "await runner." not in code
        _execute_sync(code, runner=BoundaryRunner(), graph=_failed_graph(), values={"customer_id": ["x"]})
    assert "result.inspect()" not in code
    assert not re.search(r"^(?:result|batch)\.", code, flags=re.MULTILINE)


def test_unknown_degraded_origin_never_silently_emits_a_sync_rerun() -> None:
    artifact = replace(
        SyncRunner()
        .run(
            _failed_graph(),
            {"customer_id": "maya-23"},
            error_handling="continue",
        )
        .inspect()
        ._artifact,
        _runner_kind=None,
    )

    markup = _failure_markup(artifact)

    assert "runner.run(" not in markup
    assert "await runner.run(" not in markup


@pytest.mark.parametrize("runner_kind", ["sync", "async"])
def test_nested_map_native_snippets_execute_against_each_containing_outer_item(
    runner_kind: str,
) -> None:
    graph = _nested_failure_graph()
    values = _nested_values()
    if runner_kind == "sync":
        runner: SyncRunner | AsyncRunner = SyncRunner()
        batch = runner.map(
            graph,
            values,
            map_over="customer_id",
            inspect=True,
            error_handling="continue",
        )
    else:
        runner = AsyncRunner()

        async def run_batch():
            return await runner.map(
                graph,
                values,
                map_over="customer_id",
                inspect=True,
                error_handling="continue",
            )

        batch = asyncio.run(run_batch())

    payload = build_inspection_payload(
        batch.inspect()._artifact,
        delivery_state="saved",
        delivery_label="Saved snapshot",
    )
    data = payload["map"]
    assert isinstance(data, dict)
    items = data["items"]
    assert isinstance(items, list)

    for outer_index in (0, 1):
        selected_data = {**data, "items": [items[outer_index]]}
        code = _recovery_code(
            _native_failure_markup(
                kind="map",
                data=selected_data,
                message={},
            )
        )
        assert f"item.failure.item_index == {outer_index}" in code
        assert f"item.failure.item_index == {1 - outer_index}" not in code
        if runner_kind == "sync":
            namespace = _execute_sync(code, runner=runner, graph=graph, values=values)
        else:
            assert isinstance(runner, AsyncRunner)
            namespace = asyncio.run(_execute_async(code, runner=runner, graph=graph, values=values))
        failure = namespace["failure"]
        assert failure.item_index == outer_index
        assert failure.inputs == {"customer_id": f"reject-outer-{outer_index}"}


@pytest.mark.parametrize("runner_kind", ["sync", "async"])
@pytest.mark.parametrize("outer_index", [0, 1])
def test_native_primary_failure_names_exact_nested_leaf_and_scalar_input(
    runner_kind: str,
    outer_index: int,
) -> None:
    graph = _nested_failure_graph()
    values = _nested_values()
    if runner_kind == "sync":
        batch = SyncRunner().map(
            graph,
            values,
            map_over="customer_id",
            inspect=True,
            error_handling="continue",
        )
    else:

        async def execute_batch():
            return await AsyncRunner().map(
                graph,
                values,
                map_over="customer_id",
                inspect=True,
                error_handling="continue",
            )

        batch = asyncio.run(execute_batch())

    payload = build_inspection_payload(
        batch.inspect()._artifact,
        delivery_state="saved",
        delivery_label="Saved snapshot",
    )
    data = payload["map"]
    assert isinstance(data, dict)
    items = data["items"]
    assert isinstance(items, list)
    markup = _native_failure_markup(
        kind="map",
        data={**data, "items": [items[outer_index]]},
        message={},
    )
    plain_markup = html.unescape(markup.replace("<wbr>", ""))

    assert "Qualified node: <code>review_group/review_customer</code>" in plain_markup
    assert f"customer_id=reject-outer-{outer_index}" in plain_markup
    assert f"approve-outer-{outer_index}" not in plain_markup
    assert f"ValueError: manual review: reject-outer-{outer_index}" in plain_markup
