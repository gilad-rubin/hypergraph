"""Terminal-close seam contract (#230 F1) — memory/sqlite parity.

A resume dead end (budget exhausted, window expired, OUTCOME_UNKNOWN evidence)
has no live STARTED reservation left, yet the atomic outcome/link/close
invariant still requires the failed logical StepRecord to link its series and
the series to close. ``close_attempt_series`` therefore also accepts closing
when the LAST record is already terminal FAILED/OUTCOME_UNKNOWN: the
StepRecord is linked and the series closes atomically WITHOUT rewriting the
settled evidence. SUCCEEDED still requires a live STARTED reservation.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from hypergraph.checkpointers import (
    AttemptError,
    AttemptLedgerError,
    AttemptStatus,
    MemoryCheckpointer,
    StepRecord,
    StepStatus,
)

aiosqlite = pytest.importorskip("aiosqlite")

from hypergraph.checkpointers import SqliteCheckpointer  # noqa: E402

FP = "policy-fp-v1"
RUN = "wf-1"
NODE = "call_model"


def _step(series_id: str, *, status: StepStatus = StepStatus.FAILED, superstep: int = 0) -> StepRecord:
    return StepRecord(
        run_id=RUN,
        superstep=superstep,
        node_name=NODE,
        index=0,
        status=status,
        input_versions={},
        error="boom" if status is StepStatus.FAILED else None,
        attempt_series_id=series_id,
    )


class _MemoryBackend:
    name = "memory"

    async def make(self):
        return MemoryCheckpointer()

    async def close_all(self) -> None:
        pass


class _SqliteBackend:
    name = "sqlite"

    def __init__(self, tmp_path):
        self._tmp_path = tmp_path
        self._open: list[SqliteCheckpointer] = []

    async def make(self):
        cp = SqliteCheckpointer(str(self._tmp_path / f"terminal-close-{len(self._open)}.db"))
        self._open.append(cp)
        return cp

    async def close_all(self) -> None:
        for cp in self._open:
            await cp.close()
        self._open.clear()


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def backend(request, tmp_path):
    b = _MemoryBackend() if request.param == "memory" else _SqliteBackend(tmp_path)
    yield b
    await b.close_all()


async def _series_with_settled_failure(cp, *, max_attempts: int = 1):
    await cp.create_run(RUN, graph_name="g")
    series = await cp.open_attempt_series(RUN, NODE, policy_fingerprint=FP, max_attempts=max_attempts)
    record = await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)
    settled = await cp.record_attempt_outcome(
        series.id,
        record.attempt_number,
        AttemptStatus.FAILED,
        error=AttemptError.from_exception(ConnectionError("boom")),
        sampled_delay=0.5,
    )
    return series, settled


async def _series_with_outcome_unknown(cp, *, max_attempts: int = 1):
    await cp.create_run(RUN, graph_name="g")
    series = await cp.open_attempt_series(RUN, NODE, policy_fingerprint=FP, max_attempts=max_attempts)
    await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)
    records = await cp.resolve_stranded_attempts(series.id)
    assert [r.status for r in records] == [AttemptStatus.OUTCOME_UNKNOWN]
    return series, records[0]


async def test_close_links_step_over_settled_failed(backend):
    cp = await backend.make()
    series, settled = await _series_with_settled_failure(cp)

    await cp.close_attempt_series(series.id, settled.attempt_number, AttemptStatus.FAILED, step_record=_step(series.id))

    closed = await cp.get_attempt_series(series.id)
    assert closed is not None and not closed.is_open
    assert closed.committed_superstep == 0
    steps = await cp.get_steps(RUN)
    assert len(steps) == 1
    assert steps[0].attempt_series_id == series.id

    # The settled evidence is linked, never rewritten.
    records = await cp.get_attempt_records(series.id)
    assert [r.status for r in records] == [AttemptStatus.FAILED]
    assert records[0].error is not None and records[0].error.type_name == "ConnectionError"
    assert records[0].sampled_delay == 0.5
    assert records[0].completed_at == settled.completed_at


async def test_close_links_step_over_outcome_unknown(backend):
    cp = await backend.make()
    series, stranded = await _series_with_outcome_unknown(cp)

    await cp.close_attempt_series(
        series.id,
        stranded.attempt_number,
        AttemptStatus.OUTCOME_UNKNOWN,
        step_record=_step(series.id),
    )

    closed = await cp.get_attempt_series(series.id)
    assert closed is not None and not closed.is_open
    steps = await cp.get_steps(RUN)
    assert steps[0].attempt_series_id == series.id
    records = await cp.get_attempt_records(series.id)
    assert [r.status for r in records] == [AttemptStatus.OUTCOME_UNKNOWN]
    assert await cp.get_open_attempt_series(RUN, NODE) is None


async def test_terminal_close_requires_matching_status(backend):
    cp = await backend.make()
    series, settled = await _series_with_settled_failure(cp)

    with pytest.raises(AttemptLedgerError, match="same status"):
        await cp.close_attempt_series(
            series.id,
            settled.attempt_number,
            AttemptStatus.OUTCOME_UNKNOWN,
            step_record=_step(series.id),
        )

    still_open = await cp.get_attempt_series(series.id)
    assert still_open is not None and still_open.is_open
    assert await cp.get_steps(RUN) == []


async def test_succeeded_close_still_requires_live_reservation(backend):
    cp = await backend.make()
    series, settled = await _series_with_settled_failure(cp)

    with pytest.raises(AttemptLedgerError, match="already settled"):
        await cp.close_attempt_series(
            series.id,
            settled.attempt_number,
            AttemptStatus.SUCCEEDED,
            step_record=_step(series.id, status=StepStatus.COMPLETED),
        )
    assert (await cp.get_attempt_series(series.id)).is_open


async def test_terminal_close_targets_only_the_last_record(backend):
    cp = await backend.make()
    await cp.create_run(RUN, graph_name="g")
    series = await cp.open_attempt_series(RUN, NODE, policy_fingerprint=FP, max_attempts=3)
    first = await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)
    await cp.record_attempt_outcome(
        series.id,
        first.attempt_number,
        AttemptStatus.FAILED,
        error=AttemptError.from_exception(ConnectionError("boom")),
    )
    await cp.begin_attempt(series.id, policy_fingerprint=FP, scheduled_superstep=0)

    # Attempt 1 is settled but NOT last — a live reservation exists after it.
    with pytest.raises(AttemptLedgerError, match="already settled"):
        await cp.close_attempt_series(series.id, first.attempt_number, AttemptStatus.FAILED, step_record=_step(series.id))
    assert (await cp.get_attempt_series(series.id)).is_open


async def test_sqlite_sync_mirror_links_over_terminal_records(tmp_path):
    cp = SqliteCheckpointer(str(tmp_path / "terminal-close-sync.db"))
    try:
        cp.create_run_sync(RUN, graph_name="g")

        # Settled FAILED evidence.
        series = cp.open_attempt_series_sync(RUN, NODE, policy_fingerprint=FP, max_attempts=1)
        record = cp.begin_attempt_sync(series.id, policy_fingerprint=FP, scheduled_superstep=0)
        cp.record_attempt_outcome_sync(
            series.id,
            record.attempt_number,
            AttemptStatus.FAILED,
            error=AttemptError.from_exception(ConnectionError("boom")),
        )
        cp.close_attempt_series_sync(series.id, record.attempt_number, AttemptStatus.FAILED, step_record=_step(series.id))
        assert not cp.get_attempt_series_sync(series.id).is_open
        assert [r.status for r in cp.get_attempt_records_sync(series.id)] == [AttemptStatus.FAILED]

        # OUTCOME_UNKNOWN evidence (fresh node name → fresh series).
        series2 = cp.open_attempt_series_sync(RUN, "other_node", policy_fingerprint=FP, max_attempts=1)
        stranded = cp.begin_attempt_sync(series2.id, policy_fingerprint=FP, scheduled_superstep=0)
        cp.resolve_stranded_attempts_sync(series2.id)
        step2 = StepRecord(
            run_id=RUN,
            superstep=1,
            node_name="other_node",
            index=1,
            status=StepStatus.FAILED,
            input_versions={},
            error="unknown",
            attempt_series_id=series2.id,
        )
        cp.close_attempt_series_sync(series2.id, stranded.attempt_number, AttemptStatus.OUTCOME_UNKNOWN, step_record=step2)
        assert not cp.get_attempt_series_sync(series2.id).is_open
        assert [r.status for r in cp.get_attempt_records_sync(series2.id)] == [AttemptStatus.OUTCOME_UNKNOWN]

        # Mismatched status is rejected without closing.
        series3 = cp.open_attempt_series_sync(RUN, "third_node", policy_fingerprint=FP, max_attempts=1)
        third = cp.begin_attempt_sync(series3.id, policy_fingerprint=FP, scheduled_superstep=0)
        cp.record_attempt_outcome_sync(series3.id, third.attempt_number, AttemptStatus.FAILED)
        step3 = StepRecord(
            run_id=RUN,
            superstep=2,
            node_name="third_node",
            index=2,
            status=StepStatus.FAILED,
            input_versions={},
            attempt_series_id=series3.id,
        )
        with pytest.raises(AttemptLedgerError, match="same status"):
            cp.close_attempt_series_sync(series3.id, third.attempt_number, AttemptStatus.OUTCOME_UNKNOWN, step_record=step3)
        assert cp.get_attempt_series_sync(series3.id).is_open
    finally:
        await cp.close()
