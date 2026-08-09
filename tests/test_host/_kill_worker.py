"""Child-process worker for the issue #342 kill matrix.

Run as ``python -m tests.test_host._kill_worker <db> <ledger> <boundary>``.
It opens the SAME SQLite Run Home the parent opened, serves the ingestion
Definition, and SIGKILLs ITSELF at one named commit boundary — a real
``os.kill(os.getpid(), SIGKILL)``, never a mocked exception, so nothing runs
after it: no ``finally``, no flush, no rollback the process chose. The parent
then reopens the database in a fresh process and asserts what survived.

Each boundary is armed by wrapping the fewest methods that make the kill land
on the exact side of the commit under test — usually one. ``plain`` arms
nothing: the parent kills that child itself, which is how the "answer
settled, nobody claimed it yet" boundary is staged.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hypergraph import RunHome, serve  # noqa: E402
from hypergraph.host.views import TERMINAL_WORKFLOW_STATUSES  # noqa: E402
from tests.test_host._ingestion_fixture import LEDGER_ENV, ingestion_graph  # noqa: E402

#: The item every boundary targets; siblings prove independence.
TARGET_ITEM = "work-dup-1"

#: The step whose commit precedes the terminal domain effect on resume. The
#: `during_resumed_run` kill lands right after it, so the effect node has not
#: run yet and cannot be replayed twice.
STEP_BEFORE_EFFECT = "route_duplicate_decision"

BOUNDARIES = (
    "before_pause_commit",
    "after_pause_commit",
    "plain",  # parent-driven: answer settles while no worker is alive
    "after_answer_before_release",
    "after_claim_before_resume",
    "during_resumed_run",
    "after_terminal_before_batch_observation",
)


def die() -> None:
    """Real, uncatchable process death."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.kill(os.getpid(), signal.SIGKILL)


async def _has_settled_answer(home: RunHome, workflow_id: str) -> bool:
    slot = await home.get_pause_slot(workflow_id)
    return slot is not None and slot.settled_at is not None


def _arm_pause_boundaries(home: RunHome, boundary: str) -> None:
    """Kill on either side of the atomic pause commit."""
    original = home.record_pause

    async def record_pause(slot, **kwargs):
        if TARGET_ITEM not in slot.run_id:
            return await original(slot, **kwargs)
        if boundary == "before_pause_commit":
            die()
        await original(slot, **kwargs)
        # The slot, the PAUSED status, the parked submission, and the
        # child_paused Batch fact are ONE commit; nothing else has run.
        die()

    home.record_pause = record_pause


def _arm_release_window_boundary(home: RunHome) -> None:
    """Kill INSIDE the release window, after the answer settled.

    Two things are armed together so the kill lands on the exact side of the
    commit rather than luckily: this process stops claiming the moment the
    target parks, and its release call holds open until the parent's answer
    is durable, then dies without ever releasing. Suppressing the re-claim
    is what pins the kill to the window — it is not what makes the answer
    survive. A worker that DOES re-claim inside the window is covered
    deterministically by
    ``test_batch_stop_and_races.TestTheReleaseWindowIsNotARace``'s
    ``test_a_stale_release_cannot_settle_a_fresh_claim``: the release
    compare-and-sets on the claim it held, so it is a no-op against any
    later one.
    """
    original_claim = home._claim_eligible
    original_release = home._release_submission
    frozen = asyncio.Event()

    async def claim_eligible(*args, **kwargs):
        return [] if frozen.is_set() else await original_claim(*args, **kwargs)

    async def release(workflow_id, claim_seq):
        if TARGET_ITEM not in workflow_id:
            return await original_release(workflow_id, claim_seq)
        frozen.set()
        while not await _has_settled_answer(home, workflow_id):
            await asyncio.sleep(0.005)
        die()

    home._claim_eligible = claim_eligible
    home._release_submission = release


def _arm_resume_boundaries(home: RunHome, host, boundary: str) -> None:
    """Kill around the RESUMED execution of an answered child."""
    if boundary == "after_claim_before_resume":
        original = host._execute_submission

        async def execute(row):
            if row["item_key"] == TARGET_ITEM and await _has_settled_answer(home, row["workflow_id"]):
                die()  # claimed, answer in hand, runner never invoked
            return await original(row)

        host._execute_submission = execute
        return

    original_save = home.save_step

    async def save_step(record):
        await original_save(record)
        if TARGET_ITEM in record.run_id and record.node_name == STEP_BEFORE_EFFECT:
            # Mid-resume: the routed decision is committed, the terminal
            # domain effect has NOT run. A restart must run it exactly once.
            die()

    home.save_step = save_step


def _arm_terminal_boundary(home: RunHome) -> None:
    """Kill after the terminal Run commit, before anything observes it."""
    original = home._release_submission

    async def release(workflow_id, claim_seq):
        run = await home.get_run_async(workflow_id)
        if TARGET_ITEM in workflow_id and run is not None and run.status in TERMINAL_WORKFLOW_STATUSES:
            die()  # runs row terminal + child_settled committed; claim outstanding
        return await original(workflow_id, claim_seq)

    home._release_submission = release


def arm(home: RunHome, host, boundary: str) -> None:
    if boundary in ("before_pause_commit", "after_pause_commit"):
        _arm_pause_boundaries(home, boundary)
    elif boundary == "after_answer_before_release":
        _arm_release_window_boundary(home)
    elif boundary in ("after_claim_before_resume", "during_resumed_run"):
        _arm_resume_boundaries(home, host, boundary)
    elif boundary == "after_terminal_before_batch_observation":
        _arm_terminal_boundary(home)


async def main() -> int:
    db, ledger, boundary = sys.argv[1], sys.argv[2], sys.argv[3]
    os.environ[LEDGER_ENV] = ledger
    home = RunHome.open(f"file:{db}")
    host = serve(ingestion_graph(), home=home, deployment_version="v1")
    arm(home, host, boundary)
    print("worker-ready", flush=True)
    try:
        # lease_ttl is short on purpose. This worker is armed to SIGKILL
        # itself, and the parent's restart worker may only adopt a claim
        # whose lease has run out — a claim it does not hold is another
        # worker's live work until then. What these boundaries prove is
        # crash RECOVERY, not how long adoption waits; the lease timing
        # itself is pinned in test_leases_and_multiple_workers.py.
        await asyncio.wait_for(host.work_forever("kill-worker", poll_interval=0.01, lease_ttl=0.5), timeout=60)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        host.shutdown()
    finally:
        await home.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
