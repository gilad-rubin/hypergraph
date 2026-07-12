"""Outcome-level regression tests for the checkpointer-facing #183 contract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, node
from hypergraph.checkpointers import (
    CheckpointPolicy,
    MemoryCheckpointer,
    SqliteCheckpointer,
    SqliteRunInspector,
    StepRecord,
    StepStatus,
    WorkflowStatus,
)


@pytest.fixture(params=["memory", "sqlite"])
async def async_checkpointer(request: pytest.FixtureRequest, tmp_path):
    checkpointer = MemoryCheckpointer() if request.param == "memory" else SqliteCheckpointer(str(tmp_path / "runs.db"))

    try:
        yield checkpointer
    finally:
        await checkpointer.close()


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
