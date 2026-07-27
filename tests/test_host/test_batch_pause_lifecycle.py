"""Issue #342 — one Batch child's pause/answer lifecycle.

What this file falsifies:

1. A paused child is visible, nonterminal, and holds no active-Run slot.
2. Answering commits the settled answer and the re-admission together; the
   worker then resumes the SAME checkpointed run, in ordinary claim order.
3. The typed answer routes the DOMAIN — create, replace, archive — and a
   value the slot's schema rejects leaves the question open.
4. A second occurrence is a NEW pause id; the settled first one is stale
   and can never make it runnable again.
"""

from __future__ import annotations

import asyncio

import pytest

from hypergraph import (
    WaitingCondition,
    serve,
)
from hypergraph.checkpointers.types import (
    AnswerRejectedError,
    PauseSlot,
    StalePauseError,
    WorkflowStatus,
)
from tests.test_host._batch_interrupt import (
    answer_item,
    batch_where,
    collect,
    paused_items,
    submit_ids,
    until,
    worker,
)
from tests.test_host._ingestion_fixture import (
    answer_value,
    ingestion_graph,
    looping_graph,
    read_ledger,
)

aiosqlite = pytest.importorskip("aiosqlite")

pytestmark = pytest.mark.host_batch_interrupt


async def _paused_facts(home, receipt) -> int:
    """How many ``child_paused`` rows this Batch's durable sequence carries."""
    updates = await home._read_batch_updates(receipt.batch_ref.batch_id)
    return [kind for _seq, kind, _payload, _at in updates].count("child_paused")


# === 1. A paused child holds no active-Run slot ===


class TestPausedChildHoldsNoSlot:
    async def test_a_paused_child_is_visible_nonterminal_and_frees_its_slot(self, home, ledger):
        graph = ingestion_graph()
        home.max_active_runs = 1
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1", "work-clean"], "drop-slot")
        client = host.client

        async with worker(host):
            # With a cap of ONE, the clean sibling can only complete if the
            # paused child gave its slot back.
            view = await batch_where(client, receipt.batch_ref, lambda v: v.outcomes["work-clean"] == "completed")

        item = view.items["work-dup-1"]
        assert item.status is WorkflowStatus.PAUSED and item.waiting is WaitingCondition.PAUSED
        assert item.outcome is None and item.started is True
        assert view.counts["paused"] == 1 and view.counts["active"] == 0
        # Nonterminal: the child is not settled, so neither is the Batch.
        assert view.settled is False
        assert (await home._get_submission(item.workflow_id))["state"] == "paused"

    async def test_a_park_that_moved_nothing_publishes_no_child_paused_fact(self, home, ledger):
        """The two occurrence facts are symmetric: no transition, no fact.

        ``child_runnable`` was already conditional on the re-admission
        compare-and-set matching; ``child_paused`` was appended whether the
        park moved the submission or not. A Batch stream would then carry a
        state change that never happened — and an accounting consumer that
        pairs pauses with answers would go permanently out of step.
        """
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-symmetry")
        client = host.client
        workflow_id = "drop-symmetry:work-dup-1"

        task = asyncio.create_task(host.work_forever("w-342-park"))
        await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
        host.shutdown()
        await asyncio.wait_for(task, timeout=25)

        assert (await home._get_submission(workflow_id))["state"] == "paused"
        assert await _paused_facts(home, receipt) == 1

        # A pause commit the park cannot act on: the submission is already
        # parked, so its compare-and-set on 'claimed' matches nothing. The
        # occurrence is a NEW pause id, so the repeat-occurrence dedupe is
        # not what has to suppress the fact — the park's own result is.
        await home.record_pause(
            PauseSlot(
                run_id=workflow_id,
                superstep=9,
                node_name="review_duplicate",
                node_path="review_duplicate",
                response_key="duplicate_decision",
                question={"prompt": "again?"},
                answer_schema={},
            )
        )

        assert (await home._get_submission(workflow_id))["state"] == "paused"
        assert await _paused_facts(home, receipt) == 1

    async def test_a_paused_child_is_stoppable_not_terminal(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-stoppable")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            receipt_stop = await client.stop(view.items["work-dup-1"].run_ref, info="cancelled")
            assert receipt_stop.duplicate is False
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        assert final.outcomes["work-dup-1"] == "stopped"
        assert read_ledger(ledger) == []


# === 2. Answering makes the child runnable; the worker resumes it ===


class TestAnsweredChildResumes:
    async def test_the_answer_and_the_runnable_transition_commit_together(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-atomic")
        client = host.client

        async with worker(host) as task:
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            host.shutdown()  # no worker races the assertion below
            await asyncio.wait_for(task, timeout=25)

            settled = await answer_item(client, item, "create_new")

        # One committed transaction moved BOTH halves.
        assert settled.answer == answer_value("create_new")
        assert (await home._get_submission(item.workflow_id))["state"] == "pending"
        assert (await client.get(item.run_ref)).waiting is WaitingCondition.QUEUED
        kinds = [kind for _seq, kind, _payload, _at in await home._read_batch_updates(receipt.batch_ref.batch_id)]
        assert kinds.count("child_runnable") == 1

    async def test_the_worker_resumes_the_same_checkpointed_run(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-resume")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            await answer_item(client, item, "replace_existing", 99)
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        # SAME workflow id, one runs row, resumed rather than restarted.
        assert final.outcomes["work-dup-1"] == "completed"
        run = await home.get_run_async(item.workflow_id)
        assert run.id == item.workflow_id and run.retry_of is None and run.forked_from is None
        assert read_ledger(ledger) == ["replaced:work-dup-1:99"]
        # The head node ran exactly once across both attempts: the resume
        # replayed checkpoint state, it did not re-execute the graph.
        steps = await home.get_steps(item.workflow_id)
        assert [step.node_name for step in steps].count("stage_candidate") == 1

    async def test_an_answered_child_re_enters_ordinary_claim_order(self, home, ledger):
        """Answering never jumps the queue: admission still gates the child."""
        graph = ingestion_graph()
        home.max_active_runs = 1
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-order")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            # Fill the single slot with unrelated work before answering.
            await answer_item(client, item, "create_new")
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        assert final.outcomes["work-dup-1"] == "completed"


# === 3. The answer routes the DOMAIN: create, replace, archive ===


class TestTypedAnswerRoutesTheDomain:
    @pytest.mark.parametrize(
        ("decision", "target", "effect"),
        [
            ("create_new", None, "created:work-dup-1"),
            ("replace_existing", 3143, "replaced:work-dup-1:3143"),
            ("archive_duplicate", 2718, "archived:work-dup-1:2718"),
        ],
    )
    async def test_each_decision_takes_its_own_branch(self, home, ledger, decision, target, effect):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-route")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            await answer_item(client, view.items["work-dup-1"], decision, target)
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        assert final.outcomes["work-dup-1"] == "completed"
        assert read_ledger(ledger) == [effect]

    async def test_a_value_failing_the_slot_schema_leaves_the_pause_open(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-schema")
        client = host.client

        async with worker(host) as task:
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            host.shutdown()
            await asyncio.wait_for(task, timeout=25)

            slot = await client.get_run_slot(item.run_ref)
            with pytest.raises(AnswerRejectedError, match="answer schema"):
                await client.answer(item.run_ref, pause_id=slot.pause_id, value="replace_existing")

        # Rejected before any write: still parked, still answerable.
        assert (await home._get_submission(item.workflow_id))["state"] == "paused"
        assert (await client.get_run_slot(item.run_ref)).is_open
        kinds = [kind for _s, kind, _p, _a in await home._read_batch_updates(receipt.batch_ref.batch_id)]
        assert "child_runnable" not in kinds


# === 4. A second occurrence is a new pause id ===


class TestLoopingOccurrences:
    async def test_answering_the_first_occurrence_parks_on_a_new_one(self, home, ledger):
        graph = looping_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await host.submit_batch(
            graph,
            {"work_item_id": ["work-loop"]},
            map_over="work_item_id",
            key_by="work_item_id",
            workflow_id="drop-loop",
        )
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-loop"]
            first = await client.get_run_slot(item.run_ref)
            await client.answer(item.run_ref, pause_id=first.pause_id, value=answer_value("create_new"))

            # It becomes runnable, resumes, and parks on the SECOND question.
            second = await until(lambda: _new_slot(client, item.run_ref, first.pause_id))
            assert second.pause_id != first.pause_id
            assert second.response_key == "second_answer" and second.is_open

            # The settled first occurrence can never make this one runnable.
            with pytest.raises(StalePauseError):
                await client.answer(item.run_ref, pause_id=first.pause_id, value=answer_value("archive_duplicate"))

            await client.answer(item.run_ref, pause_id=second.pause_id, value=answer_value("replace_existing"))
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)
            facts = [u.kind for u in await collect(client.watch(receipt.batch_ref)) if u.durable]

        assert final.outcomes["work-loop"] == "completed"
        assert read_ledger(ledger) == ["mid:create_new|replace_existing"]
        # A new occurrence earns its OWN paused/runnable pair.
        assert facts.count("child_paused") == 2 and facts.count("child_runnable") == 2


async def _new_slot(client, ref, previous_pause_id):
    slot = await client.get_run_slot(ref)
    return slot if slot is not None and slot.pause_id != previous_pause_id else None
