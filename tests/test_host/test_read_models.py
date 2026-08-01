"""Issue #379 — generic, derive-on-read Run Home views for products."""

from __future__ import annotations

import json

import pytest

from hypergraph import RUN_READ_STATUS_VALUES, RunHomeReadModel, RunQuery, serve
from hypergraph.host.views import TERMINAL_STATUS_VALUES
from tests.test_host._batch_interrupt import batch_where, paused_items, submit_ids, worker
from tests.test_host._ingestion_fixture import answer_value, ingestion_graph

pytest.importorskip("aiosqlite")


async def test_runs_pauses_and_batch_census_derive_from_one_durable_truth(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await submit_ids(host, graph, ["work-dup-1", "work-clean"], "drop-read")
    read = RunHomeReadModel(host.client)

    accepted = await read.get_batch(receipt.batch_ref)
    assert accepted is not None
    assert accepted.counts["queued"] == 2
    assert [item.word for item in accepted.items.values()] == ["waiting: queued", "waiting: queued"]

    pending = await read.get_run(accepted.items["work-dup-1"].run_ref)
    assert pending is not None
    assert (pending.status, pending.condition) == ("queued", "queued")
    assert pending.inputs == {"work_item_id": "work-dup-1"}
    assert pending.accepted_at is not None
    assert pending.started_at is None and pending.settled_at is None
    assert pending.pause is None

    async with worker(host):
        view = await batch_where(host.client, receipt.batch_ref, lambda value: len(paused_items(value)) == 1)
        parked_ref = view.items["work-dup-1"].run_ref

        parked = await read.get_run(parked_ref)
        assert parked is not None
        assert (parked.status, parked.condition) == ("paused", "paused")
        assert parked.started_at is not None and parked.settled_at is None
        assert parked.pause is not None
        assert parked.pause.ask["prompt"].startswith("Possible duplicate")
        assert parked.pause.ask["evidence"]
        assert parked.pause.answer_schema
        assert parked.pause.options is None  # this gate declares a structured answer
        assert await read.get_pause(parked_ref) == parked.pause

        census = await read.get_batch(receipt.batch_ref)
        assert census is not None
        assert census.counts == view.counts
        assert census.items["work-dup-1"].word == "waiting: paused"
        json.dumps(parked.to_dict())
        json.dumps(census.to_dict())


async def test_list_runs_keeps_query_order_and_the_closed_status_vocabulary(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    await submit_ids(host, graph, ["work-b", "work-a"], "drop-list")
    read = RunHomeReadModel(host.client)

    rows = await read.list_runs(RunQuery(definition=graph.name))

    # Same deterministic oldest-first order as RunHomeClient.list; workflow
    # id is the total tie-breaker when one Batch accepted both simultaneously.
    assert [row.inputs["work_item_id"] for row in rows] == ["work-a", "work-b"]
    assert all(row.status in RUN_READ_STATUS_VALUES for row in rows)
    assert TERMINAL_STATUS_VALUES <= RUN_READ_STATUS_VALUES


async def test_a_settled_unstarted_item_never_looks_queued(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await submit_ids(host, graph, ["work-never"], "drop-unstarted-read")
    read = RunHomeReadModel(host.client)
    batch = await host.client.get(receipt.batch_ref)
    item = batch.items["work-never"]

    await host.client.stop(receipt.batch_ref, info="withdrawn before pickup")
    async with worker(host):
        await batch_where(host.client, receipt.batch_ref, lambda value: value.settled)
    row = await read.get_run(item.run_ref)

    assert row is not None
    assert (row.status, row.condition) == ("stopped", "unstarted")
    assert row.started_at is None and row.settled_at is not None
    assert row.updated_at >= row.settled_at


async def test_answer_race_never_returns_a_paused_badge_without_an_open_ask(home, ledger, monkeypatch):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-answer-race")
    read = RunHomeReadModel(host.client)

    async with worker(host):
        view = await batch_where(host.client, receipt.batch_ref, lambda value: len(paused_items(value)) == 1)
        ref = view.items["work-dup-1"].run_ref
        slot = await host.client.get_run_slot(ref)

    original = home._get_submission
    answered = False

    async def stale_submission(run_id):
        nonlocal answered
        submission = await original(run_id)
        if not answered:
            answered = True
            await host.client.answer(ref, pause_id=slot.pause_id, value=answer_value("create_new"))
        return submission

    monkeypatch.setattr(home, "_get_submission", stale_submission)
    row = await read.get_run(ref)

    assert row is not None
    assert (row.status, row.condition, row.pause) == ("queued", "queued", None)


def test_sync_read_model_mirrors_pending_run_and_batch(home):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = host.submit_batch_sync(
        graph,
        {"work_item_id": ["work-dup-1"]},
        map_over="work_item_id",
        identity="work_item_id",
        workflow_id="drop-sync-read",
    )
    read = RunHomeReadModel(host.client)

    batch = read.get_batch_sync(receipt.batch_ref)
    assert batch is not None
    row = read.get_run_sync(batch.items["work-dup-1"].run_ref)
    assert row is not None
    assert (row.status, row.condition, row.inputs) == ("queued", "queued", {"work_item_id": "work-dup-1"})
