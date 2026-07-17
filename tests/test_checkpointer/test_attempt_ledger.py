"""Contract tests for the durable attempt ledger (#229) — memory/sqlite parity.

Assertion map (validation contract, wave A):
    A1  fresh-world roundtrip           test_fresh_world_roundtrip
    A2  crash after reservation         test_crash_after_reservation_consumes_budget
    A3  crash before reservation        test_crash_before_reservation_consumes_nothing
    A4  atomic close falsifier          test_close_is_atomic_with_step_write
    A5  superstep drift                 test_resumed_reservation_continues_same_series
    A7  retention                       test_open_series_survives_retention,
                                        test_closed_series_follows_linked_step_retention
    A8  no contamination                test_attempt_rows_do_not_contaminate_run_reads
    A9  reservation failure ordering    test_reservation_failure_stops_before_user_code
    A10 parity                          every test here parametrizes memory + sqlite

A6 (write-through vs ``durability="exit"``) and A12 (migration) are
sqlite-only and live in ``test_attempt_ledger_sqlite.py``.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from hypergraph import AsyncRunner, Graph, node
from hypergraph.checkpointers import (
    AttemptError,
    AttemptLedgerError,
    AttemptStatus,
    CheckpointPolicy,
    Checkpointer,
    MemoryCheckpointer,
    StepRecord,
    StepStatus,
)

aiosqlite = pytest.importorskip("aiosqlite")

from hypergraph.checkpointers import SqliteCheckpointer  # noqa: E402

FP = "policy-fp-v1"
RUN = "wf-1"
NODE = "call_model"


def _step(
    run_id: str = RUN,
    superstep: int = 0,
    node_name: str = NODE,
    index: int = 0,
    values: dict | None = None,
    attempt_series_id: str | None = None,
) -> StepRecord:
    return StepRecord(
        run_id=run_id,
        superstep=superstep,
        node_name=node_name,
        index=index,
        status=StepStatus.COMPLETED,
        input_versions={"prompt": 1},
        values={"answer": 42} if values is None else values,
        completed_at=datetime.now(timezone.utc),
        attempt_series_id=attempt_series_id,
    )


class _FailingDict(dict):
    """Dict whose writes raise — simulates a persistence cut for memory."""

    def __setitem__(self, key, value):  # noqa: ANN001
        raise RuntimeError("injected persistence failure")


class _MemoryBackend:
    name = "memory"

    async def make(self, *, retention: str = "full") -> Checkpointer:
        cp = MemoryCheckpointer()
        if retention != "full":
            cp.policy = CheckpointPolicy(retention=retention)
        return cp

    async def crash(self, cp: Checkpointer) -> Checkpointer:
        # Memory's durability domain is the process: a persistence cut means
        # "no further ledger writes happen"; resume reuses the same instance.
        return cp

    def break_reservation_persistence(self, cp, series_id: str, monkeypatch) -> None:
        monkeypatch.setitem(cp._attempt_records, series_id, _FailingDict(cp._attempt_records[series_id]))

    def break_step_persistence(self, cp, run_id: str, monkeypatch) -> None:
        monkeypatch.setitem(cp._steps, run_id, _FailingDict(cp._steps.get(run_id, {})))

    async def close_all(self) -> None:
        pass


class _SqliteBackend:
    name = "sqlite"

    def __init__(self, tmp_path):
        self._tmp_path = tmp_path
        self._counter = 0
        self._open: list[SqliteCheckpointer] = []
        self._paths: dict[int, str] = {}

    async def make(self, *, retention: str = "full") -> Checkpointer:
        self._counter += 1
        path = str(self._tmp_path / f"ledger-{self._counter}.db")
        cp = SqliteCheckpointer(path, retention=None if retention == "full" else retention)
        self._paths[id(cp)] = path
        self._open.append(cp)
        return cp

    async def crash(self, cp: Checkpointer) -> Checkpointer:
        # At the checkpointer seam a crash means "no further writes from this
        # process". Committed data is already on disk, so a clean close + a new
        # instance over the same file is a faithful cut.
        path = self._paths[id(cp)]
        await cp.close()
        self._open.remove(cp)
        fresh = SqliteCheckpointer(path)
        self._paths[id(fresh)] = path
        self._open.append(fresh)
        return fresh

    def break_reservation_persistence(self, cp, series_id: str, monkeypatch) -> None:
        self._break_on_sql(cp, "INSERT INTO attempt_records", monkeypatch)

    def break_step_persistence(self, cp, run_id: str, monkeypatch) -> None:
        self._break_on_sql(cp, "INSERT INTO steps", monkeypatch)

    @staticmethod
    def _break_on_sql(cp, fragment: str, monkeypatch) -> None:
        original = cp._db.execute

        def wrapper(sql, *args, **kwargs):
            if fragment.lower() in " ".join(sql.split()).lower():
                raise RuntimeError("injected persistence failure")
            return original(sql, *args, **kwargs)

        monkeypatch.setattr(cp._db, "execute", wrapper)

    async def close_all(self) -> None:
        for cp in self._open:
            await cp.close()
        self._open.clear()


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def backend(request, tmp_path):
    b = _MemoryBackend() if request.param == "memory" else _SqliteBackend(tmp_path)
    yield b
    await b.close_all()


async def _open_series(cp: Checkpointer, *, run_id: str = RUN, node_name: str = NODE, max_attempts: int = 3, **kwargs):
    await cp.create_run(run_id, graph_name="g")
    return await cp.open_attempt_series(
        run_id,
        node_name,
        policy_fingerprint=FP,
        max_attempts=max_attempts,
        **kwargs,
    )


# === A1: fresh world ===


async def test_fresh_world_roundtrip(backend):
    cp = await backend.make()
    series = await _open_series(cp)
    not_before = datetime.now(timezone.utc) + timedelta(seconds=5)

    first = await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)
    assert first.attempt_number == 1
    assert first.status is AttemptStatus.STARTED
    assert first.started_at is not None

    await cp.record_attempt_outcome(
        series.id,
        1,
        AttemptStatus.FAILED,
        error=AttemptError.from_exception(ValueError("boom")),
        retry_not_before=not_before,
        sampled_delay=0.25,
    )

    second = await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)
    assert second.attempt_number == 2

    step = _step(attempt_series_id=series.id)
    await cp.close_attempt_series(series.id, 2, AttemptStatus.SUCCEEDED, step_record=step)

    reopened = await backend.crash(cp)

    got = await reopened.get_attempt_series(series.id)
    assert got is not None
    assert got.run_id == RUN
    assert got.node_name == NODE
    assert got.policy_fingerprint == FP
    assert got.max_attempts == 3
    assert got.closed_at is not None
    assert got.committed_superstep == 0

    records = await reopened.get_attempt_records(series.id)
    assert [record.attempt_number for record in records] == [1, 2]
    assert records[0].status is AttemptStatus.FAILED
    assert records[0].error is not None
    assert records[0].error.type_name == "ValueError"
    assert "boom" in records[0].error.message
    assert records[0].retry_not_before == not_before
    assert records[0].sampled_delay == 0.25
    assert records[0].completed_at is not None
    assert records[1].status is AttemptStatus.SUCCEEDED
    assert records[1].completed_at is not None

    steps = await reopened.get_steps(RUN)
    assert len(steps) == 1
    assert steps[0].attempt_series_id == series.id
    assert steps[0].values == {"answer": 42}

    assert await reopened.get_open_attempt_series(RUN, NODE) is None
    assert await reopened.remaining_attempts(series.id) == 1


# === A2: crash after reservation ===


async def test_crash_after_reservation_consumes_budget(backend):
    cp = await backend.make()
    series = await _open_series(cp, max_attempts=3)
    await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)

    reopened = await backend.crash(cp)

    resolved = await reopened.resolve_stranded_attempts(series.id)
    assert [record.status for record in resolved] == [AttemptStatus.OUTCOME_UNKNOWN]
    assert resolved[0].completed_at is not None
    assert await reopened.remaining_attempts(series.id) == 2

    # The settle is durable, not a view-level projection.
    reopened_again = await backend.crash(reopened)
    records = await reopened_again.get_attempt_records(series.id)
    assert [record.status for record in records] == [AttemptStatus.OUTCOME_UNKNOWN]
    assert await reopened_again.remaining_attempts(series.id) == 2


# === A3: crash before reservation ===


async def test_crash_before_reservation_consumes_nothing(backend, monkeypatch):
    cp = await backend.make()
    series = await _open_series(cp, max_attempts=3)

    backend.break_reservation_persistence(cp, series.id, monkeypatch)
    with pytest.raises(RuntimeError, match="injected persistence failure"):
        await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)
    monkeypatch.undo()

    # Same instance: the failed reservation rolled back, not merely un-flushed.
    assert await cp.get_attempt_records(series.id) == []
    assert await cp.remaining_attempts(series.id) == 3

    reopened = await backend.crash(cp)
    assert await reopened.get_attempt_records(series.id) == []
    assert await reopened.remaining_attempts(series.id) == 3

    first = await reopened.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)
    assert first.attempt_number == 1


# === A4: atomic close ===


async def test_close_is_atomic_with_step_write(backend, monkeypatch):
    cp = await backend.make()
    series = await _open_series(cp, max_attempts=2)
    first = await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)

    backend.break_step_persistence(cp, RUN, monkeypatch)
    with pytest.raises(RuntimeError, match="injected persistence failure"):
        await cp.close_attempt_series(
            series.id,
            first.attempt_number,
            AttemptStatus.SUCCEEDED,
            step_record=_step(attempt_series_id=series.id),
        )
    monkeypatch.undo()

    reopened = await backend.crash(cp)
    got = await reopened.get_attempt_series(series.id)
    assert got is not None
    steps = await reopened.get_steps(RUN)

    if got.closed_at is not None:
        # Only acceptable if BOTH the close and the step were durable.
        assert any(step.attempt_series_id == series.id for step in steps)
        records = await reopened.get_attempt_records(series.id)
        assert records[0].status is AttemptStatus.SUCCEEDED
    else:
        # Series still open, attempt unsettled: a resume settles the attempt
        # and the remaining budget continues — never "succeeded without step".
        assert all(step.attempt_series_id != series.id for step in steps)
        open_series = await reopened.get_open_attempt_series(RUN, NODE)
        assert open_series is not None
        assert open_series.id == series.id
        resolved = await reopened.resolve_stranded_attempts(series.id)
        assert [record.status for record in resolved] == [AttemptStatus.OUTCOME_UNKNOWN]
        assert await reopened.remaining_attempts(series.id) == 1


# === A5: superstep drift (the #187 witness) ===


async def test_resumed_reservation_continues_same_series(backend):
    cp = await backend.make()
    series = await _open_series(cp)
    await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=1)

    reopened = await backend.crash(cp)

    found = await reopened.get_open_attempt_series(RUN, NODE)
    assert found is not None
    assert found.id == series.id

    await reopened.resolve_stranded_attempts(series.id)
    resumed = await reopened.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=2)
    assert resumed.attempt_number == 2
    assert resumed.scheduled_superstep == 2

    records = await reopened.get_attempt_records(series.id)
    assert [(record.attempt_number, record.scheduled_superstep) for record in records] == [(1, 1), (2, 2)]

    still_open = await reopened.get_open_attempt_series(RUN, NODE)
    assert still_open is not None
    assert still_open.id == series.id


# === A7: retention ===


async def test_open_series_survives_retention(backend):
    cp = await backend.make(retention="latest")
    series = await _open_series(cp)
    first = await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)
    await cp.record_attempt_outcome(series.id, first.attempt_number, AttemptStatus.FAILED)

    # Unrelated steps across supersteps trigger latest-retention compaction.
    for superstep in range(3):
        await cp.save_step(_step(superstep=superstep, node_name="other", index=superstep, values={"x": superstep}))

    survivor = await cp.get_open_attempt_series(RUN, NODE)
    assert survivor is not None
    assert survivor.id == series.id
    records = await cp.get_attempt_records(series.id)
    assert [record.status for record in records] == [AttemptStatus.FAILED]


async def test_closed_series_follows_linked_step_retention(backend):
    cp = await backend.make(retention="latest")
    series = await _open_series(cp)
    first = await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=3)
    await cp.close_attempt_series(
        series.id,
        first.attempt_number,
        AttemptStatus.SUCCEEDED,
        step_record=_step(superstep=3, values={"answer": 1}, attempt_series_id=series.id),
    )
    assert await cp.get_attempt_series(series.id) is not None

    # A later non-ledger re-execution supersedes the linked step under
    # retention="latest" — the closed series follows its StepRecord.
    await cp.save_step(_step(superstep=5, index=9, values={"answer": 2}))

    assert await cp.get_attempt_series(series.id) is None
    assert await cp.get_attempt_records(series.id) == []


# === A8: no contamination ===


@node(output_name="doubled")
def _double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def _triple(doubled: int) -> int:
    return doubled * 3


async def test_attempt_rows_do_not_contaminate_run_reads(backend):
    cp = await backend.make()
    runner = AsyncRunner(checkpointer=cp)
    graph = Graph([_double, _triple])

    await runner.run(graph, {"x": 5}, workflow_id="wf-a")
    await runner.run(graph, {"x": 5}, workflow_id="wf-b")

    # Attach retry history to wf-b's `_double` step through the ledger.
    series = await cp.open_attempt_series("wf-b", "_double", policy_fingerprint=FP, max_attempts=3)
    first = await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)
    await cp.record_attempt_outcome(
        series.id,
        first.attempt_number,
        AttemptStatus.FAILED,
        error=AttemptError.from_exception(TimeoutError("slow upstream")),
    )
    second = await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)
    runner_step = next(step for step in await cp.get_steps("wf-b") if step.node_name == "_double")
    linked = dataclasses.replace(runner_step, attempt_series_id=series.id)
    await cp.close_attempt_series(series.id, second.attempt_number, AttemptStatus.SUCCEEDED, step_record=linked)

    state_a = await cp.get_state("wf-a")
    state_b = await cp.get_state("wf-b")
    assert state_a == state_b
    assert json.dumps(state_a, sort_keys=True, default=str).encode() == json.dumps(state_b, sort_keys=True, default=str).encode()

    steps_a = await cp.get_steps("wf-a")
    steps_b = await cp.get_steps("wf-b")
    assert len(steps_a) == len(steps_b)

    def _staleness_view(steps):
        return [(step.node_name, step.superstep, step.input_versions, step.values, step.status) for step in steps]

    assert _staleness_view(steps_a) == _staleness_view(steps_b)


# === A9: reservation failure stops execution before user code ===


async def test_reservation_failure_stops_before_user_code(backend, monkeypatch):
    cp = await backend.make()
    series = await _open_series(cp)
    calls: list[str] = []

    async def driver() -> None:
        calls.append("begin_attempt:enter")
        await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)
        calls.append("reservation_committed")
        calls.append("user_code")

    backend.break_reservation_persistence(cp, series.id, monkeypatch)
    with pytest.raises(RuntimeError, match="injected persistence failure"):
        await driver()
    assert calls == ["begin_attempt:enter"]
    monkeypatch.undo()

    calls.clear()
    await driver()
    assert calls == ["begin_attempt:enter", "reservation_committed", "user_code"]


# === Reservation verification (ticket: fingerprint + budget + deadline) ===


async def test_begin_attempt_rejects_fingerprint_mismatch(backend):
    cp = await backend.make()
    series = await _open_series(cp)
    with pytest.raises(AttemptLedgerError, match="fingerprint"):
        await cp.begin_attempt(series.id, policy_fingerprint="different-fp", scheduled_superstep=0)
    assert await cp.get_attempt_records(series.id) == []


async def test_begin_attempt_rejects_exhausted_budget(backend):
    cp = await backend.make()
    series = await _open_series(cp, max_attempts=1)
    first = await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)
    await cp.record_attempt_outcome(series.id, first.attempt_number, AttemptStatus.FAILED)
    with pytest.raises(AttemptLedgerError, match="max_attempts"):
        await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)
    assert await cp.remaining_attempts(series.id) == 0


async def test_begin_attempt_rejects_elapsed_deadline(backend):
    cp = await backend.make()
    deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    series = await _open_series(cp, deadline_at=deadline)
    with pytest.raises(AttemptLedgerError, match="deadline"):
        await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)
    assert await cp.get_attempt_records(series.id) == []


async def test_open_attempt_series_rejects_second_open_series(backend):
    cp = await backend.make()
    await _open_series(cp)
    with pytest.raises(AttemptLedgerError, match="open"):
        await cp.open_attempt_series(RUN, NODE, policy_fingerprint=FP, max_attempts=3)


async def test_close_rejects_unlinked_step_record(backend):
    cp = await backend.make()
    series = await _open_series(cp)
    first = await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)
    with pytest.raises(AttemptLedgerError, match="attempt_series_id"):
        await cp.close_attempt_series(
            series.id,
            first.attempt_number,
            AttemptStatus.SUCCEEDED,
            step_record=_step(attempt_series_id=None),
        )
    # Nothing was closed by the rejected call.
    still_open = await cp.get_open_attempt_series(RUN, NODE)
    assert still_open is not None


async def test_record_outcome_rejects_success_outside_close(backend):
    cp = await backend.make()
    series = await _open_series(cp)
    first = await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)
    with pytest.raises(AttemptLedgerError, match="close_attempt_series"):
        await cp.record_attempt_outcome(series.id, first.attempt_number, AttemptStatus.SUCCEEDED)
