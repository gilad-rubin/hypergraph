"""Generic, derive-on-read models for Run Home operator surfaces.

These views are deliberately one level above :class:`RunHomeClient`: they
turn durable Host facts into JSON-shaped rows while leaving product-specific
joins (document titles, tenants, corpus truth) to the product. No status is
stored here or anywhere else.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from hypergraph.checkpointers.types import PauseSlot, WorkflowStatus
from hypergraph.host.client import RunHomeClient, _RunReadSnapshot
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


def _json_copy(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value))


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


__all__ = [
    "BatchItemReadModel",
    "BatchReadModel",
    "BatchSummaryReadModel",
    "PauseReadModel",
    "RUN_READ_STATUS_VALUES",
    "RunHomeReadModel",
    "RunReadModel",
]
