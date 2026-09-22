"""Durable Host V1 — ticket 04: durable stop, real-kill restart, recovery brake, client.list.

Covers: detached durable stop of an executing run (with info), stop
after-terminal and unknown-run errors, first-stop-wins dedup, stop before
first execution (never executes, no runs row invented), a real SIGKILL
subprocess restart that resumes without re-executing committed steps, the
recovery brake (progressless crash loops park as recovery-exhausted;
progress resets the budget; rerun revives), nested runs a dead worker left
``active`` settling with their submission (#465), and client.list filtering
over the extended waiting vocabulary.
"""

import asyncio
import contextlib
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from hypergraph import (
    AlreadyTerminalError,
    AsyncRunner,
    Graph,
    HostError,
    RunHome,
    RunHomeClient,
    RunQuery,
    RunRef,
    SyncRunner,
    WaitingCondition,
    node,
    serve,
)
from hypergraph.checkpointers.types import BoundaryState, PendingNode, StepRecord, StepStatus, WorkflowStatus
from tests.test_host._batch_api import serve_graphs

aiosqlite = pytest.importorskip("aiosqlite")


# === Helpers (mirrors ticket-02/03 conventions) ===


def _sync_graph(name: str) -> Graph:
    @node(output_name="out")
    def compute(x: int) -> int:
        return x + 1

    return Graph([compute], name=name).with_runner(SyncRunner())


def _counting_sync_graph(name: str, calls: dict) -> Graph:
    @node(output_name="out")
    def compute(x: int) -> int:
        calls["n"] += 1
        return x + 1

    return Graph([compute], name=name).with_runner(SyncRunner())


def _gated_async_graph(name: str, started: asyncio.Event, release: asyncio.Event, calls: dict) -> Graph:
    """Two-node async graph whose second node blocks until released."""

    @node(output_name="seed")
    async def seed(x: int) -> int:
        calls["seed"] += 1
        return x

    @node(output_name="out")
    async def gated(seed: int) -> int:
        started.set()
        await release.wait()
        calls["gated"] += 1
        return seed * 10

    return Graph([seed, gated], name=name).with_runner(AsyncRunner())


def _home_uri(tmp_path, filename: str = "runs.db") -> str:
    return f"file:{tmp_path / filename}"


async def _wait_for(check, timeout: float = 15.0, interval: float = 0.02):
    """Poll an async zero-arg callable until it returns a truthy value."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await check()
        if value:
            return value
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def _worker(host, worker_id: str = "w-test", **kwargs):
    """Run work_forever as a task; shut it down cleanly on exit."""
    task = asyncio.create_task(host.work_forever(worker_id, **kwargs))
    try:
        yield task
    finally:
        host.shutdown()
        await asyncio.wait_for(task, timeout=20)


async def _terminal_view(client, ref):
    view = await client.get(ref)
    if view is not None and view.status in {
        WorkflowStatus.COMPLETED,
        WorkflowStatus.FAILED,
        WorkflowStatus.PARTIAL,
        WorkflowStatus.STOPPED,
    }:
        return view
    return None


async def _flag(event: asyncio.Event):
    return event.is_set()


async def _submission_state(home, workflow_id, state):
    submission = home._get_submission_sync(workflow_id)
    return submission is not None and submission["state"] == state


async def _collect(client, ref):
    return [u async for u in client.watch(ref)]


def _stop_command_rows(home, workflow_id):
    db = home._sync_db()
    return db.execute(
        "SELECT payload, applied_at FROM host_commands WHERE run_id = ? AND verb = 'stop'",
        (workflow_id,),
    ).fetchall()


async def _stop_applied(home, workflow_id):
    rows = _stop_command_rows(home, workflow_id)
    return bool(rows) and all(row[1] is not None for row in rows)


@pytest_asyncio.fixture
async def home(tmp_path):
    h = RunHome.open(_home_uri(tmp_path))
    yield h
    await h.close()


# === 1. Durable stop of an executing run ===


class TestDurableStop:
    async def test_detached_stop_settles_stopped_with_info(self, home):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = {"seed": 0, "gated": 0}
        graph = _gated_async_graph("stopdef", started, release, calls)
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await host.submit(served["stopdef"], {"x": 2}, workflow_id="wf-stop")
        # A client built from the Home alone (no host) can stop: durable
        # stop is a client verb, not a worker verb.
        detached = RunHomeClient(home)

        async with _worker(host):
            await _wait_for(lambda: _flag(started))
            stop_receipt = await detached.stop(receipt.run_ref, info={"reason": "user asked"})
            assert stop_receipt.duplicate is False
            assert stop_receipt.verb == "stop"
            assert stop_receipt.run_ref == receipt.run_ref
            # Release only after the worker observed the stop and signalled
            # the runner — otherwise the run could finish normally first.
            await _wait_for(lambda: _stop_applied(home, "wf-stop"))
            release.set()
            view = await _wait_for(lambda: _terminal_view(detached, receipt.run_ref))

        assert view.status == WorkflowStatus.STOPPED
        assert calls == {"seed": 1, "gated": 1}
        # The command was observed and applied by the worker.
        rows = _stop_command_rows(home, "wf-stop")
        assert len(rows) == 1
        assert json.loads(rows[0][0]) == {"info": {"reason": "user asked"}}
        assert rows[0][1] is not None  # applied_at
        # The durable update sequence carries the command fact with its info.
        command_updates = [u for u in home._read_run_updates_sync("wf-stop") if u[1] == "command"]
        assert len(command_updates) == 1
        # source_ref rides along as audit provenance (ticket 14): None here
        # because this stop named no caller.
        assert json.loads(command_updates[0][2]) == {"verb": "stop", "info": {"reason": "user asked"}, "source_ref": None}
        # STOPPED is terminal: the submission settled as finished.
        assert home._get_submission_sync("wf-stop")["state"] == "finished"

        # A new worker's restart scan never resumes a stopped run.
        async with _worker(host, "w-2"):
            await asyncio.sleep(0.3)
        assert calls == {"seed": 1, "gated": 1}
        assert home._get_submission_sync("wf-stop")["state"] == "finished"

    async def test_stop_after_terminal_and_unknown_run_raise(self, home):
        host, served = serve_graphs(_sync_graph("dbl"), home=home)
        receipt = await host.submit(served["dbl"], {"x": 1}, workflow_id="wf-done")
        async with _worker(host):
            await _wait_for(lambda: _terminal_view(host.client, receipt.run_ref))
        with pytest.raises(AlreadyTerminalError):
            await host.client.stop(receipt.run_ref)
        with pytest.raises(HostError, match="no such run"):
            await host.client.stop(RunRef(home=home.uri, run_id="wf-nope"))
        # Sync mirror shares the contract.
        with pytest.raises(AlreadyTerminalError):
            host.client.stop_sync(receipt.run_ref)
        with pytest.raises(HostError, match="no such run"):
            host.client.stop_sync(RunRef(home=home.uri, run_id="wf-nope"))

    async def test_double_stop_dedupes_first_info_wins(self, home):
        host, served = serve_graphs(_sync_graph("dbl"), home=home)
        receipt = await host.submit(served["dbl"], {"x": 1}, workflow_id="wf-dbl")
        first = await host.client.stop(receipt.run_ref, info={"n": 1})
        second = await host.client.stop(receipt.run_ref, info={"n": 2})
        assert first.duplicate is False
        assert second.duplicate is True
        assert first.verb == second.verb == "stop"
        # One command row; the first stop owns its info.
        rows = _stop_command_rows(home, "wf-dbl")
        assert len(rows) == 1
        assert json.loads(rows[0][0]) == {"info": {"n": 1}}
        # One durable 'command' update.
        command_updates = [u for u in home._read_run_updates_sync("wf-dbl") if u[1] == "command"]
        assert len(command_updates) == 1

        # Sync mirror dedupes identically.
        receipt2 = await host.submit(served["dbl"], {"x": 1}, workflow_id="wf-dbl2")
        s1 = host.client.stop_sync(receipt2.run_ref, info="first")
        s2 = host.client.stop_sync(receipt2.run_ref, info="second")
        assert (s1.duplicate, s2.duplicate) == (False, True)

    async def test_stop_before_first_execution_never_executes(self, home):
        calls = {"n": 0}
        host, served = serve_graphs(_counting_sync_graph("neverdef", calls), home=home)
        receipt = await host.submit(served["neverdef"], {"x": 1}, workflow_id="wf-never")
        stop_receipt = await host.client.stop(receipt.run_ref, info="too late")
        assert stop_receipt.duplicate is False

        async with _worker(host):
            await _wait_for(lambda: _submission_state(home, "wf-never", "finished"))
            await asyncio.sleep(0.2)  # a few more claim cycles: nothing starts

        assert calls["n"] == 0
        assert home.get_run("wf-never") is None  # no runs row invented
        rows = _stop_command_rows(home, "wf-never")
        assert len(rows) == 1 and rows[0][1] is not None  # applied
        view = await host.client.get(receipt.run_ref)
        assert view.status is None
        assert view.waiting is None
        # A later stop on the finished-never-started run is already-terminal.
        with pytest.raises(AlreadyTerminalError):
            await host.client.stop(receipt.run_ref)
        # watch() terminates for a settled-never-started run (no runs row).
        updates = await asyncio.wait_for(_collect(host.client, receipt.run_ref), timeout=5)
        assert [u.kind for u in updates] == ["submitted", "command"]
        assert all(u.durable for u in updates)

    async def test_resubmit_after_stop_before_start_is_already_terminal(self, home):
        """Finished submissions are terminal even with no runs row (F6)."""
        calls = {"n": 0}
        host, served = serve_graphs(_counting_sync_graph("termdef", calls), home=home)
        receipt = await host.submit(served["termdef"], {"x": 1}, workflow_id="wf-term-nb")
        await host.client.stop(receipt.run_ref)

        async with _worker(host):
            await _wait_for(lambda: _submission_state(home, "wf-term-nb", "finished"))
        assert calls["n"] == 0
        assert home.get_run("wf-term-nb") is None

        # Fingerprint-identical reuse of the finished submission raises —
        # completed history never changes identity, runs row or not.
        with pytest.raises(AlreadyTerminalError):
            await host.submit(served["termdef"], {"x": 1}, workflow_id="wf-term-nb")
        with pytest.raises(AlreadyTerminalError):
            host.submit_sync(served["termdef"], {"x": 1}, workflow_id="wf-term-nb")

    async def test_stop_records_source_ref_on_command_row(self, home):
        """ADR 0005 A11: commands may carry an opaque source_ref (F13)."""
        host, served = serve_graphs(_sync_graph("dbl"), home=home)
        receipt = await host.submit(served["dbl"], {"x": 1}, workflow_id="wf-srcref")
        await host.client.stop(receipt.run_ref, info="audit", source_ref="ops-console-7")
        db = home._sync_db()
        (stored,) = db.execute("SELECT source_ref FROM host_commands WHERE run_id = 'wf-srcref' AND verb = 'stop'").fetchone()
        assert stored == "ops-console-7"

        # Omitted source_ref stays NULL; the sync mirror accepts it too.
        receipt2 = await host.submit(served["dbl"], {"x": 2}, workflow_id="wf-srcref2")
        host.client.stop_sync(receipt2.run_ref, info="plain")
        (stored2,) = db.execute("SELECT source_ref FROM host_commands WHERE run_id = 'wf-srcref2' AND verb = 'stop'").fetchone()
        assert stored2 is None


# === 2. Real SIGKILL restart: resume without re-executing committed steps ===

_CHILD_SCRIPT = """
import asyncio
import time

from hypergraph import Graph, RunHome, SyncRunner, node, serve


@node(output_name="first_out")
def first(x: int) -> int:
    with open({marker!r}, "a") as f:
        f.write("first\\n")
    return x


@node(output_name="second_out")
def second(first_out: int) -> int:
    time.sleep(30)
    with open({marker!r}, "a") as f:
        f.write("second\\n")
    return first_out


graph = Graph([first, second], name="killdef").with_runner(SyncRunner())
home = RunHome.open({uri!r})
host = serve(graph, home=home, deployment_version="v1")
host.submit_sync(graph, {{"x": 1}}, workflow_id="wf-kill")
# lease_ttl is short on purpose: this worker is about to be SIGKILLed,
# and the successor may only adopt a claim whose lease has run out.
# What is under test is crash RECOVERY, not how long adoption waits.
asyncio.run(host.work_forever("w-child", poll_interval=0.02, lease_ttl=0.5))
"""


class TestRealKillRestart:
    async def test_sigkill_restart_resumes_without_reexecuting(self, tmp_path, home):
        marker = tmp_path / "marker.txt"
        script = _CHILD_SCRIPT.format(marker=str(marker), uri=_home_uri(tmp_path))
        proc = subprocess.Popen([sys.executable, "-c", script])
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                if marker.exists() and "first" in marker.read_text():
                    break
                if proc.poll() is not None:
                    raise AssertionError("child worker exited before executing")
                time.sleep(0.05)
            else:
                raise AssertionError("child never committed the first node")
            time.sleep(0.3)  # the slow second node is in flight
            proc.kill()  # SIGKILL: real crash, no cleanup
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()

        # A new process-shaped worker reopens the same Home. The parent's
        # graph is structurally identical (structural_hash excludes function
        # source), so the claimed submission drains and RESUME_EXISTING
        # skips the committed first step.
        marker_path = str(marker)

        @node(output_name="first_out")
        def first(x: int) -> int:
            with open(marker_path, "a") as f:
                f.write("first\n")
            return x

        @node(output_name="second_out")
        def second(first_out: int) -> int:
            with open(marker_path, "a") as f:
                f.write("second\n")
            return first_out

        graph = Graph([first, second], name="killdef").with_runner(SyncRunner())
        host = serve(graph, home=home, deployment_version="v1")
        ref = RunRef(home=home.uri, run_id="wf-kill")
        async with _worker(host, "w-parent"):
            view = await _wait_for(lambda: _terminal_view(host.client, ref), timeout=30)

        assert view.status == WorkflowStatus.COMPLETED
        assert marker.read_text().splitlines() == ["first", "second"]
        assert home._get_submission_sync("wf-kill")["state"] == "finished"

    async def test_restart_after_crash_before_first_step_starts_fresh(self, home):
        """A run claimed and lost before its first committed step leaves an
        empty active runs row that can neither resume (no seed inputs in
        checkpoint state) nor restart (input override is forbidden). The
        next worker deletes the history-less row and starts fresh from the
        submission's pinned inputs instead of crash-looping on resume."""
        calls = {"n": 0}

        @node(output_name="out")
        def compute(x: int) -> int:
            calls["n"] += 1
            return x * 10

        graph = Graph([compute], name="freshdef").with_runner(SyncRunner())
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await host.submit(served["freshdef"], {"x": 1}, workflow_id="wf-fresh")

        # Stand-in for a worker killed after claim but before the first
        # committed step: an empty active runs row, submission still claimed.
        home.create_run_sync("wf-fresh", graph_name="freshdef")
        db = home._sync_db()
        db.execute("UPDATE host_submissions SET state = 'claimed' WHERE workflow_id = 'wf-fresh'")
        db.commit()

        async with _worker(host):
            view = await _wait_for(lambda: _terminal_view(host.client, receipt.run_ref))

        assert view.status == WorkflowStatus.COMPLETED
        assert calls["n"] == 1
        assert not host.worker_errors
        # The history-less row was reset with a durable fact, then re-run.
        kinds = [u[1] for u in home._read_run_updates_sync("wf-fresh")]
        assert "run_reset" in kinds
        assert home._get_submission_sync("wf-fresh")["state"] == "finished"


# === 3. Recovery brake (A6) ===


class TestRecoveryBrake:
    async def test_poison_run_exhausts_then_rerun_revives(self, home):
        calls = {"n": 0}
        host, served = serve_graphs(_counting_sync_graph("poisondef", calls), home=home, deployment_version="v1")
        receipt = await host.submit(served["poisondef"], {"x": 1}, workflow_id="wf-poison", recovery_cap=2)
        assert home._get_submission_sync("wf-poison")["recovery_cap"] == 2

        # Two progressless crash cycles: claimed with no committed steps.
        db = home._sync_db()
        db.execute("UPDATE host_submissions SET state = 'claimed' WHERE workflow_id = 'wf-poison'")
        db.commit()
        await home._reclaim_expired()
        first = home._get_submission_sync("wf-poison")
        assert first["state"] == "pending"
        assert first["recovery_attempts"] == 1

        db.execute("UPDATE host_submissions SET state = 'claimed' WHERE workflow_id = 'wf-poison'")
        db.commit()
        await home._reclaim_expired()
        # attempts >= cap: parked as recovery-exhausted with a durable update.
        submission = home._get_submission_sync("wf-poison")
        assert submission["state"] == "exhausted"
        assert submission["recovery_attempts"] == 2
        exhausted_updates = [u for u in home._read_run_updates_sync("wf-poison") if u[1] == "recovery_exhausted"]
        assert len(exhausted_updates) == 1
        assert json.loads(exhausted_updates[0][2]) == {"recovery_attempts": 2, "recovery_cap": 2}

        # The brake holds across later worker startups; claims skip it.
        async with _worker(host, "w-1"):
            view = await host.client.get(receipt.run_ref)
            assert view.waiting is WaitingCondition.RECOVERY_EXHAUSTED
            assert view.status is None
            await asyncio.sleep(0.2)
        assert calls["n"] == 0
        assert home._get_submission_sync("wf-poison")["state"] == "exhausted"

        # Rerun revives braked work under a fresh workflow id.
        rerun_receipt = await host.client.rerun(receipt.run_ref)
        assert rerun_receipt.workflow_id == "wf-poison-retry-1"
        async with _worker(host, "w-2"):
            retry_view = await _wait_for(lambda: _terminal_view(host.client, rerun_receipt.run_ref))
        assert retry_view.status == WorkflowStatus.COMPLETED
        assert retry_view.retry_of == "wf-poison"

    async def test_killed_with_committed_step_shows_one_attempt_then_resets(self, home):
        """Prototype Scenario 3 (protocol-19 shape): a run killed mid-flight
        WITH a committed step shows recovery_attempts=1 after re-adoption —
        re-adoption always increments; only NEW committed progress (a saved
        StepRecord, a durable pause, a terminal transition) resets, at
        commit time. A status flip to active at re-claim never resets."""
        host, served = serve_graphs(_sync_graph("progdef"), home=home, deployment_version="v1")
        await host.submit(served["progdef"], {"x": 2}, workflow_id="wf-prog", recovery_cap=3)

        # Killed mid-execution WITH one committed step. The raw INSERT
        # stands in for the dead process's commit (no reset hook fired).
        home.create_run_sync("wf-prog", graph_name="progdef")
        db = home._sync_db()
        db.execute("INSERT INTO steps (run_id, step_index, superstep, node_name, status) VALUES ('wf-prog', 0, 0, 'compute', 'completed')")
        db.execute("UPDATE host_submissions SET state = 'claimed' WHERE workflow_id = 'wf-prog'")
        db.commit()
        await home._reclaim_expired()
        submission = home._get_submission_sync("wf-prog")
        assert submission["state"] == "pending"
        assert submission["recovery_attempts"] == 1

        # Re-claim flips the run back to active — recovery bookkeeping, not
        # progress: the counter must NOT reset.
        home.update_run_status_sync("wf-prog", WorkflowStatus.ACTIVE)
        assert home._get_submission_sync("wf-prog")["recovery_attempts"] == 1

        # The resumed run commits a NEW step: the counter resets at commit
        # time, in the same transaction as the step save.
        home.save_step_sync(
            StepRecord(
                run_id="wf-prog",
                superstep=1,
                node_name="downstream",
                index=1,
                status=StepStatus.COMPLETED,
                input_versions={},
            )
        )
        assert home._get_submission_sync("wf-prog")["recovery_attempts"] == 0

    async def test_pause_and_terminal_transitions_reset_the_brake(self, home):
        host, served = serve_graphs(_sync_graph("pausedef"), home=home, deployment_version="v1")
        await host.submit(served["pausedef"], {"x": 3}, workflow_id="wf-pause", recovery_cap=3)
        home.create_run_sync("wf-pause", graph_name="pausedef")
        db = home._sync_db()
        db.execute("UPDATE host_submissions SET state = 'claimed', recovery_attempts = 2 WHERE workflow_id = 'wf-pause'")
        db.commit()

        # A durable pause is committed progress: paused work waits on purpose.
        home.update_run_status_sync("wf-pause", WorkflowStatus.PAUSED)
        assert home._get_submission_sync("wf-pause")["recovery_attempts"] == 0

        # A terminal transition also resets (the submission then finishes).
        await host.submit(served["pausedef"], {"x": 4}, workflow_id="wf-term2", recovery_cap=3)
        home.create_run_sync("wf-term2", graph_name="pausedef")
        db.execute("UPDATE host_submissions SET state = 'claimed', recovery_attempts = 1 WHERE workflow_id = 'wf-term2'")
        db.commit()
        home.update_run_status_sync("wf-term2", WorkflowStatus.COMPLETED)
        assert home._get_submission_sync("wf-term2")["recovery_attempts"] == 0

    async def test_recovery_cap_validation_and_fingerprint_exclusion(self, home):
        host, served = serve_graphs(_sync_graph("dbl"), home=home)
        with pytest.raises(ValueError, match="recovery_cap"):
            await host.submit(served["dbl"], {"x": 1}, recovery_cap=-1)
        with pytest.raises(ValueError, match="recovery_cap"):
            await host.submit(served["dbl"], {"x": 1}, recovery_cap=1.5)
        with pytest.raises(ValueError, match="recovery_cap"):
            host.submit_sync(served["dbl"], {"x": 1}, recovery_cap=True)

        # recovery_cap is not part of the start fingerprint: an identical
        # resubmission with a different cap dedupes and keeps the first cap.
        first = await host.submit(served["dbl"], {"x": 1}, workflow_id="wf-cap", recovery_cap=2)
        dup = await host.submit(served["dbl"], {"x": 1}, workflow_id="wf-cap", recovery_cap=5)
        assert first.duplicate is False
        assert dup.duplicate is True
        assert home._get_submission_sync("wf-cap")["recovery_cap"] == 2


# === 3b. A tree a dead worker abandoned settles with its submission (#465) ===

_ABANDONED = {"status": "stopped", "reason": "abandoned_incarnation"}
_CLAIM = 4


async def _stage_abandoned_tree(
    home,
    *,
    root_status: WorkflowStatus = WorkflowStatus.COMPLETED,
    child_id: str = "wf/x",
    child_status: WorkflowStatus = WorkflowStatus.ACTIVE,
    child_parent: str = "wf",
    child_is_submission: bool = False,
    recovery_cap: int = 3,
    recovery_attempts: int = 0,
) -> None:
    """What a SIGKILL leaves behind, staged: submission ``wf`` claimed at
    ``_CLAIM``, its run row, and one ``active`` nested run the dead
    incarnation committed together with a boundary that never started. The
    root carries a never-started boundary of its own, which nothing may drop.
    """
    host, served = serve_graphs(_sync_graph("rootdef"), home=home, deployment_version="v1")
    await host.submit(served["rootdef"], {"x": 1}, workflow_id="wf", recovery_cap=recovery_cap)
    if child_is_submission:
        await host.submit(served["rootdef"], {"x": 2}, workflow_id=child_id)
    if child_parent != "wf":
        home.create_run_sync(child_parent, graph_name="elsewhere")
    home.create_run_sync("wf", graph_name="rootdef")
    if root_status is not WorkflowStatus.ACTIVE:
        home.update_run_status_sync("wf", root_status)
    home.create_run_sync(child_id, graph_name="page_recipe", parent_run_id=child_parent)
    if child_status is not WorkflowStatus.ACTIVE:
        home.update_run_status_sync(child_id, child_status)
    home.record_pending_nodes_sync(
        [
            PendingNode(run_id="wf", superstep=1, node_name="publish"),
            PendingNode(run_id=child_id, superstep=0, node_name="render"),
        ]
    )
    db = home._sync_db()
    db.execute(
        "UPDATE host_submissions SET state = 'claimed', claim_seq = ?, recovery_attempts = ? WHERE workflow_id = 'wf'",
        (_CLAIM, recovery_attempts),
    )
    db.commit()


def _journal(home) -> dict[str, list]:
    """Every row a settle could touch, so a test can diff exactly what moved."""
    db = home._sync_db()
    return {
        "runs": db.execute("SELECT id, status, completed_at FROM runs ORDER BY id").fetchall(),
        "run_updates": db.execute("SELECT run_id, seq, kind, payload FROM run_updates ORDER BY run_id, seq").fetchall(),
        "batch_updates": db.execute("SELECT batch_id, bseq, kind, payload FROM batch_updates ORDER BY batch_id, bseq").fetchall(),
        "pending_nodes": db.execute("SELECT run_id, superstep, node_name FROM pending_nodes ORDER BY run_id, superstep, node_name").fetchall(),
    }


def _run_row(journal, run_id):
    return next(row for row in journal["runs"] if row[0] == run_id)


def _new_updates(before, after):
    """The run updates ``after`` has that ``before`` did not, payloads decoded."""
    seen = set(before["run_updates"])
    return [(row[0], row[2], json.loads(row[3])) for row in after["run_updates"] if row not in seen]


class TestAbandonedDescendantsSettleWithTheirSubmission:
    """A nested run may claim ``active`` only while its submission is unsettled.

    A table page recipe mints a fresh run id per attempt, so the run a killed
    worker left ``active`` is never re-addressed by the resume. The settle
    runs in the one transaction that proves nothing will resume the tree
    under these ids again: the one that moves the submission out of
    'claimed' into a settled state — never at re-adoption, which resumes it.
    """

    async def test_release_settles_a_descendant_no_process_holds(self, home):
        await _stage_abandoned_tree(home, recovery_attempts=1)
        before = _journal(home)

        assert await home._release_submission("wf", _CLAIM) is True

        after = _journal(home)
        status, completed_at = _run_row(after, "wf/x")[1:]
        assert (status, completed_at is not None) == ("stopped", True)
        # One status update on the orphan, with the reason, and nothing else
        # new anywhere: no child_settled fact, no update on the submission.
        assert _new_updates(before, after) == [("wf/x", "status", _ABANDONED)]
        assert after["batch_updates"] == before["batch_updates"]
        # Its never-started boundary is dropped; the root's is not.
        assert after["pending_nodes"] == [("wf", 1, "publish")]
        # The Run's own row is untouched.
        assert _run_row(after, "wf") == _run_row(before, "wf")
        submission = home._get_submission_sync("wf")
        assert (submission["state"], submission["recovery_attempts"]) == ("finished", 1)

    async def test_release_settles_every_depth_below_the_root(self, home):
        """A recipe under a nested graph is abandoned the same way: the walk
        is ``runs.parent_run_id``, so depth is not special."""
        await _stage_abandoned_tree(home, child_id="wf/inner")
        home.create_run_sync("run-recipe", graph_name="page_recipe", parent_run_id="wf/inner")
        before = _journal(home)

        assert await home._release_submission("wf", _CLAIM) is True

        after = _journal(home)
        assert {run_id: status for run_id, status, _ in after["runs"]} == {"wf": "completed", "wf/inner": "stopped", "run-recipe": "stopped"}
        assert sorted(_new_updates(before, after)) == [("run-recipe", "status", _ABANDONED), ("wf/inner", "status", _ABANDONED)]

    async def test_a_stale_release_settles_nothing(self, home):
        await _stage_abandoned_tree(home)
        before = _journal(home)

        assert await home._release_submission("wf", _CLAIM - 1) is False

        assert _journal(home) == before
        assert home._get_submission_sync("wf")["state"] == "claimed"

    @pytest.mark.parametrize(
        "shape",
        [
            pytest.param({"child_status": WorkflowStatus.PAUSED}, id="paused-descendant"),
            pytest.param({"child_parent": "elsewhere"}, id="another-roots-run"),
            pytest.param({"child_id": "wf-sub", "child_is_submission": True}, id="a-submissions-own-run"),
        ],
    )
    async def test_release_leaves_alone_what_the_claim_does_not_own(self, home, shape):
        """A paused nested run is waiting on an answer that resumes it under
        its own id; another root's run is not this claim's; a row that is a
        submission's own run is only ever settled by that submission."""
        await _stage_abandoned_tree(home, **shape)
        before = _journal(home)

        assert await home._release_submission("wf", _CLAIM) is True

        assert _journal(home) == before
        assert home._get_submission_sync("wf")["state"] == "finished"

    async def test_re_adoption_that_returns_to_pending_leaves_the_tree_alone(self, home):
        """The tree is about to be resumed under the same ids. A ``stopped``
        GraphNode child would refuse that resume (``WorkflowStoppedError``
        from ``lineage.resolve_existing_run``), so nothing below it moves."""
        await _stage_abandoned_tree(home, root_status=WorkflowStatus.ACTIVE, child_id="wf/inner")
        before = _journal(home)

        await home._reclaim_expired()

        submission = home._get_submission_sync("wf")
        assert (submission["state"], submission["recovery_attempts"]) == ("pending", 1)
        assert _journal(home) == before

    async def test_a_recovery_exhausted_park_settles_the_tree_below_the_root(self, home):
        await _stage_abandoned_tree(home, root_status=WorkflowStatus.ACTIVE, recovery_cap=1)
        before = _journal(home)

        await home._reclaim_expired()

        assert home._get_submission_sync("wf")["state"] == "exhausted"
        after = _journal(home)
        assert _run_row(after, "wf/x")[1] == "stopped"
        # The Run's own row stays as it was: parking brakes it, it does not end it.
        assert _run_row(after, "wf") == _run_row(before, "wf") == ("wf", "active", None)
        assert _new_updates(before, after) == [
            ("wf", "recovery_exhausted", {"recovery_attempts": 1, "recovery_cap": 1}),
            ("wf/x", "status", _ABANDONED),
        ]
        assert after["pending_nodes"] == [("wf", 1, "publish")]

    async def test_re_adoption_of_a_terminal_root_settles_its_tree(self, home):
        """The worker died after its Run committed a terminal status but
        before it released the claim: the scan finishes the submission, and
        the tree below it settles in that same transaction."""
        await _stage_abandoned_tree(home, root_status=WorkflowStatus.COMPLETED)
        before = _journal(home)

        await home._reclaim_expired()

        assert home._get_submission_sync("wf")["state"] == "finished"
        after = _journal(home)
        assert _run_row(after, "wf") == _run_row(before, "wf")
        assert _new_updates(before, after) == [("wf/x", "status", _ABANDONED)]
        assert after["pending_nodes"] == [("wf", 1, "publish")]

    async def test_the_settle_keeps_dispatched_and_settled_boundaries(self, home):
        """Only a boundary that carries no information (``PENDING``) goes. A
        dispatched effect, a settled node, and a committed step are evidence."""
        await _stage_abandoned_tree(home)
        now = datetime.now(timezone.utc)
        home.save_step_sync(
            StepRecord(run_id="wf/x", superstep=0, node_name="render", index=0, status=StepStatus.COMPLETED, input_versions={}),
        )
        home.record_pending_nodes_sync(
            [
                PendingNode(run_id="wf/x", superstep=1, node_name="fetch", dispatched_at=now),
                PendingNode(run_id="wf/x", superstep=1, node_name="measure", settled_at=now),
                PendingNode(run_id="wf/x", superstep=1, node_name="publish"),
            ]
        )
        states = {b.node_name: b.state for b in home.get_node_boundaries_sync("wf/x")}
        assert states == {
            "render": BoundaryState.COMMITTED,
            "fetch": BoundaryState.UNKNOWN_EFFECT,
            "measure": BoundaryState.SETTLED_UNRECORDED,
            "publish": BoundaryState.PENDING,
        }

        assert await home._release_submission("wf", _CLAIM) is True

        kept = {b.node_name: b.state for b in home.get_node_boundaries_sync("wf/x")}
        assert kept == {"render": BoundaryState.COMMITTED, "fetch": BoundaryState.UNKNOWN_EFFECT, "measure": BoundaryState.SETTLED_UNRECORDED}

    async def test_a_settle_failure_rolls_back_the_release(self, home, monkeypatch):
        """The settle commits with the release or not at all: the claim stays
        held at the same ``claim_seq``, exactly as any failed release leaves it."""
        await _stage_abandoned_tree(home)
        before = _journal(home)
        settle = RunHome._settle_abandoned_descendants_in_txn

        async def settle_then_fail(self, root_ids):
            await settle(self, root_ids)
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(RunHome, "_settle_abandoned_descendants_in_txn", settle_then_fail)

        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            await home._release_submission("wf", _CLAIM)

        submission = home._get_submission_sync("wf")
        assert (submission["state"], submission["claim_seq"]) == ("claimed", _CLAIM)
        assert _journal(home) == before


# === 4. client.list over the extended waiting vocabulary ===


class TestClientList:
    async def _fixtures(self, home) -> RunHomeClient:
        """Manufacture one run per waiting condition plus a completed bare run."""
        host, served = serve_graphs(_sync_graph("dbl"), home=home, deployment_version="v1")
        await host.submit(served["dbl"], {"x": 1}, workflow_id="wf-q")
        time.sleep(0.002)
        await host.submit(served["dbl"], {"x": 1}, workflow_id="wf-sched", start_at="2030-01-01T00:00:00+00:00")
        time.sleep(0.002)
        await host.submit(served["dbl"], {"x": 1}, workflow_id="wf-incomp")
        time.sleep(0.002)
        await host.submit(served["dbl"], {"x": 1}, workflow_id="wf-exh", recovery_cap=1)
        # Trip the brake honestly: claimed + no progress + cap=1.
        db = home._sync_db()
        db.execute("UPDATE host_submissions SET state = 'claimed' WHERE workflow_id = 'wf-exh'")
        db.commit()
        await home._reclaim_expired()
        assert home._get_submission_sync("wf-exh")["state"] == "exhausted"
        # (restart_scan ran before the incompatible marking below, which it
        # would otherwise reset to 'compatible'.)
        db.execute("UPDATE host_submissions SET compat_state = 'incompatible' WHERE workflow_id = 'wf-incomp'")
        db.commit()
        time.sleep(0.002)
        # Bare Tier-0 runs (no submission): one paused, one completed.
        home.create_run_sync("wf-paused", graph_name="dbl")
        home.update_run_status_sync("wf-paused", WorkflowStatus.PAUSED)
        time.sleep(0.002)
        home.create_run_sync("wf-done", graph_name="other")
        home.update_run_status_sync("wf-done", WorkflowStatus.COMPLETED)
        # Backdate two rows so newest-first ordering and older_than bite.
        db.execute("UPDATE host_submissions SET created_at = '2020-01-01T00:00:00+00:00' WHERE workflow_id = 'wf-q'")
        db.execute("UPDATE runs SET created_at = '2020-06-01T00:00:00Z' WHERE id = 'wf-done'")
        db.commit()
        return host.client

    async def test_list_filters_waiting_status_and_ordering(self, home):
        client = await self._fixtures(home)

        everything = await client.list(RunQuery())
        ids = [view.workflow_id for view in everything]
        assert ids == ["wf-paused", "wf-exh", "wf-incomp", "wf-sched", "wf-done", "wf-q"]

        by_definition = await client.list(RunQuery(definition="dbl"))
        assert {view.workflow_id for view in by_definition} == {"wf-q", "wf-sched", "wf-incomp", "wf-exh", "wf-paused"}

        by_status = await client.list(RunQuery(status=WorkflowStatus.COMPLETED))
        assert [view.workflow_id for view in by_status] == ["wf-done"]
        paused_status = await client.list(RunQuery(status=WorkflowStatus.PAUSED))
        assert [view.workflow_id for view in paused_status] == ["wf-paused"]

        waiting_cases = {
            WaitingCondition.QUEUED: ["wf-q"],
            WaitingCondition.SCHEDULED: ["wf-sched"],
            WaitingCondition.VERSION_INCOMPATIBLE: ["wf-incomp"],
            WaitingCondition.RECOVERY_EXHAUSTED: ["wf-exh"],
            WaitingCondition.PAUSED: ["wf-paused"],
        }
        for condition, expected in waiting_cases.items():
            views = await client.list(RunQuery(waiting=condition))
            assert [view.workflow_id for view in views] == expected, condition

        aged = await client.list(RunQuery(older_than=timedelta(days=30)))
        assert [view.workflow_id for view in aged] == ["wf-done", "wf-q"]

        limited = await client.list(RunQuery(limit=2))
        assert [view.workflow_id for view in limited] == ["wf-paused", "wf-exh"]

    async def test_list_sync_mirror_and_validation(self, home):
        client = await self._fixtures(home)
        async_ids = [view.workflow_id for view in await client.list(RunQuery())]
        sync_ids = [view.workflow_id for view in client.list_sync(RunQuery())]
        assert async_ids == sync_ids

        with pytest.raises(TypeError, match="RunQuery"):
            await client.list("wf-q")
        with pytest.raises(ValueError, match="limit"):
            await client.list(RunQuery(limit=0))
        with pytest.raises(ValueError, match="limit"):
            client.list_sync(RunQuery(limit=True))

    async def test_rerun_sync_accepts_exhausted_source(self, home):
        host, served = serve_graphs(_sync_graph("dbl"), home=home, deployment_version="v1")
        await host.submit(served["dbl"], {"x": 1}, workflow_id="wf-exh2", recovery_cap=1)
        db = home._sync_db()
        db.execute("UPDATE host_submissions SET state = 'claimed' WHERE workflow_id = 'wf-exh2'")
        db.commit()
        await home._reclaim_expired()
        assert home._get_submission_sync("wf-exh2")["state"] == "exhausted"
        receipt = host.client.rerun_sync(RunRef(home=home.uri, run_id="wf-exh2"))
        assert receipt.workflow_id == "wf-exh2-retry-1"
        assert home._get_submission_sync("wf-exh2-retry-1")["retry_of"] == "wf-exh2"
