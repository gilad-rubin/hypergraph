"""Outcome-level regression tests for the checkpointer-facing #183 contract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hypergraph.checkpointers import MemoryCheckpointer, SqliteCheckpointer, WorkflowStatus


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
