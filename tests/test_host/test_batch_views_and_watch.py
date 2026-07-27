"""Issue #342 — what a detached operator surface can SEE.

What this file falsifies:

1. A mixed Batch keeps clean / paused / answered / stopped / failed /
   unstarted siblings independent, and accounts every manifest item
   exactly once.
2. ``items[key].run_ref`` drives every item-scoped verb with no derived
   workflow-id string, and an item prints where it is.
3. The Batch stays unsettled — and its watch stays open — while any child
   is parked on a human.
4. The durable stream reports ``child_paused`` / ``child_runnable``, and
   reconnecting from a stored cursor replays with no gaps or repeats.
5. Terminal siblings never replay when another child pauses or resumes.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from hypergraph import (
    RunQuery,
    RunRef,
    WaitingCondition,
    serve,
    set_display_mode,
)
from hypergraph.checkpointers.types import WorkflowStatus
from tests.test_host._batch_interrupt import (
    answer_item,
    batch_where,
    collect,
    paused_items,
    submit_ids,
    worker,
)
from tests.test_host._ingestion_fixture import (
    answer_value,
    ingestion_graph,
    read_ledger,
)

aiosqlite = pytest.importorskip("aiosqlite")

pytestmark = pytest.mark.host_batch_interrupt


# === 1. A mixed Batch: six sibling conditions at once ===


class TestMixedBatchIndependence:
    async def test_clean_paused_answered_stopped_failed_and_unstarted_coexist(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        ids = [
            "work-clean",  # bypasses the interrupt entirely
            "work-dup-answer",  # pauses, then gets an answer
            "work-dup-stop",  # pauses, then gets stopped
            "work-boom",  # fails in staging
            "work-dup-hold",  # pauses and stays parked
            "work-never",  # stopped before it was ever admitted
        ]
        receipt = await submit_ids(host, graph, ids, "drop-mixed")
        client = host.client

        # Cancel one item BEFORE any worker runs: it never executes at all.
        accepted = await client.get(receipt.batch_ref)
        await client.stop(accepted.items["work-never"].run_ref, info="withdrawn before pickup")

        async with worker(host):
            view = await batch_where(
                client,
                receipt.batch_ref,
                lambda v: len(paused_items(v)) == 3 and v.outcomes["work-clean"] == "completed" and v.outcomes["work-boom"] == "failed",
            )
            assert view.counts["paused"] == 3 and view.counts["active"] == 0

            await answer_item(client, view.items["work-dup-answer"], "replace_existing", 3143)
            await client.stop(view.items["work-dup-stop"].run_ref, info="operator cancelled the upload")
            final = await batch_where(
                client,
                receipt.batch_ref,
                lambda v: v.outcomes["work-dup-answer"] == "completed" and v.outcomes["work-dup-stop"] == "stopped",
            )

        # Every sibling landed on its own truth, independently.
        assert final.outcomes == {
            "work-clean": "completed",
            "work-dup-answer": "completed",
            "work-dup-stop": "stopped",
            "work-boom": "failed",
            "work-dup-hold": None,
            "work-never": None,
        }
        assert paused_items(final) == ["work-dup-hold"]
        assert final.unstarted_items == ("work-never",)
        assert final.items["work-never"].started is False
        assert final.items["work-clean"].started is True
        assert final.settled is False  # one child is still parked on a human
        # Domain effects ran once each, and only for items that reached them.
        assert sorted(read_ledger(ledger)) == ["created:work-clean", "replaced:work-dup-answer:3143"]

    async def test_every_manifest_item_is_counted_exactly_once(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        ids = ["work-clean", "work-dup-1", "work-boom", "work-dup-2"]
        receipt = await submit_ids(host, graph, ids, "drop-count")

        async with worker(host):
            # Wait for the quiescent point: two items parked on people, the
            # other two settled. Asserting on `active` while a sibling is
            # still mid-flight would be a race, not a rule.
            view = await batch_where(
                host.client,
                receipt.batch_ref,
                lambda v: len(paused_items(v)) == 2 and v.outcomes["work-clean"] == "completed" and v.outcomes["work-boom"] == "failed",
            )

        assert sum(view.counts.values()) == len(ids) == len(view.items)
        # Nobody is executing, yet two items are unmistakably in flight.
        assert view.counts["paused"] == 2 and view.counts["active"] == 0
        assert view.settled is False


# === 2. Item-scoped control needs no derived workflow ids ===


class TestItemScopedControl:
    async def test_run_ref_drives_inspect_answer_watch_stop_and_rerun(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1", "work-clean"], "drop-item")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]

            # inspect + answer, both from the item's own inert address
            run_view = await client.get(item.run_ref)
            assert run_view.workflow_id == item.workflow_id
            slot = await client.get_run_slot(item.run_ref)
            assert slot.response_key == "duplicate_decision"
            assert slot.question["prompt"].startswith("Possible duplicate")
            await client.answer(item.run_ref, pause_id=slot.pause_id, value=answer_value("create_new"))

            # watch, from the same ref
            kinds = [update.kind for update in await collect(client.watch(item.run_ref)) if update.durable]
            assert "answer" in kinds and kinds[-1] == "status"
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

            # rerun the settled item, still from the ref
            rerun = await client.rerun(item.run_ref)
            assert rerun.workflow_id == f"{item.workflow_id}-retry-1"

        assert final.outcomes["work-dup-1"] == "completed"
        # Nothing above built a "<batch>:<item>" string by hand.
        assert item.run_ref == RunRef(home=home.uri, run_id="drop-item:work-dup-1")

    async def test_the_batch_run_query_filter_finds_the_same_children(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1", "work-clean"], "drop-query")

        async with worker(host):
            view = await batch_where(host.client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            paused = await host.client.list(RunQuery(batch=receipt.batch_ref, waiting=WaitingCondition.PAUSED))

        assert [run.run_ref for run in paused] == [view.items["work-dup-1"].run_ref]

    async def test_an_item_prints_where_it_is_without_being_destructured(self, home, ledger):
        """A repr an operator can read at a glance, in both display modes."""
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1", "work-clean"], "drop-repr")
        client = host.client

        pending = (await client.get(receipt.batch_ref)).items["work-dup-1"]
        assert repr(pending) == "BatchItem: work-dup-1 | waiting: queued | drop-repr:work-dup-1"

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            parked = view.items["work-dup-1"]
            assert repr(parked) == "BatchItem: work-dup-1 | waiting: paused | drop-repr:work-dup-1"

            html = parked._repr_html_()
            assert "Batch item: work-dup-1" in html
            assert "waiting: paused" in html and "drop-repr:work-dup-1" in html
            assert home.uri in html  # the address, not a derived id
            set_display_mode("plain")
            try:
                assert parked._repr_html_() is None  # falls back to __repr__
            finally:
                set_display_mode("rich")

            await answer_item(client, parked, "create_new")
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        # A settled item leads with its outcome, not with how it got there.
        assert repr(final.items["work-dup-1"]) == "BatchItem: work-dup-1 | completed | drop-repr:work-dup-1"

    async def test_an_item_that_never_ran_says_so_rather_than_looking_clean(self, home, ledger):
        """The repr uses the Batch's own word for it, not a second one."""
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-a", "work-b"], "drop-repr-unstarted")
        client = host.client

        accepted = await client.get(receipt.batch_ref)
        await client.stop(accepted.items["work-a"].run_ref, info="withdrawn before pickup")
        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        never_ran = view.items["work-a"]
        assert never_ran.started is False and never_ran.outcome is None
        assert view.unstarted_items == ("work-a",)
        assert repr(never_ran) == "BatchItem: work-a | unstarted | drop-repr-unstarted:work-a"
        assert "Started:</span> <span" in never_ran._repr_html_()


# === 3. The Batch stays unsettled while any child is paused ===


class TestBatchSettlementWithPausedChildren:
    async def test_a_paused_child_keeps_the_batch_and_its_watch_open(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-clean", "work-dup-1"], "drop-unsettled")
        client = host.client

        async with worker(host):
            # Both halves of the quiescent point, or `outcomes` below would
            # be a race against the sibling rather than a rule about pauses.
            view = await batch_where(
                client,
                receipt.batch_ref,
                lambda v: len(paused_items(v)) == 1 and v.outcomes["work-clean"] == "completed",
            )
            assert view.settled is False

            # The stream is still open while the decision is outstanding.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(_drain_batch_watch(client, receipt.batch_ref), timeout=0.6)

            await answer_item(client, view.items["work-dup-1"], "create_new")
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        assert final.settled is True
        assert await asyncio.wait_for(_drain_batch_watch(client, receipt.batch_ref), timeout=25) is not None


async def _drain_batch_watch(client, ref):
    """Drain until the stream ENDS — used to prove it has not ended yet.

    Callers wrap this in ``asyncio.wait_for``; ``aclosing`` makes the
    cancellation close the generator instead of leaving it for the garbage
    collector, which warning-as-error CI would surface.
    """
    kinds = []
    stream = client.watch(ref)
    async with contextlib.aclosing(stream):
        async for update in stream:
            if update.durable:
                kinds.append(update.kind)
    return kinds


# === 4. Batch watch reconnects with the durable pause facts ===


class TestDurableBatchStream:
    async def test_the_stream_reports_child_paused_and_child_runnable(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-clean", "work-dup-1"], "drop-stream")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            await answer_item(client, view.items["work-dup-1"], "archive_duplicate", 7)
            await batch_where(client, receipt.batch_ref, lambda v: v.settled)
            facts = [(u.cursor, u.kind, u.payload) for u in await collect(client.watch(receipt.batch_ref)) if u.durable]

        kinds = [kind for _c, kind, _p in facts]
        assert kinds[0] == "manifest"
        assert kinds.count("child_paused") == 1 and kinds.count("child_runnable") == 1
        assert kinds.index("child_paused") < kinds.index("child_runnable")
        # Both facts carry the logical item key AND the inert child address,
        # so a consumer never parses child workflow-id syntax.
        for kind in ("child_paused", "child_runnable"):
            payload = next(p for _c, k, p in facts if k == kind)
            assert payload["item_key"] == "work-dup-1"
            assert payload["run_ref"] == {"home": home.uri, "run_id": "drop-stream:work-dup-1"}
            assert payload["pause_id"].startswith("drop-stream:work-dup-1:")
        # The cursor sequence is gap-free.
        assert [c for c, _k, _p in facts] == [f"bseq:{n}" for n in range(1, len(facts) + 1)]

    async def test_reconnecting_from_a_stored_cursor_has_no_gaps_or_repeats(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-clean", "work-dup-1"], "drop-reconnect")
        client = host.client

        async with worker(host):
            # Detach mid-stream, exactly as a UI reconnect does.
            opening = await collect(
                client.watch(receipt.batch_ref),
                stop_when=lambda u: u.durable and u.kind == "child_paused",
            )
            first = [(u.cursor, u.kind) for u in opening if u.durable]
            cursor = first[-1][0]

            view = await client.get(receipt.batch_ref)
            await answer_item(client, view.items["work-dup-1"], "create_new")
            await batch_where(client, receipt.batch_ref, lambda v: v.settled)

            rest = [(u.cursor, u.kind) for u in await collect(client.watch(receipt.batch_ref, after=cursor)) if u.durable]

        joined = first + rest
        assert [c for c, _k in joined] == [f"bseq:{n}" for n in range(1, len(joined) + 1)]
        assert [k for _c, k in rest][0] != "child_paused"  # no repeat across the seam
        assert "child_runnable" in [k for _c, k in rest]


# === 5. Terminal siblings never replay ===


class TestTerminalSiblingsNeverReplay:
    async def test_a_completed_sibling_is_untouched_by_a_pause_and_a_resume(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-clean-a", "work-clean-b", "work-dup-1"], "drop-replay")
        client = host.client

        async with worker(host):
            # Wait for BOTH: the sibling settled AND the target parked.
            # Waiting only for the pause can snapshot a sibling that is
            # still executing, and "active" trivially differs from
            # "completed" — which would prove nothing about replay.
            view = await batch_where(
                client,
                receipt.batch_ref,
                lambda v: len(paused_items(v)) == 1 and v.outcomes["work-clean-a"] == "completed",
            )
            before = await home.get_run_async("drop-replay:work-clean-a")
            await answer_item(client, view.items["work-dup-1"], "create_new")
            await batch_where(client, receipt.batch_ref, lambda v: v.settled)
            after = await home.get_run_async("drop-replay:work-clean-a")

        assert before.status is WorkflowStatus.COMPLETED and before.completed_at is not None
        assert before.completed_at == after.completed_at and before.status == after.status
        # Each deterministic effect ran exactly once.
        assert sorted(read_ledger(ledger)) == ["created:work-clean-a", "created:work-clean-b", "created:work-dup-1"]
        assert len(read_ledger(ledger)) == 3
