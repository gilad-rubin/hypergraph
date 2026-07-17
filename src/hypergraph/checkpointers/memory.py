"""In-memory checkpointer for tests and lightweight experimentation."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from hypergraph.checkpointers.base import (
    _UNSET,
    Checkpointer,
    _check_closable,
    _check_close_request,
    _check_no_live_reservation,
    _check_no_open_series,
    _check_recordable_outcome,
    _check_reservation,
    _check_run_exists,
    _new_attempt_series_id,
    _normalize_since,
    _require_series,
    _require_started,
)
from hypergraph.checkpointers.types import (
    AttemptError,
    AttemptRecord,
    AttemptSeries,
    AttemptStatus,
    Run,
    StepRecord,
    StepStatus,
    WorkflowStatus,
)

_BASELINE_NODE_NAME = "__retained_state__"
_BASELINE_NODE_TYPE = "RetentionBaseline"


def _step_sort_key(record: StepRecord) -> tuple[datetime, datetime, int, str]:
    completed_or_created = record.completed_at or record.created_at
    return (completed_or_created, record.created_at, record.index, record.node_name)


class MemoryCheckpointer(Checkpointer):
    """Simple async-only checkpointer backed by in-process memory.

    Attempt-ledger note: memory has no StepRecord buffering layer, so attempt
    reservations are immediate by nature. Its durability domain is the
    process — the ledger survives in-process resume, not process exit.
    """

    def __init__(self):
        super().__init__()
        self._runs: dict[str, Run] = {}
        self._steps: dict[str, dict[tuple[int, str], StepRecord]] = {}
        self._attempt_series: dict[str, AttemptSeries] = {}
        self._attempt_records: dict[str, dict[int, AttemptRecord]] = {}

    async def save_step(self, record: StepRecord) -> None:
        run_steps = self._steps.setdefault(record.run_id, {})
        run_steps[(record.superstep, record.node_name)] = record
        self._apply_retention_policy(record.run_id)

    async def create_run(
        self,
        run_id: str,
        *,
        graph_name: str | None = None,
        parent_run_id: str | None = None,
        forked_from: str | None = None,
        fork_superstep: int | None = None,
        retry_of: str | None = None,
        retry_index: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> Run:
        existing = self._runs.get(run_id)
        created_at = existing.created_at if existing is not None else datetime.now(timezone.utc)
        run = Run(
            id=run_id,
            status=WorkflowStatus.ACTIVE,
            graph_name=graph_name,
            duration_ms=None,
            node_count=0,
            error_count=0,
            parent_run_id=parent_run_id,
            forked_from=forked_from,
            fork_superstep=fork_superstep,
            retry_of=retry_of,
            retry_index=retry_index,
            config=config,
            created_at=created_at,
            completed_at=None,
        )
        self._runs[run_id] = run
        return run

    async def update_run_status(
        self,
        run_id: str,
        status: WorkflowStatus,
        *,
        duration_ms: float | None = None,
        node_count: int | None = None,
        error_count: int | None = None,
    ) -> None:
        existing = self._runs.get(run_id)
        if existing is None:
            raise ValueError(f"Unknown run_id: {run_id!r}")

        self._runs[run_id] = replace(
            existing,
            status=status,
            duration_ms=duration_ms if duration_ms is not None else existing.duration_ms,
            node_count=node_count if node_count is not None else existing.node_count,
            error_count=error_count if error_count is not None else existing.error_count,
            completed_at=(
                datetime.now(timezone.utc)
                if status in {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.PARTIAL, WorkflowStatus.STOPPED}
                else None
            ),
        )

    async def get_state(self, run_id: str, *, superstep: int | None = None) -> dict[str, Any]:
        state: dict[str, Any] = {}
        records = list(self._steps.get(run_id, {}).values())
        if superstep is not None:
            records = [record for record in records if record.superstep <= superstep]
        for step in sorted(records, key=_step_sort_key):
            if step.values:
                state.update(step.values)
        return state

    async def get_steps(
        self,
        run_id: str,
        *,
        superstep: int | None = None,
        show_internal: bool = False,
    ) -> list[StepRecord]:
        run_steps = self._steps.get(run_id, {})
        records = list(run_steps.values())
        if superstep is not None:
            records = [record for record in records if record.superstep <= superstep]
        if not show_internal:
            records = [record for record in records if record.node_name != _BASELINE_NODE_NAME and record.node_type != _BASELINE_NODE_TYPE]
        return sorted(records, key=_step_sort_key)

    async def get_run_async(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    async def list_runs(
        self,
        *,
        status: WorkflowStatus | None = None,
        graph_name: str | None = None,
        since: datetime | None = None,
        parent_run_id: str | None | object = _UNSET,
        limit: int | None = 100,
    ) -> list[Run]:
        runs = list(self._runs.values())
        if status is not None:
            runs = [run for run in runs if run.status == status]
        if graph_name is not None:
            runs = [run for run in runs if (run.graph_name or "") == graph_name]
        if since is not None:
            boundary = _normalize_since(since)
            runs = [run for run in runs if run.created_at >= boundary]
        if parent_run_id is not _UNSET:
            runs = [run for run in runs if run.parent_run_id == parent_run_id]

        runs.sort(key=lambda run: run.created_at, reverse=True)
        if limit is not None:
            runs = runs[:limit]
        return runs

    async def count_runs(
        self,
        *,
        status: WorkflowStatus | None = None,
        parent_run_id: str | None | object = _UNSET,
        retry_of: str | None = None,
    ) -> int:
        runs = self._runs.values()
        return sum(
            1
            for run in runs
            if (status is None or run.status == status)
            and (parent_run_id is _UNSET or run.parent_run_id == parent_run_id)
            and (retry_of is None or run.retry_of == retry_of)
        )

    # === Attempt Ledger ===

    async def open_attempt_series(
        self,
        run_id: str,
        node_name: str,
        *,
        policy_fingerprint: str,
        max_attempts: int,
        deadline_at: datetime | None = None,
    ) -> AttemptSeries:
        _check_run_exists(run_id in self._runs, run_id)
        _check_no_open_series(await self.get_open_attempt_series(run_id, node_name), run_id, node_name)
        series = AttemptSeries(
            id=_new_attempt_series_id(),
            run_id=run_id,
            node_name=node_name,
            policy_fingerprint=policy_fingerprint,
            max_attempts=max_attempts,
            opened_at=datetime.now(timezone.utc),
            deadline_at=deadline_at,
        )
        self._attempt_series[series.id] = series
        self._attempt_records[series.id] = {}
        return series

    async def get_attempt_series(self, series_id: str) -> AttemptSeries | None:
        return self._attempt_series.get(series_id)

    async def get_open_attempt_series(self, run_id: str, node_name: str) -> AttemptSeries | None:
        for series in self._attempt_series.values():
            if series.run_id == run_id and series.node_name == node_name and series.is_open:
                return series
        return None

    async def get_attempt_records(self, series_id: str) -> list[AttemptRecord]:
        records = self._attempt_records.get(series_id, {})
        return [records[number] for number in sorted(records)]

    async def remaining_attempts(self, series_id: str) -> int:
        series = _require_series(self._attempt_series.get(series_id), series_id)
        return series.max_attempts - len(self._attempt_records.get(series_id, {}))

    async def begin_attempt(
        self,
        series_id: str,
        *,
        policy_fingerprint: str,
        scheduled_superstep: int,
    ) -> AttemptRecord:
        series = _require_series(self._attempt_series.get(series_id), series_id)
        records = self._attempt_records[series_id]
        now = datetime.now(timezone.utc)
        _check_reservation(series, policy_fingerprint=policy_fingerprint, consumed=len(records), now=now)
        # A STARTED row may belong to a live invocation — never reserve over it.
        live = next((record for record in records.values() if record.status is AttemptStatus.STARTED), None)
        _check_no_live_reservation(live, series_id)
        record = AttemptRecord(
            series_id=series_id,
            attempt_number=max(records, default=0) + 1,
            scheduled_superstep=scheduled_superstep,
            status=AttemptStatus.STARTED,
            started_at=now,
        )
        records[record.attempt_number] = record
        return record

    async def record_attempt_outcome(
        self,
        series_id: str,
        attempt_number: int,
        status: AttemptStatus,
        *,
        error: AttemptError | None = None,
        retry_not_before: datetime | None = None,
        sampled_delay: float | None = None,
    ) -> AttemptRecord:
        _check_recordable_outcome(status)
        _require_series(self._attempt_series.get(series_id), series_id)
        records = self._attempt_records[series_id]
        record = _require_started(records.get(attempt_number), series_id, attempt_number)
        updated = replace(
            record,
            status=status,
            completed_at=datetime.now(timezone.utc),
            error=error,
            retry_not_before=retry_not_before,
            sampled_delay=sampled_delay,
        )
        records[attempt_number] = updated
        return updated

    async def record_attempt_deadline(
        self,
        series_id: str,
        attempt_number: int,
    ) -> AttemptRecord:
        _require_series(self._attempt_series.get(series_id), series_id)
        records = self._attempt_records[series_id]
        record = _require_started(records.get(attempt_number), series_id, attempt_number)
        updated = replace(
            record,
            deadline_elapsed=True,
            cancellation_requested=True,
        )
        records[attempt_number] = updated
        return updated

    async def close_attempt_series(
        self,
        series_id: str,
        attempt_number: int,
        status: AttemptStatus,
        *,
        step_record: StepRecord,
        error: AttemptError | None = None,
    ) -> None:
        series = _require_series(self._attempt_series.get(series_id), series_id)
        _check_close_request(series, status, step_record)
        records = self._attempt_records[series_id]
        record = records.get(attempt_number)
        settle = _check_closable(record, series_id, attempt_number, status, max(records, default=0))
        now = datetime.now(timezone.utc)
        # The step store happens first: it is the only in-principle fallible
        # write here. The attempt settle and series close below are pure dict
        # assignments, so a step-write failure leaves the series open with the
        # attempt unsettled — the atomic-close outcome.
        run_steps = self._steps.setdefault(step_record.run_id, {})
        run_steps[(step_record.superstep, step_record.node_name)] = step_record
        if settle:
            records[attempt_number] = replace(record, status=status, completed_at=now, error=error)
        self._attempt_series[series_id] = replace(series, closed_at=now, committed_superstep=step_record.superstep)
        self._apply_retention_policy(step_record.run_id)

    async def resolve_stranded_attempts(self, series_id: str) -> list[AttemptRecord]:
        _require_series(self._attempt_series.get(series_id), series_id)
        records = self._attempt_records[series_id]
        self._settle_stranded(records, datetime.now(timezone.utc))
        return [records[number] for number in sorted(records)]

    @staticmethod
    def _settle_stranded(records: dict[int, AttemptRecord], now: datetime) -> None:
        for number, record in records.items():
            if record.status is AttemptStatus.STARTED:
                records[number] = replace(record, status=AttemptStatus.OUTCOME_UNKNOWN, completed_at=now)

    def _prune_attempt_series_for_dropped(self, dropped: list[StepRecord]) -> None:
        """Closed attempt history follows its linked StepRecord's retention fate."""
        for record in dropped:
            series_id = record.attempt_series_id
            if series_id is None:
                continue
            series = self._attempt_series.get(series_id)
            if series is None or series.is_open:
                continue
            del self._attempt_series[series_id]
            self._attempt_records.pop(series_id, None)

    def _apply_retention_policy(self, run_id: str) -> None:
        retention = self.policy.retention
        if retention == "full":
            return

        run_steps = self._steps.get(run_id)
        if not run_steps:
            return

        if retention == "latest":
            ordered = sorted(run_steps.values(), key=_step_sort_key)
            latest_by_node: dict[str, StepRecord] = {}
            for record in ordered:
                if record.node_name == _BASELINE_NODE_NAME:
                    continue
                latest_by_node[record.node_name] = record
            kept = list(latest_by_node.values())
            dropped = [record for record in ordered if record not in kept]
            baseline = _make_baseline_record(
                run_id,
                dropped,
                baseline_superstep=(min((record.superstep for record in kept), default=0) - 1),
            )
            retained = [*([baseline] if baseline is not None else []), *kept]
            self._steps[run_id] = {(record.superstep, record.node_name): record for record in retained}
            self._prune_attempt_series_for_dropped(dropped)
            return

        if retention == "windowed" and self.policy.window is not None:
            ordered = sorted(run_steps.values(), key=_step_sort_key)
            non_baseline = [record for record in ordered if record.node_name != _BASELINE_NODE_NAME]
            if not non_baseline:
                return
            max_superstep = max(record.superstep for record in non_baseline)
            cutoff = max_superstep - self.policy.window + 1
            if cutoff <= 0:
                return
            kept = [record for record in ordered if record.superstep >= cutoff and record.node_name != _BASELINE_NODE_NAME]
            dropped = [record for record in ordered if record.node_name == _BASELINE_NODE_NAME or record.superstep < cutoff]
            baseline = _make_baseline_record(run_id, dropped, baseline_superstep=cutoff - 1)
            retained = [*([baseline] if baseline is not None else []), *kept]
            self._steps[run_id] = {(record.superstep, record.node_name): record for record in retained}
            self._prune_attempt_series_for_dropped(dropped)


def _merge_state(records: list[StepRecord]) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for record in sorted(records, key=_step_sort_key):
        if record.values:
            state.update(record.values)
    return state


def _make_baseline_record(
    run_id: str,
    records: list[StepRecord],
    *,
    baseline_superstep: int,
) -> StepRecord | None:
    if not records:
        return None
    values = _merge_state(records)
    if not values:
        return None
    created_at = min(record.created_at for record in records)
    completed_candidates = [record.completed_at for record in records if record.completed_at is not None]
    completed_at = max(completed_candidates) if completed_candidates else created_at
    return StepRecord(
        run_id=run_id,
        superstep=baseline_superstep,
        node_name=_BASELINE_NODE_NAME,
        index=min(record.index for record in records),
        status=StepStatus.COMPLETED,
        input_versions={},
        values=values,
        created_at=created_at,
        completed_at=completed_at,
        node_type="RetentionBaseline",
    )
