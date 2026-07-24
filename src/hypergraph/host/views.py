"""Read models for the durable host.

Views report persisted facts only. ``waiting`` names a coordination
condition — it is never a ``WorkflowStatus`` and never enters the run row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from hypergraph.checkpointers.types import WorkflowStatus
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
            ``status``, ``command`` — or an event class name for previews.
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
        waiting: Typed waiting condition, or None. Currently only
            ``WaitingCondition.QUEUED`` (submission accepted, execution not
            started) is produced; later host tickets produce further
            conditions from the same closed enum. Never a WorkflowStatus.
    """

    run_ref: RunRef
    workflow_id: str
    definition_name: str
    status: WorkflowStatus | None
    waiting: WaitingCondition | None
