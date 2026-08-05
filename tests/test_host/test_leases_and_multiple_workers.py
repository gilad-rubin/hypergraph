"""The claim lease, and the rule it replaced: one worker per Run Home.

A Run Home used to admit exactly one ``work_forever`` worker, refused by an
OS lock on the database file. That lock did not answer the hard question —
"is this half-finished Run dead, or is another worker holding it?" — it made
the question unaskable, and the price was that a notebook or a maintenance
script could configure durable work but never execute any.

A lease answers it, in the shape SQS, Kafka and Oban all ship: the claim
compare-and-set stamps a HOLDER and a DEADLINE, the holder renews while it
works, and a claim nobody is renewing is adopted. Expiry proves nothing
about the old worker, so the safety property is not the lease but
``claim_seq``: an adopted submission carries a NEW claim, and the
presumed-dead worker's release matches no row.

Four kinds of proof, and they are deliberately different kinds:

1. **Fake clock.** The lease rules — stamping, renewal, expiry, the two
   reclaim forms, clean-exit surrender — are decided against one
   store-authoritative ``now`` that these tests pass in explicitly, so
   nothing here sleeps out a real TTL.
2. **A real second process.** Worker A claims and is SIGKILLed; worker B
   adopts after the lease expires and finishes the work; A's zombie release,
   fired while B genuinely holds the claim, is refused by the fence.
3. **Two-worker steady state.** Three workers, twelve Runs, one SQLite file:
   every Run executes exactly once, and no writer trips ``database is
   locked``.
4. **The park is rescannable.** ``compat_state='incompatible'`` used to hide
   a row until the one worker restarted. It reopens when a worker able to
   drain it arrives.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from hypergraph import Graph, RunHome, RunRef, SyncRunner, node, serve
from hypergraph.checkpointers.sqlite import SQLITE_BUSY_TIMEOUT_MS
from hypergraph.checkpointers.types import WorkflowStatus
from hypergraph.host.home import LEASE_RENEWAL_FRACTION, LEASE_TTL_SECONDS, WORKER_PULSE_TTL_SECONDS
from hypergraph.host.host import heartbeat_tick
from tests.test_host._batch_api import serve_graphs
from tests.test_host._lease_fixture import (
    LEDGER_ENV,
    completing_graph,
    read_ledger,
)

aiosqlite = pytest.importorskip("aiosqlite")

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Short enough that a real SIGKILLed worker's claim is adoptable within a
#: test, long enough that the child renews it several times before it dies.
_CRASH_LEASE_TTL = 0.5


# === Helpers ===


def _home_uri(tmp_path, filename: str = "runs.db") -> str:
    return f"file:{tmp_path / filename}"


@pytest_asyncio.fixture
async def home(tmp_path):
    opened = RunHome.open(_home_uri(tmp_path))
    yield opened
    await opened.close()


def _iso(offset_seconds: float = 0.0) -> str:
    """A store-shaped instant, offset from now. THE fake clock here."""
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _sync_graph(name: str, seen: dict | None = None) -> Graph:
    @node(output_name="out")
    def compute(x: int) -> int:
        if seen is not None:
            seen.setdefault(x, 0)
            seen[x] += 1
        return x + 1

    return Graph([compute], name=name).with_runner(SyncRunner())


@contextlib.asynccontextmanager
async def _worker(host, worker_id: str, **kwargs):
    kwargs.setdefault("poll_interval", 0.01)
    kwargs.setdefault("drain_timeout", 2.0)
    task = asyncio.create_task(host.work_forever(worker_id, **kwargs))
    try:
        yield task
    finally:
        host.shutdown()
        await asyncio.wait_for(task, timeout=25)


async def _wait_for(check, timeout: float = 25.0, interval: float = 0.02, *, worker=None, what: str = "condition"):
    """Poll until ``check`` is truthy — or until the worker under it dies.

    ``worker=`` is what keeps a failure legible. Every wait in this file is
    really waiting on a worker LOOP to do something, and a loop that raised
    stops doing anything at all: without this the test spends its whole
    timeout budget and then reports "timed out waiting for condition", which
    names the symptom and hides the exception that caused it. Re-raising the
    worker's own error turns a 60-second mystery into an immediate, accurate
    failure.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await check()
        if value:
            return value
        if worker is not None and worker.done() and not worker.cancelled():
            error = worker.exception()
            if error is not None:
                raise AssertionError(f"the worker stopped while waiting for {what}") from error
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(interval)


def _row(home, workflow_id: str) -> dict:
    submission = home._get_submission_sync(workflow_id)
    assert submission is not None
    return submission


# === 1. The claim takes a lease ===


class TestTheClaimTakesALease:
    async def test_a_claim_stamps_its_holder_and_its_deadline(self, home):
        host, served = serve_graphs(_sync_graph("leased"), home=home, deployment_version="v1")
        await host.submit(served["leased"], {"x": 1}, workflow_id="wf-1")

        now = _iso()
        claimed = await home._claim_eligible(now, served=host._served_identities, worker_id="w-a", lease_ttl=60.0)

        assert [row["workflow_id"] for row in claimed] == ["wf-1"]
        # The returned row carries the lease the store now holds, not a
        # stale pre-claim projection — the same reason it carries the bumped
        # claim_seq.
        assert claimed[0]["claimed_by"] == "w-a"
        assert claimed[0]["lease_until"] > now
        stored = _row(home, "wf-1")
        assert (stored["claimed_by"], stored["lease_until"]) == ("w-a", claimed[0]["lease_until"])

    async def test_the_lease_deadline_is_the_stores_clock_plus_the_ttl(self, home):
        """Derived from the ``now`` passed in, never from the process clock.

        Two workers with skewed clocks must agree on when a claim runs out,
        which is the same reason every due predicate in this store reads the
        store's clock.
        """
        host, served = serve_graphs(_sync_graph("leased"), home=home, deployment_version="v1")
        await host.submit(served["leased"], {"x": 1}, workflow_id="wf-1")

        base = _iso(-3600)  # an hour in the past: unmistakably not "now"
        claimed = await home._claim_eligible(base, served=host._served_identities, worker_id="w-a", lease_ttl=90.0)

        expected = datetime.fromisoformat(base) + timedelta(seconds=90)
        assert datetime.fromisoformat(claimed[0]["lease_until"]) == expected

    def test_the_renewal_cadence_leaves_two_thirds_of_the_window(self):
        """Kafka's ``heartbeat.interval.ms`` rule, as a constant.

        Three consecutive missed renewals before anything is adoptable. A
        fraction above a third would let one slow pass expire a live claim.
        """
        assert LEASE_RENEWAL_FRACTION <= 1 / 3
        assert LEASE_TTL_SECONDS == 90.0
        assert heartbeat_tick(LEASE_TTL_SECONDS) <= LEASE_TTL_SECONDS / 3

    def test_a_long_lease_never_starves_the_worker_registry(self):
        """One write says two things, so it obeys the SHORTER window.

        Pacing the tick on ``lease_ttl`` alone was a live bug for any TTL
        above the registry's freshness window: a worker with an hour-long
        lease would write every twenty minutes, its ``host_workers`` row
        would go stale after ninety seconds, and every submit would be told
        nothing alive could serve its work — while that worker's claims were
        perfectly fresh and it was executing them.
        """
        assert heartbeat_tick(3600.0) <= WORKER_PULSE_TTL_SECONDS / 3
        # A short lease still paces itself, not the registry.
        assert heartbeat_tick(0.5) <= 0.5 / 3


# === 2. Renewal keeps a claim; silence gives it up ===


class TestRenewalAndExpiry:
    async def test_a_renewed_claim_is_not_adoptable_and_a_silent_one_is(self, home):
        host, served = serve_graphs(_sync_graph("leased"), home=home, deployment_version="v1")
        for n in (1, 2):
            await host.submit(served["leased"], {"x": n}, workflow_id=f"wf-{n}")
        # One each, in claim order, so the two rows have different holders.
        await home._claim_eligible(_iso(), served=host._served_identities, worker_id="w-a", lease_ttl=30.0, limit=1)
        await home._claim_eligible(_iso(), served=host._served_identities, worker_id="w-b", lease_ttl=30.0, limit=1)
        assert _row(home, "wf-1")["claimed_by"] == "w-a"
        assert _row(home, "wf-2")["claimed_by"] == "w-b"

        # A minute later, only w-a has been renewing.
        later = _iso(60)
        assert await home._renew_leases("w-a", later, lease_ttl=30.0) == 1
        await home._reclaim_expired(later, worker_id="w-c")

        assert _row(home, "wf-1")["state"] == "claimed", "a renewed claim is still its holder's"
        assert _row(home, "wf-2")["state"] == "pending", "a claim nobody renewed is adopted"
        assert _row(home, "wf-2")["claimed_by"] is None, "adoption clears the holder"
        assert _row(home, "wf-2")["lease_until"] is None

    async def test_renewal_never_reaches_another_workers_claims(self, home):
        host, served = serve_graphs(_sync_graph("leased"), home=home, deployment_version="v1")
        for n in (1, 2):
            await host.submit(served["leased"], {"x": n}, workflow_id=f"wf-{n}")
        # One each, in claim order, so the two rows have different holders.
        await home._claim_eligible(_iso(), served=host._served_identities, worker_id="w-a", lease_ttl=30.0, limit=1)
        await home._claim_eligible(_iso(), served=host._served_identities, worker_id="w-b", lease_ttl=30.0, limit=1)
        before = _row(home, "wf-2")["lease_until"]

        renewed = await home._renew_leases("w-a", _iso(60), lease_ttl=30.0)

        assert renewed == 1
        assert _row(home, "wf-2")["lease_until"] == before

    async def test_one_statement_renews_every_claim_a_worker_holds(self, home):
        """Liveness is one fact about the WORKER, not one per Run."""
        host, served = serve_graphs(_sync_graph("leased"), home=home, deployment_version="v1")
        for n in range(5):
            await host.submit(served["leased"], {"x": n}, workflow_id=f"wf-{n}")
        await home._claim_eligible(_iso(), served=host._served_identities, worker_id="w-a", lease_ttl=30.0)

        assert await home._renew_leases("w-a", _iso(1), lease_ttl=30.0) == 5

    async def test_a_settled_claim_is_never_renewed_back_to_life(self, home):
        host, served = serve_graphs(_sync_graph("leased"), home=home, deployment_version="v1")
        await host.submit(served["leased"], {"x": 1}, workflow_id="wf-1")
        claimed = await home._claim_eligible(_iso(), served=host._served_identities, worker_id="w-a", lease_ttl=30.0)
        await home._release_submission("wf-1", claimed[0]["claim_seq"])

        assert await home._renew_leases("w-a", _iso(1), lease_ttl=30.0) == 0


# === 3. Which rows a reclaim scan may adopt ===


class TestWhatAReclaimScanMayAdopt:
    async def test_a_worker_adopts_its_own_outstanding_claims_at_startup(self, home):
        """A supervised restart resumes at once, as it did under the lock.

        This process IS ``w-a`` and is executing nothing, so waiting out its
        own dead incarnation's lease would be a delay it imposes on itself
        for no information.
        """
        host, served = serve_graphs(_sync_graph("leased"), home=home, deployment_version="v1")
        await host.submit(served["leased"], {"x": 1}, workflow_id="wf-1")
        await home._claim_eligible(_iso(), served=host._served_identities, worker_id="w-a", lease_ttl=3600.0)

        await home._reclaim_expired(_iso(), worker_id="w-a", adopt_own=True)

        assert _row(home, "wf-1")["state"] == "pending"
        assert _row(home, "wf-1")["recovery_attempts"] == 1

    async def test_a_steady_state_pass_never_reclaims_its_own_live_claims(self, home):
        """The one thing a per-pass scan must not do to itself.

        A poll pass that ran late — a blocked event loop, a long GC — would
        otherwise return a Run to pending while the task in THIS process is
        still executing it, and then re-claim it as a second execution. Only
        another worker adopting is meaningful arbitration.
        """
        host, served = serve_graphs(_sync_graph("leased"), home=home, deployment_version="v1")
        await host.submit(served["leased"], {"x": 1}, workflow_id="wf-1")
        await home._claim_eligible(_iso(), served=host._served_identities, worker_id="w-a", lease_ttl=1.0)

        await home._reclaim_expired(_iso(3600), worker_id="w-a")

        assert _row(home, "wf-1")["state"] == "claimed"
        assert _row(home, "wf-1")["claimed_by"] == "w-a"

    async def test_another_worker_adopts_the_same_expired_claim(self, home):
        host, served = serve_graphs(_sync_graph("leased"), home=home, deployment_version="v1")
        await host.submit(served["leased"], {"x": 1}, workflow_id="wf-1")
        await home._claim_eligible(_iso(), served=host._served_identities, worker_id="w-a", lease_ttl=1.0)

        await home._reclaim_expired(_iso(3600), worker_id="w-b")

        assert _row(home, "wf-1")["state"] == "pending"

    async def test_an_unstamped_claim_is_adoptable_on_sight(self, home):
        """Nothing can renew a claim nobody put their name on.

        ``_renew_leases`` extends the claims of a NAMED holder, so waiting
        out an unstamped claim's lease buys no information whatsoever.
        """
        host, served = serve_graphs(_sync_graph("leased"), home=home, deployment_version="v1")
        await host.submit(served["leased"], {"x": 1}, workflow_id="wf-1")
        await home._claim_eligible(_iso(), served=host._served_identities, lease_ttl=3600.0)
        assert _row(home, "wf-1")["claimed_by"] is None

        await home._reclaim_expired(_iso(), worker_id="w-b")

        assert _row(home, "wf-1")["state"] == "pending"

    async def test_a_claim_taken_before_leases_existed_is_adoptable_on_sight(self, home):
        """How a Run Home upgraded with work in flight drains."""
        host, served = serve_graphs(_sync_graph("leased"), home=home, deployment_version="v1")
        await host.submit(served["leased"], {"x": 1}, workflow_id="wf-1")
        db = home._sync_db()
        db.execute("UPDATE host_submissions SET state = 'claimed', claimed_by = 'v7-worker', lease_until = NULL WHERE workflow_id = 'wf-1'")
        db.commit()

        await home._reclaim_expired(_iso(), worker_id="w-new")

        assert _row(home, "wf-1")["state"] == "pending"

    async def test_a_terminal_run_under_an_expired_lease_settles_instead_of_replaying(self, home):
        host, served = serve_graphs(_sync_graph("leased"), home=home, deployment_version="v1")
        await host.submit(served["leased"], {"x": 1}, workflow_id="wf-1")
        await home._claim_eligible(_iso(), served=host._served_identities, worker_id="w-a", lease_ttl=1.0)
        home.create_run_sync("wf-1", graph_name="leased")
        home.update_run_status_sync("wf-1", WorkflowStatus.COMPLETED)

        await home._reclaim_expired(_iso(3600), worker_id="w-b")

        assert _row(home, "wf-1")["state"] == "finished"

    async def test_a_live_workers_terminal_run_is_left_for_its_own_release(self, home):
        """The window between a terminal commit and the holder's release.

        It belongs to the holder. Sweeping it from another worker's pass
        would settle a row whose claimant is a heartbeat away from settling
        it itself, for no gain.
        """
        host, served = serve_graphs(_sync_graph("leased"), home=home, deployment_version="v1")
        await host.submit(served["leased"], {"x": 1}, workflow_id="wf-1")
        await home._claim_eligible(_iso(), served=host._served_identities, worker_id="w-a", lease_ttl=3600.0)
        home.create_run_sync("wf-1", graph_name="leased")
        home.update_run_status_sync("wf-1", WorkflowStatus.COMPLETED)

        await home._reclaim_expired(_iso(), worker_id="w-b")

        assert _row(home, "wf-1")["state"] == "claimed"


# === 4. The fence: adoption revokes the right to commit ===


class TestTheClaimFence:
    async def test_an_adopted_run_gets_a_new_claim_and_the_old_release_is_a_no_op(self, home):
        host, served = serve_graphs(_sync_graph("leased"), home=home, deployment_version="v1")
        await host.submit(served["leased"], {"x": 1}, workflow_id="wf-1")
        stale = (await home._claim_eligible(_iso(), served=host._served_identities, worker_id="w-a", lease_ttl=1.0))[0]

        await home._reclaim_expired(_iso(3600), worker_id="w-b")
        fresh = (await home._claim_eligible(_iso(), served=host._served_identities, worker_id="w-b", lease_ttl=30.0))[0]
        assert fresh["claim_seq"] > stale["claim_seq"]

        # w-a wakes up and finishes. The state name it expects is there;
        # the claim it held is not.
        released = await home._release_submission("wf-1", stale["claim_seq"])

        assert released is False
        submission = _row(home, "wf-1")
        assert (submission["state"], submission["claim_seq"], submission["claimed_by"]) == ("claimed", fresh["claim_seq"], "w-b")

    async def test_a_stale_claimant_cannot_dead_letter_the_run_it_lost(self, home):
        """The worker's other claim-scoped transition, fenced the same way.

        A presumed-dead worker whose builder registry shrank would otherwise
        retire work another worker is executing right now.
        """
        host, served = serve_graphs(_sync_graph("leased"), home=home, deployment_version="v1")
        await host.submit(served["leased"], {"x": 1}, workflow_id="wf-1")
        stale = (await home._claim_eligible(_iso(), served=host._served_identities, worker_id="w-a", lease_ttl=1.0))[0]
        await home._reclaim_expired(_iso(3600), worker_id="w-b")
        await home._claim_eligible(_iso(), served=host._served_identities, worker_id="w-b", lease_ttl=30.0)

        retired = await home._dead_letter("wf-1", "builder_missing", claim_seq=stale["claim_seq"])

        assert retired is False
        assert _row(home, "wf-1")["state"] == "claimed"


# === 5. A clean exit surrenders its leases ===


class TestCleanExitSurrender:
    async def test_expiring_leases_moves_work_without_waiting_the_ttl(self, home):
        host, served = serve_graphs(_sync_graph("leased"), home=home, deployment_version="v1")
        await host.submit(served["leased"], {"x": 1}, workflow_id="wf-1")
        await home._claim_eligible(_iso(), served=host._served_identities, worker_id="w-a", lease_ttl=3600.0)

        surrendered = await home._expire_leases("w-a", _iso())

        assert surrendered == 1
        await home._reclaim_expired(_iso(), worker_id="w-b")
        assert _row(home, "wf-1")["state"] == "pending", "an hour of TTL was not waited out"

    async def test_a_stopped_worker_hands_its_cancelled_run_to_the_next_one(self, home, tmp_path):
        """End to end, through ``work_forever``'s own shutdown path.

        The first worker is stopped while its node is gated open, so the
        bounded drain cancels the run and leaves the claim outstanding. A
        second worker on the same file picks it up immediately rather than
        after the lease, because the first said it was leaving.
        """
        gate = threading.Event()
        started = threading.Event()

        @node(output_name="out")
        def compute(x: int) -> int:
            started.set()
            gate.wait(timeout=20)
            return x + 1

        def build() -> Graph:
            return Graph([compute], name="handover").with_runner(SyncRunner())

        host_a = serve(build(), home=home, deployment_version="v1")
        second = RunHome.open(_home_uri(tmp_path))
        try:
            host_b = serve(build(), home=second, deployment_version="v1")
            receipt = await host_a.submit(build(), {"x": 1}, workflow_id="wf-hand")
            async with _worker(host_a, "w-a", lease_ttl=3600.0, drain_timeout=0.05):
                # ONE waiter, not one per poll. A `to_thread` inside a poll
                # loop queues a fresh blocking thread every 20 ms, and the
                # default executor is small — the later tests in this file
                # run real nodes through `to_thread` too, and a saturated
                # pool starves them.
                assert await asyncio.to_thread(started.wait, 20), "the gated node never started"
            # w-a is gone and said so; the lease it held is surrendered.
            assert _row(home, "wf-hand")["lease_until"] <= _iso()

            gate.set()
            async with _worker(host_b, "w-b", lease_ttl=3600.0):
                view = await _wait_for(lambda: _settled(host_b.client, receipt.run_ref))
            assert view.status == WorkflowStatus.COMPLETED
        finally:
            gate.set()
            await second.close()


async def _settled(client, ref):
    view = await client.get(ref)
    if view is not None and view.status in {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.PARTIAL, WorkflowStatus.STOPPED}:
        return view
    return None


# === 6. The version-incompatible park is rescannable ===


class TestTheParkIsRescannable:
    """A memo has to be invalidated by the event that changes its answer.

    ``compat_state='incompatible'`` records "nothing that scanned this row
    could serve its pinned identity". Under one worker the only event that
    could change that was the worker restarting, so the reset lived in the
    restart scan. Under several workers the event is a worker ARRIVING —
    and leaving the reset at restart would hide a parked row from exactly
    the worker that just showed up able to drain it.
    """

    async def _parked(self, home, tmp_path):
        graph = _sync_graph("rolling")
        old = serve(graph, home=home, deployment_version="v1")
        detached = RunHome.open(_home_uri(tmp_path))
        new = serve(_sync_graph("rolling"), home=detached, deployment_version="v2")
        await old.submit(graph, {"x": 1}, workflow_id="wf-park")

        # The v2 deployment scans it, cannot serve the v1 identity, but
        # serves the NAME: a rolling deployment, so the row parks.
        assert await home._claim_eligible(_iso(), served=new._served_identities, worker_id="w-v2") == []
        assert _row(home, "wf-park")["compat_state"] == "incompatible"
        return old, new, detached

    async def test_a_parked_row_is_hidden_from_the_scan_until_something_arrives(self, home, tmp_path):
        old, _new, detached = await self._parked(home, tmp_path)
        try:
            # Even the deployment that CAN serve it does not see it: the
            # park is a filter, which is the whole reason it needed a reset.
            assert await home._claim_eligible(_iso(), served=old._served_identities, worker_id="w-v1") == []

            arrived = await home._pulse_worker("w-v1", served=old._served_identities, builders=())

            assert arrived is True
            assert _row(home, "wf-park")["compat_state"] == "compatible"
            claimed = await home._claim_eligible(_iso(), served=old._served_identities, worker_id="w-v1")
            assert [row["workflow_id"] for row in claimed] == ["wf-park"]
        finally:
            await detached.close()

    async def test_an_unchanged_pulse_leaves_the_park_alone(self, home, tmp_path):
        """Otherwise every poll pass would re-evaluate and re-warn forever.

        The park exists so a row waiting for its deployment does not cost a
        scan (and a log line) twenty times a second.
        """
        _old, new, detached = await self._parked(home, tmp_path)
        try:
            assert await home._pulse_worker("w-v2", served=new._served_identities, builders=()) is True
            # The park was reopened by w-v2 arriving, and w-v2 re-parks it.
            assert await home._claim_eligible(_iso(), served=new._served_identities, worker_id="w-v2") == []
            assert _row(home, "wf-park")["compat_state"] == "incompatible"

            repeat = await home._pulse_worker("w-v2", served=new._served_identities, builders=())

            assert repeat is False
            assert _row(home, "wf-park")["compat_state"] == "incompatible"
        finally:
            await detached.close()

    async def test_a_worker_never_parks_a_row_another_live_worker_can_run(self, home, tmp_path):
        """The steady-state half, which arrival alone does not cover.

        Two workers, both already registered and neither changing: whichever
        scans first sees the row. If the one that CANNOT serve it wins the
        race and parks it, the row is hidden from the one that can — and no
        arrival is coming to reopen it, because both are already here. So a
        row another live worker covers exactly is left alone.

        Under one worker this could not happen: a row that worker could not
        serve was a row nothing could serve, which is why the park used to
        be the only disposition here.
        """
        graph = _sync_graph("rolling")
        old = serve(graph, home=home, deployment_version="v1")
        detached = RunHome.open(_home_uri(tmp_path))
        try:
            new = serve(_sync_graph("rolling"), home=detached, deployment_version="v2")
            await old.submit(graph, {"x": 1}, workflow_id="wf-steady")
            # The v1 worker is already live and registered — it is the one
            # that can run this row, and it is not arriving, it is here.
            await home._pulse_worker("w-v1", served=old._served_identities, builders=())

            assert await home._claim_eligible(_iso(), served=new._served_identities, worker_id="w-v2") == []

            assert _row(home, "wf-steady")["compat_state"] == "compatible", "a row w-v1 can run must stay visible to w-v1"
            claimed = await home._claim_eligible(_iso(), served=old._served_identities, worker_id="w-v1")
            assert [row["workflow_id"] for row in claimed] == ["wf-steady"]
        finally:
            await detached.close()

    async def test_the_arriving_worker_drains_the_park_end_to_end(self, home, tmp_path):
        old, _new, detached = await self._parked(home, tmp_path)
        try:
            ref = RunRef(home=home.uri, run_id="wf-park")
            async with _worker(old, "w-v1"):
                view = await _wait_for(lambda: _settled(old.client, ref))
            assert view.status == WorkflowStatus.COMPLETED
        finally:
            await detached.close()


# === 7. Two-worker steady state on one SQLite file ===


class TestSeveralWorkersOnOneHome:
    async def test_every_run_executes_exactly_once_across_three_workers(self, tmp_path):
        """The rule the OS lock enforced, now enforced per submission.

        Three workers, three connections, one file. Counting executions is
        the assertion: the claim compare-and-set is what makes two workers
        unable to hold one submission, and nothing here renews late enough
        for a lease to expire, so at-least-once is exactly-once.
        """
        seen: dict[int, int] = {}
        homes = [RunHome.open(_home_uri(tmp_path)) for _ in range(3)]
        try:
            hosts = [serve(_sync_graph("shared", seen), home=h, deployment_version="v1") for h in homes]
            receipts = [await hosts[0].submit(_sync_graph("shared"), {"x": n}, workflow_id=f"wf-{n}") for n in range(12)]

            async with (
                _worker(hosts[0], "w-0"),
                _worker(hosts[1], "w-1"),
                _worker(hosts[2], "w-2"),
            ):
                for receipt in receipts:
                    await _wait_for(lambda ref=receipt.run_ref: _settled(hosts[0].client, ref))

            assert sorted(seen) == list(range(12))
            assert set(seen.values()) == {1}, f"every Run executes exactly once, got {seen}"
            assert all(not host.worker_errors for host in hosts)

            # Each Run is settled, and each is stamped with the worker that
            # actually ran it — three names appear, not one.
            holders = {homes[0]._get_submission_sync(f"wf-{n}")["claimed_by"] for n in range(12)}
            assert holders <= {"w-0", "w-1", "w-2"}
            assert all(homes[0]._get_submission_sync(f"wf-{n}")["state"] == "finished" for n in range(12))
        finally:
            for opened in homes:
                await opened.close()

    def test_every_connection_states_its_busy_timeout(self, tmp_path):
        """WAL solves reader/writer blocking, not writer/writer contention.

        With several worker processes opening short ``BEGIN IMMEDIATE``
        transactions on one file, the wait for another writer has to be a
        number this project chose rather than the driver's 5 s default —
        including on the short-lived MIGRATION connection, which is the one
        that would otherwise fail a process START because another worker
        happened to be mid-write.
        """
        import sqlite3

        opened = RunHome.open(_home_uri(tmp_path))
        try:
            assert opened._sync_db().execute("PRAGMA busy_timeout").fetchone()[0] == SQLITE_BUSY_TIMEOUT_MS
            assert opened._sync_db().execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            # A default connection to the same file proves the pragma is set
            # per connection and not a property of the database file.
            plain = sqlite3.connect(str(tmp_path / "runs.db"))
            try:
                assert plain.execute("PRAGMA busy_timeout").fetchone()[0] != SQLITE_BUSY_TIMEOUT_MS
            finally:
                plain.close()
        finally:
            asyncio.run(opened.close())

    async def test_the_async_connection_states_it_too(self, home):
        await home._ensure_db()
        cursor = await home._db.execute("PRAGMA busy_timeout")
        assert (await cursor.fetchone())[0] == SQLITE_BUSY_TIMEOUT_MS


# === 8. A real second process: kill, adopt, and fence the zombie ===


_CHILD = """
import asyncio, os, sys
sys.path.insert(0, {repo!r})
os.environ[{ledger_env!r}] = {ledger!r}

from hypergraph import RunHome, serve
from tests.test_host._lease_fixture import blocking_graph

graph = blocking_graph()
home = RunHome.open({uri!r})
host = serve(graph, home=home, deployment_version="v1")
host.submit_sync(graph, {{"x": 7}}, workflow_id="wf-lease")
asyncio.run(host.work_forever("worker-a", poll_interval=0.01, lease_ttl={ttl!r}))
"""


class TestARealWorkerDiesAndAnotherAdopts:
    """The scenario the exclusive lock existed to avoid having to handle.

    Worker A is a real interpreter that really claims the work, really
    starts executing it, and is really SIGKILLed — no mocked exception, so
    nothing runs afterwards: no ``finally``, no release, no withdrawal. Its
    claim, its holder stamp and its lease are all left behind exactly as a
    crash leaves them.
    """

    async def test_b_adopts_after_expiry_and_as_zombie_release_is_fenced(self, tmp_path, home):
        ledger = tmp_path / "ledger.txt"
        script = _CHILD.format(
            repo=str(_REPO_ROOT),
            ledger_env=LEDGER_ENV,
            ledger=str(ledger),
            uri=_home_uri(tmp_path),
            ttl=_CRASH_LEASE_TTL,
        )
        proc = subprocess.Popen([sys.executable, "-c", script])
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                if any(line.endswith(":started") for line in read_ledger(ledger)):
                    break
                if proc.poll() is not None:
                    raise AssertionError("child worker exited before executing")
                time.sleep(0.02)
            else:
                raise AssertionError("child never started the run")

            stale = _row(home, "wf-lease")
            assert stale["state"] == "claimed"
            assert stale["claimed_by"] == "worker-a", "the dead worker really held the claim"
            stale_seq = stale["claim_seq"]

            os.kill(proc.pid, signal.SIGKILL)  # real crash, no cleanup
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()

        # A's registration is still fresh and its lease is still stamped —
        # nothing "noticed" the death, and nothing has to.
        abandoned = _row(home, "wf-lease")
        assert abandoned["claimed_by"] == "worker-a"

        # Wait for the lease to be a FACT rather than a race. The rule under
        # test is "an expired claim is adoptable", not "adoption beats a
        # half-second TTL on a loaded machine", so the test states the
        # premise instead of hoping the setup outlasts it.
        await _wait_for(lambda: _lease_has_run_out(home, "wf-lease"), timeout=30.0)

        gate = threading.Event()
        os.environ[LEDGER_ENV] = str(ledger)
        try:
            host_b = serve(completing_graph(gate), home=home, deployment_version="v1")
            ref = RunRef(home=home.uri, run_id="wf-lease")
            async with _worker(host_b, "worker-b", lease_ttl=30.0, drain_timeout=5.0) as worker_b:
                # B adopts the expired claim, and holds it while its node
                # waits on the gate.
                await _wait_for(
                    lambda: _adopted_by(home, "wf-lease", "worker-b"),
                    timeout=60.0,
                    worker=worker_b,
                    what="worker-b to adopt the expired claim",
                )
                # ...and is genuinely EXECUTING it, not merely holding the
                # row. The claim stamp lands before the runner is invoked, so
                # asserting only on it would fence a claim whose node had not
                # started — and would blame the fence for a node that never
                # got off the ground.
                await _wait_for(
                    lambda: _ledger_has(ledger, f"{os.getpid()}:adopted"),
                    timeout=60.0,
                    worker=worker_b,
                    what="worker-b's node to start running the adopted Run",
                )
                fresh_seq = _row(home, "wf-lease")["claim_seq"]
                assert fresh_seq > stale_seq

                # THE FENCE. A wakes up and settles the run it was executing.
                # The row reads 'claimed', which is the state name a release
                # matching only the name would accept.
                released = await home._release_submission("wf-lease", stale_seq)
                retired = await home._dead_letter("wf-lease", "builder_missing", claim_seq=stale_seq)

                assert released is False and retired is False
                still = _row(home, "wf-lease")
                assert (still["state"], still["claim_seq"], still["claimed_by"]) == ("claimed", fresh_seq, "worker-b")

                gate.set()
                view = await _wait_for(
                    lambda: _settled(host_b.client, ref),
                    timeout=60.0,
                    worker=worker_b,
                    what="worker-b to finish the adopted Run",
                )
        finally:
            gate.set()
            os.environ.pop(LEDGER_ENV, None)

        assert view.status == WorkflowStatus.COMPLETED
        settled = _row(home, "wf-lease")
        assert settled["state"] == "finished"
        assert settled["claimed_by"] == "worker-b", "the completed result is B's"

        # The ledger is the cross-process witness: A started it, B finished
        # it, and A never completed anything.
        phases = [line.split(":", 1) for line in read_ledger(ledger)]
        by_pid = {pid: [phase for other, phase in phases if other == pid] for pid, _ in phases}
        assert by_pid[str(proc.pid)] == ["started"]
        assert by_pid[str(os.getpid())] == ["adopted", "completed"]


async def _adopted_by(home, workflow_id: str, worker_id: str):
    submission = home._get_submission_sync(workflow_id)
    return submission is not None and submission["state"] == "claimed" and submission["claimed_by"] == worker_id


async def _ledger_has(ledger, line: str):
    return line in read_ledger(ledger)


async def _lease_has_run_out(home, workflow_id: str):
    """Whether this claim is adoptable by the STORE's clock, not the test's."""
    submission = home._get_submission_sync(workflow_id)
    if submission is None or submission["lease_until"] is None:
        return submission is not None
    return submission["lease_until"] <= await home._store_now()
