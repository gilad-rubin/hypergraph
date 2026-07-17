"""Contract tests for the retry attempt coordinator (#230).

Assertion map (ticket red-green items + wave-A2 sharpened falsifiers):
    1   exhaustion truth                 test_exhaustion_reraises_exact_exception_and_ledger_truth
    2   ineligible = one invocation      test_eligibility_flip_controls_invocations
    3   failure then success             test_failure_then_success_runs_downstream_once
    4   persisted backoff, no redraw     test_kill_resume_honors_persisted_backoff (S3)
    5   RetryAfterError vs deadline      test_retry_after_beyond_window_ends_without_sleeping
    6   sync/async parity                every runner-driven test parametrizes both families
    7   cache hit consumes nothing       test_cache_hit_consumes_no_attempts
    8   -W error CI-equivalent           the full suite run (no test-local assertion)
    S1  fresh world                      test_fresh_world_sqlite_end_to_end
    S2  eligibility flip                 test_eligibility_flip_controls_invocations
    S4  concurrent series isolation      test_concurrent_nodes_have_isolated_series
    S5  control-flow immunity            test_keyboard_interrupt_passes_through_untouched,
                                         test_pause_execution_passes_through_untouched,
                                         test_stop_signal_does_not_consume_attempts,
                                         test_cancelled_error_passes_through_untouched
    S7  no-checkpointer truth            test_no_checkpointer_budget_is_process_local,
                                         test_no_checkpointer_backoff_still_honored
    S8  direct call raw                  lives in test_retry_policy.py

Repo flaky rule: no wall-clock assertions. Sleeps are intercepted via the
coordinator's _sleep_sync/_sleep_async seams; time is fixed via _utcnow.
"""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from hypergraph import (
    END,
    AsyncRunner,
    Graph,
    InMemoryCache,
    RetryAfterError,
    RetryPolicy,
    RunStatus,
    SyncRunner,
    node,
    route,
)
from hypergraph.checkpointers import AttemptStatus, MemoryCheckpointer, SqliteCheckpointer, StepStatus
from hypergraph.events import NodeEndEvent, NodeErrorEvent, NodeStartEvent
from hypergraph.events.processor import EventProcessor
from hypergraph.runners._shared import attempts as attempts_module

aiosqlite = pytest.importorskip("aiosqlite")


# === Helpers ===


def _policy(**overrides) -> RetryPolicy:
    kwargs = {
        "max_attempts": 3,
        "retry_on": (ConnectionError,),
        "initial_delay": 0.001,
        "jitter": "none",
    }
    kwargs.update(overrides)
    return RetryPolicy(**kwargs)


def _make_runner(family: str, **kwargs):
    return SyncRunner(**kwargs) if family == "sync" else AsyncRunner(**kwargs)


async def _run(runner, *args, **kwargs):
    result = runner.run(*args, **kwargs)
    if inspect.iscoroutine(result):
        result = await result
    return result


@pytest.fixture(params=["sync", "async"])
def family(request) -> str:
    return request.param


@pytest_asyncio.fixture
async def make_sqlite(tmp_path):
    created = []

    def factory(name: str = "retry.db") -> SqliteCheckpointer:
        cp = SqliteCheckpointer(str(tmp_path / name))
        created.append(cp)
        return cp

    yield factory
    for cp in created:
        await cp.close()


@pytest.fixture
def recorded_sleeps(monkeypatch):
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def fake_sleep_async(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(attempts_module, "_sleep_sync", fake_sleep)
    monkeypatch.setattr(attempts_module, "_sleep_async", fake_sleep_async)
    return sleeps


class _Recorder(EventProcessor):
    def __init__(self) -> None:
        self.events: list = []

    def on_event(self, event) -> None:
        self.events.append(event)


class _CountingCache(InMemoryCache):
    def __init__(self) -> None:
        super().__init__()
        self.set_calls = 0

    def set(self, key, value) -> None:
        self.set_calls += 1
        super().set(key, value)


def _series_rows(db_path, run_id: str) -> list[tuple]:
    with sqlite3.connect(str(db_path)) as db:
        return db.execute(
            "SELECT id, closed_at FROM attempt_series WHERE run_id = ?",
            (run_id,),
        ).fetchall()


async def _closed_series_for_step(cp, run_id: str, node_name: str):
    """Return (series, records) linked from the node's StepRecord."""
    steps = [s for s in await cp.get_steps(run_id) if s.node_name == node_name]
    assert len(steps) == 1, f"expected ONE logical step for {node_name}, got {len(steps)}"
    step = steps[0]
    assert step.attempt_series_id is not None, "StepRecord must link its attempt series"
    series = await cp.get_attempt_series(step.attempt_series_id)
    records = await cp.get_attempt_records(step.attempt_series_id)
    return step, series, records


# === Ticket 1: exhaustion truth ===


async def test_exhaustion_reraises_exact_exception_and_ledger_truth(family, make_sqlite, recorded_sleeps):
    cp = make_sqlite()
    calls: list[int] = []
    downstream_calls: list[int] = []
    final_error = ConnectionError("always down")

    @node(output_name="fetched", retry=_policy(max_attempts=3))
    def flaky(x: int) -> int:
        calls.append(x)
        raise final_error

    @node(output_name="done")
    def downstream(fetched: int) -> int:
        downstream_calls.append(fetched)
        return fetched

    runner = _make_runner(family, checkpointer=cp)
    with pytest.raises(ConnectionError) as exc_info:
        await _run(runner, Graph([flaky, downstream]), {"x": 1}, workflow_id="wf-exhaust")

    assert exc_info.value is final_error, "the exact final underlying exception must escape, unwrapped"
    assert calls == [1, 1, 1], "max_attempts=3 means exactly three invocations"
    assert downstream_calls == [], "downstream must never run"

    step, series, records = await _closed_series_for_step(cp, "wf-exhaust", "flaky")
    assert step.status is StepStatus.FAILED
    assert series is not None and not series.is_open, "the series must close with the failed step"
    assert [r.status for r in records] == [
        AttemptStatus.FAILED,
        AttemptStatus.FAILED,
        AttemptStatus.FAILED,
    ]
    assert records[0].retry_not_before is not None
    assert records[1].retry_not_before is not None
    assert records[2].retry_not_before is None, "no backoff is drawn for the final attempt"
    assert records[2].error is not None and records[2].error.type_name == "ConnectionError"
    # Two backoffs slept, none for the terminal attempt.
    assert len(recorded_sleeps) == 2


# === Ticket 2 + S2: eligibility flip ===


@pytest.mark.parametrize("eligible", [True, False])
async def test_eligibility_flip_controls_invocations(family, make_sqlite, recorded_sleeps, eligible):
    cp = make_sqlite()
    calls: list[int] = []
    retry_on = (KeyError,) if eligible else (ConnectionError,)

    @node(output_name="fetched", retry=_policy(max_attempts=3, retry_on=retry_on))
    def flaky(x: int) -> int:
        calls.append(x)
        raise KeyError("missing")

    runner = _make_runner(family, checkpointer=cp)
    with pytest.raises(KeyError):
        await _run(runner, Graph([flaky]), {"x": 1}, workflow_id="wf-flip")

    assert len(calls) == (3 if eligible else 1), "flipping retry_on must flip the invocation count"

    _, series, records = await _closed_series_for_step(cp, "wf-flip", "flaky")
    assert not series.is_open
    assert len(records) == (3 if eligible else 1)


# === Ticket 3: failure then success ===


async def test_failure_then_success_runs_downstream_once(family, make_sqlite, recorded_sleeps):
    cp = make_sqlite()
    calls: list[int] = []
    downstream_calls: list[int] = []

    @node(output_name="fetched", retry=_policy())
    def flaky(x: int) -> int:
        calls.append(x)
        if len(calls) == 1:
            raise ConnectionError("transient")
        return x * 10

    @node(output_name="done")
    def downstream(fetched: int) -> int:
        downstream_calls.append(fetched)
        return fetched + 1

    runner = _make_runner(family, checkpointer=cp)
    result = await _run(runner, Graph([flaky, downstream]), {"x": 1}, workflow_id="wf-recover")

    assert result["done"] == 11
    assert calls == [1, 1]
    assert downstream_calls == [10], "downstream runs exactly once, after final success"

    step, series, records = await _closed_series_for_step(cp, "wf-recover", "flaky")
    assert step.status is StepStatus.COMPLETED
    assert not series.is_open
    assert [r.status for r in records] == [AttemptStatus.FAILED, AttemptStatus.SUCCEEDED]


# === Ticket 4 + S3: kill/resume honors persisted backoff, no redraw ===


async def test_kill_resume_honors_persisted_backoff(family, make_sqlite, monkeypatch):
    class _SimulatedProcessDeath(BaseException):
        pass

    cp = make_sqlite()
    calls: list[int] = []
    frozen_now = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(attempts_module, "_utcnow", lambda: frozen_now)

    def dying_sleep(seconds: float) -> None:
        raise _SimulatedProcessDeath

    async def dying_sleep_async(seconds: float) -> None:
        raise _SimulatedProcessDeath

    monkeypatch.setattr(attempts_module, "_sleep_sync", dying_sleep)
    monkeypatch.setattr(attempts_module, "_sleep_async", dying_sleep_async)

    # x has a default so the "new process" can resume the SAME workflow_id
    # bare — the resume contract rejects re-supplying input values.
    @node(output_name="fetched", retry=_policy(max_attempts=3, initial_delay=3600.0, jitter="full"))
    def flaky(x: int = 1) -> int:
        calls.append(x)
        if len(calls) == 1:
            raise ConnectionError("transient")
        return x * 10

    graph = Graph([flaky])
    runner = _make_runner(family, checkpointer=cp)
    with pytest.raises(_SimulatedProcessDeath):
        await _run(runner, graph, workflow_id="wf-crash")

    assert calls == [1]
    series = await cp.get_open_attempt_series("wf-crash", "flaky")
    assert series is not None, "the series must survive the crash open"
    records_before = await cp.get_attempt_records(series.id)
    assert len(records_before) == 1
    first = records_before[0]
    assert first.status is AttemptStatus.FAILED
    assert first.sampled_delay is not None and 0.0 <= first.sampled_delay <= 3600.0
    persisted_wake = first.retry_not_before
    assert persisted_wake == frozen_now + timedelta(seconds=first.sampled_delay)

    # Resume in a "new process": later clock, recording sleep. The wait must be
    # derived from the PERSISTED wake time — never redrawn, never restarted.
    resumed_now = frozen_now + timedelta(seconds=1)
    monkeypatch.setattr(attempts_module, "_utcnow", lambda: resumed_now)
    resumed_sleeps: list[float] = []

    def recording_sleep(seconds: float) -> None:
        resumed_sleeps.append(seconds)

    async def recording_sleep_async(seconds: float) -> None:
        resumed_sleeps.append(seconds)

    monkeypatch.setattr(attempts_module, "_sleep_sync", recording_sleep)
    monkeypatch.setattr(attempts_module, "_sleep_async", recording_sleep_async)

    resumed_runner = _make_runner(family, checkpointer=make_sqlite())
    result = await _run(resumed_runner, graph, workflow_id="wf-crash")

    assert result["fetched"] == 10
    assert calls == [1, 1], "resume continues the same budget with attempt 2"
    assert resumed_sleeps == [(persisted_wake - resumed_now).total_seconds()]

    records_after = await cp.get_attempt_records(series.id)
    assert records_after[0].retry_not_before == persisted_wake, "persisted wake time must not be redrawn"
    assert records_after[0].sampled_delay == first.sampled_delay
    assert [r.status for r in records_after] == [AttemptStatus.FAILED, AttemptStatus.SUCCEEDED]
    assert (await cp.get_open_attempt_series("wf-crash", "flaky")) is None


# === Ticket 5: RetryAfterError bounded by the series window ===


async def test_retry_after_beyond_window_ends_without_sleeping(family, make_sqlite, recorded_sleeps):
    cp = make_sqlite()
    calls: list[int] = []
    underlying = ConnectionError("rate limited")

    @node(output_name="fetched", retry=_policy(max_attempts=5, retry_window=10.0))
    def flaky(x: int) -> int:
        calls.append(x)
        raise RetryAfterError(underlying, retry_after=30)

    runner = _make_runner(family, checkpointer=cp)
    with pytest.raises(ConnectionError) as exc_info:
        await _run(runner, Graph([flaky]), {"x": 1}, workflow_id="wf-window")

    assert exc_info.value is underlying, "the carrier must unwrap to the exact underlying exception"
    assert calls == [1]
    assert recorded_sleeps == [], "a wait that cannot fit before deadline_at must not sleep"

    _, series, records = await _closed_series_for_step(cp, "wf-window", "flaky")
    assert not series.is_open
    assert len(records) == 1
    assert records[0].status is AttemptStatus.FAILED
    assert records[0].error is not None and records[0].error.type_name == "ConnectionError"


async def test_retry_after_delay_is_persisted_exactly(family, make_sqlite, recorded_sleeps):
    cp = make_sqlite()
    calls: list[int] = []

    # max_delay far below the server delay: the server delay must NOT be capped or jittered.
    @node(output_name="fetched", retry=_policy(max_attempts=3, max_delay=0.001, jitter="full"))
    def flaky(x: int) -> int:
        calls.append(x)
        if len(calls) == 1:
            raise RetryAfterError(ConnectionError("rate limited"), retry_after=5)
        return x

    runner = _make_runner(family, checkpointer=cp)
    result = await _run(runner, Graph([flaky]), {"x": 7}, workflow_id="wf-after")

    assert result["fetched"] == 7
    assert recorded_sleeps == [5.0], "the server-supplied delay is honored exactly"

    _, _, records = await _closed_series_for_step(cp, "wf-after", "flaky")
    assert records[0].sampled_delay == 5.0
    assert records[0].retry_not_before is not None


# === Ticket 7: cache hit consumes nothing ===


async def test_cache_hit_consumes_no_attempts(family, make_sqlite, recorded_sleeps, tmp_path):
    cp = make_sqlite()
    cache = _CountingCache()
    calls: list[int] = []

    @node(output_name="fetched", cache=True, retry=_policy())
    def flaky(x: int) -> int:
        calls.append(x)
        if len(calls) == 1:
            raise ConnectionError("transient")
        return x * 10

    graph = Graph([flaky])
    runner = _make_runner(family, checkpointer=cp, cache=cache)

    first = await _run(runner, graph, {"x": 1}, workflow_id="wf-cache-1")
    assert first["fetched"] == 10
    assert calls == [1, 1]
    assert cache.set_calls == 1, "cache write happens once, after final success"

    second = await _run(runner, graph, {"x": 1}, workflow_id="wf-cache-2")
    assert second["fetched"] == 10
    assert calls == [1, 1], "a cache hit invokes nothing"
    assert cache.set_calls == 1

    assert _series_rows(tmp_path / "retry.db", "wf-cache-2") == [], "a cache hit opens no attempt series"
    _, series, records = await _closed_series_for_step(cp, "wf-cache-1", "flaky")
    assert not series.is_open
    assert len(records) == 2


# === S1: fresh world ===


async def test_fresh_world_sqlite_end_to_end(tmp_path):
    db_path = tmp_path / "fresh.db"
    calls: list[int] = []

    @node(output_name="fetched", retry=_policy(initial_delay=0.001, jitter="full"))
    def flaky(x: int) -> int:
        calls.append(x)
        if len(calls) == 1:
            raise ConnectionError("transient")
        return x * 10

    @node(output_name="done")
    def downstream(fetched: int) -> int:
        return fetched + 1

    runner = SyncRunner(checkpointer=SqliteCheckpointer(str(db_path)))
    result = runner.run(Graph([flaky, downstream]), {"x": 4}, workflow_id="wf-fresh")
    assert result["done"] == 41

    reopened = SqliteCheckpointer(str(db_path))
    try:
        step, series, records = await _closed_series_for_step(reopened, "wf-fresh", "flaky")
        assert step.status is StepStatus.COMPLETED
        assert series is not None and not series.is_open
        assert series.max_attempts == 3
        assert [r.status for r in records] == [AttemptStatus.FAILED, AttemptStatus.SUCCEEDED]
        assert records[0].sampled_delay is not None
        assert records[0].retry_not_before is not None
        downstream_steps = [s for s in await reopened.get_steps("wf-fresh") if s.node_name == "downstream"]
        assert len(downstream_steps) == 1
        assert downstream_steps[0].attempt_series_id is None
    finally:
        await reopened.close()


# === S4: concurrent nodes keep isolated series/budgets ===


async def test_concurrent_nodes_have_isolated_series(recorded_sleeps):
    cp = MemoryCheckpointer()
    a_started = asyncio.Event()
    b_started = asyncio.Event()
    a_calls: list[int] = []
    b_calls: list[int] = []

    @node(output_name="a_out", retry=_policy())
    async def worker_a(x: int) -> int:
        a_calls.append(x)
        a_started.set()
        await asyncio.wait_for(b_started.wait(), timeout=5)
        if len(a_calls) == 1:
            raise ConnectionError("a transient")
        return x + 1

    @node(output_name="b_out", retry=_policy())
    async def worker_b(x: int) -> int:
        b_calls.append(x)
        b_started.set()
        await asyncio.wait_for(a_started.wait(), timeout=5)
        if len(b_calls) == 1:
            raise ConnectionError("b transient")
        return x + 2

    runner = AsyncRunner(checkpointer=cp)
    result = await runner.run(Graph([worker_a, worker_b]), {"x": 1}, workflow_id="wf-pair")

    assert result["a_out"] == 2
    assert result["b_out"] == 3
    assert len(a_calls) == 2
    assert len(b_calls) == 2

    _, series_a, records_a = await _closed_series_for_step(cp, "wf-pair", "worker_a")
    _, series_b, records_b = await _closed_series_for_step(cp, "wf-pair", "worker_b")
    assert series_a.id != series_b.id
    assert [r.status for r in records_a] == [AttemptStatus.FAILED, AttemptStatus.SUCCEEDED]
    assert [r.status for r in records_b] == [AttemptStatus.FAILED, AttemptStatus.SUCCEEDED]
    assert [r.series_id for r in records_a] == [series_a.id, series_a.id]
    assert [r.series_id for r in records_b] == [series_b.id, series_b.id]


# === S5: control-flow immunity ===


async def test_keyboard_interrupt_passes_through_untouched(make_sqlite):
    cp = make_sqlite()
    calls: list[int] = []

    @node(output_name="fetched", retry=_policy(max_attempts=5))
    def flaky(x: int) -> int:
        calls.append(x)
        raise KeyboardInterrupt

    runner = SyncRunner(checkpointer=cp)
    with pytest.raises(KeyboardInterrupt):
        runner.run(Graph([flaky]), {"x": 1}, workflow_id="wf-kbd")

    assert calls == [1], "BaseException control flow is never retried"
    series = await cp.get_open_attempt_series("wf-kbd", "flaky")
    assert series is not None, "the series stays open for resume semantics"
    records = await cp.get_attempt_records(series.id)
    assert [r.status for r in records] == [AttemptStatus.STARTED], "no FAILED outcome is invented for control flow"


def test_pause_execution_passes_through_untouched():
    from hypergraph.runners._shared.results import PauseInfo
    from hypergraph.runners._shared.state import PauseExecution

    calls: list[int] = []
    policy = _policy(max_attempts=5)

    def invoke():
        calls.append(1)
        raise PauseExecution(PauseInfo(node_name="flaky", value="q", response_key="answer"))

    with pytest.raises(PauseExecution):
        attempts_module.run_attempts_sync(
            invoke,
            node_name="flaky",
            policy=policy,
            checkpointer=None,
            run_id=None,
            scheduled_superstep=0,
        )
    assert calls == [1], "PauseExecution must pass through without consuming attempts"


async def test_cancelled_error_passes_through_untouched():
    cp = MemoryCheckpointer()
    calls: list[int] = []
    second_attempt_running = asyncio.Event()
    release = asyncio.Event()

    @node(output_name="fetched", retry=_policy(max_attempts=3))
    async def flaky(x: int) -> int:
        calls.append(x)
        if len(calls) == 1:
            raise ConnectionError("transient")
        second_attempt_running.set()
        await release.wait()
        return x

    runner = AsyncRunner(checkpointer=cp)
    task = asyncio.create_task(runner.run(Graph([flaky]), {"x": 1}, workflow_id="wf-cancel"))
    await asyncio.wait_for(second_attempt_running.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == [1, 1]
    series = await cp.get_open_attempt_series("wf-cancel", "flaky")
    assert series is not None
    records = await cp.get_attempt_records(series.id)
    assert [r.status for r in records] == [
        AttemptStatus.FAILED,
        AttemptStatus.STARTED,
    ], "cancellation must not settle the running attempt as FAILED"


async def test_stop_signal_does_not_consume_attempts(recorded_sleeps):
    cp = MemoryCheckpointer()
    calls: list[int] = []
    first_failure_recorded = asyncio.Event()
    stop_requested = asyncio.Event()

    @node(output_name="fetched", retry=_policy(max_attempts=3))
    async def flaky(x: int) -> int:
        calls.append(x)
        if len(calls) == 1:
            first_failure_recorded.set()
            raise ConnectionError("transient")
        await asyncio.wait_for(stop_requested.wait(), timeout=5)
        return x * 10

    runner = AsyncRunner(checkpointer=cp)
    task = asyncio.create_task(runner.run(Graph([flaky]), {"x": 1}, workflow_id="wf-stop"))
    await asyncio.wait_for(first_failure_recorded.wait(), timeout=5)
    runner.stop("wf-stop")
    stop_requested.set()
    result = await task

    # Cooperative stop: the in-flight retry series completes its attempt.
    assert result.status is RunStatus.STOPPED
    assert calls == [1, 1], "stop consumed no extra attempts"
    _, series, records = await _closed_series_for_step(cp, "wf-stop", "flaky")
    assert not series.is_open
    assert [r.status for r in records] == [AttemptStatus.FAILED, AttemptStatus.SUCCEEDED]


# === S7: no-checkpointer truth ===


async def test_no_checkpointer_budget_is_process_local(family, recorded_sleeps):
    calls: list[int] = []
    final_error = ConnectionError("always down")

    @node(output_name="fetched", retry=_policy(max_attempts=3))
    def flaky(x: int) -> int:
        calls.append(x)
        raise final_error

    runner = _make_runner(family)
    with pytest.raises(ConnectionError) as exc_info:
        await _run(runner, Graph([flaky]), {"x": 1})

    assert exc_info.value is final_error
    assert calls == [1, 1, 1]
    assert len(recorded_sleeps) == 2


async def test_no_checkpointer_backoff_still_honored(family, recorded_sleeps):
    calls: list[int] = []

    @node(
        output_name="fetched",
        retry=_policy(max_attempts=4, initial_delay=1.0, backoff_multiplier=2.0, max_delay=60.0, jitter="full"),
    )
    def flaky(x: int) -> int:
        calls.append(x)
        if len(calls) < 3:
            raise ConnectionError("transient")
        return x

    runner = _make_runner(family)
    result = await _run(runner, Graph([flaky]), {"x": 1})

    assert result["fetched"] == 1
    assert calls == [1, 1, 1]
    assert len(recorded_sleeps) == 2
    # Full jitter samples uniformly in [0, nominal]; nominal doubles per attempt.
    assert 0.0 <= recorded_sleeps[0] <= 1.0
    assert 0.0 <= recorded_sleeps[1] <= 2.0


# === Events: intermediate attempts never disturb the logical node shape ===


@pytest.mark.parametrize("outcome", ["success", "exhausted"])
async def test_intermediate_attempts_do_not_disturb_event_shape(family, recorded_sleeps, outcome):
    calls: list[int] = []

    @node(output_name="fetched", retry=_policy(max_attempts=3))
    def flaky(x: int) -> int:
        calls.append(x)
        if outcome == "exhausted" or len(calls) == 1:
            raise ConnectionError("boom")
        return x

    recorder = _Recorder()
    runner = _make_runner(family)
    if outcome == "success":
        await _run(runner, Graph([flaky]), {"x": 1}, event_processors=[recorder])
    else:
        with pytest.raises(ConnectionError):
            await _run(runner, Graph([flaky]), {"x": 1}, event_processors=[recorder])

    starts = [e for e in recorder.events if isinstance(e, NodeStartEvent) and e.node_name == "flaky"]
    ends = [e for e in recorder.events if isinstance(e, NodeEndEvent) and e.node_name == "flaky"]
    errors = [e for e in recorder.events if isinstance(e, NodeErrorEvent) and e.node_name == "flaky"]
    assert len(starts) == 1, "one logical NodeStartEvent regardless of attempts"
    if outcome == "success":
        assert len(ends) == 1 and len(errors) == 0
    else:
        assert len(ends) == 0 and len(errors) == 1, "intermediate failures emit no NodeErrorEvent"
    assert not any(type(e).__name__.startswith("NodeAttempt") for e in recorder.events), "attempt events belong to a later ticket"


# === Composition: map items and nested graphs ===


async def test_map_items_own_separate_series(make_sqlite, recorded_sleeps):
    cp = make_sqlite()
    failed_once: set[int] = set()
    calls: list[int] = []

    @node(output_name="fetched", retry=_policy())
    def flaky(x: int) -> int:
        calls.append(x)
        if x not in failed_once:
            failed_once.add(x)
            raise ConnectionError("transient")
        return x * 10

    runner = SyncRunner(checkpointer=cp)
    result = runner.map(Graph([flaky]), {"x": [1, 2, 3]}, map_over="x", workflow_id="wf-map")

    assert [item["fetched"] for item in result] == [10, 20, 30]
    assert sorted(calls) == [1, 1, 2, 2, 3, 3], "each item retried independently"

    child_runs = await cp.list_runs(parent_run_id="wf-map")
    assert len(child_runs) == 3
    seen_series = set()
    for child in child_runs:
        _, series, records = await _closed_series_for_step(cp, child.id, "flaky")
        assert not series.is_open
        assert [r.status for r in records] == [AttemptStatus.FAILED, AttemptStatus.SUCCEEDED]
        seen_series.add(series.id)
    assert len(seen_series) == 3, "map items own separate attempt series"


async def test_nested_function_node_carries_policy(family, recorded_sleeps):
    calls: list[int] = []

    @node(output_name="fetched", retry=_policy())
    def flaky(x: int) -> int:
        calls.append(x)
        if len(calls) == 1:
            raise ConnectionError("transient")
        return x * 10

    inner = Graph([flaky], name="inner")
    outer = Graph([inner.as_node()])

    runner = _make_runner(family)
    result = await _run(runner, outer, {"x": 1})

    assert result["fetched"] == 10
    assert calls == [1, 1], "a nested FunctionNode carries its retry declaration"


# === Cycles: each logical execution owns a fresh series ===


async def test_cycle_reexecution_gets_fresh_series(make_sqlite, recorded_sleeps):
    cp = make_sqlite()
    calls: list[int] = []
    failed_for: set[int] = set()

    @node(output_name="count", retry=_policy())
    def worker(count: int) -> int:
        calls.append(count)
        if count not in failed_for:
            failed_for.add(count)
            raise ConnectionError("transient")
        return count + 1

    @route(targets=["worker", END])
    def keep_going(count: int) -> str:
        return END if count >= 2 else "worker"

    runner = SyncRunner(checkpointer=cp)
    result = runner.run(
        Graph([worker, keep_going], entrypoint="worker"),
        {"count": 0},
        workflow_id="wf-cycle",
    )

    assert result["count"] == 2
    # Two logical executions (0 -> 1 -> 2), each failing once then succeeding.
    assert calls == [0, 0, 1, 1]

    steps = [s for s in await cp.get_steps("wf-cycle") if s.node_name == "worker"]
    assert len(steps) == 2
    series_ids = {s.attempt_series_id for s in steps}
    assert None not in series_ids
    assert len(series_ids) == 2, "each logical execution owns its own closed series"
    for series_id in series_ids:
        series = await cp.get_attempt_series(series_id)
        assert series is not None and not series.is_open
        records = await cp.get_attempt_records(series_id)
        assert [r.status for r in records] == [AttemptStatus.FAILED, AttemptStatus.SUCCEEDED]


# === Backoff math (pure helpers) ===


class TestBackoffMath:
    def test_nominal_delay_formula(self):
        policy = _policy(initial_delay=1.0, backoff_multiplier=2.0, max_delay=60.0)
        assert attempts_module.nominal_delay(policy, 1) == 1.0
        assert attempts_module.nominal_delay(policy, 2) == 2.0
        assert attempts_module.nominal_delay(policy, 3) == 4.0
        assert attempts_module.nominal_delay(policy, 7) == 60.0, "nominal is capped by max_delay"

    def test_constant_delay_with_multiplier_one(self):
        policy = _policy(initial_delay=5.0, backoff_multiplier=1.0)
        assert attempts_module.nominal_delay(policy, 1) == 5.0
        assert attempts_module.nominal_delay(policy, 4) == 5.0

    def test_jitter_none_uses_nominal(self):
        policy = _policy(initial_delay=3.0, jitter="none")
        now = datetime.now(timezone.utc)
        decision = attempts_module.draw_backoff(policy, 1, now=now)
        assert decision.sampled_delay == 3.0
        assert decision.effective_delay == 3.0
        assert decision.retry_not_before == now + timedelta(seconds=3.0)

    def test_full_jitter_samples_within_nominal(self):
        policy = _policy(initial_delay=10.0, jitter="full")
        now = datetime.now(timezone.utc)
        for _ in range(50):
            decision = attempts_module.draw_backoff(policy, 1, now=now)
            assert 0.0 <= decision.sampled_delay <= 10.0
            assert decision.retry_not_before == now + timedelta(seconds=decision.sampled_delay)

    def test_server_delay_is_neither_jittered_nor_capped(self):
        policy = _policy(initial_delay=0.5, max_delay=1.0, jitter="full")
        now = datetime.now(timezone.utc)
        decision = attempts_module.draw_backoff(policy, 1, now=now, retry_after=30.0)
        assert decision.sampled_delay == 30.0
        assert decision.effective_delay == 30.0
        assert decision.retry_not_before == now + timedelta(seconds=30.0)
