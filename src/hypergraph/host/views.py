"""Read models for the durable host.

Views report persisted facts only. ``waiting`` names a coordination
condition — it is never a ``WorkflowStatus`` and never enters the run row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import Any

from hypergraph.checkpointers.types import WorkflowStatus
from hypergraph.host.definition import DefinitionId
from hypergraph.host.refs import BatchRef, RunRef

# Terminal run statuses (mirrors the checkpointer's completed_at semantics).
TERMINAL_WORKFLOW_STATUSES: frozenset[WorkflowStatus] = frozenset(
    {
        WorkflowStatus.COMPLETED,
        WorkflowStatus.FAILED,
        WorkflowStatus.PARTIAL,
        WorkflowStatus.STOPPED,
    }
)
# The same vocabulary as stored strings, for the store's own row values.
TERMINAL_STATUS_VALUES: frozenset[str] = frozenset(status.value for status in TERMINAL_WORKFLOW_STATUSES)

# Submission states in which the host will never touch a submission again:
# 'finished' (terminal run, stop-before-start, or a tolerance trip that
# closed admission) and 'exhausted' (parked by the recovery brake).
SUBMISSION_STATE_FINISHED = "finished"
SUBMISSION_STATE_EXHAUSTED = "exhausted"
SETTLED_SUBMISSION_STATES: frozenset[str] = frozenset({SUBMISSION_STATE_FINISHED, SUBMISSION_STATE_EXHAUSTED})


def is_child_settled(submission_state: str | None, run_status: str | None) -> bool:
    """True when a Batch child can never change outcome again.

    THE settled-child rule. Every caller — the rerun gate, ``BatchView``,
    Batch stop, and Batch-owned workflow-id reuse — routes through this one
    predicate so a child is never settled for one and in flight for another.
    Both arguments are stored row values (``host_submissions.state`` and
    ``runs.status``), not enums, because the store is what they compare.

    Args:
        submission_state: The child's ``host_submissions.state``, or None
            when no submission row exists.
        run_status: The child's ``runs.status`` value, or None when the
            child has no runs row yet.

    Returns:
        True when the child reached a terminal run status or its submission
        is settled (finished or recovery-exhausted).
    """
    return run_status in TERMINAL_STATUS_VALUES or submission_state in SETTLED_SUBMISSION_STATES


class WaitingCondition(Enum):
    """Closed typed vocabulary naming why a Run waits.

    A waiting condition is a coordination fact, never a ``WorkflowStatus``.
    ``None`` on ``RunView.waiting`` means the Run is executing or terminal.
    """

    QUEUED = "queued"  # eligible, awaiting claim
    SCHEDULED = "scheduled"  # future start_at
    PAUSED = "paused"  # durable pause slot open
    VERSION_INCOMPATIBLE = "version_incompatible"  # no serving worker
    ADMISSION_LIMITED = "admission_limited"  # over the active-Run cap
    RECOVERY_EXHAUSTED = "recovery_exhausted"  # pinned recovery cap hit


@dataclass(frozen=True)
class RunUpdate:
    """One update observed through ``RunHomeClient.watch``.

    Attributes:
        cursor: Reconnectable cursor string (``"seq:N"``). Only durable
            updates advance it; live previews repeat the last durable cursor.
        durable: True for committed Run Home facts; False for best-effort
            live previews. Callers must only store cursors from durable
            updates.
        kind: Fact kind — ``submitted``, ``run_started``, ``step``,
            ``status``, ``command``, ``recovery_exhausted``, ``run_reset``
            — or an event class name for previews.
        payload: JSON-safe fact payload.
        timestamp: ISO timestamp of the fact (or of preview observation).
    """

    cursor: str
    durable: bool
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""


@dataclass(frozen=True)
class RunView:
    """Persisted facts about one Run, plus why it waits.

    Attributes:
        run_ref: Inert address of the run.
        workflow_id: The run's workflow id (same as ``run_ref.run_id``).
        definition_name: Definition the run was submitted against.
        status: The run's ``WorkflowStatus``, or None while no runs row
            exists yet (submission still pending).
        waiting: Typed waiting condition, or None. Produced today:
            ``QUEUED`` (accepted, execution not started), ``SCHEDULED``
            (future ``start_at``), ``PAUSED`` (runs row paused),
            ``VERSION_INCOMPATIBLE`` (no serving worker claims the pinned
            identity), and ``RECOVERY_EXHAUSTED`` (the pinned recovery cap
            tripped). ``ADMISSION_LIMITED`` stays reserved for a later host
            ticket. Never a WorkflowStatus.
        definition_id: The pinned Definition identity from the submission,
            or reconstructed from the runs row for host-less (Tier 0) runs.
            None only when neither exists.
        retry_of: Source workflow id when this run is a rerun (repetition),
            else None. Lineage never merges: when ``retry_of`` is set,
            ``forked_from`` is None.
        forked_from: Source workflow id when this run is a fork (migration),
            else None. Taken from the runs row when present, else the
            submission's recorded lineage.
    """

    run_ref: RunRef
    workflow_id: str
    definition_name: str
    status: WorkflowStatus | None
    waiting: WaitingCondition | None
    definition_id: DefinitionId | None
    retry_of: str | None
    forked_from: str | None


@dataclass(frozen=True)
class RunQuery:
    """Typed filter for ``RunHomeClient.list``.

    Every field is a typed value — never a free string — so queries branch
    on the same closed vocabulary views report. Omitted fields match
    everything.

    Attributes:
        definition: Restrict to one Definition name.
        status: Restrict to one ``WorkflowStatus`` (runs row required).
        waiting: Restrict to one ``WaitingCondition`` (the same typed
            waiting computation ``RunView.waiting`` uses).
        older_than: Restrict to work created at least this long ago
            (aged-unclaimed and backlog queries).
        limit: Maximum views returned, oldest first. Defaults to 100.
        batch: Restrict to children of one Batch — a ``BatchRef`` or a bare
            batch id string. Runs without Batch membership never match.
    """

    definition: str | None = None
    status: WorkflowStatus | None = None
    waiting: WaitingCondition | None = None
    older_than: timedelta | None = None
    limit: int = 100
    batch: BatchRef | str | None = None


# Closed bucket vocabulary for BatchView.counts. Every manifest item is
# accounted in exactly one bucket; terminal buckets are WorkflowStatus
# values so child outcomes share the Run vocabulary.
BATCH_COUNT_KEYS: tuple[str, ...] = (
    "completed",
    "failed",
    "partial",
    "stopped",
    "active",
    "queued",
    "recovery_exhausted",
    "unstarted",
)


@dataclass(frozen=True)
class BatchView:
    """Persisted facts about one Batch, keyed by logical item key.

    Attributes:
        batch_ref: Inert address of the Batch.
        workflow_id: The Batch's caller-chosen workflow id.
        definition_id: The pinned Definition identity from the manifest.
        counts: Children per state bucket over the closed
            ``BATCH_COUNT_KEYS`` vocabulary (all keys always present):
            terminal buckets (``completed``/``failed``/``partial``/
            ``stopped``) from the child runs row; ``active`` for a started
            nonterminal child; ``queued`` for a child not yet started but
            still claimable; ``recovery_exhausted`` for a parked child;
            ``unstarted`` for a child that finished without ever executing
            (stop-before-start). Every manifest item is accounted exactly
            once.
        outcomes: Logical item key → outcome, in manifest order: the
            terminal status string for settled children,
            ``"recovery_exhausted"`` for parked children, None while a
            child is in flight, and None for unstarted items (Hypergraph
            never fabricates results for items that never ran).
        unstarted_items: Manifest keys whose child never executed, in
            manifest order — requested but never admitted.
        settled: True when no child is active or queued (terminal,
            unstarted, and recovery-exhausted children are settled). A
            recovery-exhausted child counts as settled.
        tolerance_tripped: True when failure-equivalent children strictly
            exceeded a pinned tolerance, closing new child admission. A
            trip is a Batch fact, never a ``WorkflowStatus``: the Batch
            stays truthfully partial — mixed outcomes with the remaining
            items explicitly unstarted, never a failed or stopped Batch.
        retry_of: Source ``batch_id`` when this Batch was minted by
            ``client.rerun(batch_ref, ...)``, else None. The source Batch
            is never mutated; lineage points backwards only.
    """

    batch_ref: BatchRef
    workflow_id: str
    definition_id: DefinitionId
    counts: dict[str, int]
    outcomes: dict[str, str | None]
    unstarted_items: tuple[str, ...]
    settled: bool
    tolerance_tripped: bool
    retry_of: str | None


@dataclass(frozen=True)
class BatchUpdate:
    """One update observed through ``RunHomeClient.watch(batch_ref)``.

    Attributes:
        cursor: Reconnectable cursor string (``"bseq:N"``). Only durable
            updates advance it; live previews repeat the last durable cursor.
        durable: True for committed Run Home facts; False for best-effort
            live previews fanned in from child runs (same process only).
            Callers must only store cursors from durable updates.
        kind: Fact kind — ``manifest`` (bseq 1, the accepted start intent),
            ``child_settled`` (a child's terminal transition, committed in
            the same transaction as the child fact), ``tolerance_tripped``
            (a pinned tolerance was strictly exceeded, committed in that
            same transaction at the next ``bseq``), ``child_unstarted`` (an
            item that ended unstarted AFTER the trip fact already named its
            unstarted items — a claimed child a restart returned to pending
            and admission then refused), ``stopped`` (the durable Batch
            stop) — or an event class name for previews.
        payload: JSON-safe fact payload. ``child_settled`` carries
            ``item_key``, ``workflow_id``, and ``status``;
            ``tolerance_tripped`` carries ``failed``, ``total_items``, the
            pinned ``max_failed``/``max_failed_percent``, and the
            ``unstarted_items`` admission closed; ``child_unstarted``
            carries ``item_key`` and ``workflow_id``. Between them, the
            durable stream accounts every manifest item exactly once — a
            detached ``watch`` never needs the view to learn an outcome.
        timestamp: ISO timestamp of the fact (or of preview observation).
    """

    cursor: str
    durable: bool
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""
