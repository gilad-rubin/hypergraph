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
from hypergraph.host.refs import RunRef

# Terminal run statuses (mirrors the checkpointer's completed_at semantics).
TERMINAL_WORKFLOW_STATUSES: frozenset[WorkflowStatus] = frozenset(
    {
        WorkflowStatus.COMPLETED,
        WorkflowStatus.FAILED,
        WorkflowStatus.PARTIAL,
        WorkflowStatus.STOPPED,
    }
)


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
            ``status``, ``command``, ``recovery_exhausted`` — or an event
            class name for previews.
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
    """

    definition: str | None = None
    status: WorkflowStatus | None = None
    waiting: WaitingCondition | None = None
    older_than: timedelta | None = None
    limit: int = 100
