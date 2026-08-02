"""Issue #386 — a human gate must not hold a watch open forever.

"Settled" and "resting" are different predicates, and only the second is
watchable when a durable interrupt is in play: a gate never answers itself,
so `watch(batch_ref)` on a Batch with one parked child waits for as long as
nobody looks at it. A 770-document re-ingest hit exactly this and hand-rolled
`resting()` over item statuses in the notebook.

`until="resting"` makes it the library's answer. It truncates the WAIT, never
the facts — `child_paused` commits in the same transaction as the pause it
reports, so it is already delivered when the stream ends — and it invents no
outcome: a parked child is still not settled, and watching again from the
stored cursor resumes exactly where the resting stream stopped.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from hypergraph import serve
from tests.test_host._batch_interrupt import answer_item, batch_where, collect, paused_items, submit_ids, worker
from tests.test_host._ingestion_fixture import ingestion_graph

pytest.importorskip("aiosqlite")


async def test_a_parked_child_ends_a_resting_watch_and_holds_a_settled_one(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await submit_ids(host, graph, ["work-dup-1", "work-clean"], "drop-resting")

    async with worker(host):
        await batch_where(host.client, receipt.batch_ref, lambda value: len(paused_items(value)) == 1 and value.counts["completed"] == 1)

        # Resting ARRIVES: nothing is running or queued, one item waits on a person.
        updates = await collect(host.client.watch(receipt.batch_ref, until="resting"), timeout=20.0)

        # And the facts were delivered, not truncated: the paused child's own
        # fact is in the stream that ended because of it.
        kinds = [update.kind for update in updates if update.durable]
        assert "manifest" in kinds and "child_settled" in kinds and "child_paused" in kinds

        # The default predicate is unchanged and still waits for the answer.
        stream = host.client.watch(receipt.batch_ref)
        async with contextlib.aclosing(stream):
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(collect(stream, timeout=15.0), timeout=1.5)


async def test_answering_resumes_the_stream_exactly_where_resting_left_it(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-resume-cursor")

    async with worker(host):
        view = await batch_where(host.client, receipt.batch_ref, lambda value: len(paused_items(value)) == 1)
        first = await collect(host.client.watch(receipt.batch_ref, until="resting"), timeout=20.0)
        cursor = [update.cursor for update in first if update.durable][-1]

        await answer_item(host.client, view.items["work-dup-1"], "create_new")
        second = await collect(host.client.watch(receipt.batch_ref, after=cursor), timeout=20.0)

    # No gaps, no repeats: the second stream starts after the first's last
    # durable cursor and carries the settlement the answer unlocked.
    durable = [update for update in second if update.durable]
    assert [update.cursor for update in durable] == sorted({update.cursor for update in durable}, key=lambda value: int(value.split(":")[1]))
    assert cursor not in {update.cursor for update in durable}
    assert "child_settled" in {update.kind for update in durable}
    assert (await host.client.get(receipt.batch_ref)).settled


async def test_a_parked_run_ends_its_own_resting_watch(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-run-resting")

    async with worker(host):
        view = await batch_where(host.client, receipt.batch_ref, lambda value: len(paused_items(value)) == 1)
        ref = view.items["work-dup-1"].run_ref

        updates = await collect(host.client.watch(ref, until="resting"), timeout=20.0)

        assert "status" in {update.kind for update in updates if update.durable}
        # Still parked — resting ended the WAIT, not the run.
        assert (await host.client.get(ref)).waiting.value == "paused"


async def test_resting_and_settled_are_the_same_arrival_when_nobody_is_asked(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await submit_ids(host, graph, ["work-clean"], "drop-no-gate")

    async with worker(host):
        updates = await collect(host.client.watch(receipt.batch_ref, until="resting"), timeout=20.0)

    view = await host.client.get(receipt.batch_ref)
    assert view.settled and view.resting
    assert "child_settled" in {update.kind for update in updates if update.durable}


async def test_the_batch_view_reports_resting_without_claiming_settled(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await submit_ids(host, graph, ["work-dup-1", "work-clean"], "drop-view-resting")

    assert not (await host.client.get(receipt.batch_ref)).resting  # queued work is not resting

    async with worker(host):
        view = await batch_where(host.client, receipt.batch_ref, lambda value: len(paused_items(value)) == 1 and value.counts["completed"] == 1)

    # A parked child rests; it does not settle. Both words stay honest.
    assert view.resting and not view.settled
    assert view.counts["active"] == 0 and view.counts["queued"] == 0 and view.counts["paused"] == 1


async def test_an_unknown_until_is_refused_with_both_options_named(home):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await submit_ids(host, graph, ["work-clean"], "drop-bad-until")

    with pytest.raises(ValueError, match="until must be 'settled' or 'resting'"):
        await collect(host.client.watch(receipt.batch_ref, until="done"))
