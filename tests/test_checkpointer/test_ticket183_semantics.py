"""Outcome-level regression tests for the checkpointer-facing #183 contract."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, node
from hypergraph.checkpointers import (
    Checkpointer,
    CheckpointPolicy,
    MemoryCheckpointer,
    SqliteCheckpointer,
    SqliteRunInspector,
    StepRecord,
    StepStatus,
    WorkflowStatus,
)
from hypergraph.exceptions import WorkflowForkError
from hypergraph.runners import RunStatus


@pytest.fixture(params=["memory", "sqlite"])
async def async_checkpointer(request: pytest.FixtureRequest, tmp_path):
    checkpointer = MemoryCheckpointer() if request.param == "memory" else SqliteCheckpointer(str(tmp_path / "runs.db"))

    try:
        yield checkpointer
    finally:
        await checkpointer.close()


@pytest.fixture
def sync_checkpointer(tmp_path):
    checkpointer = SqliteCheckpointer(str(tmp_path / "sync-runs.db"))
    checkpointer._sync_db()
    try:
        yield checkpointer
    finally:
        if checkpointer._sync_conn is not None:
            checkpointer._sync_conn.close()


async def test_async_run_filters_distinguish_omitted_parent_from_none(async_checkpointer):
    assert (
        await async_checkpointer.list_runs(
            graph_name="missing",
            since=datetime.now(timezone.utc),
            parent_run_id=None,
            limit=None,
        )
        == []
    )
    assert await async_checkpointer.count_runs(parent_run_id=None) == 0

    parent = await async_checkpointer.create_run("parent", graph_name="alpha")
    child_a = await async_checkpointer.create_run("child-a", graph_name="alpha", parent_run_id=parent.id)
    await async_checkpointer.create_run("child-b", graph_name="beta", parent_run_id=parent.id)
    await async_checkpointer.create_run("unrelated", graph_name="alpha")
    await async_checkpointer.update_run_status(child_a.id, WorkflowStatus.COMPLETED)

    all_runs = await async_checkpointer.list_runs(limit=None)
    top_level = await async_checkpointer.list_runs(parent_run_id=None, limit=None)
    children = await async_checkpointer.list_runs(parent_run_id=parent.id, limit=None)

    assert {run.id for run in all_runs} == {"parent", "child-a", "child-b", "unrelated"}
    assert {run.id for run in top_level} == {"parent", "unrelated"}
    assert {run.id for run in children} == {"child-a", "child-b"}
    assert await async_checkpointer.count_runs() == 4
    assert await async_checkpointer.count_runs(parent_run_id=None) == 2
    assert await async_checkpointer.count_runs(parent_run_id=parent.id) == 2

    alpha_runs = await async_checkpointer.list_runs(graph_name="alpha", limit=None)
    assert {run.id for run in alpha_runs} == {"parent", "child-a", "unrelated"}
    assert [run.created_at for run in alpha_runs] == sorted((run.created_at for run in alpha_runs), reverse=True)
    assert [run.id for run in await async_checkpointer.list_runs(graph_name="alpha", limit=1)] == ["unrelated"]

    boundary = child_a.created_at
    equivalent_offset = boundary.astimezone(timezone(timedelta(hours=5, minutes=30)))
    naive_utc = boundary.astimezone(timezone.utc).replace(tzinfo=None)
    expected_since = {"child-a", "child-b", "unrelated"}
    for since in (boundary, equivalent_offset, naive_utc):
        filtered = await async_checkpointer.list_runs(since=since, limit=None)
        assert {run.id for run in filtered} == expected_since
        composed = await async_checkpointer.list_runs(
            status=WorkflowStatus.COMPLETED,
            graph_name="alpha",
            since=since,
            parent_run_id=parent.id,
            limit=1,
        )
        assert [run.id for run in composed] == ["child-a"]

    if isinstance(async_checkpointer, SqliteCheckpointer):
        for since in (boundary, equivalent_offset, naive_utc):
            assert {run.id for run in async_checkpointer.runs(since=since, limit=None)} == expected_since


async def test_default_count_runs_omits_parent_filter_for_third_party_backends():
    backend_unset = object()

    class ThirdPartyCheckpointer(MemoryCheckpointer):
        count_runs = Checkpointer.count_runs

        def __init__(self):
            super().__init__()
            self.seen_parent_run_id: object | str | None = None

        async def list_runs(
            self,
            *,
            status=None,
            graph_name=None,
            since=None,
            parent_run_id=backend_unset,
            limit=100,
        ):
            self.seen_parent_run_id = parent_run_id
            if parent_run_id is not backend_unset:
                return []
            return await super().list_runs(
                status=status,
                graph_name=graph_name,
                since=since,
                limit=limit,
            )

    checkpointer = ThirdPartyCheckpointer()
    await checkpointer.create_run("run-1")

    assert await checkpointer.count_runs() == 1
    assert checkpointer.seen_parent_run_id is backend_unset
    assert await checkpointer.count_runs(parent_run_id=None) == 0
    assert checkpointer.seen_parent_run_id is None


async def test_retention_carriers_are_hidden_but_state_remains_folded(async_checkpointer):
    run_id = "retention-source"
    async_checkpointer.policy = CheckpointPolicy(durability="sync", retention="latest")
    await async_checkpointer.create_run(run_id)
    await async_checkpointer.save_step(
        StepRecord(
            run_id=run_id,
            superstep=0,
            node_name="a",
            index=0,
            status=StepStatus.COMPLETED,
            input_versions={},
            values={"x": 1},
            node_type=None,
        )
    )
    await async_checkpointer.save_step(
        StepRecord(
            run_id=run_id,
            superstep=0,
            node_name="b",
            index=1,
            status=StepStatus.COMPLETED,
            input_versions={},
            values={"y": 2},
            node_type=None,
        )
    )
    await async_checkpointer.save_step(
        StepRecord(
            run_id=run_id,
            superstep=1,
            node_name="a",
            index=2,
            status=StepStatus.COMPLETED,
            input_versions={},
            values={"x": 3},
            node_type=None,
        )
    )

    public_latest = await async_checkpointer.get_steps(run_id)
    assert {step.node_name for step in public_latest} == {"a", "b"}
    assert all(step.node_type != "RetentionBaseline" for step in public_latest)

    internal_latest = await async_checkpointer.get_steps(run_id, show_internal=True)
    assert any(step.node_name == "__retained_state__" for step in internal_latest)
    assert any(step.node_type == "RetentionBaseline" for step in internal_latest)
    assert any(step.node_name == "b" and step.node_type is None for step in public_latest)
    assert await async_checkpointer.get_state(run_id) == {"x": 3, "y": 2}

    async_checkpointer.policy = CheckpointPolicy(durability="sync", retention="windowed", window=1)
    await async_checkpointer.save_step(
        StepRecord(
            run_id=run_id,
            superstep=2,
            node_name="c",
            index=3,
            status=StepStatus.COMPLETED,
            input_versions={},
            values={"z": 4},
            node_type=None,
        )
    )

    async_checkpointer.policy = CheckpointPolicy(durability="sync", retention="full")
    await async_checkpointer.save_step(
        StepRecord(
            run_id=run_id,
            superstep=3,
            node_name="__retained_state__",
            index=4,
            status=StepStatus.COMPLETED,
            input_versions={},
            values={"name_carrier": 5},
            node_type="LegacyCarrier",
        )
    )
    await async_checkpointer.save_step(
        StepRecord(
            run_id=run_id,
            superstep=4,
            node_name="legacy-carrier",
            index=5,
            status=StepStatus.COMPLETED,
            input_versions={},
            values={"type_carrier": 6},
            node_type="RetentionBaseline",
        )
    )

    public_windowed = await async_checkpointer.get_steps(run_id)
    internal_windowed = await async_checkpointer.get_steps(run_id, show_internal=True)
    assert [step.node_name for step in public_windowed] == ["c"]
    assert any(step.node_name == "__retained_state__" for step in internal_windowed)
    assert any(step.node_name == "legacy-carrier" for step in internal_windowed)
    expected_state = {
        "x": 3,
        "y": 2,
        "z": 4,
        "name_carrier": 5,
        "type_carrier": 6,
    }
    assert await async_checkpointer.get_state(run_id) == expected_state

    checkpoint = await async_checkpointer.get_checkpoint(run_id)
    assert [step.node_name for step in checkpoint.steps] == ["c"]
    assert checkpoint.values == expected_state

    calls = 0

    @node(output_name="total")
    def add_retained_values(
        x: int,
        y: int,
        z: int,
        name_carrier: int,
        type_carrier: int,
    ) -> int:
        nonlocal calls
        calls += 1
        return x + y + z + name_carrier + type_carrier

    resumed = await AsyncRunner(checkpointer=async_checkpointer).run(
        Graph([add_retained_values]),
        checkpoint=checkpoint,
        workflow_id="retention-fork",
    )
    assert resumed["total"] == 20
    assert calls == 1

    if isinstance(async_checkpointer, SqliteCheckpointer):
        assert [step.node_name for step in async_checkpointer.steps(run_id)] == ["c"]
        assert any(step.node_name == "__retained_state__" for step in async_checkpointer.steps(run_id, show_internal=True))
        assert async_checkpointer.state(run_id) == expected_state
        assert [step.node_name for step in async_checkpointer.checkpoint(run_id).steps] == ["c"]
        assert async_checkpointer.search("__retained_state__", field="node_name") == []
        assert await async_checkpointer.search_async("__retained_state__", field="node_name") == []
        assert async_checkpointer.search('"legacy-carrier"', field="node_name") == []
        assert "__retained_state__" not in async_checkpointer.stats(run_id)
        assert "legacy-carrier" not in async_checkpointer.stats(run_id)

        inspector = SqliteRunInspector(async_checkpointer)
        assert [step.node_name for step in inspector.steps(run_id)] == ["c"]
        assert len(inspector.steps(run_id, show_internal=True)) == len(internal_windowed)
        lineage = inspector.lineage(run_id)
        assert [step.node_name for step in lineage.steps_by_run[run_id]] == ["c"]

        sync_result = SyncRunner(checkpointer=async_checkpointer).run(
            Graph([add_retained_values]),
            checkpoint=async_checkpointer.checkpoint(run_id),
            workflow_id="sync-retention-fork",
        )
        assert sync_result["total"] == 20
        assert calls == 2


async def test_async_fork_ids_are_source_derived_and_retry_ids_stay_generic(async_checkpointer):
    @node(output_name="doubled")
    def double(x: int) -> int:
        return x * 2

    runner = AsyncRunner(checkpointer=async_checkpointer)
    graph = Graph([double])
    await runner.run(graph, {"x": 1}, workflow_id="source-a")
    await runner.run(graph, {"x": 2}, workflow_id="source-b")
    source_a_before = await async_checkpointer.get_run_async("source-a")
    source_b_before = await async_checkpointer.get_run_async("source-b")

    direct_id, _ = await async_checkpointer.fork_workflow_async("source-a")
    assert re.fullmatch(r"source-a-fork-[0-9a-f]{6}", direct_id)

    fork_a = await runner.run(graph, {"x": 10}, fork_from="source-a")
    fork_b = await runner.run(graph, {"x": 20}, fork_from="source-b")
    explicit = await runner.run(
        graph,
        {"x": 30},
        fork_from="source-a",
        workflow_id="exact-fork-target",
    )

    assert re.fullmatch(r"source-a-fork-[0-9a-f]{6}", fork_a.workflow_id or "")
    assert re.fullmatch(r"source-b-fork-[0-9a-f]{6}", fork_b.workflow_id or "")
    assert explicit.workflow_id == "exact-fork-target"
    assert (await async_checkpointer.get_run_async(fork_a.workflow_id)).forked_from == "source-a"
    assert (await async_checkpointer.get_run_async(fork_b.workflow_id)).forked_from == "source-b"
    assert await async_checkpointer.get_run_async("source-a") == source_a_before
    assert await async_checkpointer.get_run_async("source-b") == source_b_before

    should_fail = True

    @node(output_name="seed")
    def seed(x: int) -> int:
        return x

    @node(output_name="out")
    def flaky(seed: int) -> int:
        if should_fail:
            raise RuntimeError("transient")
        return seed * 10

    retry_graph = Graph([seed, flaky])
    failed = await runner.run(
        retry_graph,
        {"x": 5},
        workflow_id="async-retry-source",
        error_handling="continue",
    )
    assert failed.status == RunStatus.FAILED
    should_fail = False

    retried = await runner.run(retry_graph.with_entrypoint("flaky"), retry_from="async-retry-source")
    assert (retried.workflow_id or "").startswith("run-")
    assert not (retried.workflow_id or "").startswith("async-retry-source-")


async def test_async_fork_rejects_missing_and_nested_sources(async_checkpointer):
    @node(output_name="doubled")
    def double(x: int) -> int:
        return x * 2

    runner = AsyncRunner(checkpointer=async_checkpointer)
    graph = Graph([double])
    await runner.map(graph, {"x": [1]}, map_over="x", workflow_id="nested-parent")

    count_before = await async_checkpointer.count_runs()
    with pytest.raises((ValueError, WorkflowForkError), match="Unknown source"):
        await runner.run(graph, {"x": 2}, fork_from="missing-source")
    assert await async_checkpointer.count_runs() == count_before

    with pytest.raises(WorkflowForkError, match="How to fix:"):
        await runner.run(graph, {"x": 3}, fork_from="nested-parent/0")
    assert await async_checkpointer.count_runs() == count_before

    explicit_id, checkpoint = await async_checkpointer.fork_workflow_async(
        "nested-parent/0",
        workflow_id="explicit-nested-target",
    )
    assert explicit_id == "explicit-nested-target"
    assert checkpoint.source_run_id == "nested-parent/0"
    assert await async_checkpointer.count_runs() == count_before

    explicit = await runner.run(
        graph,
        {"x": 4},
        fork_from="nested-parent/0",
        workflow_id="runner-nested-target",
    )
    assert explicit.workflow_id == "runner-nested-target"
    assert (await async_checkpointer.get_run_async("runner-nested-target")).forked_from == "nested-parent/0"


def test_sync_fork_ids_are_source_derived_and_retry_ids_stay_generic(sync_checkpointer):
    @node(output_name="doubled")
    def double(x: int) -> int:
        return x * 2

    runner = SyncRunner(checkpointer=sync_checkpointer)
    graph = Graph([double])
    runner.run(graph, {"x": 1}, workflow_id="sync-source-a")
    runner.run(graph, {"x": 2}, workflow_id="sync-source-b")
    source_a_before = sync_checkpointer.get_run("sync-source-a")
    source_b_before = sync_checkpointer.get_run("sync-source-b")

    direct_id, _ = sync_checkpointer.fork_workflow("sync-source-a")
    assert re.fullmatch(r"sync-source-a-fork-[0-9a-f]{6}", direct_id)

    fork_a = runner.run(graph, {"x": 10}, fork_from="sync-source-a")
    fork_b = runner.run(graph, {"x": 20}, fork_from="sync-source-b")
    explicit = runner.run(
        graph,
        {"x": 30},
        fork_from="sync-source-a",
        workflow_id="sync-exact-fork-target",
    )

    assert re.fullmatch(r"sync-source-a-fork-[0-9a-f]{6}", fork_a.workflow_id or "")
    assert re.fullmatch(r"sync-source-b-fork-[0-9a-f]{6}", fork_b.workflow_id or "")
    assert explicit.workflow_id == "sync-exact-fork-target"
    assert sync_checkpointer.get_run(fork_a.workflow_id).forked_from == "sync-source-a"
    assert sync_checkpointer.get_run(fork_b.workflow_id).forked_from == "sync-source-b"
    assert sync_checkpointer.get_run("sync-source-a") == source_a_before
    assert sync_checkpointer.get_run("sync-source-b") == source_b_before

    should_fail = True

    @node(output_name="seed")
    def seed(x: int) -> int:
        return x

    @node(output_name="out")
    def flaky(seed: int) -> int:
        if should_fail:
            raise RuntimeError("transient")
        return seed * 10

    retry_graph = Graph([seed, flaky])
    failed = runner.run(
        retry_graph,
        {"x": 5},
        workflow_id="sync-retry-source",
        error_handling="continue",
    )
    assert failed.status == RunStatus.FAILED
    should_fail = False

    retried = runner.run(retry_graph.with_entrypoint("flaky"), retry_from="sync-retry-source")
    assert (retried.workflow_id or "").startswith("run-")
    assert not (retried.workflow_id or "").startswith("sync-retry-source-")


def test_sync_fork_rejects_missing_and_nested_sources(sync_checkpointer):
    @node(output_name="doubled")
    def double(x: int) -> int:
        return x * 2

    runner = SyncRunner(checkpointer=sync_checkpointer)
    graph = Graph([double])
    runner.map(graph, {"x": [1]}, map_over="x", workflow_id="sync-nested-parent")

    count_before = len(sync_checkpointer.runs(limit=None))
    with pytest.raises((ValueError, WorkflowForkError), match="Unknown source"):
        runner.run(graph, {"x": 2}, fork_from="sync-missing-source")
    assert len(sync_checkpointer.runs(limit=None)) == count_before

    with pytest.raises(WorkflowForkError, match="How to fix:"):
        runner.run(graph, {"x": 3}, fork_from="sync-nested-parent/0")
    assert len(sync_checkpointer.runs(limit=None)) == count_before

    explicit_id, checkpoint = sync_checkpointer.fork_workflow(
        "sync-nested-parent/0",
        workflow_id="sync-explicit-nested-target",
    )
    assert explicit_id == "sync-explicit-nested-target"
    assert checkpoint.source_run_id == "sync-nested-parent/0"
    assert len(sync_checkpointer.runs(limit=None)) == count_before

    explicit = runner.run(
        graph,
        {"x": 4},
        fork_from="sync-nested-parent/0",
        workflow_id="sync-runner-nested-target",
    )
    assert explicit.workflow_id == "sync-runner-nested-target"
    assert sync_checkpointer.get_run("sync-runner-nested-target").forked_from == "sync-nested-parent/0"
