"""Durable Host V1 — ticket 02: submit, execute, and watch one Run.

Covers: durable submission before execution, execution through the
Definition's runner, detached (cross-process) get/watch, reconnectable
durable cursors with non-advancing live previews, the exclusive worker
lock with bounded drain, restart re-adoption, sync/async parity, and
serve() construction validation.
"""

import asyncio
import contextlib
import json
import subprocess
import sys
import threading
import time

import pytest
import pytest_asyncio

from hypergraph import (
    AlreadyTerminalError,
    AsyncRunner,
    BaseRunner,
    Graph,
    GraphNode,
    RunHome,
    RunHomeClient,
    RunRef,
    SyncRunner,
    WaitingCondition,
    WorkerLockError,
    node,
    serve,
)
from hypergraph.checkpointers.types import WorkflowStatus
from hypergraph.runners._shared.state import RunnerCapabilities
from tests.test_host._batch_api import graph_of

aiosqlite = pytest.importorskip("aiosqlite")


# === Helpers ===


def _sync_graph(name: str, *, delay: float = 0.0, gate: threading.Event | None = None) -> Graph:
    @node(output_name="out")
    def compute(x: int) -> int:
        if gate is not None:
            gate.wait(timeout=15)
        if delay:
            time.sleep(delay)
        return x + 1

    return Graph([compute], name=name).with_runner(SyncRunner())


def _async_graph(name: str, *, delay: float = 0.05) -> Graph:
    @node(output_name="out")
    async def compute(x: int) -> int:
        await asyncio.sleep(delay)
        return x + 1

    return Graph([compute], name=name).with_runner(AsyncRunner())


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


def _seq(update) -> int:
    return int(update.cursor.split(":", 1)[1])


async def _collect_watch(client, ref, **kwargs):
    return [update async for update in client.watch(ref, **kwargs)]


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


async def _run_started(home, workflow_id):
    return home.get_run(workflow_id) is not None


async def _submission_state(home, workflow_id, state):
    submission = home._get_submission_sync(workflow_id)
    return submission is not None and submission["state"] == state


@pytest_asyncio.fixture
async def home(tmp_path):
    h = RunHome.open(_home_uri(tmp_path))
    yield h
    await h.close()


# === Value-object and binding behavior ===


class TestRefsAndBinding:
    def test_run_ref_round_trip_and_equality(self):
        ref = RunRef(home="file:./runs.db", run_id="wf-1")
        data = ref.to_dict()
        json.dumps(data)  # JSON-serializable
        assert RunRef.from_dict(json.loads(json.dumps(data))) == ref
        assert ref == RunRef(home="file:./runs.db", run_id="wf-1")
        assert ref != RunRef(home="file:./runs.db", run_id="wf-2")
        with pytest.raises(AttributeError):
            ref.run_id = "other"  # frozen

    def test_run_ref_has_no_live_methods(self):
        ref = RunRef(home="file:./runs.db", run_id="wf-1")
        for forbidden in ("status", "result", "stop", "watch", "get"):
            assert not hasattr(ref, forbidden)

    def test_with_runner_returns_new_graph_and_keeps_original_unbound(self):
        graph = Graph([_one_node()], name="dbl")
        bound = graph.with_runner(SyncRunner())
        assert bound is not graph
        assert graph._bound_runner is None
        assert isinstance(bound._bound_runner, SyncRunner)
        assert bound.structural_hash == graph.structural_hash

    def test_with_runner_rejects_non_runner(self):
        graph = Graph([_one_node()], name="dbl")
        with pytest.raises(TypeError, match="runner instance"):
            graph.with_runner(object())

    async def test_serve_rejects_runner_like_non_baserunner(self):
        # The strict BaseRunner check lives in serve() (graph facade must not
        # import runners at runtime — issue #264).
        class _DuckRunner:
            def run(self):  # pragma: no cover - never called
                raise NotImplementedError

        home = RunHome.open(":memory:")
        try:
            bound = Graph([_one_node()], name="dbl").with_runner(_DuckRunner())
            with pytest.raises(TypeError, match="BaseRunner"):
                serve(bound, home=home)
        finally:
            await home.close()

    def test_with_checkpointer_clones_without_mutating(self):
        runner = SyncRunner()
        clone = runner.with_checkpointer(object())
        assert clone is not runner
        assert runner._checkpointer is None
        assert clone._checkpointer is not None
        assert clone._active_workflows is not runner._active_workflows

    def test_with_checkpointer_rebinds_executors_to_the_clone(self):
        """F1: the clone's GraphNode executor must delegate to the CLONE.

        A shallow copy would share the original's executor dict, whose
        GraphNode executor captured the original runner — nested child
        workflows would then persist nowhere and register in the wrong
        live registry.
        """
        runner = SyncRunner()
        clone = runner.with_checkpointer(object())
        assert clone._executors is not runner._executors
        assert clone._executors[GraphNode].runner is clone
        assert runner._executors[GraphNode].runner is runner

    def test_has_active_run_reflects_the_live_registry(self):
        runner = SyncRunner()
        assert runner.has_active_run("wf-x") is False
        reservation = runner._active_workflows.reserve("wf-x")
        assert runner.has_active_run("wf-x") is True
        reservation.release()
        assert runner.has_active_run("wf-x") is False


# === 1. Submission persists before execution ===


class TestSubmitPersistsBeforeExecution:
    async def test_submit_then_worker_completes_through_definition_runner(self, tmp_path, home):
        graph = _sync_graph("dbl", delay=0.1)
        host = serve(graph, home=home, deployment_version="v1")

        receipt = await host.submit(graph_of(host, "dbl"), {"x": 1}, workflow_id="wf-1")
        assert receipt.run_ref == RunRef(home=home.uri, run_id="wf-1")
        assert receipt.workflow_id == "wf-1"
        assert receipt.duplicate is False

        # Durable intent exists BEFORE any execution: submission row +
        # run_updates('submitted') at seq 1, and no runs row yet.
        submission = home._get_submission_sync("wf-1")
        assert submission is not None
        assert submission["state"] == "pending"
        assert submission["definition_name"] == "dbl"
        assert submission["def_version"] == "v1"
        assert submission["def_struct_hash"] == graph.structural_hash
        updates = home._read_run_updates_sync("wf-1")
        assert [(seq, kind) for seq, kind, _, _ in updates] == [(1, "submitted")]
        assert home.get_run("wf-1") is None

        view = await host.client.get(receipt.run_ref)
        assert view is not None
        assert view.status is None
        assert view.waiting is WaitingCondition.QUEUED

        # Start the worker: the run completes through the Definition's runner.
        async with _worker(host):
            await _wait_for(lambda: _terminal_view(host.client, receipt.run_ref))

        run = home.get_run("wf-1")
        assert run is not None
        assert run.status == WorkflowStatus.COMPLETED
        view = await host.client.get(receipt.run_ref)
        assert view.status == WorkflowStatus.COMPLETED
        assert view.waiting is None
        assert view.definition_name == "dbl"
        assert home._get_submission_sync("wf-1")["state"] == "finished"
        # Outputs landed in the checkpointer state.
        assert home.values("wf-1")["out"] == 2

    async def test_duplicate_submit_uses_existing_receipt(self, home):
        host = serve(_sync_graph("dbl"), home=home)
        first = await host.submit(graph_of(host, "dbl"), {"x": 1}, workflow_id="wf-dup")
        second = await host.submit(graph_of(host, "dbl"), {"x": 1}, workflow_id="wf-dup")
        assert second.duplicate is True
        assert second.run_ref == first.run_ref
        # No new rows written for the duplicate.
        assert len(home._read_run_updates_sync("wf-dup")) == 1

    async def test_terminal_reuse_raises_typed_error(self, home):
        host = serve(_sync_graph("dbl"), home=home)
        receipt = await host.submit(graph_of(host, "dbl"), {"x": 1}, workflow_id="wf-term")
        async with _worker(host):
            await _wait_for(lambda: _terminal_view(host.client, receipt.run_ref))
        with pytest.raises(AlreadyTerminalError):
            await host.submit(graph_of(host, "dbl"), {"x": 2}, workflow_id="wf-term")

    async def test_submit_sync_mirror(self, home):
        host = serve(_sync_graph("dbl"), home=home)
        receipt = host.submit_sync(graph_of(host, "dbl"), {"x": 3}, workflow_id="wf-sync-submit")
        assert receipt.duplicate is False
        assert home._get_submission_sync("wf-sync-submit")["inputs_json"] == json.dumps({"x": 3})


# === 2. Detached client (process that did not submit) ===


class TestDetachedClient:
    async def test_second_home_instance_get_and_watch(self, tmp_path, home):
        host = serve(_sync_graph("dbl"), home=home)
        receipt = await host.submit(graph_of(host, "dbl"), {"x": 1}, workflow_id="wf-1")
        async with _worker(host):
            await _wait_for(lambda: _terminal_view(host.client, receipt.run_ref))

        # A second RunHome.open on the same file simulates another process.
        detached = RunHome.open(_home_uri(tmp_path))
        try:
            client2 = RunHomeClient(detached)
            ref = RunRef(home=detached.uri, run_id="wf-1")
            view = await client2.get(ref)
            assert view is not None
            assert view.status == WorkflowStatus.COMPLETED
            assert view.waiting is None
            assert view.definition_name == "dbl"

            updates = await _collect_watch(client2, ref)
            assert [u.kind for u in updates] == ["submitted", "run_started", "step", "status"]
            assert all(u.durable for u in updates)
            assert [u.cursor for u in updates] == ["seq:1", "seq:2", "seq:3", "seq:4"]
            assert updates[-1].payload["status"] == "completed"
        finally:
            await detached.close()

    async def test_actual_subprocess_get_and_watch(self, tmp_path, home):
        host = serve(_sync_graph("dbl"), home=home)
        receipt = await host.submit(graph_of(host, "dbl"), {"x": 1}, workflow_id="wf-1")
        async with _worker(host):
            await _wait_for(lambda: _terminal_view(host.client, receipt.run_ref))
        await home.close()  # release this process's connections before the child opens the file

        uri = _home_uri(tmp_path)
        script = f"""
import asyncio, json
from hypergraph import RunHome, RunHomeClient, RunRef

async def main():
    home = RunHome.open({uri!r})
    client = RunHomeClient(home)
    ref = RunRef(home={uri!r}, run_id="wf-1")
    view = await client.get(ref)
    updates = [u async for u in client.watch(ref)]
    print(json.dumps({{
        "status": view.status.value if view.status else None,
        "waiting": view.waiting.value if view.waiting else None,
        "definition": view.definition_name,
        "kinds": [u.kind for u in updates],
        "cursors": [u.cursor for u in updates],
        "durable": [u.durable for u in updates],
    }}))
    await home.close()

asyncio.run(main())
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, f"subprocess failed:\n{completed.stderr}"
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        assert result["status"] == "completed"
        assert result["waiting"] is None
        assert result["definition"] == "dbl"
        assert result["kinds"] == ["submitted", "run_started", "step", "status"]
        assert result["cursors"] == ["seq:1", "seq:2", "seq:3", "seq:4"]
        assert all(result["durable"])


# === 3. Reconnectable durable cursor ===


class TestCursorReconnection:
    async def test_resume_from_stored_cursor_has_no_gaps_or_repeats(self, tmp_path, home):
        host = serve(_sync_graph("dbl"), home=home)
        receipt = await host.submit(graph_of(host, "dbl"), {"x": 1}, workflow_id="wf-1")
        async with _worker(host):
            updates1 = await _collect_watch(host.client, receipt.run_ref)
        durable1 = [u for u in updates1 if u.durable]
        seqs1 = [_seq(u) for u in durable1]
        assert seqs1 == sorted(seqs1) and len(set(seqs1)) == len(seqs1)

        stored_cursor = durable1[1].cursor  # e.g. "seq:2"

        # A NEW client resumes from the stored cursor.
        detached = RunHome.open(_home_uri(tmp_path))
        try:
            client2 = RunHomeClient(detached)
            updates2 = await _collect_watch(client2, receipt.run_ref, after=stored_cursor)
        finally:
            await detached.close()
        seqs2 = [_seq(u) for u in updates2]
        assert seqs2 == [seq for seq in seqs1 if seq > _seq(durable1[1])]
        assert seqs2 == sorted(seqs2) and len(set(seqs2)) == len(seqs2)  # no gaps, no repeats
        assert all(u.durable for u in updates2)

    async def test_watch_cursor_accepts_none_int_and_string(self, home):
        host = serve(_sync_graph("dbl"), home=home)
        receipt = await host.submit(graph_of(host, "dbl"), {"x": 1}, workflow_id="wf-1")
        async with _worker(host):
            await _wait_for(lambda: _terminal_view(host.client, receipt.run_ref))

        full = await _collect_watch(host.client, receipt.run_ref)
        from_int = await _collect_watch(host.client, receipt.run_ref, after=2)
        from_str = await _collect_watch(host.client, receipt.run_ref, after="seq:2")
        assert [_seq(u) for u in from_int] == [_seq(u) for u in from_str] == [_seq(u) for u in full if _seq(u) > 2]
        with pytest.raises(ValueError, match="Invalid watch cursor"):
            await _collect_watch(host.client, receipt.run_ref, after="bogus")


# === 4. Live previews never advance the cursor ===


class TestPreviewsNeverAdvanceCursor:
    async def test_in_process_previews_repeat_last_durable_cursor(self, tmp_path, home):
        gate = threading.Event()
        graph = _sync_graph("dbl", gate=gate)
        host = serve(graph, home=home)
        receipt = await host.submit(graph_of(host, "dbl"), {"x": 1}, workflow_id="wf-1")

        updates = []

        async def consume():
            async for update in host.client.watch(receipt.run_ref, poll_interval=0.02):
                updates.append(update)

        watcher = asyncio.create_task(consume())
        await asyncio.sleep(0.1)  # let the watcher subscribe before execution starts
        async with _worker(host):
            await _wait_for(lambda: _first_preview(updates))
            gate.set()
            await asyncio.wait_for(watcher, timeout=20)

        previews = [u for u in updates if not u.durable]
        durables = [u for u in updates if u.durable]
        assert previews, "expected live previews from the in-process worker"
        assert durables

        # Every preview carries the last durable cursor at that moment — never a later one.
        last_durable_cursor = None
        for update in updates:
            if update.durable:
                last_durable_cursor = update.cursor
            else:
                assert update.cursor == last_durable_cursor

        # Storing a preview's cursor loses nothing: replay from it yields
        # every durable fact after that point.
        stored = previews[-1].cursor
        replay = await _collect_watch(host.client, receipt.run_ref, after=stored)
        assert [_seq(u) for u in replay] == [_seq(u) for u in durables if _seq(u) > _seq(previews[-1])]
        assert all(u.durable for u in replay)


async def _first_preview(updates):
    previews = [u for u in updates if not u.durable]
    return previews or None


# === 5. Exclusive worker lock ===


class TestWorkerLock:
    async def test_second_worker_fails_loudly_and_lock_releases(self, tmp_path, home):
        host1 = serve(_sync_graph("dbl"), home=home)
        first = asyncio.create_task(host1.work_forever("w-1"))
        await asyncio.sleep(0.15)  # first worker holds the lock

        detached = RunHome.open(_home_uri(tmp_path))
        try:
            host2 = serve(_sync_graph("dbl"), home=detached)
            with pytest.raises(WorkerLockError):
                await host2.work_forever("w-2")

            host1.shutdown()
            await asyncio.wait_for(first, timeout=20)

            # After the first drains, the lock is released: a third worker succeeds.
            async with _worker(host2, "w-3"):
                await asyncio.sleep(0.1)
        finally:
            await detached.close()


# === 6. Bounded drain ===


class TestBoundedDrain:
    async def test_shutdown_mid_run_finishes_within_drain_timeout(self, tmp_path, home):
        graph = _sync_graph("dbl", delay=0.3)
        host = serve(graph, home=home)
        await host.submit(graph_of(host, "dbl"), {"x": 1}, workflow_id="wf-1")

        worker = asyncio.create_task(host.work_forever("w-1", drain_timeout=5.0))
        # Wait until the run has actually started executing, then shut down mid-run.
        await _wait_for(lambda: _run_started(home, "wf-1"))
        host.shutdown()
        await asyncio.wait_for(worker, timeout=20)

        run = home.get_run("wf-1")
        assert run.status == WorkflowStatus.COMPLETED
        assert home._get_submission_sync("wf-1")["state"] == "finished"
        assert host.worker_errors == []

        # The lock was released on drain completion.
        async with _worker(host, "w-2"):
            await asyncio.sleep(0.05)


# === 7. Restart scan re-adopts unfinished work ===


class TestRestartScan:
    async def test_killed_worker_claimed_submission_completes_without_resubmission(self, tmp_path, home):
        gate = threading.Event()
        graph = _sync_graph("dbl", gate=gate)
        host1 = serve(graph, home=home)
        receipt = await host1.submit(graph_of(host1, "dbl"), {"x": 1}, workflow_id="wf-1")

        crashed = asyncio.create_task(host1.work_forever("w-1", drain_timeout=0.05))
        await _wait_for(lambda: _run_started(home, "wf-1"))
        crashed.cancel()
        with pytest.raises(asyncio.CancelledError):
            await crashed

        # Crash state: claimed, run active but unfinished, worker dead.
        submission = home._get_submission_sync("wf-1")
        assert submission["state"] == "claimed"
        assert home.get_run("wf-1").status == WorkflowStatus.ACTIVE

        # A new worker re-adopts the claimed-but-unfinished submission.
        detached = RunHome.open(_home_uri(tmp_path))
        try:
            host2 = serve(graph, home=detached)
            restarted = asyncio.create_task(host2.work_forever("w-2"))
            try:
                # Wait for the re-claim, then unblock both the orphaned thread
                # and the re-adopted execution (at-least-once is documented).
                await _wait_for(lambda: _submission_state(detached, "wf-1", "claimed"))
                gate.set()
                await _wait_for(lambda: _terminal_view(host2.client, receipt.run_ref))
            finally:
                host2.shutdown()
                await asyncio.wait_for(restarted, timeout=20)

            run = detached.get_run("wf-1")
            assert run.status == WorkflowStatus.COMPLETED
            assert detached._get_submission_sync("wf-1")["state"] == "finished"
            assert detached.values("wf-1")["out"] == 2
            await asyncio.sleep(0.3)  # let the orphaned thread settle before closing its home
        finally:
            await detached.close()


# === 9. Sync + async Definition parity ===


class TestSyncAsyncParity:
    async def test_sync_and_async_definitions_complete_through_one_worker(self, tmp_path, home):
        host = serve(_sync_graph("sync-def", delay=0.05), _async_graph("async-def", delay=0.05), home=home, deployment_version="v1")

        receipt_sync = await host.submit(graph_of(host, "sync-def"), {"x": 1}, workflow_id="wf-sync")
        receipt_async = await host.submit(graph_of(host, "async-def"), {"x": 10}, workflow_id="wf-async")

        async with _worker(host):
            view_sync = await _wait_for(lambda: _terminal_view(host.client, receipt_sync.run_ref))
            view_async = await _wait_for(lambda: _terminal_view(host.client, receipt_async.run_ref))

        assert view_sync.status == WorkflowStatus.COMPLETED
        assert view_async.status == WorkflowStatus.COMPLETED
        assert home.values("wf-sync")["out"] == 2
        assert home.values("wf-async")["out"] == 11
        assert host.worker_errors == []


# === 10. serve() construction validation ===


class _NoCheckpointRunner(BaseRunner):
    """Runner without a checkpointer/event seam (stands in for DaftRunner)."""

    @property
    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(
            supports_cycles=False,
            supports_async_nodes=False,
            supports_streaming=False,
            returns_coroutine=False,
            supports_checkpointing=False,
        )

    def run(self, graph, values=None, **kwargs):  # pragma: no cover - never called
        raise NotImplementedError

    def map(self, graph, values=None, **kwargs):  # pragma: no cover - never called
        raise NotImplementedError


class TestServeValidation:
    async def test_unnamed_graph_rejected(self):
        home = RunHome.open(":memory:")
        try:
            unnamed = Graph([_one_node()])  # no name=
            with pytest.raises(ValueError, match="name"):
                serve(unnamed.with_runner(SyncRunner()), home=home)
        finally:
            await home.close()

    async def test_unbound_runner_rejected(self):
        home = RunHome.open(":memory:")
        try:
            named = Graph([_one_node()], name="dbl")
            with pytest.raises(ValueError, match="with_runner"):
                serve(named, home=home)
        finally:
            await home.close()

    async def test_runner_without_checkpointer_support_rejected(self):
        home = RunHome.open(":memory:")
        try:
            bound = Graph([_one_node()], name="dbl").with_runner(_NoCheckpointRunner())
            with pytest.raises(ValueError, match="cannot serve durable runs"):
                serve(bound, home=home)
        finally:
            await home.close()

    async def test_non_graph_and_non_home_rejected(self):
        home = RunHome.open(":memory:")
        try:
            with pytest.raises(TypeError):
                serve("not-a-graph", home=home)
            with pytest.raises(TypeError, match="RunHome"):
                serve(Graph([_one_node()], name="dbl").with_runner(SyncRunner()), home=object())
        finally:
            await home.close()

    async def test_runhome_rejects_exit_durability(self):
        from hypergraph.checkpointers import CheckpointPolicy

        with pytest.raises(ValueError, match="exit"):
            RunHome.open(":memory:", policy=CheckpointPolicy(durability="exit", retention="latest"))

    async def test_runhome_forces_sync_durability(self):
        home = RunHome.open(":memory:")
        try:
            assert home.policy.durability == "sync"
        finally:
            await home.close()


# === 11. Nested GraphNode children persist to the Run Home (F1) ===


def _nested_sync_graph(name: str) -> Graph:
    @node(output_name="inner_out")
    def inner_compute(x: int) -> int:
        return x * 2

    inner = Graph([inner_compute], name=f"{name}-inner")
    return Graph([inner.as_node()], name=name).with_runner(SyncRunner())


def _nested_async_graph(name: str) -> Graph:
    @node(output_name="inner_out")
    async def inner_compute(x: int) -> int:
        return x * 2

    inner = Graph([inner_compute], name=f"{name}-inner")
    return Graph([inner.as_node()], name=name).with_runner(AsyncRunner())


class TestNestedGraphNodePersistence:
    """A host worker's runner clone must execute GraphNode children against
    ITSELF: child workflow step state lands in the Run Home, and the child
    registers in the clone's live registry (never the original runner's)."""

    async def test_sync_nested_child_steps_land_in_run_home(self, home):
        graph = _nested_sync_graph("nestdef")
        host = serve(graph, home=home)
        receipt = await host.submit(graph_of(host, "nestdef"), {"x": 3}, workflow_id="wf-nest")

        async with _worker(host):
            view = await _wait_for(lambda: _terminal_view(host.client, receipt.run_ref))

        assert view.status == WorkflowStatus.COMPLETED
        # The GraphNode child ran as its own persisted workflow in the Home.
        child_id = "wf-nest/nestdef-inner"
        child_run = home.get_run(child_id)
        assert child_run is not None
        assert child_run.status == WorkflowStatus.COMPLETED
        assert home.values(child_id)["inner_out"] == 6
        child_steps = await home.get_steps(child_id)
        assert [step.node_name for step in child_steps] == ["inner_compute"]
        # The ORIGINAL bound runner never saw the child: no live registry
        # entries leaked out of the clone.
        assert graph.bound_runner.has_active_run(child_id) is False
        assert graph.bound_runner.has_active_run("wf-nest") is False

    async def test_async_nested_child_steps_land_in_run_home(self, home):
        graph = _nested_async_graph("anestdef")
        host = serve(graph, home=home)
        receipt = await host.submit(graph_of(host, "anestdef"), {"x": 4}, workflow_id="wf-anest")

        async with _worker(host):
            view = await _wait_for(lambda: _terminal_view(host.client, receipt.run_ref))

        assert view.status == WorkflowStatus.COMPLETED
        child_id = "wf-anest/anestdef-inner"
        child_run = home.get_run(child_id)
        assert child_run is not None
        assert child_run.status == WorkflowStatus.COMPLETED
        assert home.values(child_id)["inner_out"] == 8
        assert host.worker_errors == []


# === 12. watch() on an unknown ref terminates immediately (F7) ===


class TestWatchUnknownRef:
    async def test_watch_unknown_ref_returns_no_updates(self, home):
        serve(_sync_graph("dbl"), home=home)
        client = RunHomeClient(home)
        unknown = RunRef(home=home.uri, run_id="wf-nope")
        assert await client.get(unknown) is None
        updates = await asyncio.wait_for(_collect_watch(client, unknown), timeout=5)
        assert updates == []

    async def test_watch_negative_cursor_clamps_to_stream_start(self, home):
        host = serve(_sync_graph("dbl"), home=home)
        receipt = await host.submit(graph_of(host, "dbl"), {"x": 1}, workflow_id="wf-neg")
        async with _worker(host):
            await _wait_for(lambda: _terminal_view(host.client, receipt.run_ref))
        clamped = await _collect_watch(host.client, receipt.run_ref, after=-5)
        full = await _collect_watch(host.client, receipt.run_ref)
        assert [u.cursor for u in clamped] == [u.cursor for u in full]


# === 13. start_at normalization (F10) ===


class TestStartAtNormalization:
    async def test_offset_and_naive_spellings_normalize_to_utc(self, home):
        host = serve(_sync_graph("dbl"), home=home)
        receipt = await host.submit(graph_of(host, "dbl"), {"x": 1}, workflow_id="wf-tz", start_at="2030-01-01T02:00:00+02:00")
        submission = home._get_submission_sync("wf-tz")
        assert submission["start_at"] == "2030-01-01T00:00:00+00:00"
        # The same instant spelled differently dedupes (same fingerprint).
        dup = await host.submit(graph_of(host, "dbl"), {"x": 1}, workflow_id="wf-tz", start_at="2030-01-01T00:00:00")
        assert dup.duplicate is True
        assert dup.run_ref == receipt.run_ref
        # A naive datetime normalizes as UTC too.
        from datetime import datetime

        other = await host.submit(graph_of(host, "dbl"), {"x": 1}, workflow_id="wf-tz2", start_at=datetime(2030, 6, 1, 12, 0, 0))
        assert home._get_submission_sync("wf-tz2")["start_at"] == "2030-06-01T12:00:00+00:00"
        assert other.duplicate is False

    async def test_unparseable_start_at_rejected(self, home):
        host = serve(_sync_graph("dbl"), home=home)
        with pytest.raises(ValueError, match="start_at"):
            await host.submit(graph_of(host, "dbl"), {"x": 1}, workflow_id="wf-badtz", start_at="next tuesday")


def _one_node():
    @node(output_name="out")
    def compute(x: int) -> int:
        return x + 1

    return compute
