"""Durable Host V1 — ticket 05: submit and watch an immutable durable Batch.

Covers: atomic acceptance (manifest + child identities + start intent +
bseq 1 in one transaction, with a mid-transaction rollback proof), item and
tolerance validation, dedup/conflict/terminal rules mirroring Run submit
(including the shared Run/Batch workflow_id namespace), keyed outcomes and
counts, explicit unstarted items via Batch stop, the gap-free per-Batch
bseq cursor with mid-stream resume, Batch stop semantics, the RunQuery
batch filter, a real SIGKILL restart mid-batch, and sync mirrors.
"""

import asyncio
import contextlib
import json
import pathlib
import subprocess
import sys
import time
from collections import Counter

import pytest
import pytest_asyncio

from hypergraph import (
    AlreadyTerminalError,
    AsyncRunner,
    BatchCommandReceipt,
    BatchRef,
    BatchSubmitReceipt,
    BatchTolerance,
    BatchUpdate,
    BatchView,
    Graph,
    HostError,
    ItemKeyError,
    RunHome,
    RunQuery,
    RunRef,
    SyncRunner,
    UnservedGraphError,
    WorkflowIdConflictError,
    interrupt,
    node,
    serve,
)
from hypergraph.checkpointers.types import WorkflowStatus
from tests._interrupt_questions import StringQuestion
from tests.test_host._batch_api import graph_of, submit_keyed, submit_keyed_sync

aiosqlite = pytest.importorskip("aiosqlite")

#: Repo root, injected into the `python -c` child so it can import `tests.*`.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# === Helpers (mirrors ticket-02/03/04 conventions) ===


def _sync_graph(name: str) -> Graph:
    @node(output_name="out")
    def compute(x: int, item: str = "") -> int:
        return x * 10

    return Graph([compute], name=name).with_runner(SyncRunner())


def _flaky_sync_graph(name: str) -> Graph:
    """One-node graph that fails for x == 1."""

    @node(output_name="out")
    def compute(x: int, item: str = "") -> int:
        if x == 1:
            raise ValueError("boom")
        return x * 10

    return Graph([compute], name=name).with_runner(SyncRunner())


def _gated_async_graph(name: str, started: asyncio.Event, release: asyncio.Event, calls: dict) -> Graph:
    """Two-node async graph whose second node blocks until released."""

    @node(output_name="seed")
    async def seed(x: int, item: str = "") -> int:
        calls["seed"] += 1
        return x

    @node(output_name="out")
    async def gated(seed: int) -> int:
        started.set()
        await release.wait()
        calls["gated"] += 1
        return seed * 10

    return Graph([seed, gated], name=name).with_runner(AsyncRunner())


def _mixed_async_graph(name: str, started: asyncio.Event, release: asyncio.Event) -> Graph:
    """One-node graph: item x == 2 blocks until released; others complete."""

    @node(output_name="out")
    async def compute(x: int, item: str = "") -> int:
        if x == 2:
            started.set()
            await release.wait()
        return x * 10

    return Graph([compute], name=name).with_runner(AsyncRunner())


def _pausing_async_graph(name: str) -> Graph:
    """One-node-plus-interrupt graph: every child parks on a human answer."""

    @node(output_name="seed")
    async def seed(x: int, item: str = "") -> int:
        return x

    @interrupt(answer_name="ans")
    def ask(seed: int) -> StringQuestion:
        return StringQuestion(prompt=f"continue with {seed}?")

    return Graph([seed, ask], name=name).with_runner(AsyncRunner())


async def _claim(host, home, limit: int | None = None):
    """Run one real admission scan through the single claim choke point."""
    kwargs = {} if limit is None else {"limit": limit}
    return await home._claim_eligible(served=host._served_identities, **kwargs)


def _stop_command_rows(home, workflow_id):
    db = home._sync_db()
    return db.execute(
        "SELECT payload, applied_at FROM host_commands WHERE run_id = ? AND verb = 'stop'",
        (workflow_id,),
    ).fetchall()


async def _stop_applied(home, workflow_id):
    rows = _stop_command_rows(home, workflow_id)
    return bool(rows) and all(row[1] is not None for row in rows)


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


async def _flag(event: asyncio.Event):
    return event.is_set()


async def _settled_view(client, batch_ref):
    view = await client.get(batch_ref)
    return view if view is not None and view.settled else None


def _batch_rows(home, batch_id):
    db = home._sync_db()
    return {
        "batch": db.execute("SELECT * FROM host_batches WHERE batch_id = ?", (batch_id,)).fetchall(),
        "children": db.execute("SELECT workflow_id, item_key, state FROM host_submissions WHERE batch_id = ?", (batch_id,)).fetchall(),
        "updates": db.execute("SELECT bseq, kind FROM batch_updates WHERE batch_id = ? ORDER BY bseq", (batch_id,)).fetchall(),
    }


@pytest_asyncio.fixture
async def home(tmp_path):
    h = RunHome.open(_home_uri(tmp_path))
    yield h
    await h.close()


# === 1. Atomic acceptance ===


class TestAtomicAcceptance:
    async def test_acceptance_persists_manifest_children_and_start_intent(self, home):
        host = serve(_sync_graph("ingest"), home=home, deployment_version="v1")
        receipt = await submit_keyed(
            host,
            graph_of(host, "ingest"),
            {"p-17": {"x": 17}, "p-18": {"x": 18}, "p-19": {"x": 19}},
            workflow_id="drop-42",
            tolerance=BatchTolerance(max_failed=2, max_failed_percent=25),
            start_at="2030-01-01T00:00:00+00:00",
            source_ref="webhook-7",
        )
        assert isinstance(receipt, BatchSubmitReceipt)
        assert receipt.duplicate is False
        assert receipt.workflow_id == "drop-42"
        assert receipt.batch_ref.home == home.uri
        batch_id = receipt.batch_ref.batch_id

        db = home._sync_db()
        (manifest,) = db.execute(
            "SELECT workflow_id, definition_name, def_version, items_json, tolerance_json, start_at, fingerprint, source_ref "
            "FROM host_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchall()
        assert manifest[0:3] == ("drop-42", "ingest", "v1")
        # The manifest preserves item order and pinned per-item inputs.
        assert json.loads(manifest[3]) == {
            "p-17": {"item": "p-17", "x": 17},
            "p-18": {"item": "p-18", "x": 18},
            "p-19": {"item": "p-19", "x": 19},
        }
        assert json.loads(manifest[4]) == {"max_failed": 2, "max_failed_percent": 25}
        assert manifest[5] == "2030-01-01T00:00:00+00:00"
        assert manifest[6]  # fingerprint pinned
        assert manifest[7] == "webhook-7"

        # One child submission per item key, each carrying Batch membership,
        # pinned inputs, and the delayed start intent.
        children = db.execute(
            "SELECT workflow_id, item_key, state, inputs_json, start_at FROM host_submissions WHERE batch_id = ? ORDER BY rowid",
            (batch_id,),
        ).fetchall()
        assert [(row[0], row[1], row[2]) for row in children] == [
            ("drop-42:p-17", "p-17", "pending"),
            ("drop-42:p-18", "p-18", "pending"),
            ("drop-42:p-19", "p-19", "pending"),
        ]
        assert all(row[4] == "2030-01-01T00:00:00+00:00" for row in children)
        assert json.loads(children[0][3]) == {"item": "p-17", "x": 17}

        # The manifest event is bseq 1; each child has its own seq-1 submitted fact.
        updates = db.execute("SELECT bseq, kind, payload FROM batch_updates WHERE batch_id = ?", (batch_id,)).fetchall()
        assert [(row[0], row[1]) for row in updates] == [(1, "manifest")]
        payload = json.loads(updates[0][2])
        assert payload["workflow_id"] == "drop-42"
        assert payload["item_keys"] == ["p-17", "p-18", "p-19"]
        assert payload["tolerance"] == {"max_failed": 2, "max_failed_percent": 25}
        assert payload["definition_id"]["name"] == "ingest"
        for child_workflow_id, _, _, _, _ in children:
            child_updates = home._read_run_updates_sync(child_workflow_id)
            assert [(u[0], u[1]) for u in child_updates] == [(1, "submitted")]
            assert json.loads(child_updates[0][2])["batch_id"] == batch_id

        # BatchRef is an inert JSON-serializable value object.
        assert BatchRef.from_dict(receipt.batch_ref.to_dict()) == receipt.batch_ref

    async def test_acceptance_rolls_back_atomically_on_failure(self, home, monkeypatch):
        """A failure anywhere inside acceptance leaves the Batch fully absent."""
        host = serve(_sync_graph("ingest"), home=home)

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated crash mid-acceptance")

        monkeypatch.setattr(home, "_append_batch_update_sync", _boom)
        with pytest.raises(RuntimeError, match="simulated crash"):
            submit_keyed_sync(host, graph_of(host, "ingest"), {"a": {"x": 1}, "b": {"x": 2}}, workflow_id="drop-x")

        db = home._sync_db()
        assert db.execute("SELECT COUNT(*) FROM host_batches").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM host_submissions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM batch_updates").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM run_updates").fetchone()[0] == 0
        # The Home is still usable afterwards.
        monkeypatch.undo()
        receipt = submit_keyed_sync(host, graph_of(host, "ingest"), {"a": {"x": 1}}, workflow_id="drop-y")
        assert receipt.duplicate is False

    async def test_item_and_argument_validation(self, home):
        """Refusals on the runner-shaped submission surface (issue #342).

        Item identity is now derived by ``key_by`` from an expanded input
        rather than supplied as a mapping key, so the empty / non-scalar /
        duplicate refusals moved onto the expanded values and raise the
        typed ``ItemKeyError``. The argument-level refusals (workflow_id,
        tolerance, unserved Graph) are unchanged.
        """
        host = serve(_sync_graph("ingest"), home=home)
        graph = graph_of(host, "ingest")

        with pytest.raises(ValueError, match="an empty Batch is not a Batch"):
            await submit_keyed(host, graph, {}, workflow_id="b-empty")
        with pytest.raises(TypeError, match="values must be a Mapping"):
            await host.submit_batch(graph, [("a", 1)], map_over="item", key_by="item", workflow_id="b-list")
        with pytest.raises(ItemKeyError, match="empty"):
            await submit_keyed(host, graph, {"": {"x": 1}}, workflow_id="b-key")
        with pytest.raises(ItemKeyError, match="a float"):
            await host.submit_batch(graph, {"item": [1.5], "x": [1]}, map_over=["item", "x"], key_by="item", workflow_id="b-key2")
        with pytest.raises(TypeError, match="JSON-serializable"):
            await submit_keyed(host, graph, {"a": {"x": object()}}, workflow_id="b-ser")
        with pytest.raises(ValueError, match="workflow_id"):
            await submit_keyed(host, graph, {"a": {"x": 1}}, workflow_id="")
        with pytest.raises(TypeError, match="BatchTolerance"):
            await submit_keyed(host, graph, {"a": {"x": 1}}, workflow_id="b-tol", tolerance={"max_failed": 1})
        with pytest.raises(UnservedGraphError, match="not served by this host"):
            await submit_keyed(host, _sync_graph("nope"), {"a": {"x": 1}}, workflow_id="b-def")
        with pytest.raises(ItemKeyError, match="duplicate item key"):
            await host.submit_batch(graph, {"item": ["a", "a"], "x": [1, 2]}, map_over=["item", "x"], key_by="item", workflow_id="b-dup")
        # Every refusal happened before acceptance: nothing was written.
        assert home._sync_db().execute("SELECT COUNT(*) FROM host_batches").fetchone()[0] == 0

    async def test_batch_tolerance_validation_and_roundtrip(self, home):
        with pytest.raises(ValueError, match="at least one"):
            BatchTolerance()
        with pytest.raises(ValueError, match="max_failed"):
            BatchTolerance(max_failed=-1)
        with pytest.raises(ValueError, match="max_failed"):
            BatchTolerance(max_failed=True)
        with pytest.raises(ValueError, match="max_failed_percent"):
            BatchTolerance(max_failed_percent=101)
        with pytest.raises(ValueError, match="max_failed_percent"):
            BatchTolerance(max_failed_percent=-5)
        tolerance = BatchTolerance(max_failed=2, max_failed_percent=25)
        assert BatchTolerance.from_dict(tolerance.to_dict()) == tolerance
        assert BatchTolerance.from_dict({"max_failed": None, "max_failed_percent": 0}) == BatchTolerance(max_failed_percent=0)


# === 2. Dedup, conflict, terminal ===


class TestDedupConflictTerminal:
    async def test_identical_resubmission_dedupes(self, home):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = {"seed": 0, "gated": 0}
        host = serve(_gated_async_graph("ingest", started, release, calls), home=home, deployment_version="v1")
        receipt = await submit_keyed(
            host,
            graph_of(host, "ingest"),
            {"a": {"x": 1}, "b": {"x": 2}},
            workflow_id="drop-1",
            tolerance=BatchTolerance(max_failed=1),
        )
        # Mapping order never affects the fingerprint.
        dup = await submit_keyed(
            host,
            graph_of(host, "ingest"),
            {"b": {"x": 2}, "a": {"x": 1}},
            workflow_id="drop-1",
            tolerance=BatchTolerance(max_failed=1),
        )
        assert dup.duplicate is True
        assert dup.batch_ref == receipt.batch_ref

        db = home._sync_db()
        assert db.execute("SELECT COUNT(*) FROM host_batches").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM host_submissions").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM batch_updates").fetchone()[0] == 1

        # Sync mirror dedupes identically.
        dup_sync = submit_keyed_sync(
            host,
            graph_of(host, "ingest"),
            {"a": {"x": 1}, "b": {"x": 2}},
            workflow_id="drop-1",
            tolerance=BatchTolerance(max_failed=1),
        )
        assert dup_sync.duplicate is True
        assert dup_sync.batch_ref == receipt.batch_ref

    async def test_fingerprint_mismatch_conflicts(self, home):
        host = serve(_sync_graph("ingest"), home=home, deployment_version="v1")
        receipt = await submit_keyed(
            host,
            graph_of(host, "ingest"),
            {"a": {"x": 1}},
            workflow_id="drop-2",
            tolerance=BatchTolerance(max_failed=1),
        )
        with pytest.raises(WorkflowIdConflictError, match="items differs"):
            await submit_keyed(host, graph_of(host, "ingest"), {"a": {"x": 2}}, workflow_id="drop-2", tolerance=BatchTolerance(max_failed=1))
        with pytest.raises(WorkflowIdConflictError, match="tolerance differs"):
            await submit_keyed(host, graph_of(host, "ingest"), {"a": {"x": 1}}, workflow_id="drop-2", tolerance=BatchTolerance(max_failed=2))
        with pytest.raises(WorkflowIdConflictError, match="tolerance differs"):
            await submit_keyed(host, graph_of(host, "ingest"), {"a": {"x": 1}}, workflow_id="drop-2")
        with pytest.raises(WorkflowIdConflictError, match="start_at differs"):
            await submit_keyed(
                host,
                graph_of(host, "ingest"),
                {"a": {"x": 1}},
                workflow_id="drop-2",
                tolerance=BatchTolerance(max_failed=1),
                start_at="2030-01-01T00:00:00+00:00",
            )
        # Nothing new was written by any conflict.
        db = home._sync_db()
        assert db.execute("SELECT COUNT(*) FROM host_batches").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM host_submissions").fetchone()[0] == 1
        view = await host.client.get(receipt.batch_ref)
        assert view.counts["queued"] == 1

    async def test_settled_batch_resubmission_is_already_terminal(self, home):
        host = serve(_sync_graph("ingest"), home=home, deployment_version="v1")
        receipt = await submit_keyed(host, graph_of(host, "ingest"), {"a": {"x": 1}, "b": {"x": 2}}, workflow_id="drop-3")
        async with _worker(host):
            view = await _wait_for(lambda: _settled_view(host.client, receipt.batch_ref))
        assert view.counts["completed"] == 2
        with pytest.raises(AlreadyTerminalError):
            await submit_keyed(host, graph_of(host, "ingest"), {"a": {"x": 1}, "b": {"x": 2}}, workflow_id="drop-3")
        with pytest.raises(AlreadyTerminalError):
            submit_keyed_sync(host, graph_of(host, "ingest"), {"a": {"x": 1}, "b": {"x": 2}}, workflow_id="drop-3")

    async def test_run_and_batch_share_workflow_id_namespace(self, home):
        """Prototype Scenario 2: a run submit on a Batch's id is a conflict."""
        host = serve(_sync_graph("ingest"), home=home, deployment_version="v1")
        batch_receipt = await submit_keyed(host, graph_of(host, "ingest"), {"a": {"x": 1}}, workflow_id="drop-4")
        # Run submit reusing the live Batch's id: conflict, not silent reuse.
        with pytest.raises(WorkflowIdConflictError, match="Batch owns"):
            await host.submit(graph_of(host, "ingest"), {"x": 1}, workflow_id="drop-4")
        # Batch submit reusing a live run's id: same rule, other direction.
        run_receipt = await host.submit(graph_of(host, "ingest"), {"x": 9}, workflow_id="run-9")
        with pytest.raises(WorkflowIdConflictError, match="Run owns"):
            await submit_keyed(host, graph_of(host, "ingest"), {"a": {"x": 1}}, workflow_id="run-9")
        # Child workflow ids are claimed too: a run submit on a child id
        # with different inputs conflicts through the ordinary rules.
        with pytest.raises(WorkflowIdConflictError):
            await host.submit(graph_of(host, "ingest"), {"x": 999}, workflow_id="drop-4:a")

        async with _worker(host):
            await _wait_for(lambda: _settled_view(host.client, batch_receipt.batch_ref))
            await _wait_for(lambda: _terminal_run(host.client, run_receipt.run_ref))
        # Settled on both sides: completed history never changes identity.
        with pytest.raises(AlreadyTerminalError):
            await host.submit(graph_of(host, "ingest"), {"x": 1}, workflow_id="drop-4")
        with pytest.raises(AlreadyTerminalError):
            await submit_keyed(host, graph_of(host, "ingest"), {"a": {"x": 9}}, workflow_id="run-9")


async def _terminal_run(client, ref):
    view = await client.get(ref)
    if view is not None and view.status in {
        WorkflowStatus.COMPLETED,
        WorkflowStatus.FAILED,
        WorkflowStatus.PARTIAL,
        WorkflowStatus.STOPPED,
    }:
        return view
    return None


# === 3. Keyed outcomes ===


class TestKeyedOutcomes:
    async def test_mixed_outcomes_stay_keyed_by_item(self, home):
        host = serve(_flaky_sync_graph("ingest"), home=home, deployment_version="v1")
        receipt = await submit_keyed(
            host,
            graph_of(host, "ingest"),
            {"ok-0": {"x": 0}, "bad-1": {"x": 1}, "ok-2": {"x": 2}},
            workflow_id="drop-5",
        )
        async with _worker(host):
            view = await _wait_for(lambda: _settled_view(host.client, receipt.batch_ref))

        assert view.counts == {
            "completed": 2,
            "failed": 1,
            "partial": 0,
            "stopped": 0,
            "active": 0,
            "paused": 0,
            "queued": 0,
            "recovery_exhausted": 0,
            "unstarted": 0,
            "abandoned": 0,
        }
        # Keyed by logical item key in manifest order — completion order
        # never changes result identity.
        assert list(view.outcomes) == ["ok-0", "bad-1", "ok-2"]
        assert view.outcomes == {"ok-0": "completed", "bad-1": "failed", "ok-2": "completed"}
        assert view.unstarted_items == ()
        assert view.settled is True
        assert view.workflow_id == "drop-5"
        assert view.definition_id.name == "ingest"

        # Every child settled fact carries its item key; the bseq stream is
        # gap-free regardless of completion order.
        updates = home._read_batch_updates_sync(receipt.batch_ref.batch_id)
        assert [u[0] for u in updates] == [1, 2, 3, 4]
        assert [u[1] for u in updates] == ["manifest", "child_settled", "child_settled", "child_settled"]
        settled_payloads = sorted(json.loads(u[2])["item_key"] for u in updates[1:])
        assert settled_payloads == ["bad-1", "ok-0", "ok-2"]

        # Children are ordinary Runs: run-level get works and records the
        # same terminal truth.
        child_view = await host.client.get(RunRef(home=home.uri, run_id="drop-5:bad-1"))
        assert child_view.status is WorkflowStatus.FAILED

        # Sync mirror builds the same view.
        sync_view = host.client.get_sync(receipt.batch_ref)
        assert sync_view == view

    async def test_child_settled_is_not_duplicated_on_repeated_terminal_write(self, home):
        host = serve(_sync_graph("ingest"), home=home)
        receipt = await submit_keyed(host, graph_of(host, "ingest"), {"a": {"x": 1}}, workflow_id="drop-6")
        async with _worker(host):
            await _wait_for(lambda: _settled_view(host.client, receipt.batch_ref))
        # A repeated terminal write (idempotent status path) must not add a
        # second child_settled fact for the same item.
        home.update_run_status_sync("drop-6:a", WorkflowStatus.COMPLETED)
        updates = home._read_batch_updates_sync(receipt.batch_ref.batch_id)
        assert [(u[0], u[1]) for u in updates] == [(1, "manifest"), (2, "child_settled")]


# === 4. Unstarted items via Batch stop ===


class TestUnstartedItems:
    async def test_stop_before_any_child_starts_marks_all_unstarted(self, home):
        calls = {"seed": 0, "gated": 0}
        started = asyncio.Event()
        release = asyncio.Event()
        host = serve(_gated_async_graph("ingest", started, release, calls), home=home, deployment_version="v1")
        receipt = await submit_keyed(host, graph_of(host, "ingest"), {"a": {"x": 1}, "b": {"x": 2}, "c": {"x": 3}}, workflow_id="drop-7")
        stop_receipt = await host.client.stop(receipt.batch_ref, info={"reason": "drop recalled"})
        assert isinstance(stop_receipt, BatchCommandReceipt)
        assert stop_receipt.duplicate is False
        assert stop_receipt.verb == "stop"

        async with _worker(host):
            view = await _wait_for(lambda: _settled_view(host.client, receipt.batch_ref))
            await asyncio.sleep(0.2)  # a few more claim cycles: nothing starts

        # No child ever executed; no runs rows were invented.
        assert calls == {"seed": 0, "gated": 0}
        for key in ("a", "b", "c"):
            assert home.get_run(f"drop-7:{key}") is None
        assert view.counts["unstarted"] == 3
        assert view.unstarted_items == ("a", "b", "c")
        assert view.outcomes == {"a": None, "b": None, "c": None}
        assert view.settled is True

        # The durable Batch sequence records manifest, the stop, and then
        # one child_unstarted fact per item that ended without executing —
        # the stop fact names no items, so this is how a detached watch()
        # accounts all three from the stream alone (PRD 0019 A9).
        updates = home._read_batch_updates_sync(receipt.batch_ref.batch_id)
        assert [u[1] for u in updates] == ["manifest", "stopped", "child_unstarted", "child_unstarted", "child_unstarted"]
        assert [u[0] for u in updates] == [1, 2, 3, 4, 5]
        assert json.loads(updates[1][2])["info"] == {"reason": "drop recalled"}
        unstarted = [json.loads(u[2]) for u in updates if u[1] == "child_unstarted"]
        assert sorted(fact["item_key"] for fact in unstarted) == list(view.unstarted_items)
        assert all(fact["workflow_id"] == f"drop-7:{fact['item_key']}" for fact in unstarted)

    async def test_stop_mid_batch_settles_running_children_and_skips_settled(self, home):
        started = asyncio.Event()
        release = asyncio.Event()
        host = serve(_mixed_async_graph("ingest", started, release), home=home, deployment_version="v1")
        receipt = await submit_keyed(host, graph_of(host, "ingest"), {"f-0": {"x": 0}, "f-1": {"x": 1}, "slow": {"x": 2}}, workflow_id="drop-8")

        async with _worker(host):
            # Two fast children complete while the slow one blocks mid-run.
            await _wait_for(lambda: _flag(started))

            async def _two_completed():
                view = await host.client.get(receipt.batch_ref)
                return view if view.counts["completed"] == 2 else None

            await _wait_for(_two_completed)
            stop_receipt = await host.client.stop(receipt.batch_ref, info="enough")
            assert stop_receipt.duplicate is False
            # A second stop dedupes: the first stop owns its info.
            second = await host.client.stop(receipt.batch_ref, info="again")
            assert second.duplicate is True
            # Release only after the worker observed the stop and signalled
            # the runner — otherwise the run could finish normally first.
            await _wait_for(lambda: _stop_applied(home, "drop-8:slow"))
            release.set()
            view = await _wait_for(lambda: _settled_view(host.client, receipt.batch_ref))

        # Completed children were untouched; the running child settled STOPPED.
        assert view.counts["completed"] == 2
        assert view.counts["stopped"] == 1
        assert view.outcomes == {"f-0": "completed", "f-1": "completed", "slow": "stopped"}
        assert view.unstarted_items == ()
        # One durable stop row; the settled children carry no stop commands.
        db = home._sync_db()
        commands = db.execute("SELECT run_id FROM host_commands WHERE verb = 'stop'").fetchall()
        assert [row[0] for row in commands] == ["drop-8:slow"]

    async def test_stop_unknown_and_settled_batch_errors(self, home):
        host = serve(_sync_graph("ingest"), home=home)
        unknown = BatchRef(home=home.uri, batch_id="b-nope")
        with pytest.raises(HostError, match="no such batch"):
            await host.client.stop(unknown)
        with pytest.raises(HostError, match="no such batch"):
            host.client.stop_sync(unknown)

        receipt = await submit_keyed(host, graph_of(host, "ingest"), {"a": {"x": 1}}, workflow_id="drop-9")
        async with _worker(host):
            await _wait_for(lambda: _settled_view(host.client, receipt.batch_ref))
        with pytest.raises(AlreadyTerminalError):
            await host.client.stop(receipt.batch_ref)
        with pytest.raises(AlreadyTerminalError):
            host.client.stop_sync(receipt.batch_ref)

        # Ref type checking on the shared verbs.
        with pytest.raises(TypeError, match="RunRef or BatchRef"):
            await host.client.get("drop-9")
        with pytest.raises(TypeError, match="RunRef or BatchRef"):
            await host.client.stop(42)


class TestPausedChildIsNotSettled:
    """A child parked on a human answer is in flight, not settled.

    The bucket ladder counts a paused child ``paused`` — its own bucket,
    disjoint from ``active`` (issue #342): it holds no active-Run slot, so
    reporting it as running told an operator that human response time was
    throughput. Settlement must agree: if the worker released the submission
    as 'finished', ``is_child_settled`` would call the same child settled
    and ``watch(batch_ref)`` would end while a human decision is still
    outstanding.
    """

    async def _both_children_paused(self, home):
        host = serve(_pausing_async_graph("ask"), home=home, deployment_version="v1")
        receipt = await submit_keyed(host, graph_of(host, "ask"), {"a": {"x": 1}, "b": {"x": 2}}, workflow_id="drop-pause")
        for row in await _claim(host, home):
            await host._execute_submission(row)
        for key in ("a", "b"):
            assert home.get_run(f"drop-pause:{key}").status is WorkflowStatus.PAUSED
        return host, receipt

    async def test_a_paused_child_is_paused_and_the_batch_is_not_settled(self, home):
        host, receipt = await self._both_children_paused(home)

        # The worker released the slot but did NOT finish the submission:
        # parked awaiting an answer is its own durable state.
        assert [home._get_submission_sync(f"drop-pause:{key}")["state"] for key in ("a", "b")] == ["paused", "paused"]
        assert home._get_submission_sync("drop-pause:a")["finished_at"] is None

        view = await host.client.get(receipt.batch_ref)
        # Parked on a human is its own bucket, never "active".
        assert view.counts["paused"] == 2
        assert view.counts["active"] == 0
        assert view.counts["completed"] == 0
        assert view.outcomes == {"a": None, "b": None}
        # The bucket ladder and THE settled-child rule agree.
        assert view.settled is False
        assert host.client.get_sync(receipt.batch_ref) == view

    async def test_watch_does_not_end_while_a_child_awaits_an_answer(self, home):
        host, receipt = await self._both_children_paused(home)

        gen = host.client.watch(receipt.batch_ref)
        try:
            first = await asyncio.wait_for(gen.__anext__(), timeout=10)
            assert (first.kind, first.durable, first.cursor) == ("manifest", True, "bseq:1")
            # Each pause commits its own durable child_paused fact, so a
            # detached consumer learns "these items are waiting for you"
            # from the stream alone (issue #342).
            paused = [await asyncio.wait_for(gen.__anext__(), timeout=10) for _ in range(2)]
            assert [update.kind for update in paused] == ["child_paused", "child_paused"]
            assert {update.payload["item_key"] for update in paused} == {"a", "b"}
            assert all(update.payload["run_ref"]["run_id"].startswith("drop-pause:") for update in paused)
            # No child has SETTLED, so the stream must block, not end.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(gen.__anext__(), timeout=0.4)
        finally:
            await gen.aclose()

    async def test_a_parked_run_can_still_be_stopped(self, home):
        """A paused run is not terminal, so a detached stop is accepted."""
        host, receipt = await self._both_children_paused(home)

        run_ref = RunRef(home=home.uri, run_id="drop-pause:a")
        command = await host.client.stop(run_ref, info={"reason": "recalled"})
        assert command.duplicate is False
        # The Batch is not settled either, so a Batch-wide stop is accepted.
        batch_stop = await host.client.stop(receipt.batch_ref, info={"reason": "recalled"})
        assert batch_stop.duplicate is False
        # The durable intent is recorded for both children; it lands the next
        # time each run executes, exactly like a stop aimed at a crashed run.
        db = home._sync_db()
        commands = db.execute("SELECT run_id, applied_at FROM host_commands WHERE verb = 'stop' ORDER BY id").fetchall()
        assert [row[0] for row in commands] == ["drop-pause:a", "drop-pause:b"]
        assert all(row[1] is None for row in commands)


# === 5. Gap-free watch ===


class TestGapFreeWatch:
    async def test_watch_replays_gap_free_with_cursor_resume(self, home):
        release = asyncio.Event()
        entered = {"n": 0}

        @node(output_name="out")
        async def compute(x: int, item: str = "") -> int:
            entered["n"] += 1
            await release.wait()
            return x * 10

        graph = Graph([compute], name="ingest").with_runner(AsyncRunner())
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(host, graph_of(host, "ingest"), {"a": {"x": 1}, "b": {"x": 2}, "c": {"x": 3}}, workflow_id="drop-10")
        client = host.client
        manifest_seen = asyncio.Event()
        observed: list = []

        async def _phase_one():
            gen = client.watch(receipt.batch_ref)
            try:
                async for update in gen:
                    observed.append(update)
                    if update.durable and update.kind == "manifest":
                        # The watcher subscribed to every child's preview
                        # queue before its first durable replay.
                        manifest_seen.set()
                    if len([u for u in observed if u.durable]) >= 2:
                        return list(observed)
            finally:
                await gen.aclose()

        async def _three_entered():
            return entered["n"] == 3

        async def _previews_observed():
            return any(not u.durable for u in observed)

        phase_task = asyncio.create_task(_phase_one())
        await asyncio.wait_for(manifest_seen.wait(), timeout=5)
        async with _worker(host):
            await _wait_for(_three_entered)  # all children blocked mid-run
            # The watcher drains in-flight child previews while they block.
            await _wait_for(_previews_observed)
            release.set()
            phase_one = await asyncio.wait_for(phase_task, timeout=10)
            cursor = [u for u in phase_one if u.durable][-1].cursor
            assert cursor.startswith("bseq:")
            # Reconnect mid-stream from the stored cursor.
            phase_two = await asyncio.wait_for(
                self._collect(client, receipt.batch_ref, after=cursor),
                timeout=10,
            )

        observed = phase_one + phase_two
        durable = [u for u in observed if u.durable]
        # No gaps, no repeats across the reconnect: exactly bseq 1..4.
        assert [u.cursor for u in durable] == ["bseq:1", "bseq:2", "bseq:3", "bseq:4"]
        assert [u.kind for u in durable] == ["manifest", "child_settled", "child_settled", "child_settled"]
        assert all(isinstance(u, BatchUpdate) for u in observed)
        # Live previews (in-process worker) never advance the durable cursor.
        previews = [u for u in observed if not u.durable]
        assert previews, "expected in-process child previews"
        durable_cursors = {u.cursor for u in durable}
        assert all(u.cursor == "bseq:0" or u.cursor in durable_cursors for u in previews)
        assert all("item_key" in u.payload for u in previews)

    async def _collect(self, client, batch_ref, after=None):
        return [u async for u in client.watch(batch_ref, after=after)]

    async def test_watch_unknown_batch_terminates_immediately(self, home):
        host = serve(_sync_graph("ingest"), home=home)
        updates = await asyncio.wait_for(
            self._collect(host.client, BatchRef(home=home.uri, batch_id="b-nope")),
            timeout=5,
        )
        assert updates == []
        assert await host.client.get(BatchRef(home=home.uri, batch_id="b-nope")) is None

    async def test_watch_does_not_end_at_the_stop_fact(self, home):
        """``stopped`` is a durable control fact, not end-of-stream.

        The stop names no items: each child's ``child_unstarted`` fact
        commits later, when the pre-run gate finishes it. Ending the stream
        at ``stopped`` dropped those facts on the floor (PRD 0019 A9).
        """
        started = asyncio.Event()
        release = asyncio.Event()
        calls = {"seed": 0, "gated": 0}
        host = serve(_gated_async_graph("ingest", started, release, calls), home=home)
        receipt = await submit_keyed(host, graph_of(host, "ingest"), {"a": {"x": 1}}, workflow_id="drop-11")
        await host.client.stop(receipt.batch_ref)

        # Nothing has closed the item out yet, so the stream must stay open.
        gen = host.client.watch(receipt.batch_ref)
        try:
            assert [(u.cursor, u.kind) async for u in self._take(gen, 2)] == [("bseq:1", "manifest"), ("bseq:2", "stopped")]
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(gen.__anext__(), timeout=0.4)
        finally:
            await gen.aclose()

        # The worker's pre-run gate accounts the item; only then does the
        # stream end, with every manifest item delivered.
        async with _worker(host):
            await _wait_for(lambda: _settled_view(host.client, receipt.batch_ref))
        updates = await asyncio.wait_for(self._collect(host.client, receipt.batch_ref), timeout=5)
        durable = [u for u in updates if u.durable]
        assert [(u.cursor, u.kind) for u in durable] == [
            ("bseq:1", "manifest"),
            ("bseq:2", "stopped"),
            ("bseq:3", "child_unstarted"),
        ]
        assert durable[2].payload["item_key"] == "a"
        assert calls == {"seed": 0, "gated": 0}

    @staticmethod
    async def _take(gen, count: int):
        for _ in range(count):
            yield await asyncio.wait_for(gen.__anext__(), timeout=10)

    async def test_watch_invalid_cursor_raises(self, home):
        host = serve(_sync_graph("ingest"), home=home)
        receipt = await submit_keyed(host, graph_of(host, "ingest"), {"a": {"x": 1}}, workflow_id="drop-12")
        with pytest.raises(ValueError, match="Invalid batch watch cursor"):
            [u async for u in host.client.watch(receipt.batch_ref, after="seq:3")]
        with pytest.raises(ValueError, match="Invalid batch watch cursor"):
            [u async for u in host.client.watch(receipt.batch_ref, after="bseq:abc")]
        # An int cursor works and replays from the start of the stream.
        async with _worker(host):
            updates = await asyncio.wait_for(self._collect(host.client, receipt.batch_ref, after=0), timeout=10)
        assert [u.kind for u in updates if u.durable] == ["manifest", "child_settled"]


# === 6. RunQuery batch filter ===


class TestRunQueryBatchFilter:
    async def test_list_filters_children_by_batch(self, home):
        host = serve(_sync_graph("ingest"), home=home, deployment_version="v1")
        batch_receipt = await submit_keyed(host, graph_of(host, "ingest"), {"a": {"x": 1}, "b": {"x": 2}}, workflow_id="drop-13")
        run_receipt = await host.submit(graph_of(host, "ingest"), {"x": 9}, workflow_id="run-solo")
        batch_id = batch_receipt.batch_ref.batch_id

        by_ref = await host.client.list(RunQuery(batch=batch_receipt.batch_ref))
        assert {view.workflow_id for view in by_ref} == {"drop-13:a", "drop-13:b"}
        by_id = await host.client.list(RunQuery(batch=batch_id))
        assert {view.workflow_id for view in by_id} == {"drop-13:a", "drop-13:b"}
        # The plain run and unrelated work never match a batch filter.
        everything = await host.client.list(RunQuery())
        assert {view.workflow_id for view in everything} == {"drop-13:a", "drop-13:b", "run-solo"}
        assert run_receipt.run_ref.run_id == "run-solo"
        # Sync mirror and validation.
        sync_ids = {view.workflow_id for view in host.client.list_sync(RunQuery(batch=batch_receipt.batch_ref))}
        assert sync_ids == {"drop-13:a", "drop-13:b"}
        with pytest.raises(TypeError, match="BatchRef"):
            await host.client.list(RunQuery(batch=42))


# === 7. Real SIGKILL restart mid-batch ===

_CHILD_SCRIPT = """
import asyncio
import sys
import time

sys.path.insert(0, {repo!r})

from hypergraph import Graph, RunHome, SyncRunner, node, serve
from tests.test_host._batch_api import submit_keyed_sync


@node(output_name="first_out")
def first(x: int, item: str = "") -> int:
    if x >= 3:
        time.sleep(30)  # items 3,4: claimed but never commit a step
    with open({marker!r}, "a") as f:
        f.write("first:%d\\n" % x)
    return x


@node(output_name="second_out")
def second(first_out: int) -> int:
    if first_out == 2:
        time.sleep(30)  # item 2: first committed, second in flight
    with open({marker!r}, "a") as f:
        f.write("second:%d\\n" % first_out)
    return first_out


graph = Graph([first, second], name="bkilldef").with_runner(SyncRunner())
home = RunHome.open({uri!r})
host = serve(graph, home=home, deployment_version="v1")
submit_keyed_sync(host, graph,
    {{"item-%d" % n: {{"x": n}} for n in range(5)}},
    workflow_id="wf-bkill",
)
asyncio.run(host.work_forever("w-child", poll_interval=0.02))
"""


class TestRealKillRestart:
    async def test_sigkill_mid_batch_preserves_completed_and_continues_rest(self, tmp_path, home):
        marker = tmp_path / "marker.txt"
        script = _CHILD_SCRIPT.format(marker=str(marker), uri=_home_uri(tmp_path), repo=str(_REPO_ROOT))
        proc = subprocess.Popen([sys.executable, "-c", script])
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                if marker.exists():
                    lines = marker.read_text().splitlines()
                    # items 0,1 fully complete; item 2 committed its first
                    # step and blocks in the second; items 3,4 block in the
                    # first with nothing committed.
                    if Counter(lines) == Counter({"first:0", "first:1", "first:2", "second:0", "second:1"}):
                        break
                if proc.poll() is not None:
                    raise AssertionError("child worker exited before executing")
                time.sleep(0.05)
            else:
                raise AssertionError("child never reached the mid-batch state")
            time.sleep(0.3)  # the slow nodes are in flight
            proc.kill()  # SIGKILL: real crash, no cleanup
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()

        marker_path = str(marker)

        # Byte-for-byte the child's Definition: a restart worker claims a
        # submission only when its pinned structural_hash matches, so this
        # signature carries the manifest's `item` input too.
        @node(output_name="first_out")
        def first(x: int, item: str = "") -> int:
            with open(marker_path, "a") as f:
                f.write(f"first:{x}\n")
            return x

        @node(output_name="second_out")
        def second(first_out: int) -> int:
            with open(marker_path, "a") as f:
                f.write(f"second:{first_out}\n")
            return first_out

        graph = Graph([first, second], name="bkilldef").with_runner(SyncRunner())
        host = serve(graph, home=home, deployment_version="v1")
        (batch_id,) = home._sync_db().execute("SELECT batch_id FROM host_batches WHERE workflow_id = 'wf-bkill'").fetchone()
        batch_ref = BatchRef(home=home.uri, batch_id=batch_id)
        async with _worker(host, "w-parent"):
            view = await _wait_for(lambda: _settled_view(host.client, batch_ref), timeout=30)

        # Completed children were never re-executed; the in-flight child
        # journal-skipped its committed first step; the never-started
        # children executed exactly once each.
        assert Counter(marker.read_text().splitlines()) == Counter(
            {
                "first:0": 1,
                "first:1": 1,
                "first:2": 1,
                "first:3": 1,
                "first:4": 1,
                "second:0": 1,
                "second:1": 1,
                "second:2": 1,
                "second:3": 1,
                "second:4": 1,
            }
        )
        assert view.counts["completed"] == 5
        assert view.settled is True
        assert view.unstarted_items == ()
        # The re-adopted children committed progress and reset their brake.
        for key in ("item-2", "item-3", "item-4"):
            submission = home._get_submission_sync(f"wf-bkill:{key}")
            assert submission["state"] == "finished"
            assert submission["recovery_attempts"] == 0
        # The durable Batch sequence stayed gap-free across the crash.
        updates = home._read_batch_updates_sync(batch_ref.batch_id)
        assert [u[0] for u in updates] == [1, 2, 3, 4, 5, 6]
        assert [u[1] for u in updates] == ["manifest"] + ["child_settled"] * 5


# === 8. Sync mirrors ===


class TestSyncMirrors:
    async def test_submit_batch_sync_end_to_end(self, home):
        host = serve(_sync_graph("ingest"), home=home, deployment_version="v1")
        receipt = submit_keyed_sync(
            host,
            graph_of(host, "ingest"),
            {"a": {"x": 1}, "b": {"x": 2}},
            workflow_id="drop-20",
            tolerance=BatchTolerance(max_failed=1),
        )
        assert isinstance(receipt, BatchSubmitReceipt)
        assert receipt.duplicate is False
        async with _worker(host):
            view = await _wait_for(lambda: _settled_view(host.client, receipt.batch_ref))
        assert view.counts["completed"] == 2
        sync_view = host.client.get_sync(receipt.batch_ref)
        assert isinstance(sync_view, BatchView)
        assert sync_view.outcomes == {"a": "completed", "b": "completed"}

    async def test_stop_sync_before_start_marks_unstarted(self, home):
        host = serve(_sync_graph("ingest"), home=home)
        receipt = submit_keyed_sync(host, graph_of(host, "ingest"), {"a": {"x": 1}, "b": {"x": 2}}, workflow_id="drop-21")
        stop_receipt = host.client.stop_sync(receipt.batch_ref)
        assert isinstance(stop_receipt, BatchCommandReceipt)
        assert stop_receipt.duplicate is False
        assert host.client.stop_sync(receipt.batch_ref).duplicate is True
        async with _worker(host):
            view = await _wait_for(lambda: _settled_view(host.client, receipt.batch_ref))
        assert view.unstarted_items == ("a", "b")
        assert view.counts["unstarted"] == 2
