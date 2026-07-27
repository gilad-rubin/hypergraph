"""Issue #342 — execution control, closed admission, and who wins a race.

What this file falsifies:

1. A tolerance trip CLOSES admission: answering a tripped Batch's paused
   child settles its question without re-admitting it, and an item that
   never began is still reported unstarted.
2. Stop is execution control, never a duplicate-resolution decision — a
   stopped child records no domain outcome and cannot then be answered.
3. Concurrent answers, and answer-versus-stop, elect exactly one committed
   outcome.
4. The worker's release window is not a race: the pause transaction parks
   the submission and the answer transaction re-admits it, so a release
   owns neither and can undo neither.
"""

from __future__ import annotations

import asyncio

import pytest

from hypergraph import (
    RunRef,
    serve,
)
from hypergraph.checkpointers.types import (
    AnswerRejectedError,
    PauseAlreadySettledError,
    PauseSlot,
)
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


# === 1. A tolerance trip closes admission, for good ===


class TestClosedAdmission:
    """A trip is not a pause in admission — it is the end of it."""

    async def test_answering_a_child_of_a_tripped_batch_does_not_reopen_admission(self, home, ledger):
        """A trip CLOSES admission — an answer cannot reopen it.

        A paused child never counts toward tolerance, so it is still parked
        when the Batch trips. Answering it returns it to claim order, and
        closed admission then settles it instead of running it: tolerance is
        a stop-the-line decision, not an advisory threshold.

        It settles as ``abandoned``, never ``unstarted``. It committed steps
        and could have landed side effects, so an operator must reconcile it
        before rerunning — the exact distinction ``unstarted`` would erase.
        """
        from hypergraph import BatchTolerance

        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(
            host,
            graph,
            ["work-dup-1", "work-boom-a", "work-boom-b"],
            "drop-tripped",
            tolerance=BatchTolerance(max_failed=1),
        )
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: v.tolerance_tripped and len(paused_items(v)) == 1)
            await answer_item(client, view.items["work-dup-1"], "create_new")
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        assert final.outcomes["work-dup-1"] == "abandoned"
        assert final.counts["failed"] == 2 and final.counts["abandoned"] == 1
        assert final.counts["unstarted"] == 0
        assert final.abandoned_items == ("work-dup-1",)
        # The decision was accepted, but no domain effect followed it.
        assert read_ledger(ledger) == []
        # Accounted exactly once, by the honest fact.
        kinds = [k for _s, k, _p, _a in await home._read_batch_updates(receipt.batch_ref.batch_id)]
        assert kinds.count("child_abandoned") == 1
        assert kinds.count("child_unstarted") == 0

    async def test_a_never_started_child_of_a_tripped_batch_is_still_unstarted(self, home, ledger):
        """The other half of the split: nothing ran, so nothing to reconcile."""
        from hypergraph import BatchTolerance

        graph = ingestion_graph()
        home.max_active_runs = 1  # keep later items from ever being claimed
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(
            host,
            graph,
            ["work-boom-a", "work-boom-b", "work-clean-c", "work-clean-d"],
            "drop-tripped-cold",
            tolerance=BatchTolerance(max_failed=1),
        )

        async with worker(host):
            final = await batch_where(host.client, receipt.batch_ref, lambda v: v.settled)

        assert final.tolerance_tripped is True
        assert final.counts["abandoned"] == 0
        assert set(final.unstarted_items) == {"work-clean-c", "work-clean-d"}
        assert all(final.outcomes[key] is None for key in final.unstarted_items)


# === 2. Stop is execution control, not a duplicate decision ===


class TestStopIsNotADecision:
    async def test_stopping_a_paused_child_records_no_domain_outcome(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1", "work-dup-2"], "drop-notadecision")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 2)
            await client.stop(view.items["work-dup-1"].run_ref, info="cancelled")
            await answer_item(client, view.items["work-dup-2"], "archive_duplicate", 5)
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        assert final.outcomes == {"work-dup-1": "stopped", "work-dup-2": "completed"}
        # A stop produced NO create/replace/archive effect; only the answer did.
        assert read_ledger(ledger) == ["archived:work-dup-2:5"]

    async def test_a_stopped_child_cannot_then_be_answered(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-stopped-answer")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            slot = await client.get_run_slot(item.run_ref)
            await client.stop(item.run_ref, info="cancelled")
            await batch_where(client, receipt.batch_ref, lambda v: v.settled)

            with pytest.raises(AnswerRejectedError, match="is stopped, not paused"):
                await client.answer(item.run_ref, pause_id=slot.pause_id, value=answer_value("create_new"))


# === 3. Races elect exactly one committed outcome ===


class TestRaces:
    async def test_concurrent_answers_elect_exactly_one_winner(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-race-answer")
        client = host.client

        async with worker(host) as task:
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            host.shutdown()
            await asyncio.wait_for(task, timeout=25)
            slot = await client.get_run_slot(item.run_ref)

            decisions = ["create_new", "replace_existing", "archive_duplicate", "create_new"]
            results = await asyncio.gather(
                *(client.answer(item.run_ref, pause_id=slot.pause_id, value=answer_value(decision, 1)) for decision in decisions),
                return_exceptions=True,
            )

        winners = [r for r in results if isinstance(r, PauseSlot)]
        losers = [r for r in results if isinstance(r, BaseException)]
        assert len(winners) == 1 and len(losers) == 3
        assert all(isinstance(loser, PauseAlreadySettledError) for loser in losers)
        # Exactly one committed answer, and exactly one runnable transition.
        assert (await client.get_run_slot(item.run_ref)).answer == winners[0].answer
        kinds = [k for _s, k, _p, _a in await home._read_batch_updates(receipt.batch_ref.batch_id)]
        assert kinds.count("child_runnable") == 1
        assert (await home._get_submission(item.workflow_id))["state"] == "pending"

    async def test_answer_versus_stop_resolves_by_commit_order(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-race-stop")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            slot = await client.get_run_slot(item.run_ref)
            ready = asyncio.Event()

            async def answer():
                await ready.wait()
                return await client.answer(item.run_ref, pause_id=slot.pause_id, value=answer_value("create_new"))

            async def stop():
                await ready.wait()
                return await client.stop(item.run_ref, info="cancelled")

            answer_task = asyncio.create_task(answer())
            stop_task = asyncio.create_task(stop())
            ready.set()
            answered, _stopped = await asyncio.gather(answer_task, stop_task, return_exceptions=True)
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        # Both commands are legal against a parked run; commit order decides
        # which terminal outcome the child reaches — and only one is recorded.
        assert final.outcomes["work-dup-1"] in {"completed", "stopped"}
        if isinstance(answered, BaseException):
            assert final.outcomes["work-dup-1"] == "stopped"
            assert read_ledger(ledger) == []
        else:
            assert len(read_ledger(ledger)) <= 1


# === 4. The worker's release window owns no transition ===


class TestTheReleaseWindowIsNotARace:
    """Each transition has ONE owner and ONE commit.

    The pause transaction parks the submission; the answer transaction
    re-admits it. ``_release_submission`` owns neither, so an answer that
    lands while the worker is still finishing cannot be missed — and process
    death anywhere in the window cannot separate a durable decision from a
    claimable run.
    """

    async def test_the_pause_transaction_parks_the_submission_itself(self, home, ledger):
        """No instant where the run is PAUSED but the submission is claimed."""
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-park")
        workflow_id = "drop-park:work-dup-1"
        observed: list[tuple[str, str]] = []

        original = home.record_pause

        async def observe(*args, **kwargs):
            result = await original(*args, **kwargs)
            # First read after the pause COMMITTED: both halves must agree.
            run = await home.get_run_async(workflow_id)
            submission = await home._get_submission(workflow_id)
            observed.append((run.status.value, submission["state"]))
            return result

        home.record_pause = observe  # type: ignore[method-assign]
        try:
            async with worker(host):
                await batch_where(host.client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
        finally:
            home.record_pause = original  # type: ignore[method-assign]

        assert observed == [("paused", "paused")]

    async def test_an_answer_inside_the_release_window_still_re_admits(self, home, ledger):
        """THE deterministic race: answer between pause commit and release."""
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-window")
        client = host.client
        workflow_id = "drop-window:work-dup-1"
        answered = asyncio.Event()

        original = home._release_submission

        async def answer_first(wid: str) -> None:
            # Hold the release open, answer, THEN let the worker release.
            if wid == workflow_id and not answered.is_set():
                slot = await home.get_pause_slot(wid)
                if slot is not None and slot.is_open:
                    await client.answer(RunRef(home=home.uri, run_id=wid), pause_id=slot.pause_id, value=answer_value("create_new"))
                    answered.set()
            await original(wid)

        home._release_submission = answer_first  # type: ignore[method-assign]
        try:
            async with worker(host):
                final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)
        finally:
            home._release_submission = original  # type: ignore[method-assign]

        assert answered.is_set()
        assert final.outcomes["work-dup-1"] == "completed"
        assert read_ledger(ledger) == ["created:work-dup-1"]
        # The release did NOT own the re-admission, and did not duplicate it.
        kinds = [k for _s, k, _p, _a in await home._read_batch_updates(receipt.batch_ref.batch_id)]
        assert kinds.count("child_runnable") == 1
        assert kinds.count("child_paused") == 1

    async def test_the_release_never_undoes_an_answer(self, home, ledger):
        """A release arriving after an answer is a compare-and-set no-op."""
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-noop")
        client = host.client

        async with worker(host) as task:
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            host.shutdown()
            await asyncio.wait_for(task, timeout=25)
            await answer_item(client, item, "create_new")

            assert (await home._get_submission(item.workflow_id))["state"] == "pending"
            # A late release for the same run must change nothing.
            await home._release_submission(item.workflow_id)

        assert (await home._get_submission(item.workflow_id))["state"] == "pending"
        kinds = [k for _s, k, _p, _a in await home._read_batch_updates(receipt.batch_ref.batch_id)]
        assert kinds.count("child_runnable") == 1

    async def test_a_watch_replay_shows_one_pause_and_one_runnable(self, home, ledger):
        """Reconnectable history, from the durable sequence alone."""
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-replay")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            await answer_item(client, view.items["work-dup-1"], "create_new")
            await batch_where(client, receipt.batch_ref, lambda v: v.settled)
            replayed = [u.kind for u in await collect(client.watch(receipt.batch_ref)) if u.durable]

        assert replayed.count("child_paused") == 1
        assert replayed.count("child_runnable") == 1
        assert replayed.index("child_paused") < replayed.index("child_runnable")
