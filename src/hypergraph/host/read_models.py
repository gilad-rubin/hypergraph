"""Generic, derive-on-read models for Run Home operator surfaces.

These views are deliberately one level above :class:`RunHomeClient`: they
turn durable Host facts into JSON-shaped rows while leaving product-specific
joins (document titles, tenants, corpus truth) to the product. No status is
stored here or anywhere else.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from hypergraph.checkpointers.types import PauseSlot, WorkflowStatus
from hypergraph.host.client import RunHomeClient, _parse_iso, _RunReadSnapshot, _StepFact, _TimingSnapshot
from hypergraph.host.definition import DefinitionId
from hypergraph.host.refs import BatchRef, RunRef
from hypergraph.host.views import (
    BATCH_OUTCOME_ABANDONED,
    TERMINAL_STATUS_VALUES,
    BatchView,
    RunQuery,
    RunView,
    WaitingCondition,
    item_condition,
)

RUNNING = "running"
RUN_READ_STATUS_VALUES: frozenset[str] = TERMINAL_STATUS_VALUES | frozenset({WaitingCondition.QUEUED.value, WaitingCondition.PAUSED.value, RUNNING})


@dataclass(frozen=True)
class PauseReadModel:
    """One open durable question, exactly as the graph authored it."""

    run_ref: RunRef
    pause_id: str
    node_name: str
    node_path: str | None
    response_key: str
    created_at: datetime
    ask: dict[str, Any]
    answer_schema: dict[str, Any]
    options: tuple[str, ...] | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dictionary suitable for an HTTP response."""
        return {
            "run_ref": self.run_ref.to_dict(),
            "pause_id": self.pause_id,
            "node_name": self.node_name,
            "node_path": self.node_path,
            "response_key": self.response_key,
            "created_at": self.created_at.isoformat(),
            "ask": _json_copy(self.ask),
            "answer_schema": _json_copy(self.answer_schema),
            "options": None if self.options is None else list(self.options),
        }


@dataclass(frozen=True)
class RunReadModel:
    """One Run as a UI can truthfully render it."""

    run_ref: RunRef
    workflow_id: str
    definition_id: DefinitionId | None
    status: str
    condition: str
    accepted_at: datetime | None
    started_at: datetime | None
    settled_at: datetime | None
    updated_at: datetime
    inputs: dict[str, Any]
    pause: PauseReadModel | None
    retry_of: str | None
    forked_from: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dictionary suitable for an HTTP response."""
        return {
            "run_ref": self.run_ref.to_dict(),
            "workflow_id": self.workflow_id,
            "definition_id": None if self.definition_id is None else self.definition_id.to_dict(),
            "status": self.status,
            "condition": self.condition,
            "accepted_at": _iso(self.accepted_at),
            "started_at": _iso(self.started_at),
            "settled_at": _iso(self.settled_at),
            "updated_at": self.updated_at.isoformat(),
            "inputs": _json_copy(self.inputs),
            "pause": None if self.pause is None else self.pause.to_dict(),
            "retry_of": self.retry_of,
            "forked_from": self.forked_from,
        }


@dataclass(frozen=True)
class BatchItemReadModel:
    """One Batch item with THE existing operator-facing condition word."""

    item_key: str
    run_ref: RunRef
    workflow_id: str
    word: str
    started: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_key": self.item_key,
            "run_ref": self.run_ref.to_dict(),
            "workflow_id": self.workflow_id,
            "word": self.word,
            "started": self.started,
        }


@dataclass(frozen=True)
class BatchReadModel:
    """One immutable Batch manifest's current census."""

    batch_ref: BatchRef
    workflow_id: str
    definition_id: DefinitionId
    counts: dict[str, int]
    items: dict[str, BatchItemReadModel]
    settled: bool
    tolerance_tripped: bool
    retry_of: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_ref": self.batch_ref.to_dict(),
            "workflow_id": self.workflow_id,
            "definition_id": self.definition_id.to_dict(),
            "counts": dict(self.counts),
            "items": {key: item.to_dict() for key, item in self.items.items()},
            "settled": self.settled,
            "tolerance_tripped": self.tolerance_tripped,
            "retry_of": self.retry_of,
        }


@dataclass(frozen=True)
class BatchSummaryReadModel:
    """One Batch's census WITHOUT its per-item detail — a listing row.

    Deliberately not a trimmed :class:`BatchReadModel`: a listing is bounded
    by design, and a page of Batches that each carried a full item map would
    grow with the manifests rather than with the page. Ask ``get_batch`` for
    the item detail of the one Batch an operator opened.
    """

    batch_ref: BatchRef
    workflow_id: str
    definition_id: DefinitionId
    created_at: datetime
    item_count: int
    counts: dict[str, int]
    settled: bool
    tolerance_tripped: bool
    retry_of: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_ref": self.batch_ref.to_dict(),
            "workflow_id": self.workflow_id,
            "definition_id": self.definition_id.to_dict(),
            "created_at": self.created_at.isoformat(),
            "item_count": self.item_count,
            "counts": dict(self.counts),
            "settled": self.settled,
            "tolerance_tripped": self.tolerance_tripped,
            "retry_of": self.retry_of,
        }


@dataclass(frozen=True)
class StepTimingReadModel:
    """One durable step execution — the raw fact, addressed.

    ``run_ref`` names the run the step actually executed in (a nested
    graph's child run, or one item of a ``map``); ``root_workflow_id``,
    ``batch_id`` and ``item_key`` name the Host Run that drove it. A caller
    folds per document by ``item_key`` and still sees which inner run was
    slow.
    """

    run_ref: RunRef
    workflow_id: str
    root_workflow_id: str
    batch_id: str | None
    item_key: str | None
    node_name: str
    node_type: str | None
    status: str
    superstep: int
    duration_ms: float
    cached: bool
    error: str | None
    completed_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_ref": self.run_ref.to_dict(),
            "workflow_id": self.workflow_id,
            "root_workflow_id": self.root_workflow_id,
            "batch_id": self.batch_id,
            "item_key": self.item_key,
            "node_name": self.node_name,
            "node_type": self.node_type,
            "status": self.status,
            "superstep": self.superstep,
            "duration_ms": self.duration_ms,
            "cached": self.cached,
            "error": self.error,
            "completed_at": _iso(self.completed_at),
        }


@dataclass(frozen=True)
class RunTimingReadModel:
    """One Host Run's own durable totals.

    ``duration_ms`` is the Run's WALL time, which is not the sum of its
    nodes': a fan-out runs pages concurrently, so the parts add up to more
    than the whole. Both numbers are true and answer different questions —
    "how long did this document take?" versus "what did this node cost?".
    """

    run_ref: RunRef
    workflow_id: str
    definition_id: DefinitionId | None
    batch_id: str | None
    item_key: str | None
    status: str | None
    duration_ms: float | None
    node_count: int
    error_count: int
    started_at: datetime | None
    settled_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_ref": self.run_ref.to_dict(),
            "workflow_id": self.workflow_id,
            "definition_id": None if self.definition_id is None else self.definition_id.to_dict(),
            "batch_id": self.batch_id,
            "item_key": self.item_key,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "node_count": self.node_count,
            "error_count": self.error_count,
            "started_at": _iso(self.started_at),
            "settled_at": _iso(self.settled_at),
        }


@dataclass(frozen=True)
class NodeTimingReadModel:
    """What one node cost across every execution in the selection.

    ``average_ms`` averages over executions that ACTUALLY RAN — a cache hit
    returns in microseconds and would otherwise quietly report a node as
    fast when what really happened is that it was skipped. It is None when
    every execution was a cache hit, which is an honest "nothing ran", not
    a zero.
    """

    node_name: str
    executions: int
    cached: int
    errors: int
    total_seconds: float
    average_ms: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_name": self.node_name,
            "executions": self.executions,
            "cached": self.cached,
            "errors": self.errors,
            "total_seconds": self.total_seconds,
            "average_ms": self.average_ms,
        }


@dataclass(frozen=True)
class NodeTimingsReadModel:
    """Durable per-node cost for one selection of Host Runs."""

    definition: str | None
    runs: tuple[RunTimingReadModel, ...]
    nodes: tuple[NodeTimingReadModel, ...]
    steps: tuple[StepTimingReadModel, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "definition": self.definition,
            "runs": [run.to_dict() for run in self.runs],
            "nodes": [node.to_dict() for node in self.nodes],
            "steps": [step.to_dict() for step in self.steps],
        }


class RunHomeReadModel:
    """Derive generic operator views from a backend-neutral Run Home client.

    Every method here READS. The statements it issues are ``SELECT``s
    against the Run Home the caller already opened — it opens no second
    connection, creates nothing, and migrates nothing, so an operator
    surface (or a notebook that outlived the process that wrote the facts)
    can project durable truth without becoming a writer of it.
    """

    def __init__(self, client: RunHomeClient) -> None:
        if not isinstance(client, RunHomeClient):
            raise TypeError(f"RunHomeReadModel expects a RunHomeClient, got {type(client).__name__}.")
        self._client = client

    async def get_run(self, ref: RunRef) -> RunReadModel | None:
        snapshot = await self._client._read_model_snapshot(ref)
        return None if snapshot is None else await self._run(snapshot)

    def get_run_sync(self, ref: RunRef) -> RunReadModel | None:
        snapshot = self._client._read_model_snapshot_sync(ref)
        return None if snapshot is None else self._run_sync(snapshot)

    async def list_runs(self, query: RunQuery) -> list[RunReadModel]:
        return [await self._run(snapshot) for snapshot in await self._client._list_read_model_snapshots(query)]

    def list_runs_sync(self, query: RunQuery) -> list[RunReadModel]:
        return [self._run_sync(snapshot) for snapshot in self._client._list_read_model_snapshots_sync(query)]

    async def get_pause(self, ref: RunRef) -> PauseReadModel | None:
        snapshot = await self._client._read_model_snapshot(ref)
        if snapshot is None or snapshot.view.waiting is not WaitingCondition.PAUSED:
            return None
        return _pause(ref, snapshot.pause_slot)

    def get_pause_sync(self, ref: RunRef) -> PauseReadModel | None:
        snapshot = self._client._read_model_snapshot_sync(ref)
        if snapshot is None or snapshot.view.waiting is not WaitingCondition.PAUSED:
            return None
        return _pause(ref, snapshot.pause_slot)

    async def get_batch(self, ref: BatchRef) -> BatchReadModel | None:
        view = await self._client.get(ref)
        return _batch(view) if isinstance(view, BatchView) else None

    def get_batch_sync(self, ref: BatchRef) -> BatchReadModel | None:
        view = self._client.get_sync(ref)
        return _batch(view) if isinstance(view, BatchView) else None

    async def list_batches(self, definition: str | None = None, limit: int = 50) -> list[BatchSummaryReadModel]:
        """List recent Batches, newest first.

        The listing an operator opens a bulk run on: which sweeps this Run
        Home accepted, when, and how each one's manifest is doing right now.
        Counts come from the SAME bucket ladder ``get_batch`` uses, so a row
        in the list and the Batch it opens can never tell different stories.

        Args:
            definition: Only Batches pinned to this Definition name, or None
                for every Definition in the Home.
            limit: Cap on returned rows, newest first (default 50).

        Returns:
            One :class:`BatchSummaryReadModel` per Batch, newest acceptance
            first, with ``batch_id`` as the total tie-breaker.
        """
        return [_batch_summary(view, created_at) for view, created_at in await self._client._list_batch_views(definition, limit)]

    def list_batches_sync(self, definition: str | None = None, limit: int = 50) -> list[BatchSummaryReadModel]:
        """Sync mirror of ``list_batches``."""
        return [_batch_summary(view, created_at) for view, created_at in self._client._list_batch_views_sync(definition, limit)]

    async def node_timings(
        self,
        definition: str | None = None,
        *,
        batch: BatchRef | str | None = None,
        limit: int = 200,
    ) -> NodeTimingsReadModel:
        """Fold the DURABLE per-node timing facts a Run Home already holds.

        Every number here was committed by the execution journal while the
        work ran — ``steps.duration_ms``/``cached``/``error`` and the Run's
        own ``duration_ms``/``node_count``. Nothing is measured here, so the
        answer survives the process that produced it: a notebook kernel that
        died mid-sweep still owes its operator the cost of the work it drove,
        and this is where that answer lives.

        The walk descends ``runs.parent_run_id``, so a Host Run's nested
        graphs and every item of a ``map`` are folded into the same
        aggregate. A join that matched only
        ``runs.id = host_submissions.workflow_id`` threw exactly that
        evidence away.

        Args:
            definition: Only Runs pinned to this Definition name, or None
                for every Definition in the Home.
            batch: Narrow to one Batch (a ``BatchRef`` or batch id string).
            limit: Cap on the Host Runs covered, newest acceptance first
                (default 200). Nested runs beneath them are not capped —
                they belong to a Run that was already selected.

        Returns:
            :class:`NodeTimingsReadModel`: per-node aggregates ordered by
            total cost (heaviest first, node name as the tie-breaker), the
            per-Run totals, and every step record so a caller can fold the
            same facts its own way — per document, per superstep, per hour.
        """
        return _node_timings(await self._client._timing_snapshot(definition, batch, limit))

    def node_timings_sync(
        self,
        definition: str | None = None,
        *,
        batch: BatchRef | str | None = None,
        limit: int = 200,
    ) -> NodeTimingsReadModel:
        """Sync mirror of ``node_timings``."""
        return _node_timings(self._client._timing_snapshot_sync(definition, batch, limit))

    async def _run(self, snapshot: _RunReadSnapshot) -> RunReadModel:
        pause = _pause(snapshot.view.run_ref, snapshot.pause_slot) if snapshot.view.waiting is WaitingCondition.PAUSED else None
        return _run(snapshot, pause)

    def _run_sync(self, snapshot: _RunReadSnapshot) -> RunReadModel:
        pause = _pause(snapshot.view.run_ref, snapshot.pause_slot) if snapshot.view.waiting is WaitingCondition.PAUSED else None
        return _run(snapshot, pause)


def _status(view: RunView, condition: str) -> tuple[str, str]:
    """Derive one coarse badge and preserve the exact Run Home condition."""
    if view.status is not None and view.status.value in TERMINAL_STATUS_VALUES:
        return view.status.value, condition
    if condition == BATCH_OUTCOME_ABANDONED:
        return WorkflowStatus.FAILED.value, condition
    if condition == "unstarted":
        return WorkflowStatus.STOPPED.value, condition
    if view.waiting is WaitingCondition.RECOVERY_EXHAUSTED:
        return WorkflowStatus.FAILED.value, condition
    if view.waiting is WaitingCondition.PAUSED:
        return WaitingCondition.PAUSED.value, condition
    if view.waiting is not None:
        return WaitingCondition.QUEUED.value, condition
    if view.status is not None:
        return RUNNING, condition
    return WaitingCondition.QUEUED.value, condition


def _run(snapshot: _RunReadSnapshot, pause: PauseReadModel | None) -> RunReadModel:
    status, condition = _status(snapshot.view, snapshot.condition)
    return RunReadModel(
        run_ref=snapshot.view.run_ref,
        workflow_id=snapshot.view.workflow_id,
        definition_id=snapshot.view.definition_id,
        status=status,
        condition=condition,
        accepted_at=snapshot.accepted_at,
        started_at=snapshot.started_at,
        settled_at=snapshot.settled_at,
        updated_at=snapshot.updated_at,
        inputs=deepcopy(snapshot.inputs),
        pause=pause,
        retry_of=snapshot.view.retry_of,
        forked_from=snapshot.view.forked_from,
    )


def _pause(ref: RunRef, slot: PauseSlot | None) -> PauseReadModel | None:
    if slot is None or not slot.is_open:
        return None
    return PauseReadModel(
        run_ref=ref,
        pause_id=slot.pause_id,
        node_name=slot.node_name,
        node_path=slot.node_path,
        response_key=slot.response_key,
        created_at=slot.created_at,
        ask=_json_copy(slot.question),
        answer_schema=_json_copy(slot.answer_schema),
        options=None if slot.options is None else tuple(slot.options),
    )


def _batch(view: BatchView) -> BatchReadModel:
    return BatchReadModel(
        batch_ref=view.batch_ref,
        workflow_id=view.workflow_id,
        definition_id=view.definition_id,
        counts=dict(view.counts),
        items={
            key: BatchItemReadModel(
                item_key=item.item_key,
                run_ref=item.run_ref,
                workflow_id=item.workflow_id,
                word=item_condition(item),
                started=item.started,
            )
            for key, item in view.items.items()
        },
        settled=view.settled,
        tolerance_tripped=view.tolerance_tripped,
        retry_of=view.retry_of,
    )


def _batch_summary(view: BatchView, created_at: datetime) -> BatchSummaryReadModel:
    return BatchSummaryReadModel(
        batch_ref=view.batch_ref,
        workflow_id=view.workflow_id,
        definition_id=view.definition_id,
        created_at=created_at,
        item_count=len(view.items),
        counts=dict(view.counts),
        settled=view.settled,
        tolerance_tripped=view.tolerance_tripped,
        retry_of=view.retry_of,
    )


def _node_timings(snapshot: _TimingSnapshot) -> NodeTimingsReadModel:
    """Project durable timing facts, then fold them once per node."""
    runs = tuple(
        RunTimingReadModel(
            run_ref=fact.view.run_ref,
            workflow_id=fact.view.workflow_id,
            definition_id=fact.view.definition_id,
            batch_id=fact.batch_id,
            item_key=fact.item_key,
            status=None if fact.view.status is None else fact.view.status.value,
            duration_ms=fact.duration_ms,
            node_count=fact.node_count,
            error_count=fact.error_count,
            started_at=fact.view.created_at,
            settled_at=fact.view.completed_at,
        )
        for fact in snapshot.runs
    )
    address = {run.workflow_id: run for run in runs}
    steps = tuple(_step_timing(fact, address.get(fact.root_run_id), snapshot.home) for fact in snapshot.steps)
    return NodeTimingsReadModel(definition=snapshot.definition, runs=runs, nodes=_fold_nodes(steps), steps=steps)


def _step_timing(fact: _StepFact, root: RunTimingReadModel | None, home: str) -> StepTimingReadModel:
    return StepTimingReadModel(
        # The step's OWN run. A nested run has no submission of its own, so
        # its ref is built from THIS Home's uri exactly as every other ref
        # is — the Batch address it inherits comes from its root.
        run_ref=RunRef(home=home, run_id=fact.run_id),
        workflow_id=fact.run_id,
        root_workflow_id=fact.root_run_id,
        batch_id=None if root is None else root.batch_id,
        item_key=None if root is None else root.item_key,
        node_name=fact.node_name,
        node_type=fact.node_type,
        status=fact.status,
        superstep=fact.superstep,
        duration_ms=fact.duration_ms,
        cached=fact.cached,
        error=fact.error,
        completed_at=None if fact.completed_at is None else _parse_iso(fact.completed_at),
    )


def _fold_nodes(steps: Sequence[StepTimingReadModel]) -> tuple[NodeTimingReadModel, ...]:
    """One aggregate per node name, heaviest total first.

    Ordered by cost rather than by name because the question an operator
    brings to this table is "what is this sweep spending its time on?".
    """
    folds: dict[str, _NodeFold] = {}
    for step in steps:
        folds.setdefault(step.node_name, _NodeFold()).add(step)
    nodes = [fold.finish(node_name) for node_name, fold in folds.items()]
    nodes.sort(key=lambda node: (-node.total_seconds, node.node_name))
    return tuple(nodes)


@dataclass
class _NodeFold:
    """One node's running totals across the selection."""

    executions: int = 0
    cached: int = 0
    errors: int = 0
    total_ms: float = 0.0
    ran: int = 0
    ran_ms: float = 0.0

    def add(self, step: StepTimingReadModel) -> None:
        self.executions += 1
        self.total_ms += step.duration_ms
        self.errors += step.error is not None
        if step.cached:
            self.cached += 1
        else:
            self.ran += 1
            self.ran_ms += step.duration_ms

    def finish(self, node_name: str) -> NodeTimingReadModel:
        return NodeTimingReadModel(
            node_name=node_name,
            executions=self.executions,
            cached=self.cached,
            errors=self.errors,
            total_seconds=self.total_ms / 1000.0,
            average_ms=(self.ran_ms / self.ran) if self.ran else None,
        )


def _json_copy(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value))


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


__all__ = [
    "BatchItemReadModel",
    "BatchReadModel",
    "BatchSummaryReadModel",
    "NodeTimingReadModel",
    "NodeTimingsReadModel",
    "PauseReadModel",
    "RUN_READ_STATUS_VALUES",
    "RunHomeReadModel",
    "RunReadModel",
    "RunTimingReadModel",
    "StepTimingReadModel",
]
