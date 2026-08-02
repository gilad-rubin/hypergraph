"""Issue #386 — listing recent Batches without raw SQL over host_batches.

A bulk operator's first screen is "which sweeps did this Run Home accept,
and how is each one doing?". Before ``list_batches`` the only public read
was ``get(batch_ref)`` for a ref you already held, so a product that had
lost its refs (a dead notebook kernel, a restarted API process) had to
SELECT ``host_batches`` itself — reading a private schema it can never be
told has changed.
"""

from __future__ import annotations

import json

import pytest

from hypergraph import RunHomeReadModel, serve
from tests.test_host._batch_interrupt import batch_where, submit_ids, worker
from tests.test_host._ingestion_fixture import ingestion_graph

pytest.importorskip("aiosqlite")


async def test_recent_batches_list_newest_first_with_the_same_census_get_batch_reports(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    first = await submit_ids(host, graph, ["work-clean", "work-two"], "sweep-1")
    second = await submit_ids(host, graph, ["work-three"], "sweep-2")
    read = RunHomeReadModel(host.client)

    rows = await read.list_batches()

    # Newest acceptance first — the order an operator's "recent sweeps"
    # screen shows, without the product re-sorting anything.
    assert [row.workflow_id for row in rows] == ["sweep-2", "sweep-1"]
    assert [row.item_count for row in rows] == [1, 2]
    assert rows[0].created_at >= rows[1].created_at
    assert [row.batch_ref for row in rows] == [second.batch_ref, first.batch_ref]
    assert all(row.definition_id.name == graph.name for row in rows)
    assert all(not row.settled and not row.tolerance_tripped and row.retry_of is None for row in rows)

    # THE bucket ladder, not a second one: a listed census and the Batch it
    # opens can never tell different stories.
    detail = await read.get_batch(first.batch_ref)
    assert detail is not None
    assert rows[1].counts == detail.counts
    assert sum(rows[1].counts.values()) == rows[1].item_count
    json.dumps([row.to_dict() for row in rows])


async def test_the_listing_tracks_settlement_and_filters_by_definition(home, ledger):
    graph = ingestion_graph()
    other = ingestion_graph(name="other-definition")
    host = serve(graph, home=home, deployment_version="v1")
    other_host = serve(other, home=home, deployment_version="v1")
    receipt = await submit_ids(host, graph, ["work-clean"], "sweep-settling")
    await submit_ids(other_host, other, ["work-clean"], "sweep-other")
    read = RunHomeReadModel(host.client)

    assert [row.workflow_id for row in await read.list_batches(definition=graph.name)] == ["sweep-settling"]
    assert [row.workflow_id for row in await read.list_batches(definition="other-definition")] == ["sweep-other"]
    assert await read.list_batches(definition="never-served") == []

    async with worker(host):
        await batch_where(host.client, receipt.batch_ref, lambda view: view.settled)

    settled = next(row for row in await read.list_batches() if row.workflow_id == "sweep-settling")
    assert settled.settled
    assert settled.counts["completed"] == 1
    assert settled.counts["queued"] == 0


async def test_limit_caps_the_page_and_refuses_a_nonsense_request(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    for index in range(3):
        await submit_ids(host, graph, [f"work-{index}"], f"sweep-{index}")
    read = RunHomeReadModel(host.client)

    assert [row.workflow_id for row in await read.list_batches(limit=2)] == ["sweep-2", "sweep-1"]

    with pytest.raises(ValueError, match="limit must be a positive int"):
        await read.list_batches(limit=0)
    with pytest.raises(TypeError, match="definition must be a Definition name string"):
        await read.list_batches(definition=object())


async def test_an_empty_run_home_lists_nothing_rather_than_failing(home):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")

    assert await RunHomeReadModel(host.client).list_batches() == []


def test_sync_listing_mirrors_the_async_one(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    host.submit_batch_sync(
        graph,
        {"work_item_id": ["work-clean", "work-two"]},
        map_over="work_item_id",
        identity="work_item_id",
        workflow_id="sweep-sync",
    )
    read = RunHomeReadModel(host.client)

    rows = read.list_batches_sync()

    assert [row.workflow_id for row in rows] == ["sweep-sync"]
    assert rows[0].counts == read.get_batch_sync(rows[0].batch_ref).counts
    assert read.list_batches_sync(definition="never-served") == []


def test_the_listing_writes_nothing_to_the_run_home(home, ledger):
    """A read model READS: no rows change, whoever else owns the store."""
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    host.submit_batch_sync(
        graph,
        {"work_item_id": ["work-clean"]},
        map_over="work_item_id",
        identity="work_item_id",
        workflow_id="sweep-readonly",
    )
    read = RunHomeReadModel(host.client)
    before = home._sync_db().total_changes

    read.list_batches_sync()
    read.get_batch_sync(read.list_batches_sync()[0].batch_ref)

    assert home._sync_db().total_changes == before
