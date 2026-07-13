"""Adversarial truth tests for the typed inspection artifact."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from hypergraph import AsyncRunner, Graph, RunStatus, SyncRunner, node
from hypergraph.runners._shared._inspect import MapInspectionSession


class _ExplodingGetCache:
    def __init__(self, error: RuntimeError) -> None:
        self.error = error

    def get(self, key: str) -> tuple[bool, Any]:
        raise self.error

    def set(self, key: str, value: Any) -> None:
        raise AssertionError("cache set must not run")


class _ExplodingSetCache:
    def __init__(self, error: RuntimeError) -> None:
        self.error = error

    def get(self, key: str) -> tuple[bool, Any]:
        return False, None

    def set(self, key: str, value: Any) -> None:
        raise self.error


def test_terminal_map_session_ignores_late_child_publication_and_settlement() -> None:
    @node(output_name="answer")
    def identity(value: int) -> int:
        return value

    completed = SyncRunner().run(
        Graph([identity], name="completed-map-sibling"),
        {"value": 1},
        inspect=True,
    )
    session = MapInspectionSession(
        graph_name="terminal-map",
        workflow_id="terminal-map",
        requested_count=3,
        map_over=("value",),
        map_mode="zip",
    )
    session.bind_run("terminal-map-run")
    session.claim_item(item_index=0, requested_inputs={"value": 1}, workflow_id="terminal-map/0")
    session.settle_item(item_index=0, result=completed)
    late_child = session.claim_item(item_index=1, requested_inputs={"value": 2}, workflow_id="terminal-map/1")
    session.claim_item(item_index=2, requested_inputs={"value": 3}, workflow_id="terminal-map/2")

    terminal = session.finish(
        status="failed",
        total_duration_ms=1.0,
        error=RuntimeError("batch failed"),
    )
    assert [(item.item_index, item.status) for item in terminal.items] == [
        (0, "completed"),
        (1, "failed"),
        (2, "failed"),
    ]
    assert terminal.items[0].run is completed.inspect().artifact

    with pytest.raises(RuntimeError, match="terminal"):
        session.claim_item(item_index=3, requested_inputs={"value": 4}, workflow_id="terminal-map/3")
    session.bind_run("terminal-map-run")
    with pytest.raises(RuntimeError, match="another batch run"):
        session.bind_run("different-map-run")
    assert session.finish(status="completed", total_duration_ms=3.0) is terminal
    late_child.finish(status="completed", total_duration_ms=2.0)
    session.settle_item(item_index=2, result=completed)

    assert session.snapshot() is terminal


async def test_nested_infrastructure_failure_settles_every_terminal_node_sync_and_async() -> None:
    sync_error = RuntimeError("sync child cache lookup failed")

    @node(output_name="answer", cache=True)
    def sync_leaf(value: int) -> int:
        raise AssertionError("executor must not run")

    sync_child = Graph([sync_leaf], name="sync-child")
    sync_result = SyncRunner(cache=_ExplodingGetCache(sync_error)).run(
        Graph([sync_child.as_node(name="child")], name="sync-outer"),
        {"value": 3},
        inspect=True,
        error_handling="continue",
    )

    async_error = RuntimeError("async child cache lookup failed")

    @node(output_name="answer", cache=True)
    async def async_leaf(value: int) -> int:
        raise AssertionError("executor must not run")

    async_child = Graph([async_leaf], name="async-child")
    async_result = await AsyncRunner(cache=_ExplodingGetCache(async_error)).run(
        Graph([async_child.as_node(name="child")], name="async-outer"),
        {"value": 3},
        inspect=True,
        error_handling="continue",
    )

    for result, error in ((sync_result, sync_error), (async_result, async_error)):
        artifact = result.inspect().artifact
        assert result.status is RunStatus.FAILED
        assert result.error is error
        assert artifact.terminal is True
        assert artifact.status == "failed"
        assert artifact.error is error
        assert artifact.failures == ()
        assert (artifact.nodes[0].qualified_name, artifact.nodes[0].status) == ("child", "failed")
        assert all(item.status != "running" for item in artifact.nodes)


async def test_run_level_error_survives_capture_degradation_and_raise_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hypergraph.runners._shared import template_async, template_sync

    sync_publications: list[tuple[Any, bool]] = []
    original_sync_session = template_sync.InspectionSession

    class RecordingSyncInspection(original_sync_session):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            self.subscribe(lambda artifact, urgent: sync_publications.append((artifact, urgent)))

    monkeypatch.setattr(template_sync, "InspectionSession", RecordingSyncInspection)
    sync_error = RuntimeError("sync cache save failed")

    @node(output_name="answer", cache=True)
    def sync_cached(value: int) -> int:
        return value * 2

    sync_graph = Graph([sync_cached], name="sync-infrastructure-error")
    sync_captured = SyncRunner(cache=_ExplodingSetCache(sync_error)).run(
        sync_graph,
        {"value": 4},
        inspect=True,
        error_handling="continue",
    )
    sync_degraded = SyncRunner(cache=_ExplodingSetCache(sync_error)).run(
        sync_graph,
        {"value": 4},
        error_handling="continue",
    )
    with pytest.raises(RuntimeError) as sync_raised:
        SyncRunner(cache=_ExplodingSetCache(sync_error)).run(sync_graph, {"value": 4}, inspect=True)

    async_publications: list[tuple[Any, bool]] = []
    original_async_session = template_async.InspectionSession

    class RecordingAsyncInspection(original_async_session):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            self.subscribe(lambda artifact, urgent: async_publications.append((artifact, urgent)))

    monkeypatch.setattr(template_async, "InspectionSession", RecordingAsyncInspection)
    async_error = RuntimeError("async cache save failed")

    @node(output_name="answer", cache=True)
    async def async_cached(value: int) -> int:
        return value * 2

    async_graph = Graph([async_cached], name="async-infrastructure-error")
    async_captured = await AsyncRunner(cache=_ExplodingSetCache(async_error)).run(
        async_graph,
        {"value": 4},
        inspect=True,
        error_handling="continue",
    )
    async_degraded = await AsyncRunner(cache=_ExplodingSetCache(async_error)).run(
        async_graph,
        {"value": 4},
        error_handling="continue",
    )
    with pytest.raises(RuntimeError) as async_raised:
        await AsyncRunner(cache=_ExplodingSetCache(async_error)).run(async_graph, {"value": 4}, inspect=True)

    assert sync_raised.value is sync_error
    assert async_raised.value is async_error
    assert any(urgent and not artifact.terminal and artifact.error is sync_error for artifact, urgent in sync_publications)
    assert any(urgent and not artifact.terminal and artifact.error is async_error for artifact, urgent in async_publications)
    for result, error, captured in (
        (sync_captured, sync_error, True),
        (sync_degraded, sync_error, False),
        (async_captured, async_error, True),
        (async_degraded, async_error, False),
    ):
        artifact = result.inspect().artifact
        assert result.error is error
        assert artifact.error is error
        assert artifact.captured is captured
        assert artifact.failures == ()
        assert artifact.nodes[0].failure is None


async def test_nested_container_keeps_its_own_elapsed_duration_sync_and_async() -> None:
    @node(output_name="prepared")
    def sync_prepare(value: int) -> int:
        time.sleep(0.03)
        return value

    @node(output_name="answer")
    def sync_fail(prepared: int) -> int:
        raise ValueError(f"cannot process {prepared}")

    sync_child = Graph([sync_prepare, sync_fail], name="timed-sync-child")
    sync_result = SyncRunner().run(
        Graph([sync_child.as_node(name="child")], name="timed-sync-outer"),
        {"value": 7},
        inspect=True,
        error_handling="continue",
    )

    @node(output_name="prepared")
    async def async_prepare(value: int) -> int:
        await asyncio.sleep(0.03)
        return value

    @node(output_name="answer")
    async def async_fail(prepared: int) -> int:
        raise ValueError(f"cannot process {prepared}")

    async_child = Graph([async_prepare, async_fail], name="timed-async-child")
    async_result = await AsyncRunner().run(
        Graph([async_child.as_node(name="child")], name="timed-async-outer"),
        {"value": 7},
        inspect=True,
        error_handling="continue",
    )

    for result, prepare_name, fail_name in (
        (sync_result, "child/sync_prepare", "child/sync_fail"),
        (async_result, "child/async_prepare", "child/async_fail"),
    ):
        nodes = {item.qualified_name: item for item in result.inspect().artifact.nodes}
        assert nodes["child"].duration_ms >= nodes[prepare_name].duration_ms
        assert nodes["child"].duration_ms > nodes[fail_name].duration_ms


def test_captured_mappings_are_read_only_shallow_snapshots() -> None:
    payload: list[str] = ["identity-must-survive"]

    @node(output_name="echoed")
    def echo(payload: list[str], item: int) -> list[str]:
        return payload

    graph = Graph([echo], name="snapshot-truth")
    run = SyncRunner().run(graph, {"payload": payload, "item": 1}, inspect=True)
    batch = SyncRunner().map(
        graph,
        {"payload": payload, "item": [1]},
        map_over="item",
        inspect=True,
    )

    inspected_node = run.inspect().artifact.nodes[0]
    requested_inputs = batch.inspect().artifact.items[0].requested_inputs
    assert inspected_node.inputs is not None
    assert inspected_node.outputs is not None
    assert requested_inputs is not None
    assert inspected_node.inputs["payload"] is payload
    assert inspected_node.outputs["echoed"] is payload
    assert requested_inputs["payload"] is payload
    with pytest.raises(TypeError):
        inspected_node.inputs["payload"] = []  # type: ignore[index]
    with pytest.raises(TypeError):
        inspected_node.outputs["echoed"] = []  # type: ignore[index]
    with pytest.raises(TypeError):
        requested_inputs["payload"] = []  # type: ignore[index]


async def test_degraded_nested_failure_has_one_qualified_leaf_node_sync_and_async() -> None:
    @node(output_name="decision")
    def sync_fail_leaf(customer_id: str) -> str:
        raise ValueError(f"manual review: {customer_id}")

    sync_child = Graph([sync_fail_leaf], name="sync-review-child")
    sync_result = SyncRunner().run(
        Graph([sync_child.as_node(name="child")], name="sync-review-outer"),
        {"customer_id": "maya-23"},
        error_handling="continue",
    )

    @node(output_name="decision")
    async def async_fail_leaf(customer_id: str) -> str:
        raise ValueError(f"manual review: {customer_id}")

    async_child = Graph([async_fail_leaf], name="async-review-child")
    async_result = await AsyncRunner().run(
        Graph([async_child.as_node(name="child")], name="async-review-outer"),
        {"customer_id": "maya-23"},
        error_handling="continue",
    )

    for result, leaf_name in (
        (sync_result, "child/sync_fail_leaf"),
        (async_result, "child/async_fail_leaf"),
    ):
        artifact = result.inspect().artifact
        assert [item.qualified_name for item in artifact.nodes] == ["child", leaf_name]
        assert [item.sequence for item in artifact.nodes] == [0, 1]
        assert sum(item.qualified_name == leaf_name for item in artifact.nodes) == 1
        leaf = artifact.nodes[1]
        assert leaf.status == "failed"
        assert leaf.failure is result.node_failures[0]
        assert leaf.inputs == {"customer_id": "maya-23"}
        assert artifact.failures == (leaf.failure,)
