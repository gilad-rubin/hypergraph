"""In-memory checkpointer for tests and lightweight experimentation."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from hypergraph.checkpointers.base import Checkpointer
from hypergraph.checkpointers.types import Run, StepRecord, StepStatus, WorkflowStatus

_BASELINE_NODE_NAME = "__retained_state__"


def _step_sort_key(record: StepRecord) -> tuple[datetime, datetime, int, str]:
    completed_or_created = record.completed_at or record.created_at
    return (completed_or_created, record.created_at, record.index, record.node_name)


class MemoryCheckpointer(Checkpointer):
    """Simple async-only checkpointer backed by in-process memory."""

    def __init__(self):
        super().__init__()
        self._runs: dict[str, Run] = {}
        self._steps: dict[str, dict[tuple[int, str], StepRecord]] = {}

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
        for step in await self.get_steps(run_id, superstep=superstep):
            if step.values:
                state.update(step.values)
        return state

    async def get_steps(self, run_id: str, *, superstep: int | None = None) -> list[StepRecord]:
        run_steps = self._steps.get(run_id, {})
        records = list(run_steps.values())
        if superstep is not None:
            records = [record for record in records if record.superstep <= superstep]
        return sorted(records, key=_step_sort_key)

    async def get_run_async(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    async def list_runs(
        self,
        *,
        status: WorkflowStatus | None = None,
        parent_run_id: str | None = None,
        limit: int | None = 100,
    ) -> list[Run]:
        runs = list(self._runs.values())
        if status is not None:
            runs = [run for run in runs if run.status == status]
        if parent_run_id is not None:
            runs = [run for run in runs if run.parent_run_id == parent_run_id]

        runs.sort(key=lambda run: run.created_at, reverse=True)
        if limit is not None:
            runs = runs[:limit]
        return runs

    async def count_runs(
        self,
        *,
        status: WorkflowStatus | None = None,
        parent_run_id: str | None = None,
        retry_of: str | None = None,
    ) -> int:
        runs = self._runs.values()
        return sum(
            1
            for run in runs
            if (status is None or run.status == status)
            and (parent_run_id is None or run.parent_run_id == parent_run_id)
            and (retry_of is None or run.retry_of == retry_of)
        )

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
            dropped = [record for record in ordered if record.node_name != _BASELINE_NODE_NAME and record not in kept]
            baseline = _make_baseline_record(
                run_id,
                dropped,
                baseline_superstep=(min((record.superstep for record in kept), default=0) - 1),
            )
            retained = [*([baseline] if baseline is not None else []), *kept]
            self._steps[run_id] = {(record.superstep, record.node_name): record for record in retained}
            return

        if retention == "windowed" and self.policy.window is not None:
            ordered = sorted(run_steps.values(), key=_step_sort_key)
            max_superstep = max(record.superstep for record in ordered)
            cutoff = max_superstep - self.policy.window + 1
            if cutoff <= 0:
                return
            kept = [record for record in ordered if record.superstep >= cutoff and record.node_name != _BASELINE_NODE_NAME]
            dropped = [record for record in ordered if record.superstep < cutoff and record.node_name != _BASELINE_NODE_NAME]
            baseline = _make_baseline_record(run_id, dropped, baseline_superstep=cutoff - 1)
            retained = [*([baseline] if baseline is not None else []), *kept]
            self._steps[run_id] = {(record.superstep, record.node_name): record for record in retained}


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
