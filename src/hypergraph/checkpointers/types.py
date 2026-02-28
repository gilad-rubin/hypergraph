"""Checkpointer types for workflow persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _utcnow() -> datetime:
    """UTC-aware datetime (avoids deprecated utcnow)."""
    return datetime.now(timezone.utc)


class StepStatus(Enum):
    """Status of a single step execution."""

    COMPLETED = "completed"
    FAILED = "failed"


class WorkflowStatus(Enum):
    """Status of a workflow."""

    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class StepRecord:
    """Single atomic record of a node execution.

    Contains both metadata and output values. The checkpointer
    saves each StepRecord atomically — either all data is saved
    or nothing.

    Attributes:
        workflow_id: Workflow this step belongs to.
        superstep: Parallel execution round (0-indexed).
        node_name: Name of the executed node.
        index: Global sequential index across all supersteps.
        status: Whether the node completed or failed.
        input_versions: Version numbers of inputs consumed.
        values: Output values produced by the node.
        duration_ms: Wall-clock execution time.
        cached: Whether this was a cache hit.
        decision: Gate routing decision, if applicable.
        error: Error message if status is FAILED.
        created_at: When execution started.
        completed_at: When execution finished.
        child_workflow_id: For nested graphs (GraphNode).
    """

    workflow_id: str
    superstep: int
    node_name: str
    index: int
    status: StepStatus
    input_versions: dict[str, int]
    values: dict[str, Any] | None = None
    duration_ms: float = 0.0
    cached: bool = False
    decision: str | list[str] | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    completed_at: datetime | None = None
    child_workflow_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict with only primitive types."""
        return {
            "workflow_id": self.workflow_id,
            "superstep": self.superstep,
            "node_name": self.node_name,
            "index": self.index,
            "status": self.status.value,
            "input_versions": self.input_versions,
            "values": self.values,
            "duration_ms": self.duration_ms,
            "cached": self.cached,
            "decision": self.decision,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "child_workflow_id": self.child_workflow_id,
        }


@dataclass
class Workflow:
    """Workflow metadata record."""

    id: str
    status: WorkflowStatus
    graph_name: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    completed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict."""
        return {
            "id": self.id,
            "status": self.status.value,
            "graph_name": self.graph_name,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


@dataclass
class Checkpoint:
    """Point-in-time snapshot for forking workflows.

    Combines accumulated state and step history at a given superstep.
    """

    values: dict[str, Any]
    steps: list[StepRecord]
