"""Durable Host V1 — ticket 06: enforce Batch tolerance and item-scoped rerun.

Covers: the strictly-exceeds trip predicate for count and percentage
thresholds, the manifest-pinned percentage denominator, failure equivalence
(failed and recovery-exhausted only), the trip's atomic same-transaction
Batch fact, closed admission with claimed children still settling and every
remaining item explicitly unstarted, and item-scoped rerun minting a new
immutable Batch with Batch and per-child lineage while the source Batch is
never mutated.

The maintainer-approved decision prototype's Scenario 5 table (8 items,
``max_failed=2``, ``max_failed_percent=25`` → 3 completed / 3 failed / 2
unstarted, 8 of 8 accounted) is encoded verbatim in
``TestScenarioFiveTable``.
"""

import asyncio
import contextlib
import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from hypergraph import (
    AsyncRunner,
    BatchRef,
    BatchSubmitReceipt,
    BatchTolerance,
    Graph,
    RunHome,
    RunRef,
    SubmitReceipt,
    SyncRunner,
    node,
    serve,
)
from hypergraph.checkpointers.types import WorkflowStatus
from hypergraph.host.batch import tolerance_trips
from hypergraph.host.errors import RerunError
from tests.test_host._batch_api import serve_graphs, submit_keyed

aiosqlite = pytest.importorskip("aiosqlite")


# === Helpers (mirrors ticket-05 conventions) ===


def _home_uri(tmp_path, filename: str = "runs.db") -> str:
    return f"file:{tmp_path / filename}"


@pytest_asyncio.fixture
async def home(tmp_path):
    h = RunHome.open(_home_uri(tmp_path))
    yield h
    await h.close()


def _sequenced_graph(name: str, outcomes: list[str], runner: str = "sync") -> tuple[Graph, dict]:
    """One-node graph whose Nth *execution* takes ``outcomes[N]``.

    Outcome by execution order, not by item key, so the scenario holds
    whatever order the store admits items in — the tests drive admission
    one item at a time, so N is deterministic. ``outcomes`` shorter than
    the manifest means every later execution completes.
    """
    state: dict = {"n": 0, "seen": []}

    def _outcome(x: int) -> int:
        index = state["n"]
        state["n"] += 1
        state["seen"].append(x)
        if index < len(outcomes) and outcomes[index] == "fail":
            raise ValueError(f"item x={x} failed (execution {index})")
        return x * 10

    if runner == "sync":

        @node(output_name="out")
        def compute(x: int, item: str = "") -> int:
            return _outcome(x)

        return Graph([compute], name=name).with_runner(SyncRunner()), state

    @node(output_name="out")
    async def compute_async(x: int, item: str = "") -> int:
        return _outcome(x)

    return Graph([compute_async], name=name).with_runner(AsyncRunner()), state


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _claim(host, home, limit: int | None = None):
    """Run one real admission scan through the single claim choke point."""
    kwargs = {} if limit is None else {"limit": limit}
    return await home._claim_eligible(_now_iso(), served=host._served_identities, **kwargs)


async def _drive(host, home, *, max_cycles: int = 64) -> list[str]:
    """Claim one item, execute it to settlement, repeat until admission is dry.

    This is the worker's own claim/execute cycle serialized to one item at
    a time: a tolerance trip that closes admission stops the loop, so the
    items that never ran are exactly the items the Batch never admitted.
    """
    executed: list[str] = []
    for _ in range(max_cycles):
        claimed = await _claim(host, home, limit=1)
        if not claimed:
            return executed
        await host._execute_submission(claimed[0])
        executed.append(claimed[0]["workflow_id"])
    raise AssertionError("drive loop never reached a settled Batch")


async def _claim_all_then_execute(host, home) -> list[str]:
    """Claim every eligible child FIRST, then settle them one at a time.

    Models the real worker's batched admission: a trip mid-way through can
    only close admission for work not yet claimed, so already-claimed
    children still settle.
    """
    claimed = await _claim(host, home)
    for row in claimed:
        await host._execute_submission(row)
    return [row["workflow_id"] for row in claimed]


def _batch_updates(home, batch_id) -> list[tuple[int, str, dict]]:
    return [(bseq, kind, json.loads(payload)) for bseq, kind, payload, _created in home._read_batch_updates_sync(batch_id)]


def _trip_fact(home, batch_id) -> dict:
    """The one ``tolerance_tripped`` payload for this Batch."""
    facts = [payload for _bseq, kind, payload in _batch_updates(home, batch_id) if kind == "tolerance_tripped"]
    assert len(facts) == 1, f"expected exactly one trip fact, got {len(facts)}"
    return facts[0]


def _snapshot_batch(home, batch_id) -> dict:
    """Every persisted byte of one Batch: manifest row, children, updates."""
    db = home._sync_db()
    return {
        "batch": db.execute("SELECT * FROM host_batches WHERE batch_id = ?", (batch_id,)).fetchall(),
        "children": db.execute("SELECT * FROM host_submissions WHERE batch_id = ? ORDER BY workflow_id", (batch_id,)).fetchall(),
        "updates": db.execute("SELECT * FROM batch_updates WHERE batch_id = ? ORDER BY bseq", (batch_id,)).fetchall(),
    }


def _items(count: int) -> dict[str, dict]:
    return {f"p-{n}": {"x": n} for n in range(count)}


async def _wait_until(check, timeout: float = 15.0, interval: float = 0.01):
    """Poll a zero-arg predicate until it is truthy."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not check():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(interval)


# === 1. Strictly exceeds ===


class TestStrictlyExceeds:
    @pytest.mark.parametrize(
        "max_failed,percent,failures,total,expected",
        [
            # Count: the exact threshold is tolerated, one more trips.
            (2, None, 2, 8, False),
            (2, None, 3, 8, True),
            (0, None, 0, 4, False),
            (0, None, 1, 4, True),
            # Percentage: 25% of 8 is exactly 2 — tolerated; 3 trips.
            (None, 25, 2, 8, False),
            (None, 25, 3, 8, True),
            (None, 0, 0, 5, False),
            (None, 0, 1, 5, True),
            (None, 100, 5, 5, False),  # tolerating everything really does
            (None, 33, 2, 6, True),  # 33% of 6 is 1.98: 2 strictly exceeds
            # Both pinned: they are evaluated independently, either trips.
            (5, 25, 3, 8, True),  # count tolerant, percentage exceeded
            (2, 90, 3, 8, True),  # percentage tolerant, count exceeded
            (5, 90, 3, 8, False),  # both tolerant
            (2, 25, 2, 8, False),  # both exactly at threshold
            (2, 25, 3, 8, True),  # prototype Scenario 5's tripping count
        ],
    )
    def test_tolerance_trips_only_on_strictly_exceeds(self, max_failed, percent, failures, total, expected):
        tolerance = BatchTolerance(max_failed=max_failed, max_failed_percent=percent)
        assert tolerance_trips(tolerance, failure_count=failures, total_items=total) is expected

    def test_absent_tolerance_never_trips(self):
        assert tolerance_trips(None, failure_count=99, total_items=1) is False


# === 2. Prototype Scenario 5 ===


class TestScenarioFiveTable:
    @pytest.mark.parametrize("runner", ["sync", "async"])
    async def test_scenario_five_trip_accounts_all_eight_items(self, home, runner):
        """8 items, max_failed=2, max_failed_percent=25 → 3/3/2, 8 of 8."""
        graph, state = _sequenced_graph("ingest", ["ok", "ok", "ok", "fail", "fail", "fail"], runner)
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(
            host,
            served["ingest"],
            _items(8),
            workflow_id="drop-42",
            tolerance=BatchTolerance(max_failed=2, max_failed_percent=25),
        )
        executed = await _drive(host, home)

        # Admission closed on the third failure: two items never ran.
        assert state["n"] == 6
        assert len(executed) == 6

        view = await host.client.get(receipt.batch_ref)
        assert view.counts == {
            "completed": 3,
            "failed": 3,
            "partial": 0,
            "stopped": 0,
            "active": 0,
            "paused": 0,
            "queued": 0,
            "recovery_exhausted": 0,
            "unstarted": 2,
            "abandoned": 0,
        }
        # Every manifest item accounted exactly once.
        assert sum(view.counts.values()) == 8
        assert len(view.outcomes) == 8
        assert set(view.outcomes) == set(_items(8))
        assert len(view.unstarted_items) == 2
        # Unstarted items are named, never fabricated results.
        for key in view.unstarted_items:
            assert view.outcomes[key] is None
            assert home.get_run(f"drop-42:{key}") is None
        assert view.tolerance_tripped is True
        assert view.settled is True

        # Truthfully PARTIAL: not a failed Batch, not a stopped one.
        kinds = [kind for _bseq, kind, _payload in _batch_updates(home, receipt.batch_ref.batch_id)]
        assert "stopped" not in kinds
        assert view.counts["failed"] < 8
        assert view.counts["completed"] > 0

        # A trip is a Batch fact, never a WorkflowStatus: the settled
        # children keep their own ordinary run statuses.
        assert {view.outcomes[key] for key in view.outcomes if view.outcomes[key] is not None} == {"completed", "failed"}

        # Sync mirror reads the identical view.
        assert host.client.get_sync(receipt.batch_ref) == view

    async def test_trip_commits_with_the_tripping_child_fact(self, home):
        """A9: the trip lands at the very next bseq, one transaction."""
        graph, _state = _sequenced_graph("ingest", ["ok", "ok", "ok", "fail", "fail", "fail"])
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(
            host,
            served["ingest"],
            _items(8),
            workflow_id="drop-42",
            tolerance=BatchTolerance(max_failed=2, max_failed_percent=25),
        )
        await _drive(host, home)

        updates = _batch_updates(home, receipt.batch_ref.batch_id)
        # Gap-free: manifest, six child_settled facts, then the trip.
        assert [bseq for bseq, _kind, _payload in updates] == list(range(1, 9))
        assert [kind for _bseq, kind, _payload in updates] == ["manifest"] + ["child_settled"] * 6 + ["tolerance_tripped"]
        # The trip is immediately after the child_settled that caused it.
        tripping_child = updates[-2]
        assert tripping_child[1] == "child_settled"
        assert tripping_child[2]["status"] == "failed"

        payload = updates[-1][2]
        assert payload["failed"] == 3
        assert payload["total_items"] == 8
        assert payload["max_failed"] == 2
        assert payload["max_failed_percent"] == 25
        view = await host.client.get(receipt.batch_ref)
        assert payload["unstarted_items"] == list(view.unstarted_items)


# === 3. The percentage denominator is pinned ===


class TestFixedDenominator:
    @pytest.mark.parametrize("runner", ["sync", "async"])
    async def test_denominator_never_shrinks_to_settled_items(self, home, runner):
        """4 of 8 at 50% is tolerated even though the first item is 1-of-1."""
        graph, state = _sequenced_graph("ingest", ["fail", "fail", "fail", "fail"], runner)
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(
            host,
            served["ingest"],
            _items(8),
            workflow_id="drop-pct",
            tolerance=BatchTolerance(max_failed_percent=50),
        )
        await _drive(host, home)

        # A live denominator would have tripped at the first failure
        # (1 of 1 = 100%). The pinned denominator is 8, so 4 is tolerated.
        assert state["n"] == 8
        view = await host.client.get(receipt.batch_ref)
        assert view.counts["failed"] == 4
        assert view.counts["completed"] == 4
        assert view.counts["unstarted"] == 0
        assert view.tolerance_tripped is False

    async def test_trip_payload_reports_the_pinned_manifest_total(self, home):
        graph, state = _sequenced_graph("ingest", ["fail"] * 5)
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(
            host,
            served["ingest"],
            _items(8),
            workflow_id="drop-pct2",
            tolerance=BatchTolerance(max_failed_percent=50),
        )
        await _drive(host, home)

        assert state["n"] == 5
        payload = _trip_fact(home, receipt.batch_ref.batch_id)
        assert payload["failed"] == 5
        assert payload["total_items"] == 8  # not the 5 items that ran
        view = await host.client.get(receipt.batch_ref)
        assert view.tolerance_tripped is True
        assert view.counts["unstarted"] == 3


# === 4. Failure equivalence ===


class TestFailureEquivalence:
    async def _batch_of(self, home, keys: int = 6, tolerance: BatchTolerance | None = None):
        graph, _state = _sequenced_graph("ingest", [])
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(
            host,
            served["ingest"],
            _items(keys),
            workflow_id="drop-eq",
            tolerance=tolerance or BatchTolerance(max_failed=0),
        )
        return host, receipt

    async def test_only_failed_and_exhausted_children_count(self, home):
        """max_failed=0 trips on the FIRST failure-equivalent child only."""
        host, receipt = await self._batch_of(home)
        batch_id = receipt.batch_ref.batch_id

        # Paused: a durable pause waits on purpose; it never counts. The
        # submission state mirrors what `_release_submission` writes for a
        # parked child, so the bucket ladder sees the real shape.
        home.create_run_sync("drop-eq:p-0", graph_name="ingest")
        home.update_run_status_sync("drop-eq:p-0", WorkflowStatus.PAUSED)
        home._sync_db().execute("UPDATE host_submissions SET state = 'paused' WHERE workflow_id = 'drop-eq:p-0'")
        home._sync_db().commit()
        assert home._batch_tripped_sync(batch_id) is False

        # Partial and stopped are terminal but NOT failure-equivalent.
        home.create_run_sync("drop-eq:p-1", graph_name="ingest")
        home.update_run_status_sync("drop-eq:p-1", WorkflowStatus.PARTIAL)
        assert home._batch_tripped_sync(batch_id) is False
        home.create_run_sync("drop-eq:p-2", graph_name="ingest")
        home.update_run_status_sync("drop-eq:p-2", WorkflowStatus.STOPPED)
        assert home._batch_tripped_sync(batch_id) is False

        # Completed obviously never counts either.
        home.create_run_sync("drop-eq:p-3", graph_name="ingest")
        home.update_run_status_sync("drop-eq:p-3", WorkflowStatus.COMPLETED)
        assert home._batch_tripped_sync(batch_id) is False

        # Queued and delayed children never count: p-4 and p-5 are still
        # pending, and admission stayed open the whole way through.
        view = await host.client.get(receipt.batch_ref)
        assert view.counts["queued"] == 2
        # Parked on a human is its own bucket, disjoint from active.
        assert view.counts["paused"] == 1 and view.counts["active"] == 0
        assert view.tolerance_tripped is False

        # One failed child is one failure-equivalent child: the trip fires.
        home.create_run_sync("drop-eq:p-4", graph_name="ingest")
        home.update_run_status_sync("drop-eq:p-4", WorkflowStatus.FAILED)
        assert home._batch_tripped_sync(batch_id) is True
        view = await host.client.get(receipt.batch_ref)
        assert view.tolerance_tripped is True
        assert view.unstarted_items == ("p-5",)

    async def test_only_failed_and_exhausted_children_count_async(self, home):
        """Async mirror: the same rule through ``update_run_status``."""
        host, receipt = await self._batch_of(home)
        batch_id = receipt.batch_ref.batch_id

        await home.create_run("drop-eq:p-0", graph_name="ingest")
        await home.update_run_status("drop-eq:p-0", WorkflowStatus.PAUSED)
        await home.create_run("drop-eq:p-1", graph_name="ingest")
        await home.update_run_status("drop-eq:p-1", WorkflowStatus.PARTIAL)
        await home.create_run("drop-eq:p-2", graph_name="ingest")
        await home.update_run_status("drop-eq:p-2", WorkflowStatus.STOPPED)
        assert await home._batch_tripped(batch_id) is False

        await home.create_run("drop-eq:p-3", graph_name="ingest")
        await home.update_run_status("drop-eq:p-3", WorkflowStatus.FAILED)
        assert await home._batch_tripped(batch_id) is True
        view = await host.client.get(receipt.batch_ref)
        assert view.tolerance_tripped is True
        assert view.unstarted_items == ("p-4", "p-5")

    async def test_recovery_exhausted_children_count(self, home):
        """A parked child is failure-equivalent, and trips in-transaction."""
        host, receipt = await self._batch_of(home, tolerance=BatchTolerance(max_failed=0))
        batch_id = receipt.batch_ref.batch_id

        # Trip the recovery brake honestly: claimed, no progress, at cap.
        db = home._sync_db()
        db.execute("UPDATE host_submissions SET state = 'claimed', recovery_attempts = 2 WHERE workflow_id = 'drop-eq:p-0'")
        db.commit()
        await home._restart_scan()

        assert home._get_submission_sync("drop-eq:p-0")["state"] == "exhausted"
        assert home._batch_tripped_sync(batch_id) is True
        view = await host.client.get(receipt.batch_ref)
        assert view.counts["recovery_exhausted"] == 1
        assert view.counts["unstarted"] == 5
        assert view.tolerance_tripped is True
        # The trip fact records the exhausted child as the failure.
        payload = _trip_fact(home, batch_id)
        assert payload["failed"] == 1
        assert payload["total_items"] == 6

    async def test_delayed_children_are_never_failure_equivalent(self, home):
        """A future start_at keeps every item unadmitted, so nothing counts."""
        graph, state = _sequenced_graph("ingest", ["fail"] * 4)
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        start_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        receipt = await submit_keyed(
            host,
            served["ingest"],
            _items(4),
            workflow_id="drop-delay",
            tolerance=BatchTolerance(max_failed=0),
            start_at=start_at,
        )
        assert await _drive(host, home) == []
        assert state["n"] == 0
        view = await host.client.get(receipt.batch_ref)
        assert view.counts["queued"] == 4
        assert view.tolerance_tripped is False


# === 5. Trip behavior ===


class TestTripBehavior:
    @pytest.mark.parametrize("runner", ["sync", "async"])
    async def test_claimed_children_settle_after_the_trip(self, home, runner):
        graph, state = _sequenced_graph("ingest", ["fail", "fail", "fail"], runner)
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(
            host,
            served["ingest"],
            _items(8),
            workflow_id="drop-claimed",
            tolerance=BatchTolerance(max_failed=2),
        )
        # Every child is claimed before the third failure trips the Batch.
        assert len(await _claim_all_then_execute(host, home)) == 8

        assert state["n"] == 8  # claimed work is never killed
        view = await host.client.get(receipt.batch_ref)
        assert view.tolerance_tripped is True
        assert view.counts["failed"] == 3
        assert view.counts["completed"] == 5
        assert view.counts["unstarted"] == 0
        assert sum(view.counts.values()) == 8
        assert view.settled is True

    async def test_trip_is_recorded_exactly_once(self, home):
        graph, _state = _sequenced_graph("ingest", ["fail"] * 6)
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(
            host,
            served["ingest"],
            _items(8),
            workflow_id="drop-once",
            tolerance=BatchTolerance(max_failed=2),
        )
        await _claim_all_then_execute(host, home)

        kinds = [kind for _bseq, kind, _payload in _batch_updates(home, receipt.batch_ref.batch_id)]
        assert kinds.count("tolerance_tripped") == 1
        # Six failures happened, but the trip records the count at the
        # moment admission closed, not the final tally.
        assert _trip_fact(home, receipt.batch_ref.batch_id)["failed"] == 3

    async def test_closed_admission_survives_a_child_returned_to_pending(self, home):
        """A crash cannot reopen a tripped Batch's admission."""
        graph, state = _sequenced_graph("ingest", ["fail", "fail", "fail"])
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(
            host,
            served["ingest"],
            _items(8),
            workflow_id="drop-reopen",
            tolerance=BatchTolerance(max_failed=2),
        )
        claimed = await _claim(host, home)
        assert len(claimed) == 8
        for row in claimed[:3]:
            await host._execute_submission(row)
        assert state["n"] == 3
        assert home._batch_tripped_sync(receipt.batch_ref.batch_id) is True

        # The worker dies with five children claimed but unexecuted; the
        # restart scan returns them to pending.
        await home._restart_scan()
        assert [row["state"] for row in (home._get_submission_sync(f"drop-reopen:p-{n}") for n in range(8))].count("pending") == 5

        # Admission refuses them and records them as explicitly unstarted.
        assert await _claim(host, home) == []
        assert state["n"] == 3
        view = await host.client.get(receipt.batch_ref)
        assert view.counts == {
            "completed": 0,
            "failed": 3,
            "partial": 0,
            "stopped": 0,
            "active": 0,
            "paused": 0,
            "queued": 0,
            "recovery_exhausted": 0,
            "unstarted": 5,
            "abandoned": 0,
        }
        assert len(view.unstarted_items) == 5
        assert view.settled is True

    async def test_batch_without_tolerance_never_closes_admission(self, home):
        graph, state = _sequenced_graph("ingest", ["fail"] * 8)
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(host, served["ingest"], _items(8), workflow_id="drop-notol")
        await _drive(host, home)

        assert state["n"] == 8
        view = await host.client.get(receipt.batch_ref)
        assert view.counts["failed"] == 8
        assert view.tolerance_tripped is False
        kinds = [kind for _bseq, kind, _payload in _batch_updates(home, receipt.batch_ref.batch_id)]
        assert "tolerance_tripped" not in kinds

    async def test_watch_delivers_the_trip_and_terminates(self, home):
        graph, _state = _sequenced_graph("ingest", ["fail"] * 3)
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(
            host,
            served["ingest"],
            _items(8),
            workflow_id="drop-watch",
            tolerance=BatchTolerance(max_failed=2),
        )
        await _drive(host, home)

        updates = [u async for u in host.client.watch(receipt.batch_ref)]
        durable = [u for u in updates if u.durable]
        assert [u.cursor for u in durable] == ["bseq:1", "bseq:2", "bseq:3", "bseq:4", "bseq:5"]
        assert [u.kind for u in durable] == ["manifest", "child_settled", "child_settled", "child_settled", "tolerance_tripped"]
        assert durable[-1].payload["failed"] == 3


# === 6. Item-scoped rerun ===


class TestSubsetRerun:
    async def _settled_source(self, home, workflow_id: str = "drop-42"):
        graph, state = _sequenced_graph("ingest", ["ok", "ok", "ok", "fail", "fail", "fail"])
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(
            host,
            served["ingest"],
            _items(8),
            workflow_id=workflow_id,
            tolerance=BatchTolerance(max_failed=2, max_failed_percent=25),
        )
        await _drive(host, home)
        return host, receipt, state

    def _failed_keys(self, view) -> list[str]:
        return [key for key, outcome in view.outcomes.items() if outcome == "failed"]

    async def test_subset_rerun_mints_a_new_batch_and_never_mutates_the_source(self, home):
        host, source, _state = await self._settled_source(home)
        source_batch_id = source.batch_ref.batch_id
        view = await host.client.get(source.batch_ref)
        keys = self._failed_keys(view)[:2]
        before = _snapshot_batch(home, source_batch_id)

        rerun = await host.client.rerun(source.batch_ref, item_keys=keys)

        assert isinstance(rerun, BatchSubmitReceipt)
        assert rerun.duplicate is False
        assert rerun.batch_ref != source.batch_ref
        assert rerun.batch_ref.batch_id != source_batch_id
        assert rerun.workflow_id == "drop-42-retry-1"

        # The source Batch is byte-identical afterwards.
        assert _snapshot_batch(home, source_batch_id) == before
        assert await host.client.get(source.batch_ref) == view

        db = home._sync_db()
        (retry_of,) = db.execute("SELECT retry_of FROM host_batches WHERE batch_id = ?", (rerun.batch_ref.batch_id,)).fetchone()
        assert retry_of == source_batch_id
        new_view = await host.client.get(rerun.batch_ref)
        assert new_view.retry_of == source_batch_id
        assert view.retry_of is None

        # A NEW immutable manifest: the selected keys only, source inputs
        # and source Definition identity verbatim.
        (items_json, name, version, struct_hash, tolerance_json) = db.execute(
            "SELECT items_json, definition_name, def_version, def_struct_hash, tolerance_json FROM host_batches WHERE batch_id = ?",
            (rerun.batch_ref.batch_id,),
        ).fetchone()
        source_items = json.loads(before["batch"][0][5])
        assert json.loads(items_json) == {key: source_items[key] for key in keys}
        assert (name, version, struct_hash) == (view.definition_id.name, view.definition_id.deployment_version, view.definition_id.structural_hash)
        assert json.loads(tolerance_json) == {"max_failed": 2, "max_failed_percent": 25}
        assert set(new_view.outcomes) == set(keys)

        # Each new child records retry_of against its SOURCE child.
        children = db.execute(
            "SELECT workflow_id, item_key, retry_of, inputs_json FROM host_submissions WHERE batch_id = ? ORDER BY rowid",
            (rerun.batch_ref.batch_id,),
        ).fetchall()
        assert [(row[0], row[1], row[2]) for row in children] == [(f"drop-42-retry-1:{key}", key, f"drop-42:{key}") for key in keys]
        assert [json.loads(row[3]) for row in children] == [source_items[key] for key in keys]

        # The new Batch has its own durable sequence starting at bseq 1.
        updates = _batch_updates(home, rerun.batch_ref.batch_id)
        assert [(bseq, kind) for bseq, kind, _payload in updates] == [(1, "manifest")]
        assert updates[0][2]["retry_of"] == source_batch_id
        assert updates[0][2]["item_keys"] == keys

    async def test_reran_children_execute_with_the_source_inputs(self, home):
        host, source, state = await self._settled_source(home)
        view = await host.client.get(source.batch_ref)
        keys = self._failed_keys(view)[:2]
        source_inputs = [json.loads(home._get_submission_sync(f"drop-42:{key}")["inputs_json"])["x"] for key in keys]

        rerun = await host.client.rerun(source.batch_ref, item_keys=keys)
        state["seen"].clear()
        await _drive(host, home)

        # Exactly the selected items ran again, with their pinned inputs.
        assert sorted(state["seen"]) == sorted(source_inputs)
        new_view = await host.client.get(rerun.batch_ref)
        assert new_view.counts["completed"] == 2
        assert new_view.settled is True
        # The source Batch's own outcomes never moved.
        assert await host.client.get(source.batch_ref) == view

    async def test_second_rerun_increments_the_retry_id(self, home):
        host, source, _state = await self._settled_source(home)
        view = await host.client.get(source.batch_ref)
        keys = self._failed_keys(view)[:1]

        first = await host.client.rerun(source.batch_ref, item_keys=keys)
        second = await host.client.rerun(source.batch_ref, item_keys=keys)
        assert first.workflow_id == "drop-42-retry-1"
        assert second.workflow_id == "drop-42-retry-2"
        assert first.batch_ref != second.batch_ref

    async def test_concurrent_batch_reruns_mint_one_id_each(self, home):
        """The Batch ordinal is allocated inside the insertion transaction.

        Counting ``host_batches`` outside it let concurrent callers all read
        the same count and mint the same ``<source>-retry-N``, so all but
        one silently deduped into the first Batch.
        """
        host, source, _state = await self._settled_source(home)
        view = await host.client.get(source.batch_ref)
        keys = self._failed_keys(view)[:1]

        receipts = await asyncio.gather(*(host.client.rerun(source.batch_ref, item_keys=keys) for _ in range(4)))

        assert sorted(r.workflow_id for r in receipts) == [f"drop-42-retry-{n}" for n in range(1, 5)]
        assert len({r.batch_ref.batch_id for r in receipts}) == 4
        assert not any(r.duplicate for r in receipts)
        # Each new Batch records lineage against the one source Batch.
        db = home._sync_db()
        (lineage_rows,) = db.execute("SELECT COUNT(*) FROM host_batches WHERE retry_of = ?", (source.batch_ref.batch_id,)).fetchone()
        assert lineage_rows == 4

    async def test_omitted_item_keys_repeat_the_whole_manifest(self, home):
        host, source, _state = await self._settled_source(home)
        rerun = await host.client.rerun(source.batch_ref)
        new_view = await host.client.get(rerun.batch_ref)
        assert set(new_view.outcomes) == set(_items(8))
        assert new_view.retry_of == source.batch_ref.batch_id

    async def test_rerun_rejects_keys_outside_the_source_manifest(self, home):
        host, source, _state = await self._settled_source(home)
        with pytest.raises(RerunError, match="not in the source manifest") as excinfo:
            await host.client.rerun(source.batch_ref, item_keys=["p-0", "nope", "also-nope"])
        message = str(excinfo.value)
        # The failure line names the offending keys and only those...
        failure_line = message.splitlines()[0]
        assert "'nope'" in failure_line
        assert "'also-nope'" in failure_line
        assert "'p-0'" not in failure_line
        # ...and the caller is shown the source manifest's valid keys plus
        # actionable guidance (dev/CODE-CONVENTIONS.md § Error Messages).
        assert "Valid item keys: ['p-0', 'p-1'" in message
        assert "How to fix:" in message
        # Nothing was written by the rejected rerun.
        db = home._sync_db()
        assert db.execute("SELECT COUNT(*) FROM host_batches").fetchone()[0] == 1

    async def test_rerun_suggests_a_close_item_key(self, home):
        """A near-miss key gets a fuzzy suggestion, not just a rejection."""
        host, source, _state = await self._settled_source(home)
        with pytest.raises(RerunError) as excinfo:
            await host.client.rerun(source.batch_ref, item_keys=["p-9"])
        message = str(excinfo.value)
        assert "Did you mean " in message
        # The suggestion is always a real key of the source manifest.
        assert message.split("Did you mean ")[1].split("?")[0].strip("'") in _items(8)

    async def test_rerun_rejects_empty_and_duplicate_key_lists(self, home):
        host, source, _state = await self._settled_source(home)
        with pytest.raises(ValueError, match="at least one source item"):
            await host.client.rerun(source.batch_ref, item_keys=[])
        with pytest.raises(ValueError, match="duplicate item key"):
            await host.client.rerun(source.batch_ref, item_keys=["p-0", "p-0"])
        with pytest.raises(ValueError, match="non-empty strings"):
            await host.client.rerun(source.batch_ref, item_keys=[""])
        with pytest.raises(TypeError, match="sequence of item key strings"):
            await host.client.rerun(source.batch_ref, item_keys="p-0")

    async def test_rerun_rejects_children_still_in_flight(self, home):
        graph, _state = _sequenced_graph("ingest", [])
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(host, served["ingest"], _items(3), workflow_id="drop-live")
        with pytest.raises(RerunError, match="still in flight"):
            await host.client.rerun(receipt.batch_ref, item_keys=["p-0"])

    async def test_rerun_rejects_item_keys_for_a_run(self, home):
        graph, _state = _sequenced_graph("ingest", [])
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        await host.submit(served["ingest"], {"x": 1}, workflow_id="wf-solo")
        run_ref = RunRef(home=home.uri, run_id="wf-solo")
        with pytest.raises(TypeError, match="item_keys is only valid for a BatchRef"):
            await host.client.rerun(run_ref, item_keys=["p-0"])
        with pytest.raises(TypeError, match="item_keys is only valid for a BatchRef"):
            host.client.rerun_sync(run_ref, item_keys=["p-0"])
        with pytest.raises(TypeError, match="RunRef or BatchRef"):
            await host.client.rerun("wf-solo")

    async def test_rerun_accepts_no_input_override(self, home):
        host, source, _state = await self._settled_source(home)
        for verb in (host.client.rerun, host.client.rerun_sync):
            parameters = inspect.signature(verb).parameters
            # source_ref is audit provenance, not work definition: a rerun
            # still repeats the source's pinned identity and inputs verbatim.
            assert set(parameters) == {"ref", "item_keys", "source_ref"}
        with pytest.raises(TypeError):
            await host.client.rerun(source.batch_ref, item_keys=["p-3"], inputs={"x": 99})

    async def test_rerun_unknown_batch_raises(self, home):
        graph, _state = _sequenced_graph("ingest", [])
        host = serve(graph, home=home, deployment_version="v1")
        unknown = BatchRef(home=home.uri, batch_id="b-nope")
        with pytest.raises(RerunError, match="no such Batch"):
            await host.client.rerun(unknown)
        with pytest.raises(RerunError, match="no such Batch"):
            host.client.rerun_sync(unknown)

    async def test_rerun_sync_mirrors_the_async_form(self, home):
        host, source, _state = await self._settled_source(home, workflow_id="drop-sync")
        view = await host.client.get(source.batch_ref)
        keys = self._failed_keys(view)[:2]
        before = _snapshot_batch(home, source.batch_ref.batch_id)

        rerun = host.client.rerun_sync(source.batch_ref, item_keys=keys)

        assert isinstance(rerun, BatchSubmitReceipt)
        assert rerun.workflow_id == "drop-sync-retry-1"
        assert _snapshot_batch(home, source.batch_ref.batch_id) == before
        new_view = host.client.get_sync(rerun.batch_ref)
        assert new_view.retry_of == source.batch_ref.batch_id
        assert set(new_view.outcomes) == set(keys)
        db = home._sync_db()
        children = db.execute(
            "SELECT item_key, retry_of FROM host_submissions WHERE batch_id = ? ORDER BY rowid",
            (rerun.batch_ref.batch_id,),
        ).fetchall()
        assert children == [(key, f"drop-sync:{key}") for key in keys]
        # Rerun lineage never merges with fork lineage.
        assert all(row[1] is not None for row in children)
        forked = db.execute("SELECT COUNT(*) FROM host_submissions WHERE batch_id = ? AND forked_from IS NOT NULL", (rerun.batch_ref.batch_id,))
        assert forked.fetchone()[0] == 0

    async def test_run_rerun_is_unchanged(self, home):
        """The RunRef form keeps its ticket-03 shape and lineage."""
        graph, _state = _sequenced_graph("ingest", ["fail"])
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        await host.submit(served["ingest"], {"x": 1}, workflow_id="wf-one")
        await _drive(host, home)

        receipt = await host.client.rerun(RunRef(home=home.uri, run_id="wf-one"))
        assert isinstance(receipt, SubmitReceipt)
        assert receipt.workflow_id == "wf-one-retry-1"
        assert home._get_submission_sync("wf-one-retry-1")["retry_of"] == "wf-one"


# === 7. A9: the durable sequence accounts every manifest item ===


def _accounted_from_stream(updates: list[tuple[int, str, dict]]) -> tuple[list[str], list[str]]:
    """Reconstruct (manifest keys, accounted keys) from batch_updates ALONE.

    A detached ``watch(batch_ref, after=cursor)`` consumer sees nothing but
    these rows — no view, no store queries. PRD 0019 A9 says it follows the
    whole Batch gap-free, so the stream must account every manifest item on
    its own, however the item ended:

    - settled children by their ``child_settled`` fact — terminal runs AND
      children the recovery brake parked (``status`` is then the same
      ``"recovery_exhausted"`` string ``BatchView.outcomes`` reports, so
      the parked child needs no separate kind);
    - items a trip closed admission on, by the trip payload;
    - every other item that ends unstarted — a stopped Batch's child that
      never executed, or one a crash returned to pending after the trip —
      by its own ``child_unstarted`` fact.
    """
    manifest: list[str] = []
    accounted: list[str] = []
    for _bseq, kind, payload in updates:
        if kind == "manifest":
            manifest = list(payload["item_keys"])
        elif kind in ("child_settled", "child_unstarted"):
            accounted.append(payload["item_key"])
        elif kind == "tolerance_tripped":
            accounted.extend(payload["unstarted_items"])
    return manifest, accounted


class TestDurableStreamAccountsEveryItem:
    """A9: the stream and the view must reconstruct each other.

    The regression these cover: three paths used to settle a Batch child
    with no ``batch_updates`` row at all. ``BatchView`` recomputes from the
    submission rows so it stayed right every time; a ``watch()`` consumer
    following the durable cursor never learned those items' outcomes, so
    the sequence was gap-free in numbering but not in meaning.

    1. A child CLAIMED at trip time is rightly absent from the trip fact's
       ``unstarted_items`` (it may still settle) — but when a worker death
       returns it to pending, admission refuses it.
    2. A Batch stop finishes a child that never executed. The ``stopped``
       fact's payload is only the verb and info; it names no items.
    3. The recovery brake parks a child as ``exhausted`` — a settled,
       failure-equivalent child outcome, so it is Batch truth.
    """

    async def _tripped_with_claimed_children(self, home, runner: str = "sync"):
        """8 items all claimed, then 3 failures trip with NOTHING pending.

        ``unstarted_items`` is empty in the trip fact by construction: at
        trip time every remaining item was already claimed, so the trip
        could not name it. Those five items become unstarted only later.
        """
        graph, _state = _sequenced_graph("ingest", ["fail", "fail", "fail"], runner)
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(
            host,
            served["ingest"],
            _items(8),
            workflow_id="drop-a9",
            tolerance=BatchTolerance(max_failed=2),
        )
        claimed = await _claim(host, home)
        assert len(claimed) == 8
        for row in claimed[:3]:
            await host._execute_submission(row)
        assert home._batch_tripped_sync(receipt.batch_ref.batch_id) is True
        assert _trip_fact(home, receipt.batch_ref.batch_id)["unstarted_items"] == []
        return host, receipt

    @pytest.mark.parametrize("runner", ["sync", "async"])
    async def test_stream_accounts_every_item_across_the_restart_path(self, home, runner):
        host, receipt = await self._tripped_with_claimed_children(home, runner)
        batch_id = receipt.batch_ref.batch_id

        # The worker dies with five children claimed but never executed;
        # the restart scan returns them to pending and admission refuses
        # them. Each refusal is a durable fact, not a silent state flip.
        await home._restart_scan()
        assert await _claim(host, home) == []

        updates = _batch_updates(home, batch_id)
        # Gap-free numbering AND gap-free meaning.
        assert [bseq for bseq, _kind, _payload in updates] == list(range(1, len(updates) + 1))
        manifest, accounted = _accounted_from_stream(updates)
        assert len(manifest) == 8
        assert sorted(accounted) == sorted(manifest)  # 8 of 8, from the stream alone
        assert len(accounted) == len(set(accounted))  # each item accounted exactly once

        # The stream's unstarted truth is the view's unstarted truth.
        unstarted = [payload for _bseq, kind, payload in updates if kind == "child_unstarted"]
        view = await host.client.get(receipt.batch_ref)
        assert {fact["item_key"] for fact in unstarted} == set(view.unstarted_items)
        assert len(unstarted) == 5
        assert all(fact["workflow_id"] == f"drop-a9:{fact['item_key']}" for fact in unstarted)
        assert view.counts["unstarted"] == 5
        assert view.settled is True
        assert host.client.get_sync(receipt.batch_ref) == view

    async def test_a_watch_consumer_learns_every_item_without_reading_the_view(self, home):
        host, receipt = await self._tripped_with_claimed_children(home)
        await home._restart_scan()
        assert await _claim(host, home) == []

        durable = [update async for update in host.client.watch(receipt.batch_ref) if update.durable]

        assert [u.kind for u in durable] == ["manifest"] + ["child_settled"] * 3 + ["tolerance_tripped"] + ["child_unstarted"] * 5
        assert [u.cursor for u in durable] == [f"bseq:{n}" for n in range(1, 11)]
        manifest, accounted = _accounted_from_stream([(n, u.kind, u.payload) for n, u in enumerate(durable, start=1)])
        assert sorted(accounted) == sorted(manifest)

    async def _stopped_mid_flight(self, home, runner: str = "sync"):
        """6 items: 2 settled, 2 claimed-not-executed, 2 pending, then stop.

        The ``stopped`` fact names no items, so the four that never execute
        are Batch truth only their own facts can carry.
        """
        graph, state = _sequenced_graph("ingest", ["ok", "fail"], runner)
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(host, served["ingest"], _items(6), workflow_id="drop-a9-stop")
        claimed = await _claim(host, home, limit=4)
        assert len(claimed) == 4
        for row in claimed[:2]:
            await host._execute_submission(row)

        stop = await host.client.stop(receipt.batch_ref, info={"reason": "drop recalled"})
        assert stop.duplicate is False

        # The worker resumes. The two claimed-but-unexecuted children hit
        # the pre-run gate and finish without inventing a runs row; the two
        # still pending are claimed and hit the same gate.
        for row in claimed[2:]:
            await host._execute_submission(row)
        await _drive(host, home)
        assert state["n"] == 2  # the stop really did prevent four executions
        return host, receipt

    @pytest.mark.parametrize("runner", ["sync", "async"])
    async def test_stream_accounts_every_item_of_a_batch_stopped_mid_flight(self, home, runner):
        host, receipt = await self._stopped_mid_flight(home, runner)
        batch_id = receipt.batch_ref.batch_id

        updates = _batch_updates(home, batch_id)
        assert [bseq for bseq, _kind, _payload in updates] == list(range(1, len(updates) + 1))
        manifest, accounted = _accounted_from_stream(updates)
        assert len(manifest) == 6
        assert sorted(accounted) == sorted(manifest)  # 6 of 6, from the stream alone
        assert len(accounted) == len(set(accounted))  # each item accounted exactly once
        assert [kind for _bseq, kind, _payload in updates] == (["manifest"] + ["child_settled"] * 2 + ["stopped"] + ["child_unstarted"] * 4)

        # The stream's unstarted truth is the view's unstarted truth.
        unstarted = [payload for _bseq, kind, payload in updates if kind == "child_unstarted"]
        view = await host.client.get(receipt.batch_ref)
        assert {fact["item_key"] for fact in unstarted} == set(view.unstarted_items)
        assert all(fact["workflow_id"] == f"drop-a9-stop:{fact['item_key']}" for fact in unstarted)
        assert view.counts["unstarted"] == 4
        assert view.counts["completed"] == 1
        assert view.counts["failed"] == 1
        assert view.settled is True
        assert host.client.get_sync(receipt.batch_ref) == view

    async def test_a_watch_consumer_learns_every_stopped_item_without_the_view(self, home):
        host, receipt = await self._stopped_mid_flight(home)

        durable = [update async for update in host.client.watch(receipt.batch_ref) if update.durable]

        manifest, accounted = _accounted_from_stream([(n, u.kind, u.payload) for n, u in enumerate(durable, start=1)])
        assert sorted(accounted) == sorted(manifest)
        assert [u.kind for u in durable] == ["manifest"] + ["child_settled"] * 2 + ["stopped"] + ["child_unstarted"] * 4
        assert [u.cursor for u in durable] == [f"bseq:{n}" for n in range(1, 9)]
        # The Batch stop itself never names an item — only the per-item facts do.
        stopped = next(u for u in durable if u.kind == "stopped")
        assert set(stopped.payload) == {"verb", "info", "source_ref"}

    async def test_a_live_watcher_follows_a_stopped_batch_past_the_stop_fact(self, home):
        """A9: ``stopped`` is a durable control fact, never end-of-stream.

        ``_write_batch_stop`` appends ``stopped`` FIRST and writes the child
        stop commands that the pre-run gate applies later, each committing
        its own ``child_unstarted`` fact. A watcher that treated ``stopped``
        as EOF returned at that instant and never delivered them, so the
        stream under-accounted the manifest — the exact A9 invariant this
        branch has already repaired three times.
        """
        graph, state = _sequenced_graph("ingest", ["ok", "fail"])
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(host, served["ingest"], _items(6), workflow_id="drop-a9-live-stop")

        observed: list = []

        async def _follow():
            async for update in host.client.watch(receipt.batch_ref):
                observed.append(update)

        watcher = asyncio.create_task(_follow())
        try:
            await _wait_until(lambda: any(u.kind == "manifest" for u in observed))
            claimed = await _claim(host, home, limit=4)
            assert len(claimed) == 4
            for row in claimed[:2]:
                await host._execute_submission(row)

            await host.client.stop(receipt.batch_ref, info={"reason": "drop recalled"})
            await _wait_until(lambda: any(u.kind == "stopped" for u in observed))
            after_stop = len([u for u in observed if u.durable])

            # The four children that never executed are accounted only NOW,
            # by facts that commit strictly after the stop fact.
            for row in claimed[2:]:
                await host._execute_submission(row)
            await _drive(host, home)
            assert state["n"] == 2
            await asyncio.wait_for(watcher, timeout=15)
        finally:
            if not watcher.done():
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher

        durable = [u for u in observed if u.durable]
        assert [u.kind for u in durable] == ["manifest"] + ["child_settled"] * 2 + ["stopped"] + ["child_unstarted"] * 4
        assert [u.cursor for u in durable] == [f"bseq:{n}" for n in range(1, 9)]
        # Everything after the stop arrived after the watcher had seen it.
        assert after_stop == 4
        assert all(u.kind == "child_unstarted" for u in durable[after_stop:])
        manifest, accounted = _accounted_from_stream([(n, u.kind, u.payload) for n, u in enumerate(durable, start=1)])
        assert len(manifest) == 6
        assert sorted(accounted) == sorted(manifest)  # 6 of 6, live, from the stream alone

    async def _parked_by_the_recovery_brake(self, home):
        """4 items: 2 settle, 2 are parked exhausted by honest re-adoption.

        No tolerance is pinned, so nothing trips: the ONLY way the parked
        children reach the stream is their own settle fact.
        """
        graph, _state = _sequenced_graph("ingest", ["ok", "fail"])
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(host, served["ingest"], _items(4), workflow_id="drop-a9-parked")
        claimed = await _claim(host, home)
        assert len(claimed) == 4
        for row in claimed[:2]:
            await host._execute_submission(row)

        # The worker dies with two children claimed and no committed
        # progress, three times over: the brake parks both at the default
        # recovery_cap of 3.
        for _ in range(3):
            await home._restart_scan()
            await _claim(host, home)
        assert [home._get_submission_sync(f"drop-a9-parked:p-{n}")["state"] for n in range(4)].count("exhausted") == 2
        return host, receipt

    async def test_stream_accounts_recovery_exhausted_children(self, home):
        host, receipt = await self._parked_by_the_recovery_brake(home)

        updates = _batch_updates(home, receipt.batch_ref.batch_id)
        assert [bseq for bseq, _kind, _payload in updates] == list(range(1, len(updates) + 1))
        manifest, accounted = _accounted_from_stream(updates)
        assert len(manifest) == 4
        assert sorted(accounted) == sorted(manifest)  # 4 of 4, from the stream alone
        assert len(accounted) == len(set(accounted))
        assert [kind for _bseq, kind, _payload in updates] == ["manifest"] + ["child_settled"] * 4

        # The parked children reuse child_settled with the SAME outcome
        # string the view reports, so a stream consumer rebuilds outcomes
        # verbatim — no extra kind in the closed vocabulary.
        view = await host.client.get(receipt.batch_ref)
        assert {payload["item_key"]: payload["status"] for _bseq, kind, payload in updates if kind == "child_settled"} == view.outcomes
        assert view.counts["recovery_exhausted"] == 2
        assert view.counts["completed"] == 1
        assert view.counts["failed"] == 1
        assert view.tolerance_tripped is False
        assert view.settled is True

    async def test_a_watch_consumer_learns_a_parked_child_without_the_view(self, home):
        host, receipt = await self._parked_by_the_recovery_brake(home)

        durable = [update async for update in host.client.watch(receipt.batch_ref) if update.durable]

        manifest, accounted = _accounted_from_stream([(n, u.kind, u.payload) for n, u in enumerate(durable, start=1)])
        assert sorted(accounted) == sorted(manifest)
        assert [u.kind for u in durable] == ["manifest"] + ["child_settled"] * 4
        assert [u.cursor for u in durable] == [f"bseq:{n}" for n in range(1, 6)]
        parked = [u.payload for u in durable if u.payload.get("status") == "recovery_exhausted"]
        assert len(parked) == 2
        assert all(fact["workflow_id"] == f"drop-a9-parked:{fact['item_key']}" for fact in parked)

    async def test_a_trip_that_names_its_unstarted_items_never_repeats_them(self, home):
        """One item is accounted once: by the trip fact OR by its own fact."""
        graph, _state = _sequenced_graph("ingest", ["ok", "ok", "ok", "fail", "fail", "fail"])
        host, served = serve_graphs(graph, home=home, deployment_version="v1")
        receipt = await submit_keyed(
            host,
            served["ingest"],
            _items(8),
            workflow_id="drop-a9-drive",
            tolerance=BatchTolerance(max_failed=2),
        )
        await _drive(host, home)

        updates = _batch_updates(home, receipt.batch_ref.batch_id)
        # The trip named both remaining items, so nothing is left to report.
        assert [kind for _bseq, kind, _payload in updates].count("child_unstarted") == 0
        manifest, accounted = _accounted_from_stream(updates)
        assert sorted(accounted) == sorted(manifest)
        assert len(accounted) == len(set(accounted))
