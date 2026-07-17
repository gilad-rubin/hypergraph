"""Checkpointer base class and checkpoint policy."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from hypergraph.checkpointers.types import (
    TERMINAL_ATTEMPT_STATUSES,
    AttemptError,
    AttemptLedgerError,
    AttemptRecord,
    AttemptSeries,
    AttemptStatus,
    Checkpoint,
    Run,
    StepRecord,
    WorkflowStatus,
)
from hypergraph.exceptions import WorkflowForkError

_UNSET = object()


def _new_attempt_series_id() -> str:
    """Generate a stable series id (mirrors the run-id convention)."""
    return f"series-{uuid.uuid4().hex[:12]}"


def _require_series(series: AttemptSeries | None, series_id: str) -> AttemptSeries:
    if series is None:
        raise AttemptLedgerError(f"Unknown attempt series: {series_id!r}")
    return series


def _check_run_exists(run_exists: bool, run_id: str) -> None:
    if not run_exists:
        raise AttemptLedgerError(f"Cannot open an attempt series for unknown run {run_id!r}; create the run first.")


def _check_no_open_series(existing: AttemptSeries | None, run_id: str, node_name: str) -> None:
    if existing is not None:
        raise AttemptLedgerError(
            f"An open attempt series already exists for ({run_id!r}, {node_name!r}): {existing.id!r}.\n\n"
            "How to fix:\n"
            "  Resume the open series (get_open_attempt_series) or close it before opening a new one."
        )


def _check_reservation(
    series: AttemptSeries,
    *,
    policy_fingerprint: str,
    consumed: int,
    now: datetime,
) -> None:
    """Verify fingerprint + remaining budget + deadline before reserving."""
    if not series.is_open:
        raise AttemptLedgerError(f"Attempt series {series.id!r} for node {series.node_name!r} is closed; it cannot reserve new attempts.")
    if series.policy_fingerprint != policy_fingerprint:
        raise AttemptLedgerError(
            f"Policy fingerprint mismatch for attempt series {series.id!r} "
            f"(node {series.node_name!r}): stored {series.policy_fingerprint!r}, "
            f"requested {policy_fingerprint!r}.\n\n"
            "How to fix:\n"
            "  Resume with the same retry policy, or start a new/forked workflow to adopt a new policy."
        )
    if consumed >= series.max_attempts:
        raise AttemptLedgerError(
            f"Attempt budget exhausted for node {series.node_name!r}: {consumed} of max_attempts={series.max_attempts} consumed."
        )
    if series.deadline_at is not None and now >= series.deadline_at:
        raise AttemptLedgerError(f"Attempt deadline elapsed for node {series.node_name!r}: deadline_at={series.deadline_at.isoformat()} has passed.")


def _check_no_live_reservation(record: AttemptRecord | None, series_id: str) -> None:
    """A STARTED row has no dead/live discriminator — reserving over it is a conflict."""
    if record is not None:
        raise AttemptLedgerError(
            f"Attempt #{record.attempt_number} in series {series_id!r} is still STARTED; "
            "a live invocation may exist and the ledger cannot tell dead from live.\n\n"
            "How to fix:\n"
            "  If resuming after a crash — when the caller can assert no other invocation\n"
            "  remains live — call resolve_stranded_attempts() first to settle it as\n"
            "  OUTCOME_UNKNOWN, then reserve the next attempt."
        )


def _check_recordable_outcome(status: AttemptStatus) -> None:
    if status is AttemptStatus.STARTED:
        raise AttemptLedgerError("STARTED is a reservation, not an outcome; use begin_attempt().")
    if status is AttemptStatus.SUCCEEDED:
        raise AttemptLedgerError("A SUCCEEDED outcome must commit atomically with its linked StepRecord; use close_attempt_series().")


def _require_started(record: AttemptRecord | None, series_id: str, attempt_number: int) -> AttemptRecord:
    if record is None:
        raise AttemptLedgerError(f"Unknown attempt #{attempt_number} in series {series_id!r}")
    if record.status is not AttemptStatus.STARTED:
        raise AttemptLedgerError(f"Attempt #{attempt_number} in series {series_id!r} is already settled as {record.status.value!r}.")
    return record


def _check_close_request(series: AttemptSeries, status: AttemptStatus, step_record: StepRecord) -> None:
    if not series.is_open:
        raise AttemptLedgerError(f"Attempt series {series.id!r} is already closed.")
    if status not in TERMINAL_ATTEMPT_STATUSES:
        raise AttemptLedgerError(f"Cannot close a series with non-terminal status {status.value!r}.")
    if step_record.attempt_series_id != series.id:
        raise AttemptLedgerError(
            f"StepRecord.attempt_series_id {step_record.attempt_series_id!r} does not link series {series.id!r}; "
            "the closing StepRecord must carry the series id."
        )
    if step_record.run_id != series.run_id or step_record.node_name != series.node_name:
        raise AttemptLedgerError(
            f"StepRecord ({step_record.run_id!r}, {step_record.node_name!r}) does not belong to "
            f"attempt series {series.id!r} ({series.run_id!r}, {series.node_name!r})."
        )


def _normalize_since(value: datetime) -> datetime:
    """Return an aware UTC boundary for run-list filtering."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _resolve_fork_workflow_id(source_run_id: str, workflow_id: str | None) -> str:
    """Keep an explicit target or derive one for a top-level source."""
    if workflow_id is not None:
        return workflow_id
    if "/" in source_run_id:
        raise WorkflowForkError(
            f"Cannot auto-derive a top-level workflow_id from nested source {source_run_id!r}.\n\n"
            "How to fix:\n"
            "  Top-level callers should pass an explicit slash-free workflow_id for the new fork."
        )
    return f"{source_run_id}-fork-{uuid.uuid4().hex[:6]}"


@dataclass
class CheckpointPolicy:
    """Controls checkpoint durability and retention.

    Attributes:
        durability: When to write checkpoints.
            "sync" — block until written after each step (safest).
            "async" — write in background (default, good balance).
            "exit" — only at run completion (fastest, no mid-run recovery).
        retention: What history to keep.
            "full" — all steps, time travel enabled (default).
            "latest" — only materialized latest state.
            "windowed" — keep last N supersteps.
        window: Supersteps to keep (required if retention="windowed").
        ttl: Auto-expire completed runs after this duration.
    """

    durability: Literal["sync", "async", "exit"] = "async"
    retention: Literal["full", "latest", "windowed"] = "full"
    window: int | None = None
    ttl: timedelta | None = None

    def __post_init__(self) -> None:
        if self.durability == "exit" and self.retention != "latest":
            raise ValueError(
                f'durability="exit" requires retention="latest", got retention="{self.retention}". With exit mode, steps are not persisted mid-run.'
            )
        if self.retention == "windowed" and self.window is None:
            raise ValueError('retention="windowed" requires window parameter')
        if self.window is not None and (not isinstance(self.window, int) or self.window <= 0):
            raise ValueError("window must be a positive integer")
        if self.retention != "windowed" and self.window is not None:
            raise ValueError(f'window parameter only valid with retention="windowed", got retention="{self.retention}"')
        if self.ttl is not None and self.ttl <= timedelta(0):
            raise ValueError("ttl must be greater than 0")


class Checkpointer(ABC):
    """Base class for run persistence.

    Steps are the source of truth. State is computed from steps.
    Implementations store run steps and provide state retrieval.

    The runner calls save_step() after each node completes, and
    create_run/update_run_status for lifecycle management.
    """

    def __init__(self, policy: CheckpointPolicy | None = None):
        self.policy = policy or CheckpointPolicy()

    # === Write Operations ===

    @abstractmethod
    async def save_step(self, record: StepRecord) -> None:
        """Save a step atomically.

        Uses upsert semantics with unique constraint on
        (run_id, superstep, node_name).
        """
        ...

    @abstractmethod
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
        """Create or reset a run record (upsert). Called by runner at run start.

        If a run with this ID already exists, reset it to ACTIVE status.
        This allows re-running with the same workflow_id after interruption.
        """
        ...

    @abstractmethod
    async def update_run_status(
        self,
        run_id: str,
        status: WorkflowStatus,
        *,
        duration_ms: float | None = None,
        node_count: int | None = None,
        error_count: int | None = None,
    ) -> None:
        """Update run status (ACTIVE, COMPLETED, or FAILED) with optional stats."""
        ...

    # === Read Operations ===

    @abstractmethod
    async def get_state(self, run_id: str, *, superstep: int | None = None) -> dict[str, Any]:
        """Get accumulated state through a superstep.

        State is computed by folding step values. superstep=None means latest.
        """
        ...

    @abstractmethod
    async def get_steps(
        self,
        run_id: str,
        *,
        superstep: int | None = None,
        show_internal: bool = False,
    ) -> list[StepRecord]:
        """Get step records through a superstep, hiding internal carriers by default."""
        ...

    async def get_checkpoint(self, run_id: str, *, superstep: int | None = None) -> Checkpoint:
        """Get a checkpoint for forking runs.

        Default implementation calls get_state + get_steps.
        """
        values = await self.get_state(run_id, superstep=superstep)
        steps = await self.get_steps(run_id, superstep=superstep)
        return Checkpoint(
            values=values,
            steps=steps,
            source_run_id=run_id,
            source_superstep=superstep,
        )

    async def fork_workflow_async(
        self,
        source_run_id: str,
        *,
        workflow_id: str | None = None,
        superstep: int | None = None,
    ) -> tuple[str, Checkpoint]:
        """Prepare a fork by materializing a checkpoint and suggested workflow_id."""
        source = await self.get_run_async(source_run_id)
        if source is None:
            raise ValueError(f"Unknown source workflow_id: {source_run_id!r}")
        new_workflow_id = _resolve_fork_workflow_id(source_run_id, workflow_id)
        checkpoint = await self.get_checkpoint(source_run_id, superstep=superstep)
        return new_workflow_id, checkpoint

    async def retry_workflow_async(
        self,
        source_run_id: str,
        *,
        workflow_id: str | None = None,
        superstep: int | None = None,
    ) -> tuple[str, Checkpoint]:
        """Prepare a retry fork with retry lineage metadata."""
        source = await self.get_run_async(source_run_id)
        if source is None:
            raise ValueError(f"Unknown source workflow_id: {source_run_id!r}")
        checkpoint = await self.get_checkpoint(source_run_id, superstep=superstep)
        retry_count = await self.count_runs(retry_of=source_run_id)
        retry_index = retry_count + 1
        new_workflow_id = workflow_id or f"{source_run_id}-retry-{retry_index}"
        checkpoint.retry_of = source_run_id
        checkpoint.retry_index = retry_index
        return new_workflow_id, checkpoint

    @abstractmethod
    async def get_run_async(self, run_id: str) -> Run | None:
        """Get run metadata. Returns None if not found."""
        ...

    @abstractmethod
    async def list_runs(
        self,
        *,
        status: WorkflowStatus | None = None,
        graph_name: str | None = None,
        since: datetime | None = None,
        parent_run_id: str | None | object = _UNSET,
        limit: int | None = 100,
    ) -> list[Run]:
        """List runs, optionally filtered by status and/or parent.

        Args:
            status: Filter to runs with this status (None = all).
            graph_name: Filter to runs for this graph.
            since: Inclusive creation-time boundary. Naive values mean UTC.
            parent_run_id: Filter by parent relationship. Omitted means all,
                None means top-level, and a run id means direct children.
            limit: Max results to return. ``None`` returns all matches.
        """
        ...

    async def count_runs(
        self,
        *,
        status: WorkflowStatus | None = None,
        parent_run_id: str | None | object = _UNSET,
        retry_of: str | None = None,
    ) -> int:
        """Count runs matching a small set of backend-neutral filters.

        Backends with efficient query support should override this to avoid
        materializing full run objects for simple counting operations.
        """
        if parent_run_id is _UNSET:
            runs = await self.list_runs(status=status, limit=None)
        else:
            runs = await self.list_runs(status=status, parent_run_id=parent_run_id, limit=None)
        if retry_of is not None:
            runs = [run for run in runs if run.retry_of == retry_of]
        return len(runs)

    async def search_async(self, query: str, *, field: str | None = None, limit: int = 20) -> list[StepRecord]:
        """Search steps using FTS. Returns empty list if not supported."""
        return []

    # === Attempt Ledger (internal seam) ===
    #
    # Durable retry/timeout persistence (#229). Backends that support the
    # ledger override every method below; the defaults fail loudly so an
    # unsupported backend can never silently drop retry evidence.

    def _attempt_ledger_unsupported(self) -> NotImplementedError:
        return NotImplementedError(
            f"{type(self).__name__} does not support the durable attempt ledger.\n\n"
            "How to fix:\n"
            "  Use a checkpointer that implements the attempt-series operations\n"
            "  (MemoryCheckpointer or SqliteCheckpointer), or implement them on your backend."
        )

    async def open_attempt_series(
        self,
        run_id: str,
        node_name: str,
        *,
        policy_fingerprint: str,
        max_attempts: int,
        deadline_at: datetime | None = None,
    ) -> AttemptSeries:
        """Open a durable attempt series for one logical node execution.

        At most one open series may exist per (run_id, node_name); the series
        id stays stable across scheduler/superstep drift.
        """
        raise self._attempt_ledger_unsupported()

    async def get_attempt_series(self, series_id: str) -> AttemptSeries | None:
        """Get a series (open or closed) by id. Returns None if unknown."""
        raise self._attempt_ledger_unsupported()

    async def get_open_attempt_series(self, run_id: str, node_name: str) -> AttemptSeries | None:
        """Resume query: the open series for a node, if any."""
        raise self._attempt_ledger_unsupported()

    async def get_attempt_records(self, series_id: str) -> list[AttemptRecord]:
        """All attempt records of a series, ordered by attempt number."""
        raise self._attempt_ledger_unsupported()

    async def remaining_attempts(self, series_id: str) -> int:
        """Resume query: ``max_attempts`` minus consumed reservations.

        Every committed reservation counts — including crash-stranded
        ``STARTED``/``OUTCOME_UNKNOWN`` rows.
        """
        raise self._attempt_ledger_unsupported()

    async def begin_attempt(
        self,
        series_id: str,
        *,
        policy_fingerprint: str,
        scheduled_superstep: int,
    ) -> AttemptRecord:
        """Atomically verify fingerprint + budget + deadline, then commit STARTED.

        The reservation writes through immediately — independent of the
        ``CheckpointPolicy.durability`` timing StepRecords use — and only
        returns after it is durable. On persistence failure it raises before
        any user code may run; nothing is consumed.

        If the series already holds a STARTED row, this raises
        :class:`AttemptLedgerError`: the ledger has no dead/live discriminator,
        so an existing reservation is never converted or reserved over. A
        resume path settles stranded rows via
        :meth:`resolve_stranded_attempts` first.
        """
        raise self._attempt_ledger_unsupported()

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
        """Settle a non-final attempt outcome (FAILED/TIMED_OUT/CANCELLED).

        Backoff is sampled once by the caller and persisted here as data
        (``retry_not_before`` + ``sampled_delay``); a restart neither redraws
        jitter nor restarts the delay. SUCCEEDED must go through
        :meth:`close_attempt_series` so success and its StepRecord are atomic.
        """
        raise self._attempt_ledger_unsupported()

    async def close_attempt_series(
        self,
        series_id: str,
        attempt_number: int,
        status: AttemptStatus,
        *,
        step_record: StepRecord,
        error: AttemptError | None = None,
    ) -> None:
        """Atomically settle the final attempt, link its StepRecord, and close.

        The final outcome, the linked StepRecord (which must carry
        ``attempt_series_id``), series closure, and retention effects commit
        as one unit — either all are durable or none are.
        """
        raise self._attempt_ledger_unsupported()

    async def resolve_stranded_attempts(self, series_id: str) -> list[AttemptRecord]:
        """Durably settle crash-stranded STARTED rows as OUTCOME_UNKNOWN.

        This is the ONLY path that converts STARTED rows. Precondition: the
        caller holds the single-live-invocation assertion — it must know no
        other invocation of this series can still be running (the runner's
        resume path owns that via its workflow reservation). Stranded work is
        never invented as cancelled or never-run — external side effects may
        have completed. Returns the full record list after settling.
        """
        raise self._attempt_ledger_unsupported()

    # === Lifecycle ===

    async def initialize(self) -> None:  # noqa: B027
        """Initialize the checkpointer (create tables, etc.)."""

    async def close(self) -> None:  # noqa: B027
        """Clean up resources (close connections, etc.)."""
