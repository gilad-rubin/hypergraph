"""Durable Host V1 — ticket 12: tune active-Run admission and delay start.

Two admission controls that this file never lets blur together:

* **Host work admission** — ``RunHome.max_active_runs``: how many Runs one
  worker executes at once. Tunable while the worker is live; over-limit work
  waits in claim order as ``WaitingCondition.ADMISSION_LIMITED``; delayed,
  paused, version-incompatible, and recovery-exhausted Runs hold no slot,
  while a claimed Run parked on a provider permit does.
* **Provider-resource admission** — injected ``ProcessLocalLimiter`` budgets
  at graph, node, and component scope. A permit wait is neither a failure
  nor a retry attempt, and a component that owns a provider quota acquires
  it at the exact scarce call.

Plus delayed start: a future ``start_at`` persists and fingerprints at
submission, a past one is immediately eligible, stopping before due
prevents execution, and none of the excluded overflow strategies (reject,
cancel-oldest, cancel-newest, keyed fairness, expression-language keys)
exist anywhere on the public surface.
"""

import asyncio
import contextlib
import inspect
import threading
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from hypergraph import (
    AsyncRunner,
    Graph,
    ProcessLocalLimiter,
    RetryPolicy,
    RunHome,
    RunHomeClient,
    RunQuery,
    RunRef,
    SyncRunner,
    WaitingCondition,
    node,
    serve,
)
from hypergraph.checkpointers.types import WorkflowStatus
from hypergraph.events.processor import EventProcessor
from hypergraph.events.types import NodeAttemptEndEvent, NodeErrorEvent, RunEndEvent
from hypergraph.host.views import WaitingCondition as _WaitingCondition
from hypergraph.runners._shared.provider_limits import (
    compose_graph_limits,
    current_graph_limits,
    pop_graph_limits,
    provider_permits,
    push_graph_limits,
)

aiosqlite = pytest.importorskip("aiosqlite")


# === Helpers (mirrors ticket-04/05/06 conventions) ===


def _home_uri(tmp_path, filename: str = "runs.db") -> str:
    return f"file:{tmp_path / filename}"


@pytest_asyncio.fixture
async def home(tmp_path):
    h = RunHome.open(_home_uri(tmp_path))
    yield h
    await h.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _future_iso(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def _past_iso(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _sync_graph(name: str, calls: dict | None = None) -> Graph:
    @node(output_name="out")
    def compute(x: int) -> int:
        if calls is not None:
            calls["n"] += 1
        return x + 1

    return Graph([compute], name=name).with_runner(SyncRunner())


def _gated_async_graph(name: str, started: dict, gate: asyncio.Event) -> Graph:
    """One-node async graph whose node announces itself, then waits on ``gate``."""

    @node(output_name="out")
    async def compute(x: int) -> int:
        started.setdefault(x, asyncio.Event()).set()
        await gate.wait()
        return x + 1

    return Graph([compute], name=name).with_runner(AsyncRunner())


async def _claim(host, home, limit: int | None = None, now: str | None = None):
    """Run one real admission scan through the single claim choke point."""
    kwargs = {} if limit is None else {"limit": limit}
    return await home._claim_eligible(now or _now_iso(), served=host._served_identities, **kwargs)


@contextlib.asynccontextmanager
async def _worker(host, worker_id: str = "w-test", **kwargs):
    """Run work_forever as a task; shut it down cleanly on exit.

    ``drain_timeout`` is deliberately short: these tests park nodes on a
    permit or a gate, so a failing assertion inside the block can leave one
    parked. A long drain would swallow the real error behind a shutdown
    timeout — a short one cancels the straggler and lets the assertion out.
    """
    kwargs.setdefault("drain_timeout", 1.0)
    task = asyncio.create_task(host.work_forever(worker_id, poll_interval=0.01, **kwargs))
    try:
        yield task
    finally:
        host.shutdown()
        await asyncio.wait_for(task, timeout=20)


async def _wait_for(check, timeout: float = 15.0, interval: float = 0.01):
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


def _state(home, workflow_id: str) -> str | None:
    submission = home._get_submission_sync(workflow_id)
    return None if submission is None else submission["state"]


def _states(home) -> dict[str, str]:
    rows = home._sync_db().execute("SELECT workflow_id, state FROM host_submissions").fetchall()
    return {workflow_id: state for workflow_id, state in rows}


def _attempt_count(home, workflow_id: str) -> int:
    """Attempts the durable retry ledger recorded for one run."""
    row = (
        home._sync_db()
        .execute(
            "SELECT COUNT(*) FROM attempt_records r JOIN attempt_series s ON r.series_id = s.id WHERE s.run_id = ?",
            (workflow_id,),
        )
        .fetchone()
    )
    return int(row[0])


# === 1. The active-Run cap is runtime-tunable and waits in claim order ===


class TestActiveRunCap:
    async def test_uncapped_by_default(self, home):
        assert home.max_active_runs is None
        declared = RunHome.open(":memory:", max_active_runs=2)
        try:
            assert declared.max_active_runs == 2
        finally:
            await declared.close()

    @pytest.mark.parametrize("bad", [0, -1, True, False, 2.0, "2"])
    def test_cap_rejects_anything_but_a_positive_int_or_none(self, home, bad):
        with pytest.raises(ValueError, match="max_active_runs"):
            home.max_active_runs = bad
        with pytest.raises(ValueError, match="How to fix"):
            home.max_active_runs = 0

    async def test_the_cap_lives_in_the_store_not_on_one_python_object(self, tmp_path, home):
        """US41: an operator tuning the cap holds their OWN RunHome, not the worker's."""
        operator = RunHome.open(_home_uri(tmp_path))  # same store, different object
        try:
            assert operator.max_active_runs is None

            home.max_active_runs = 2  # the worker's Home

            assert operator.max_active_runs == 2

            operator.max_active_runs = 1  # tuned from the operator's side

            assert home.max_active_runs == 1
        finally:
            await operator.close()

    async def test_an_operator_client_reports_what_the_worker_holds_back(self, tmp_path, home):
        """US5 + US21: a separate inspection process must not say QUEUED here."""
        host = serve(_sync_graph("dbl"), home=home, deployment_version="v1")
        home.max_active_runs = 1
        await host.submit("dbl", {"x": 1}, workflow_id="wf-a")
        second = await host.submit("dbl", {"x": 1}, workflow_id="wf-b")
        await _claim(host, home)

        operator = RunHome.open(_home_uri(tmp_path))
        try:
            client = RunHomeClient(operator)  # never saw the worker's cap
            limited = RunQuery(waiting=WaitingCondition.ADMISSION_LIMITED)

            assert (await client.get(second.run_ref)).waiting is WaitingCondition.ADMISSION_LIMITED
            assert client.get_sync(second.run_ref).waiting is WaitingCondition.ADMISSION_LIMITED
            assert [view.workflow_id for view in await client.list(limited)] == ["wf-b"]
            assert [view.workflow_id for view in client.list_sync(limited)] == ["wf-b"]
        finally:
            await operator.close()

    async def test_open_writes_an_explicit_cap_through_and_adopts_a_stored_one(self, tmp_path):
        """The documented rule for open(): explicit writes, omitted adopts."""
        uri = _home_uri(tmp_path, "open.db")

        async def cap(**kwargs) -> int | None:
            opened = RunHome.open(uri, **kwargs)
            try:
                return opened.max_active_runs
            finally:
                await opened.close()

        assert await cap(max_active_runs=3) == 3
        assert await cap() == 3  # omitted: adopts what the store holds
        assert await cap(max_active_runs=None) is None  # explicit None writes through
        assert await cap() is None

    async def test_claim_order_stays_total_when_created_at_ties(self, home):
        """Microsecond timestamps collide; insertion order still decides."""
        host = serve(_sync_graph("dbl"), home=home, deployment_version="v1")
        home.max_active_runs = 1
        for workflow_id in ("wf-a", "wf-b", "wf-c"):
            await host.submit("dbl", {"x": 1}, workflow_id=workflow_id)
        db = home._sync_db()  # force the exact tie created_at alone cannot resolve
        db.execute("UPDATE host_submissions SET created_at = '2026-01-01T00:00:00.000000+00:00'")
        db.commit()

        for expected in ("wf-a", "wf-b", "wf-c"):
            assert [row["workflow_id"] for row in await _claim(host, home)] == [expected]
            await home._release_submission(expected)

    async def test_over_limit_work_waits_pending_in_claim_order(self, home):
        """Cap=1 claims only the oldest; the rest stay pending, untouched."""
        host = serve(_sync_graph("dbl"), home=home, deployment_version="v1")
        home.max_active_runs = 1
        for workflow_id in ("wf-a", "wf-b", "wf-c"):
            await host.submit("dbl", {"x": 1}, workflow_id=workflow_id)

        claimed = await _claim(host, home)

        assert [row["workflow_id"] for row in claimed] == ["wf-a"]
        assert _states(home) == {"wf-a": "claimed", "wf-b": "pending", "wf-c": "pending"}
        # Never rejected, never cancelled: no runs row, no command, no lost row.
        assert home.get_run("wf-b") is None
        assert home._sync_db().execute("SELECT COUNT(*) FROM host_commands").fetchone()[0] == 0

    async def test_raising_the_cap_while_work_is_queued_admits_in_claim_order(self, home):
        """PRD 0017: change the cap while work is queued; order is preserved."""
        host = serve(_sync_graph("dbl"), home=home, deployment_version="v1")
        home.max_active_runs = 1
        for workflow_id in ("wf-a", "wf-b", "wf-c"):
            await host.submit("dbl", {"x": 1}, workflow_id=workflow_id)
        assert [row["workflow_id"] for row in await _claim(host, home)] == ["wf-a"]

        home.max_active_runs = 3  # tuned with wf-a still claimed and holding a slot

        assert [row["workflow_id"] for row in await _claim(host, home)] == ["wf-b", "wf-c"]
        assert set(_states(home).values()) == {"claimed"}

    async def test_lowering_the_cap_stops_new_claims_without_touching_running_work(self, home):
        host = serve(_sync_graph("dbl"), home=home, deployment_version="v1")
        home.max_active_runs = 2
        for workflow_id in ("wf-a", "wf-b", "wf-c"):
            await host.submit("dbl", {"x": 1}, workflow_id=workflow_id)
        assert [row["workflow_id"] for row in await _claim(host, home)] == ["wf-a", "wf-b"]

        home.max_active_runs = 1  # below the work already outstanding

        assert await _claim(host, home) == []
        assert _states(home) == {"wf-a": "claimed", "wf-b": "claimed", "wf-c": "pending"}
        # The over-subscribed claims are never revoked; wf-c waits its turn.
        await home._release_submission("wf-a")
        assert await _claim(host, home) == []  # one claim still outstanding at cap 1
        await home._release_submission("wf-b")
        assert [row["workflow_id"] for row in await _claim(host, home)] == ["wf-c"]

    async def test_waiting_view_names_the_cap_and_flips_back_when_a_slot_frees(self, home):
        host = serve(_sync_graph("dbl"), home=home, deployment_version="v1")
        client = host.client
        home.max_active_runs = 1
        first = await host.submit("dbl", {"x": 1}, workflow_id="wf-a")
        second = await host.submit("dbl", {"x": 1}, workflow_id="wf-b")
        # Uncapped-so-far: both are plainly queued.
        assert (await client.get(second.run_ref)).waiting is WaitingCondition.QUEUED

        await _claim(host, home)

        assert (await client.get(first.run_ref)).waiting is WaitingCondition.QUEUED  # claimed, holds the slot
        assert (await client.get(second.run_ref)).waiting is WaitingCondition.ADMISSION_LIMITED
        assert [view.workflow_id for view in await client.list(RunQuery(waiting=WaitingCondition.ADMISSION_LIMITED))] == ["wf-b"]

        await home._release_submission("wf-a")

        assert (await client.get(second.run_ref)).waiting is WaitingCondition.QUEUED
        assert await client.list(RunQuery(waiting=WaitingCondition.ADMISSION_LIMITED)) == []

    async def test_uncapped_home_never_reports_admission_limited(self, home):
        host = serve(_sync_graph("dbl"), home=home, deployment_version="v1")
        for workflow_id in ("wf-a", "wf-b", "wf-c"):
            await host.submit("dbl", {"x": 1}, workflow_id=workflow_id)
        assert len(await _claim(host, home)) == 3
        assert await host.client.list(RunQuery(waiting=WaitingCondition.ADMISSION_LIMITED)) == []

    async def test_sync_and_async_view_mirrors_agree_on_the_cap(self, home):
        host = serve(_sync_graph("dbl"), home=home, deployment_version="v1")
        client = host.client
        home.max_active_runs = 1
        await host.submit("dbl", {"x": 1}, workflow_id="wf-a")
        second = await host.submit("dbl", {"x": 1}, workflow_id="wf-b")
        await _claim(host, home)

        assert client.get_sync(second.run_ref).waiting is WaitingCondition.ADMISSION_LIMITED
        assert (await client.get(second.run_ref)).waiting is WaitingCondition.ADMISSION_LIMITED
        limited = RunQuery(waiting=WaitingCondition.ADMISSION_LIMITED)
        assert [view.workflow_id for view in client.list_sync(limited)] == ["wf-b"]
        assert [view.workflow_id for view in await client.list(limited)] == ["wf-b"]

    async def test_cap_is_tuned_while_a_live_worker_holds_queued_work(self, tmp_path, home):
        """The named PRD 0017 test: raise the cap on a running worker."""
        started: dict[int, asyncio.Event] = {1: asyncio.Event(), 2: asyncio.Event()}
        gate = asyncio.Event()
        host = serve(_gated_async_graph("gated", started, gate), home=home, deployment_version="v1")
        home.max_active_runs = 1
        first = await host.submit("gated", {"x": 1}, workflow_id="wf-a")
        second = await host.submit("gated", {"x": 2}, workflow_id="wf-b")

        async with _worker(host):
            await asyncio.wait_for(started[1].wait(), timeout=15)
            # wf-b is over the cap: still pending, and the view says why.
            await _wait_for(lambda: _admission_limited(host.client, second.run_ref))
            assert _state(home, "wf-b") == "pending"
            assert not started[2].is_set()

            home.max_active_runs = 2  # tuned live, work already queued

            await asyncio.wait_for(started[2].wait(), timeout=15)
            gate.set()
            await _wait_for(lambda: _terminal(host.client, first.run_ref))
            await _wait_for(lambda: _terminal(host.client, second.run_ref))

        assert (await host.client.get(first.run_ref)).status is WorkflowStatus.COMPLETED
        assert (await host.client.get(second.run_ref)).status is WorkflowStatus.COMPLETED


async def _admission_limited(client, ref) -> bool:
    view = await client.get(ref)
    return view is not None and view.waiting is WaitingCondition.ADMISSION_LIMITED


async def _terminal(client, ref):
    view = await client.get(ref)
    return view is not None and view.status in {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.STOPPED}


# === 2. What does and does not consume an active slot ===


class TestSlotAccounting:
    async def _park_one_of_each(self, home) -> dict[str, str]:
        """Manufacture one honestly-parked Run per non-slot-holding condition."""
        host = serve(_sync_graph("dbl"), home=home, deployment_version="v1")
        await host.submit("dbl", {"x": 1}, workflow_id="wf-sched", start_at=_future_iso(days=1))
        await host.submit("dbl", {"x": 1}, workflow_id="wf-exh", recovery_cap=1)
        db = home._sync_db()
        db.execute("UPDATE host_submissions SET state = 'claimed' WHERE workflow_id = 'wf-exh'")
        db.commit()
        await home._restart_scan()  # progressless re-adoption at cap 1 parks it
        assert _state(home, "wf-exh") == "exhausted"

        # Version-incompatible: a worker that cannot serve the pinned
        # identity refuses it at the claim choke point.
        await host.submit("dbl", {"x": 1}, workflow_id="wf-incomp")
        other = serve(_sync_graph("dbl"), home=home, deployment_version="v2")
        assert await _claim(other, home) == []
        assert home._get_submission_sync("wf-incomp")["compat_state"] == "incompatible"
        return {"host": host}

    async def test_delayed_incompatible_and_exhausted_runs_hold_no_slot(self, home):
        parked = await self._park_one_of_each(home)
        host = parked["host"]
        home.max_active_runs = 1

        # Three parked Runs and a cap of one: a fresh submission still claims.
        assert await home._admission_is_full() is False
        await host.submit("dbl", {"x": 9}, workflow_id="wf-new")
        assert [row["workflow_id"] for row in await _claim(host, home)] == ["wf-new"]
        assert await home._admission_is_full() is True

    async def test_paused_run_holds_no_slot(self, tmp_path, home):
        """A durable pause frees the worker WITHOUT finishing the submission."""
        pytest.importorskip("aiosqlite")
        from tests._interrupt_questions import StringQuestion

        @node(output_name="seed")
        async def seed(x: int) -> int:
            return x

        from hypergraph import interrupt

        @interrupt(answer_name="ans")
        def ask(seed: int) -> StringQuestion:
            return StringQuestion(prompt=f"continue with {seed}?")

        graph = Graph([seed, ask], name="pauser").with_runner(AsyncRunner())
        host = serve(graph, home=home, deployment_version="v1")
        home.max_active_runs = 1
        receipt = await host.submit("pauser", {"x": 1}, workflow_id="wf-paused")
        claimed = await _claim(host, home)
        await host._execute_submission(claimed[0])

        view = await host.client.get(receipt.run_ref)
        assert view.status is WorkflowStatus.PAUSED
        assert view.waiting is WaitingCondition.PAUSED
        # Parked, not finished: the worker released its slot, but a human
        # answer is still outstanding, so the submission must not read as
        # settled work (that made watch() end early and refused a stop).
        assert _state(home, "wf-paused") == "paused"
        assert home._get_submission_sync("wf-paused")["finished_at"] is None
        assert await home._admission_is_full() is False
        # ...and it is still not re-claimable: parking is not re-admission.
        assert await _claim(host, home) == []

    async def test_claimed_run_waiting_on_a_provider_permit_holds_its_slot(self, home):
        """The distinguishing case: throttled work is running work."""
        quota = ProcessLocalLimiter(max_in_flight=1)
        calls = {"n": 0}

        @node(output_name="out", provider_limit=quota)
        async def call_provider(x: int) -> int:
            calls["n"] += 1
            return x + 1

        graph = Graph([call_provider], name="provider").with_runner(AsyncRunner())
        host = serve(graph, home=home, deployment_version="v1")
        client = host.client
        home.max_active_runs = 1
        first = await host.submit("provider", {"x": 1}, workflow_id="wf-a")
        second = await host.submit("provider", {"x": 2}, workflow_id="wf-b")

        async with _PermitHolder(quota) as holder, _worker(host):
            # The component's only permit is taken elsewhere, so wf-a starts
            # executing and parks before its scarce call.
            await _wait_for(lambda: _executing(home, "wf-a"))
            assert calls["n"] == 0

            # It is running work: it holds the Host slot...
            assert await home._admission_is_full() is True
            assert _state(home, "wf-a") == "claimed"
            # ...and it is not failing, not exhausted, not retried.
            view_a = await client.get(first.run_ref)
            assert view_a.status is not WorkflowStatus.FAILED
            assert view_a.waiting is None
            assert home._get_submission_sync("wf-a")["recovery_attempts"] == 0
            # ...so wf-b is the one the cap holds back.
            assert (await client.get(second.run_ref)).waiting is WaitingCondition.ADMISSION_LIMITED

            await holder.give_back()  # releasing the permit drains both

            await _wait_for(lambda: _terminal(client, first.run_ref))
            await _wait_for(lambda: _terminal(client, second.run_ref))

        assert calls["n"] == 2
        assert (await client.get(first.run_ref)).status is WorkflowStatus.COMPLETED
        assert (await client.get(second.run_ref)).status is WorkflowStatus.COMPLETED
        assert _states(home) == {"wf-a": "finished", "wf-b": "finished"}


async def _claimed(home, workflow_id: str) -> bool:
    return _state(home, workflow_id) == "claimed"


async def _executing(home, workflow_id: str) -> bool:
    """Claimed AND actually started: the runs row exists.

    ``state == 'claimed'`` flips in the claim scan, before the runner has
    created anything — waiting on the runs row is what makes "this Run is
    executing and parked on a permit" a settled fact rather than a race.
    """
    return _state(home, workflow_id) == "claimed" and await home.get_run_async(workflow_id) is not None


class _PermitHolder:
    """Holds a limiter permit from a side task, through the public surface.

    An async context manager so a failing assertion still hands the permit
    back — otherwise a parked node would outlive the test and the shutdown
    drain would report a timeout instead of the real failure.
    """

    def __init__(self, limiter: ProcessLocalLimiter) -> None:
        self._limiter = limiter
        self._taken = asyncio.Event()
        self._give_back = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "_PermitHolder":
        async def hold() -> None:
            async with self._limiter:
                self._taken.set()
                await self._give_back.wait()

        self._task = asyncio.create_task(hold())
        await asyncio.wait_for(self._taken.wait(), timeout=10)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.give_back()

    async def give_back(self) -> None:
        """Release the held permit (idempotent)."""
        self._give_back.set()
        assert self._task is not None
        await asyncio.wait_for(self._task, timeout=10)


# === 3. Provider limiters: scopes, and permits that are not failures ===


class TestProviderLimiterPrimitive:
    @pytest.mark.parametrize("bad", [0, -1, True, False, 1.5, "1"])
    def test_rejects_anything_but_a_positive_int(self, bad):
        with pytest.raises(ValueError, match="max_in_flight"):
            ProcessLocalLimiter(bad)

    def test_name_carries_the_scope_and_no_distributed_variant_is_offered(self):
        """Honest scope names: this tier ships process-local coordination only."""
        import hypergraph

        assert "ProcessLocalLimiter" in hypergraph.__all__
        assert [name for name in hypergraph.__all__ if "Distributed" in name or "Global" in name] == []
        assert "process" in ProcessLocalLimiter.__doc__.lower()
        assert "not a distributed limiter" in " ".join(ProcessLocalLimiter.__doc__.split())

    def test_sync_context_manager_takes_and_returns_one_permit(self):
        limiter = ProcessLocalLimiter(max_in_flight=2)
        assert limiter.in_flight == 0
        with limiter:
            assert limiter.in_flight == 1
            with limiter:
                assert limiter.in_flight == 2
        assert limiter.in_flight == 0
        assert repr(limiter) == "ProcessLocalLimiter(max_in_flight=2, in_flight=0)"

    async def test_async_waiters_are_served_in_arrival_order(self):
        limiter = ProcessLocalLimiter(max_in_flight=1)
        order: list[int] = []
        release = asyncio.Event()

        async def take(index: int) -> None:
            async with limiter:
                order.append(index)
                await release.wait()

        holder = asyncio.create_task(take(0))
        await _wait_for(lambda: _has(order, 1))
        waiters = [asyncio.create_task(take(index)) for index in (1, 2, 3)]
        await asyncio.sleep(0)  # let every waiter queue up
        release.set()
        await asyncio.gather(holder, *waiters)

        assert order == [0, 1, 2, 3]
        assert limiter.in_flight == 0

    async def test_a_queued_thread_is_not_starved_by_arriving_async_waiters(self):
        """One arrival-ordered queue: the thread at the head wins the next permit.

        Async arrivals never stop coming here, so an async-first release would
        park the thread forever rather than merely delaying it.
        """
        limiter = ProcessLocalLimiter(max_in_flight=1)
        served = threading.Event()
        let_go = threading.Event()
        stop_churn = asyncio.Event()

        def sync_taker() -> None:
            with limiter:
                served.set()
                let_go.wait(15)

        async def churn() -> None:
            while not stop_churn.is_set():
                async with limiter:
                    await asyncio.sleep(0)

        thread = threading.Thread(target=sync_taker, daemon=True)
        churners: list[asyncio.Task] = []
        async with limiter:  # the one permit is held right here
            thread.start()
            await _wait_for(lambda: _queued(limiter, 1))  # the thread is the head
            churners = [asyncio.create_task(churn()) for _ in range(4)]
            await _wait_for(lambda: _queued(limiter, 5))

        try:
            assert await asyncio.to_thread(served.wait, 15) is True
        finally:
            stop_churn.set()
            let_go.set()
            await asyncio.gather(*churners)
            await asyncio.to_thread(thread.join, 15)

        assert limiter.in_flight == 0

    async def test_blocking_on_a_permit_from_an_event_loop_thread_raises(self):
        """A wait that could never end is reported, not entered."""
        limiter = ProcessLocalLimiter(max_in_flight=1)

        async with limiter:  # the permit is held by a task on THIS loop
            with pytest.raises(RuntimeError, match="How to fix"):
                with limiter:  # noqa: SIM117 - the point is that it raises
                    pass

        # Uncontended is not a wait, so a loop thread may still take one.
        with limiter:
            assert limiter.in_flight == 1
        assert limiter.in_flight == 0

    async def test_a_sync_node_that_blocks_on_a_permit_under_asyncrunner_says_so(self):
        """The documented trap: sync callables run inline on the loop thread."""
        quota = ProcessLocalLimiter(max_in_flight=1)

        @node(output_name="out")
        def call_provider(x: int) -> int:
            with quota:  # component-scope pattern, but from a SYNC node
                return x + 1

        graph = Graph([call_provider], name="loopblock")
        async with quota:
            with pytest.raises(Exception) as excinfo:  # noqa: B017 - the chain is the assertion
                await AsyncRunner().run(graph, {"x": 1})

        chain: list[str] = []
        error: BaseException | None = excinfo.value
        while error is not None:
            chain.append(str(error))
            error = error.__cause__
        assert any("running an event loop" in text for text in chain), chain

    async def test_a_cancelled_waiter_never_leaks_its_permit(self):
        limiter = ProcessLocalLimiter(max_in_flight=1)
        release = asyncio.Event()

        async def take() -> None:
            async with limiter:
                await release.wait()

        holder = asyncio.create_task(take())
        await _wait_for(lambda: _has_flight(limiter, 1))
        waiter = asyncio.create_task(take())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        await holder

        assert limiter.in_flight == 0
        async with limiter:  # the pool is intact
            assert limiter.in_flight == 1

    def test_the_same_limiter_at_two_scopes_yields_one_permit(self):
        shared = ProcessLocalLimiter(max_in_flight=1)
        other = ProcessLocalLimiter(max_in_flight=1)
        outer = ProcessLocalLimiter(max_in_flight=1)
        assert provider_permits((shared,), shared) == (shared,)
        assert provider_permits((), other) == (other,)
        assert provider_permits((), None) == ()
        # Every unique limiter is taken in ONE global order (construction
        # rank), not in scope order: scope order is per-path, so two legal
        # graphs naming the same pair at opposite scopes could deadlock.
        assert provider_permits((shared,), other) == (shared, other)
        assert provider_permits((outer, shared), other) == (shared, other, outer)
        # Deduplication is unchanged: one limiter at two scopes is one permit.
        assert provider_permits((outer, shared), outer) == (shared, outer)

    def test_a_nested_graph_never_re_enters_a_budget_its_parent_holds(self):
        """compose_graph_limits is what keeps a shared budget from deadlocking."""
        shared = ProcessLocalLimiter(max_in_flight=1)
        inner = ProcessLocalLimiter(max_in_flight=1)

        assert compose_graph_limits(None) == ()
        token = push_graph_limits((shared,))
        try:
            assert compose_graph_limits(None) is current_graph_limits()  # nothing to push
            assert compose_graph_limits(shared) is current_graph_limits()  # already held
            assert compose_graph_limits(inner) == (shared, inner)
        finally:
            pop_graph_limits(token)
        assert current_graph_limits() == ()


async def _has(values: list, count: int) -> bool:
    return len(values) >= count


async def _has_flight(limiter, count: int) -> bool:
    return limiter.in_flight >= count


async def _queued(limiter, count: int) -> bool:
    """Waiters parked on the limiter, whatever kind they are."""
    return len(limiter._waiters) >= count


class TestProviderLimiterScopes:
    async def test_node_scope_caps_concurrent_executions_of_that_node(self):
        quota = ProcessLocalLimiter(max_in_flight=1)
        peak = _PeakTracker()

        @node(output_name="a", provider_limit=quota)
        async def left(x: int) -> int:
            return await peak.observe_async(x)

        @node(output_name="b", provider_limit=quota)
        async def right(x: int) -> int:
            return await peak.observe_async(x)

        graph = Graph([left, right], name="fanout")
        result = await AsyncRunner().run(graph, {"x": 1})

        assert result["a"] == 1 and result["b"] == 1
        assert peak.peak == 1

    async def test_without_a_limit_the_same_nodes_really_do_overlap(self):
        """Control: the cap above is the limiter, not the scheduler."""
        peak = _PeakTracker()

        @node(output_name="a")
        async def left(x: int) -> int:
            return await peak.observe_async(x)

        @node(output_name="b")
        async def right(x: int) -> int:
            return await peak.observe_async(x)

        await AsyncRunner().run(Graph([left, right], name="fanout"), {"x": 1})

        assert peak.peak == 2

    async def test_graph_scope_is_one_budget_shared_by_concurrent_runs(self):
        """What max_concurrency (a per-call budget) cannot express."""
        quota = ProcessLocalLimiter(max_in_flight=1)
        peak = _PeakTracker()

        @node(output_name="out")
        async def work(x: int) -> int:
            return await peak.observe_async(x)

        graph = Graph([work], name="shared").with_provider_limit(quota)
        runner = AsyncRunner()
        await asyncio.gather(runner.run(graph, {"x": 1}), runner.run(graph, {"x": 2}))

        assert peak.peak == 1
        assert quota.in_flight == 0

    async def test_a_nested_graph_inherits_the_parent_graph_budget(self):
        """Nested must match flat: as_node() never drops a node out of the budget."""

        def _nested(limit):
            peak = _PeakTracker()

            @node(output_name="a")
            async def left(x: int) -> int:
                return await peak.observe_async(x)

            @node(output_name="b")
            async def right(x: int) -> int:
                return await peak.observe_async(x)

            inner = Graph([left, right], name="inner")
            outer = Graph([inner.as_node(name="inner")], name="outer")
            return (outer if limit is None else outer.with_provider_limit(limit)), peak

        control, control_peak = _nested(None)
        await AsyncRunner().run(control, {"x": 1})
        assert control_peak.peak == 2  # control: nested siblings really do overlap

        quota = ProcessLocalLimiter(max_in_flight=1)
        limited, limited_peak = _nested(quota)
        await AsyncRunner().run(limited, {"x": 1})

        assert limited_peak.peak == 1  # the parent's budget reached inside as_node()
        assert quota.in_flight == 0

    async def test_a_nested_graphs_own_budget_composes_under_the_parents(self):
        outer_budget = ProcessLocalLimiter(max_in_flight=2)
        inner_budget = ProcessLocalLimiter(max_in_flight=1)
        held: list[tuple[int, int]] = []

        @node(output_name="a")
        async def left(x: int) -> int:
            held.append((outer_budget.in_flight, inner_budget.in_flight))
            await asyncio.sleep(0)
            return x

        @node(output_name="b")
        async def right(x: int) -> int:
            held.append((outer_budget.in_flight, inner_budget.in_flight))
            await asyncio.sleep(0)
            return x

        inner = Graph([left, right], name="inner").with_provider_limit(inner_budget)
        outer = Graph([inner.as_node(name="inner")], name="outer").with_provider_limit(outer_budget)

        await AsyncRunner().run(outer, {"x": 1})

        # Both budgets held for each node, and the narrower one serialized them.
        assert held == [(1, 1), (1, 1)]
        assert outer_budget.in_flight == 0 and inner_budget.in_flight == 0

    def test_sync_nested_graph_inherits_the_parent_graph_budget(self):
        """Sync mirror of the nested inheritance."""
        quota = ProcessLocalLimiter(max_in_flight=1)
        seen: list[int] = []

        @node(output_name="out")
        def work(x: int) -> int:
            seen.append(quota.in_flight)
            return x + 1

        inner = Graph([work], name="inner")
        outer = Graph([inner.as_node(name="inner")], name="outer").with_provider_limit(quota)

        result = SyncRunner().run(outer, {"x": 1})

        assert result["out"] == 2
        assert seen == [1]  # the permit was held inside the nested graph
        assert quota.in_flight == 0

    async def test_component_scope_holds_the_permit_only_at_the_scarce_call(self):
        """The preferred owner of a provider quota: the shared component."""
        client = _ProviderClient(max_in_flight=1)
        inside_together = _PeakTracker()

        @node(output_name="a")
        async def alpha(x: int) -> str:
            inside_together.enter()  # both nodes are inside their node bodies
            await asyncio.sleep(0)
            value = await client.call(f"a{x}")
            inside_together.exit()
            return value

        @node(output_name="b")
        async def beta(x: int) -> str:
            inside_together.enter()
            await asyncio.sleep(0)
            value = await client.call(f"b{x}")
            inside_together.exit()
            return value

        await AsyncRunner().run(Graph([alpha, beta], name="component"), {"x": 1})

        # Node bodies overlapped freely; only the provider call was serialized.
        assert inside_together.peak == 2
        assert client.peak_in_call == 1
        assert client.calls == ["a1", "b1"]

    async def test_graph_and_node_budgets_compose_as_narrower_limits(self):
        graph_budget = ProcessLocalLimiter(max_in_flight=2)
        node_budget = ProcessLocalLimiter(max_in_flight=1)
        graph_peak = _PeakTracker()
        node_peak = _PeakTracker()

        @node(output_name="a", provider_limit=node_budget)
        async def narrow_left(x: int) -> int:
            graph_peak.enter()
            node_peak.enter()
            await asyncio.sleep(0)
            node_peak.exit()
            graph_peak.exit()
            return x

        @node(output_name="b", provider_limit=node_budget)
        async def narrow_right(x: int) -> int:
            graph_peak.enter()
            node_peak.enter()
            await asyncio.sleep(0)
            node_peak.exit()
            graph_peak.exit()
            return x

        graph = Graph([narrow_left, narrow_right], name="compose").with_provider_limit(graph_budget)
        await AsyncRunner().run(graph, {"x": 1})

        assert node_peak.peak == 1  # the narrower budget wins
        assert graph_budget.in_flight == 0 and node_budget.in_flight == 0

    def test_sync_runner_takes_and_releases_the_same_permit(self):
        quota = ProcessLocalLimiter(max_in_flight=1)
        seen: list[int] = []

        @node(output_name="out", provider_limit=quota)
        def work(x: int) -> int:
            seen.append(quota.in_flight)
            return x + 1

        result = SyncRunner().run(Graph([work], name="syncnode"), {"x": 1})

        assert result["out"] == 2
        assert seen == [1]
        assert quota.in_flight == 0

    def test_sync_runner_blocks_until_a_permit_frees(self):
        """Sync mirror of the async wait: same one pool, no second budget."""
        quota = ProcessLocalLimiter(max_in_flight=1)
        entered = threading.Event()

        @node(output_name="out", provider_limit=quota)
        def work(x: int) -> int:
            entered.set()
            return x + 1

        graph = Graph([work], name="syncblock")

        # Control: with the permit free, the node runs promptly.
        SyncRunner().run(graph, {"x": 1})
        assert entered.wait(5) is True
        entered.clear()

        with quota:
            results: list[int] = []
            worker = threading.Thread(target=lambda: results.append(SyncRunner().run(graph, {"x": 5})["out"]))
            worker.start()
            assert entered.wait(0.1) is False  # blocked on the permit, not running
        worker.join(timeout=10)

        assert entered.is_set() is True
        assert results == [6]
        assert quota.in_flight == 0

    def test_graph_provider_limit_is_metadata_only(self):
        quota = ProcessLocalLimiter(max_in_flight=1)

        @node(output_name="out")
        def work(x: int) -> int:
            return x

        graph = Graph([work], name="meta")
        limited = graph.with_provider_limit(quota)

        assert graph.provider_limit is None  # immutable: the original is untouched
        assert limited.provider_limit is quota
        assert limited.structural_hash == graph.structural_hash  # identity unchanged
        assert limited.definition_hash == graph.definition_hash
        with pytest.raises(TypeError, match="ProcessLocalLimiter"):
            graph.with_provider_limit(2)

    def test_node_provider_limit_is_typed_and_readable(self):
        quota = ProcessLocalLimiter(max_in_flight=1)

        @node(output_name="out", provider_limit=quota)
        def gated(x: int) -> int:
            return x

        @node(output_name="out")
        def plain(x: int) -> int:
            return x

        assert gated.provider_limit is quota
        assert plain.provider_limit is None
        assert gated.structural_signature == plain.structural_signature.replace("plain", "gated")
        assert gated(3) == 3  # a direct call stays raw: no permit taken
        assert quota.in_flight == 0
        with pytest.raises(TypeError, match="provider_limit must be a ProcessLocalLimiter"):

            @node(output_name="out", provider_limit=object())
            def bad(x: int) -> int:
                return x


class _PeakTracker:
    """Highest observed overlap of a region (single-threaded async use)."""

    def __init__(self) -> None:
        self.current = 0
        self.peak = 0

    def enter(self) -> None:
        self.current += 1
        self.peak = max(self.peak, self.current)

    def exit(self) -> None:
        self.current -= 1

    async def observe_async(self, value: int) -> int:
        self.enter()
        for _ in range(3):  # yield: a co-scheduled node would overlap here
            await asyncio.sleep(0)
        self.exit()
        return value


class _ProviderClient:
    """A shared component that owns its provider quota (canon's preferred owner)."""

    def __init__(self, max_in_flight: int) -> None:
        self._quota = ProcessLocalLimiter(max_in_flight=max_in_flight)
        self.calls: list[str] = []
        self._in_call = 0
        self.peak_in_call = 0

    async def call(self, payload: str) -> str:
        async with self._quota:  # acquired at the exact scarce call
            self._in_call += 1
            self.peak_in_call = max(self.peak_in_call, self._in_call)
            for _ in range(3):
                await asyncio.sleep(0)
            self.calls.append(payload)
            self._in_call -= 1
            return f"ok:{payload}"


class _FailureEventRecorder(EventProcessor):
    """Every event a permit wait must NOT produce."""

    def __init__(self) -> None:
        self.failures: list[object] = []

    def on_event(self, event) -> None:
        if (
            isinstance(event, NodeErrorEvent)
            or isinstance(event, NodeAttemptEndEvent)
            and (event.outcome != "succeeded" or event.retry_scheduled)
            or isinstance(event, RunEndEvent)
            and event.error is not None
        ):
            self.failures.append(event)


class TestPermitWaitIsNeitherFailureNorRetry:
    async def test_async_permit_wait_spends_no_retry_attempt(self, home):
        quota = ProcessLocalLimiter(max_in_flight=1)
        calls = {"n": 0}
        events = _FailureEventRecorder()

        @node(
            output_name="out",
            provider_limit=quota,
            retry=RetryPolicy(max_attempts=3, retry_on=(ValueError,)),
        )
        async def call_provider(x: int) -> int:
            calls["n"] += 1
            return x + 1

        graph = Graph([call_provider], name="retrying").with_processors(events).with_runner(AsyncRunner())
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await host.submit("retrying", {"x": 1}, workflow_id="wf-retry")

        async with _PermitHolder(quota) as holder, _worker(host):
            await _wait_for(lambda: _executing(home, "wf-retry"))
            assert calls["n"] == 0
            assert events.failures == []  # parked on a permit is not failing
            await holder.give_back()
            await _wait_for(lambda: _terminal(host.client, receipt.run_ref))

        view = await host.client.get(receipt.run_ref)
        assert view.status is WorkflowStatus.COMPLETED
        assert view.waiting is None
        assert calls["n"] == 1
        assert _attempt_count(home, "wf-retry") == 1  # the wait was not an attempt
        assert events.failures == []  # ...and it dispatched no failure event either
        submission = home._get_submission_sync("wf-retry")
        assert submission["state"] == "finished"
        assert submission["recovery_attempts"] == 0

    async def test_sync_permit_wait_spends_no_retry_attempt(self, home):
        """Sync mirror, driven straight through SyncRunner + checkpointer."""
        quota = ProcessLocalLimiter(max_in_flight=1)
        calls = {"n": 0}
        entered = threading.Event()
        events = _FailureEventRecorder()

        @node(
            output_name="out",
            provider_limit=quota,
            retry=RetryPolicy(max_attempts=3, retry_on=(ValueError,)),
        )
        def call_provider(x: int) -> int:
            calls["n"] += 1
            entered.set()
            return x + 1

        graph = Graph([call_provider], name="retrying_sync").with_processors(events)
        runner = SyncRunner(checkpointer=home)
        results: list[int] = []
        with quota:
            worker = threading.Thread(target=lambda: results.append(runner.run(graph, {"x": 1}, workflow_id="wf-sync")["out"]))
            worker.start()
            assert entered.wait(0.1) is False  # blocked on the permit, no attempt spent
            assert events.failures == []
        worker.join(timeout=10)

        assert results == [2]
        assert calls["n"] == 1
        assert _attempt_count(home, "wf-sync") == 1
        assert events.failures == []  # no failure event for the wait
        assert home.get_run("wf-sync").status is WorkflowStatus.COMPLETED

    async def test_permit_wait_does_not_run_down_a_node_timeout(self):
        """The permit is taken outside the attempt, so the deadline is not burning."""
        quota = ProcessLocalLimiter(max_in_flight=1)

        @node(output_name="out", provider_limit=quota, timeout=0.05)
        async def call_provider(x: int) -> int:
            return x + 1

        graph = Graph([call_provider], name="timed")
        runner = AsyncRunner()
        async with quota:
            run = asyncio.create_task(runner.run(graph, {"x": 1}))
            await asyncio.sleep(0.2)  # far past the node timeout, permit still held
            assert not run.done()
        result = await asyncio.wait_for(run, timeout=10)

        assert result["out"] == 2


# === 4. Delayed start: persisted, fingerprinted, and eligible on time ===


class TestDelayedStart:
    async def test_future_start_persists_and_fingerprints_at_submission(self, home):
        host = serve(_sync_graph("dbl"), home=home, deployment_version="v1")
        start_at = _future_iso(days=1)
        receipt = await host.submit("dbl", {"x": 1}, workflow_id="wf-later", start_at=start_at)
        await host.submit("dbl", {"x": 1}, workflow_id="wf-now")

        later = home._get_submission_sync("wf-later")
        now = home._get_submission_sync("wf-now")
        assert later["start_at"] == start_at  # persisted before any execution
        assert now["start_at"] is None
        # start_at is part of the identity the submission dedupes on.
        assert later["fingerprint"] != now["fingerprint"]
        assert (await host.client.get(receipt.run_ref)).waiting is WaitingCondition.SCHEDULED
        assert [view.workflow_id for view in await host.client.list(RunQuery(waiting=WaitingCondition.SCHEDULED))] == ["wf-later"]

    async def test_future_work_is_not_claimable_until_store_time_reaches_it(self, home):
        host = serve(_sync_graph("dbl"), home=home, deployment_version="v1")
        await host.submit("dbl", {"x": 1}, workflow_id="wf-later", start_at="2999-01-01T00:00:00+00:00")

        assert await _claim(host, home) == []
        assert _state(home, "wf-later") == "pending"
        # Store-authoritative time decides: at a now past the start it claims.
        assert [row["workflow_id"] for row in await _claim(host, home, now="2999-01-02T00:00:00+00:00")] == ["wf-later"]

    async def test_past_start_time_is_immediately_eligible(self, home):
        host = serve(_sync_graph("dbl"), home=home, deployment_version="v1")
        receipt = await host.submit("dbl", {"x": 1}, workflow_id="wf-past", start_at=_past_iso(hours=1))

        view = await host.client.get(receipt.run_ref)
        assert view.waiting is WaitingCondition.QUEUED  # never SCHEDULED
        assert [row["workflow_id"] for row in await _claim(host, home)] == ["wf-past"]

    async def test_scheduled_work_holds_no_active_slot(self, home):
        host = serve(_sync_graph("dbl"), home=home, deployment_version="v1")
        home.max_active_runs = 1
        await host.submit("dbl", {"x": 1}, workflow_id="wf-later", start_at="2999-01-01T00:00:00+00:00")
        await host.submit("dbl", {"x": 1}, workflow_id="wf-now")

        assert [row["workflow_id"] for row in await _claim(host, home)] == ["wf-now"]
        assert await home._admission_is_full() is True

    def test_sync_submit_mirror_persists_the_same_start_at(self, home):
        host = serve(_sync_graph("dbl"), home=home, deployment_version="v1")
        start_at = _future_iso(days=1)
        receipt = host.submit_sync("dbl", {"x": 1}, workflow_id="wf-sync-later", start_at=start_at)

        assert home._get_submission_sync("wf-sync-later")["start_at"] == start_at
        assert host.client.get_sync(receipt.run_ref).waiting is WaitingCondition.SCHEDULED


class TestStopBeforeDue:
    async def test_stopping_future_work_prevents_execution(self, home):
        calls = {"n": 0}
        host = serve(_sync_graph("dbl", calls), home=home, deployment_version="v1")
        receipt = await host.submit("dbl", {"x": 1}, workflow_id="wf-later", start_at="2999-01-01T00:00:00+00:00")

        stop = await host.client.stop(receipt.run_ref, info={"reason": "recalled before it was due"})
        assert stop.verb == "stop"
        assert _state(home, "wf-later") == "pending"  # still scheduled, not yet due

        # Time arrives; the claim happens, the pre-run gate refuses to execute.
        claimed = await _claim(host, home, now="2999-01-02T00:00:00+00:00")
        assert [row["workflow_id"] for row in claimed] == ["wf-later"]
        await host._execute_submission(claimed[0])

        assert calls["n"] == 0  # the graph never ran
        assert home.get_run("wf-later") is None  # no runs row was invented
        assert _state(home, "wf-later") == "finished"
        applied = home._sync_db().execute("SELECT applied_at FROM host_commands WHERE run_id = 'wf-later'").fetchone()
        assert applied[0] is not None

    async def test_stopped_future_work_holds_no_slot_and_frees_the_worker(self, home):
        calls = {"n": 0}
        host = serve(_sync_graph("dbl", calls), home=home, deployment_version="v1")
        home.max_active_runs = 1
        receipt = await host.submit("dbl", {"x": 1}, workflow_id="wf-later", start_at="2999-01-01T00:00:00+00:00")
        await host.client.stop(receipt.run_ref)
        await host.submit("dbl", {"x": 2}, workflow_id="wf-other")

        assert [row["workflow_id"] for row in await _claim(host, home)] == ["wf-other"]
        assert calls["n"] == 0


# === 5. Excluded strategies stay absent ===


class TestExcludedOverflowStrategiesAreAbsent:
    """v1 waits in claim order — nothing rejects, cancels, or keys work."""

    FORBIDDEN = ("reject", "cancel_oldest", "cancel_newest", "overflow", "admission_key", "expression", "fairness", "priority")

    def test_no_admission_surface_offers_an_overflow_strategy(self):
        from hypergraph.host.host import Host

        surfaces = [
            RunHome.__init__,
            RunHome.open,
            serve,
            Host.submit,
            Host.submit_sync,
            Host.submit_batch,
            Host.submit_batch_sync,
            Host.work_forever,
            RunHomeClient.stop,
            RunHomeClient.rerun,
            Graph.with_provider_limit,
            ProcessLocalLimiter.__init__,
        ]
        for surface in surfaces:
            names = " ".join(inspect.signature(surface).parameters).lower()
            assert not [word for word in self.FORBIDDEN if word in names], surface

    def test_run_query_has_no_admission_key_or_priority_field(self):
        names = " ".join(RunQuery.__dataclass_fields__).lower()
        assert not [word for word in self.FORBIDDEN if word in names]

    def test_waiting_vocabulary_is_exactly_the_six_typed_conditions(self):
        assert [condition.value for condition in WaitingCondition] == [
            "queued",
            "scheduled",
            "paused",
            "version_incompatible",
            "admission_limited",
            "recovery_exhausted",
        ]
        assert WaitingCondition is _WaitingCondition

    #: Answer *validation* vocabulary (ticket 13). ``AnswerRejectedError``
    #: rejects one typed value against a pause slot's schema — it has nothing
    #: to do with admission, which never rejects work.
    PAUSE_SETTLEMENT_EXPORTS = frozenset({"AnswerRejectedError"})

    def test_no_public_export_names_an_excluded_strategy(self):
        import hypergraph
        import hypergraph.host as host_module

        for module in (hypergraph, host_module):
            exported = " ".join(name for name in module.__all__ if name not in self.PAUSE_SETTLEMENT_EXPORTS).lower()
            assert not [word for word in self.FORBIDDEN if word in exported], module.__name__

    async def test_over_limit_work_is_delayed_never_dropped(self, home):
        """The behavioral half of the exclusion: overload delays, it never drops."""
        host = serve(_sync_graph("dbl"), home=home, deployment_version="v1")
        home.max_active_runs = 1
        refs: list[RunRef] = []
        for index in range(4):
            refs.append((await host.submit("dbl", {"x": index}, workflow_id=f"wf-{index}")).run_ref)

        await _claim(host, home)
        views = {view.workflow_id: view for view in await host.client.list(RunQuery(limit=10))}

        assert len(views) == 4  # nothing was rejected at submit or dropped after
        assert [view.status for view in views.values()] == [None] * 4  # nothing cancelled
        assert {view.workflow_id for view in views.values() if view.waiting is WaitingCondition.ADMISSION_LIMITED} == {
            "wf-1",
            "wf-2",
            "wf-3",
        }
