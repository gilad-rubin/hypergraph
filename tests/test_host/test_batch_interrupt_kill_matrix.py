"""Issue #342 — the real process-kill matrix for durable Batch interrupts.

Seven commit boundaries, each proven with an ACTUAL child process that
``SIGKILL``s itself (or is killed by this one) and a fresh process that
reopens the same SQLite file:

1. before the pause commit;
2. after the pause commit;
3. after answer settlement but before any claim;
4. after answer settlement, inside the worker's own release window;
5. after the claim but before the resumed runner execution;
6. during the resumed execution;
7. after the terminal Run commit but before the Batch observation.

``SIGKILL`` is uncatchable: no ``finally``, no flush, no chosen rollback runs
after it. A mocked exception would prove none of this, so none is used.

Two invariants every boundary asserts:

- **No accepted answer is lost, and no child is left permanently
  unclaimable.** A restart either re-asks an unanswered question or
  continues an answered one.
- **One accepted answer is applied once, and routes one way.** The
  append-only ledger records each create/replace/archive; every boundary
  asserts the target item's entries are exactly the single effect its
  answer chose. That is a claim about re-admission and routing, NOT a
  general exactly-once claim for external effects — see
  ``assert_answer_applied_once`` for exactly what is and is not asserted.

Boundary 6 kills *between* the routed decision and the terminal effect
node, deliberately: replaying a node that was killed mid-effect is the
external-effect-identity problem, which this slice does not claim to solve
(PRD 0017; issue #342 "Out of Scope").
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys

import pytest
import pytest_asyncio

from hypergraph import RunHome, RunHomeClient, RunRef, WaitingCondition, serve
from hypergraph.checkpointers.types import WorkflowStatus
from tests.test_host._ingestion_fixture import (
    LEDGER_ENV,
    answer_value,
    ingestion_graph,
    read_ledger,
)
from tests.test_host._kill_worker import TARGET_ITEM

aiosqlite = pytest.importorskip("aiosqlite")

pytestmark = pytest.mark.host_batch_interrupt_kill

#: The manifest every kill scenario submits: one item that pauses, and two
#: clean siblings that must be unaffected by the kill.
ITEMS = ["work-clean-a", TARGET_ITEM, "work-clean-b"]
BATCH_ID = "drop-kill"
TARGET_RUN_ID = f"{BATCH_ID}:{TARGET_ITEM}"


@pytest.fixture
def world(tmp_path):
    """A fresh SQLite Run Home path plus its effect ledger path."""
    return str(tmp_path / "runs.db"), str(tmp_path / "effects.log")


@pytest_asyncio.fixture
async def submitted(world):
    """Accept the Batch in THIS process, then hand the file to children."""
    db, ledger = world
    home = RunHome.open(f"file:{db}")
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await host.submit_batch(
        graph,
        {"work_item_id": ITEMS},
        map_over="work_item_id",
        identity="work_item_id",
        workflow_id=BATCH_ID,
    )
    await home.close()
    return receipt, db, ledger


def spawn(db: str, ledger: str, boundary: str) -> subprocess.Popen:
    """Start a real worker child process against the same Run Home."""
    env = {**os.environ, LEDGER_ENV: ledger, "PYTHONUNBUFFERED": "1"}
    return subprocess.Popen(
        [sys.executable, "-m", "tests.test_host._kill_worker", db, ledger, boundary],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def await_death(child: subprocess.Popen, timeout: float = 60.0) -> int:
    """Wait for the child and assert it really died by signal, not cleanly."""
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:  # pragma: no cover - diagnostic path
        child.kill()
        child.wait(timeout=10)
        raise AssertionError(f"worker never reached its kill boundary; stderr={child.stderr.read()!r}") from None
    assert child.returncode == -signal.SIGKILL, f"expected SIGKILL, got {child.returncode}; stderr={child.stderr.read()!r}"
    return child.returncode


def kill_now(child: subprocess.Popen) -> None:
    """SIGKILL a child this process is driving, and reap it."""
    child.send_signal(signal.SIGKILL)
    child.wait(timeout=30)
    assert child.returncode == -signal.SIGKILL


async def reopen(db: str):
    """A FRESH RunHome object on the same file — the restart under test."""
    return RunHome.open(f"file:{db}")


async def until(check, timeout: float = 45.0, interval: float = 0.05):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await check()
        if value:
            return value
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(interval)


async def wait_paused(db: str):
    """Poll the reopened store until the target child is durably paused."""
    home = await reopen(db)
    try:
        return await until(lambda: _paused_slot(home))
    finally:
        await home.close()


async def _paused_slot(home):
    run = await home.get_run_async(TARGET_RUN_ID)
    if run is None or run.status is not WorkflowStatus.PAUSED:
        return None
    slot = await home.get_pause_slot(TARGET_RUN_ID)
    return slot if slot is not None and slot.is_open else None


async def drive_to_completion(db: str, ledger: str, batch_ref, *, answer: dict | None = None):
    """Run a fresh worker in-process until the Batch settles.

    This is the RESTART: a new RunHome object, a new Host, a new worker, on
    a database an unrelated process left behind.
    """
    os.environ[LEDGER_ENV] = ledger
    home = await reopen(db)
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    client = host.client
    task = asyncio.create_task(host.work_forever("restart-worker", poll_interval=0.01))
    try:
        if answer is not None:
            slot = await until(lambda: _open_slot(home))
            await client.answer(_ref(home), pause_id=slot.pause_id, value=answer)
        view = await until(lambda: _settled(client, batch_ref))
        return view, home
    finally:
        host.shutdown()
        await asyncio.wait_for(task, timeout=45)


def _ref(home) -> RunRef:
    """The target child's inert address on this reopened Home."""
    return RunRef(home=home.uri, run_id=TARGET_RUN_ID)


async def _open_slot(home):
    slot = await home.get_pause_slot(TARGET_RUN_ID)
    run = await home.get_run_async(TARGET_RUN_ID)
    if slot is None or not slot.is_open or run is None or run.status is not WorkflowStatus.PAUSED:
        return None
    return slot


async def _settled(client, batch_ref):
    view = await client.get(batch_ref)
    return view if view is not None and view.settled else None


def assert_clean_siblings(view):
    """The kill touched one item; the other two are ordinary completions."""
    assert view.outcomes["work-clean-a"] == "completed"
    assert view.outcomes["work-clean-b"] == "completed"


def assert_answer_applied_once(ledger: str, target_effect: str, *siblings: str):
    """The falsifier for what the Host actually owns, claimed exactly.

    THE CLAIM, in full: **one accepted answer re-admits its child exactly
    once and routes it down exactly one branch.** So at every boundary
    below, the target item's ledger entries are the single effect its
    answer chose — never one decision applied twice, never a branch nobody
    chose. That is a statement about the re-admission and routing boundary,
    which the Host owns end to end.

    Two things this deliberately does NOT claim:

    - **Exactly-once external effects, in general.** The ledger append is a
      plain file write inside an ordinary node, not a declared effect, so
      it is not atomic with that node's checkpoint. A kill landing between
      the two re-executes the node on restart. No boundary here stages that
      kill — boundary 6 stops one node short of the effect on purpose —
      because effect identity is out of scope for issue #342 (PRDs 0013,
      0014). What survives a kill *mid-effect* is not asserted anywhere in
      this file, and must not be read into these ledger assertions.
    - **Exactly-once for the clean siblings.** They execute concurrently
      with a kill aimed at the target and are asserted PRESENT, not
      counted, for the same reason.
    """
    recorded = read_ledger(ledger)
    assert [entry for entry in recorded if TARGET_ITEM in entry] == [target_effect], recorded
    for sibling in siblings:
        assert sibling in recorded, recorded
    # No third kind of outcome ever reaches the ledger.
    assert set(recorded) == {target_effect, *siblings}, recorded


# === Boundary 1: before the pause commit ===


class TestBoundary1BeforePauseCommit:
    async def test_the_question_is_asked_again_and_nothing_is_fabricated(self, submitted):
        receipt, db, ledger = submitted

        child = spawn(db, ledger, "before_pause_commit")
        await_death(child)

        # No slot was committed, so no question exists yet — and crucially
        # no answer was invented for one that was never asked.
        home = await reopen(db)
        try:
            assert await home.get_pause_slot(TARGET_RUN_ID) is None
        finally:
            await home.close()

        view, home = await drive_to_completion(db, ledger, receipt.batch_ref, answer=answer_value("create_new"))
        try:
            assert view.outcomes[TARGET_ITEM] == "completed"
            assert_clean_siblings(view)
            slots = await home.get_pause_slot(TARGET_RUN_ID)
            assert slots is not None and slots.answer == answer_value("create_new")
        finally:
            await home.close()
        assert_answer_applied_once(ledger, f"created:{TARGET_ITEM}", "created:work-clean-a", "created:work-clean-b")


# === Boundary 2: after the pause commit ===


class TestBoundary2AfterPauseCommit:
    async def test_the_committed_question_survives_and_stays_answerable(self, submitted):
        receipt, db, ledger = submitted

        child = spawn(db, ledger, "after_pause_commit")
        await_death(child)

        home = await reopen(db)
        try:
            # The slot survived the death of the process that wrote it.
            slot = await home.get_pause_slot(TARGET_RUN_ID)
            assert slot is not None and slot.is_open and slot.answer is None
            assert slot.response_key == "duplicate_decision"
            assert (await home.get_run_async(TARGET_RUN_ID)).status is WorkflowStatus.PAUSED
            first_pause_id = slot.pause_id
        finally:
            await home.close()

        view, home = await drive_to_completion(db, ledger, receipt.batch_ref, answer=answer_value("replace_existing", 3143))
        try:
            # The restart re-asked the SAME occurrence, not a new one.
            slot = await home.get_pause_slot(TARGET_RUN_ID)
            assert slot.pause_id == first_pause_id
            assert view.outcomes[TARGET_ITEM] == "completed"
            assert_clean_siblings(view)
        finally:
            await home.close()
        assert_answer_applied_once(ledger, f"replaced:{TARGET_ITEM}:3143", "created:work-clean-a", "created:work-clean-b")


# === Boundary 3: after answer settlement, before any claim ===


class TestBoundary3AfterAnswerBeforeClaim:
    async def test_an_accepted_answer_is_never_lost_by_process_death(self, submitted):
        receipt, db, ledger = submitted

        # A worker reaches the pause, then dies where it stands.
        child = spawn(db, ledger, "plain")
        await wait_paused(db)
        kill_now(child)

        # The operator answers with NO worker alive anywhere.
        home = await reopen(db)
        try:
            slot = await home.get_pause_slot(TARGET_RUN_ID)
            settled = await home.settle_pause(TARGET_RUN_ID, pause_id=slot.pause_id, value=answer_value("archive_duplicate", 77))
            assert settled.answer == answer_value("archive_duplicate", 77)
            # The answer AND the re-admission are both durable already.
            assert (await home._get_submission(TARGET_RUN_ID))["state"] == "pending"
            kinds = [k for _s, k, _p, _a in await home._read_batch_updates(receipt.batch_ref.batch_id)]
            assert kinds.count("child_runnable") == 1
        finally:
            await home.close()

        view, home = await drive_to_completion(db, ledger, receipt.batch_ref)
        try:
            assert view.outcomes[TARGET_ITEM] == "completed"
            assert_clean_siblings(view)
        finally:
            await home.close()
        assert_answer_applied_once(ledger, f"archived:{TARGET_ITEM}:77", "created:work-clean-a", "created:work-clean-b")


# === Boundary 4: after the answer, inside the worker's release window ===


class TestBoundary4AfterAnswerBeforeRelease:
    """THE race the release window used to be.

    A live worker owns the claim record it took before running. Its child
    pauses, the operator answers, and the process dies before it ever
    releases. If the release still owned a required transition, the answer
    would be durable and the child unclaimable forever.
    """

    async def test_an_answer_in_the_release_window_survives_the_worker(self, submitted):
        receipt, db, ledger = submitted

        child = spawn(db, ledger, "after_answer_before_release")
        await wait_paused(db)

        # Answer with the worker still alive and still holding the claim.
        home = await reopen(db)
        try:
            slot = await home.get_pause_slot(TARGET_RUN_ID)
            await home.settle_pause(TARGET_RUN_ID, pause_id=slot.pause_id, value=answer_value("archive_duplicate", 91))
        finally:
            await home.close()

        await_death(child)

        home = await reopen(db)
        try:
            # The ANSWER transaction alone left the child claimable, and
            # said so on the Batch stream exactly once. The dead worker
            # contributed nothing to either fact.
            assert (await home._get_submission(TARGET_RUN_ID))["state"] == "pending"
            kinds = [k for _s, k, _p, _a in await home._read_batch_updates(receipt.batch_ref.batch_id)]
            assert kinds.count("child_paused") == 1
            assert kinds.count("child_runnable") == 1
            # Nothing continued it before the kill.
            assert (await home.get_run_async(TARGET_RUN_ID)).status is WorkflowStatus.PAUSED
            assert [entry for entry in read_ledger(ledger) if TARGET_ITEM in entry] == []
        finally:
            await home.close()

        view, home = await drive_to_completion(db, ledger, receipt.batch_ref)
        try:
            assert view.outcomes[TARGET_ITEM] == "completed"
            assert_clean_siblings(view)
            # Exactly ONE continuation: the restart did not re-ask, and the
            # re-admission was never duplicated across the two processes.
            kinds = [k for _s, k, _p, _a in await home._read_batch_updates(receipt.batch_ref.batch_id)]
            assert kinds.count("child_runnable") == 1
            assert kinds.count("child_paused") == 1
        finally:
            await home.close()
        assert_answer_applied_once(ledger, f"archived:{TARGET_ITEM}:91", "created:work-clean-a", "created:work-clean-b")


# === Boundary 5: after the claim, before the resumed execution ===


class TestBoundary5AfterClaimBeforeResume:
    async def test_a_claim_lost_before_the_resume_is_re_adopted(self, submitted):
        receipt, db, ledger = submitted

        child = spawn(db, ledger, "plain")
        await wait_paused(db)
        kill_now(child)

        home = await reopen(db)
        try:
            slot = await home.get_pause_slot(TARGET_RUN_ID)
            await home.settle_pause(TARGET_RUN_ID, pause_id=slot.pause_id, value=answer_value("replace_existing", 5))
        finally:
            await home.close()

        # This worker claims the answered child and dies before running it.
        killer = spawn(db, ledger, "after_claim_before_resume")
        await_death(killer)

        home = await reopen(db)
        try:
            # Claimed at death, and still holding the settled answer.
            assert (await home._get_submission(TARGET_RUN_ID))["state"] == "claimed"
            assert (await home.get_pause_slot(TARGET_RUN_ID)).answer == answer_value("replace_existing", 5)
        finally:
            await home.close()

        view, home = await drive_to_completion(db, ledger, receipt.batch_ref)
        try:
            assert view.outcomes[TARGET_ITEM] == "completed"
            assert_clean_siblings(view)
        finally:
            await home.close()
        assert_answer_applied_once(ledger, f"replaced:{TARGET_ITEM}:5", "created:work-clean-a", "created:work-clean-b")


# === Boundary 6: during the resumed execution ===


class TestBoundary6DuringResumedRun:
    async def test_a_death_mid_resume_continues_without_duplicating_the_effect(self, submitted):
        receipt, db, ledger = submitted

        child = spawn(db, ledger, "plain")
        await wait_paused(db)
        kill_now(child)

        home = await reopen(db)
        try:
            slot = await home.get_pause_slot(TARGET_RUN_ID)
            await home.settle_pause(TARGET_RUN_ID, pause_id=slot.pause_id, value=answer_value("replace_existing", 42))
        finally:
            await home.close()

        # Dies after the routed decision commits, before the effect node.
        killer = spawn(db, ledger, "during_resumed_run")
        await_death(killer)

        home = await reopen(db)
        try:
            assert (await home.get_run_async(TARGET_RUN_ID)).status not in {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED}
            # The routed decision is committed history; the effect is not.
            names = [step.node_name for step in await home.get_steps(TARGET_RUN_ID)]
            assert "route_duplicate_decision" in names and "retarget_replacement" not in names
        finally:
            await home.close()
        assert read_ledger(ledger) == [] or f"replaced:{TARGET_ITEM}:42" not in read_ledger(ledger)

        view, home = await drive_to_completion(db, ledger, receipt.batch_ref)
        try:
            assert view.outcomes[TARGET_ITEM] == "completed"
            assert_clean_siblings(view)
        finally:
            await home.close()
        assert_answer_applied_once(ledger, f"replaced:{TARGET_ITEM}:42", "created:work-clean-a", "created:work-clean-b")


# === Boundary 7: after the terminal commit, before the Batch observation ===


class TestBoundary7AfterTerminalBeforeObservation:
    async def test_a_terminal_child_is_observed_after_the_writer_dies(self, submitted):
        receipt, db, ledger = submitted

        child = spawn(db, ledger, "plain")
        await wait_paused(db)
        kill_now(child)

        home = await reopen(db)
        try:
            slot = await home.get_pause_slot(TARGET_RUN_ID)
            await home.settle_pause(TARGET_RUN_ID, pause_id=slot.pause_id, value=answer_value("create_new"))
        finally:
            await home.close()

        # Dies after the terminal run commit, before releasing the claim.
        killer = spawn(db, ledger, "after_terminal_before_batch_observation")
        await_death(killer)

        home = await reopen(db)
        client = RunHomeClient(home)
        try:
            # The Run is terminal and the Batch fact is already durable, even
            # though the process that wrote them never got to release.
            assert (await home.get_run_async(TARGET_RUN_ID)).status is WorkflowStatus.COMPLETED
            facts = [
                (kind, payload) for _s, kind, payload, _a in await home._read_batch_updates(receipt.batch_ref.batch_id) if kind == "child_settled"
            ]
            assert any(TARGET_ITEM in payload for _kind, payload in facts)
            view = await client.get(receipt.batch_ref)
            assert view.outcomes[TARGET_ITEM] == "completed"
            assert view.items[TARGET_ITEM].waiting is None
        finally:
            await home.close()

        view, home = await drive_to_completion(db, ledger, receipt.batch_ref)
        try:
            assert view.outcomes[TARGET_ITEM] == "completed"
            assert_clean_siblings(view)
            # The stream accounts the item exactly once across both processes.
            kinds = [
                payload
                for _s, kind, payload, _a in await home._read_batch_updates(receipt.batch_ref.batch_id)
                if kind == "child_settled" and TARGET_ITEM in payload
            ]
            assert len(kinds) == 1
        finally:
            await home.close()
        assert_answer_applied_once(ledger, f"created:{TARGET_ITEM}", "created:work-clean-a", "created:work-clean-b")


# === Cross-boundary: a paused child is never silently dropped ===


class TestNoBoundaryLosesTheQuestion:
    async def test_the_batch_is_never_settled_while_the_question_is_open(self, submitted):
        receipt, db, ledger = submitted

        child = spawn(db, ledger, "plain")
        await wait_paused(db)
        kill_now(child)

        home = await reopen(db)
        client = RunHomeClient(home)
        try:
            view = await client.get(receipt.batch_ref)
            assert view.settled is False
            assert view.items[TARGET_ITEM].waiting is WaitingCondition.PAUSED
            # The parked item is counted paused, never active. The siblings
            # this kill left mid-flight are irrelevant to that rule, so the
            # assertion names the item rather than the whole batch.
            assert view.counts["paused"] == 1
            assert view.items[TARGET_ITEM].outcome is None
            assert view.items[TARGET_ITEM].started is True
        finally:
            await home.close()
