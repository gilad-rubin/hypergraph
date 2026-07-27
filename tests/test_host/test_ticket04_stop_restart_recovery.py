"""Durable Host V1 — ticket 04: durable stop, real-kill restart, recovery brake, client.list.

Covers: detached durable stop of an executing run (with info), stop
after-terminal and unknown-run errors, first-stop-wins dedup, stop before
first execution (never executes, no runs row invented), a real SIGKILL
subprocess restart that resumes without re-executing committed steps, the
recovery brake (progressless crash loops park as recovery-exhausted;
progress resets the budget; rerun revives), and client.list filtering over
the extended waiting vocabulary.
"""

import asyncio
import contextlib
import json
import subprocess
import sys
import time
from datetime import timedelta

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
from hypergraph.checkpointers.types import StepRecord, StepStatus, WorkflowStatus
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
asyncio.run(host.work_forever("w-child", poll_interval=0.02))
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
        await home._restart_scan()
        first = home._get_submission_sync("wf-poison")
        assert first["state"] == "pending"
        assert first["recovery_attempts"] == 1

        db.execute("UPDATE host_submissions SET state = 'claimed' WHERE workflow_id = 'wf-poison'")
        db.commit()
        await home._restart_scan()
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
        await home._restart_scan()
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
        await home._restart_scan()
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
        # Backdate two rows so oldest-first ordering and older_than bite.
        db.execute("UPDATE host_submissions SET created_at = '2020-01-01T00:00:00+00:00' WHERE workflow_id = 'wf-q'")
        db.execute("UPDATE runs SET created_at = '2020-06-01T00:00:00Z' WHERE id = 'wf-done'")
        db.commit()
        return host.client

    async def test_list_filters_waiting_status_and_ordering(self, home):
        client = await self._fixtures(home)

        everything = await client.list(RunQuery())
        ids = [view.workflow_id for view in everything]
        assert ids == ["wf-q", "wf-done", "wf-sched", "wf-incomp", "wf-exh", "wf-paused"]

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
        assert [view.workflow_id for view in aged] == ["wf-q", "wf-done"]

        limited = await client.list(RunQuery(limit=2))
        assert [view.workflow_id for view in limited] == ["wf-q", "wf-done"]

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
        await home._restart_scan()
        assert home._get_submission_sync("wf-exh2")["state"] == "exhausted"
        receipt = host.client.rerun_sync(RunRef(home=home.uri, run_id="wf-exh2"))
        assert receipt.workflow_id == "wf-exh2-retry-1"
        assert home._get_submission_sync("wf-exh2-retry-1")["retry_of"] == "wf-exh2"
