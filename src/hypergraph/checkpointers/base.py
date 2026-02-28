"""Checkpointer base class and checkpoint policy."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from hypergraph.checkpointers.types import Checkpoint, StepRecord, Workflow, WorkflowStatus


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
        ttl: Auto-expire completed workflows after this duration.
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
        if self.retention != "windowed" and self.window is not None:
            raise ValueError(f'window parameter only valid with retention="windowed", got retention="{self.retention}"')


class Checkpointer(ABC):
    """Base class for workflow persistence.

    Steps are the source of truth. State is computed from steps.
    Implementations store workflow steps and provide state retrieval.

    The runner calls save_step() after each node completes, and
    create_workflow/update_workflow_status for lifecycle management.
    """

    def __init__(self, policy: CheckpointPolicy | None = None):
        self.policy = policy or CheckpointPolicy()

    # === Write Operations ===

    @abstractmethod
    async def save_step(self, record: StepRecord) -> None:
        """Save a step atomically.

        Uses upsert semantics with unique constraint on
        (workflow_id, superstep, node_name).
        """
        ...

    @abstractmethod
    async def create_workflow(self, workflow_id: str, *, graph_name: str | None = None) -> Workflow:
        """Create a new workflow record. Called by runner at run start."""
        ...

    @abstractmethod
    async def update_workflow_status(self, workflow_id: str, status: WorkflowStatus) -> None:
        """Update workflow status (ACTIVE, COMPLETED, or FAILED)."""
        ...

    # === Read Operations ===

    @abstractmethod
    async def get_state(self, workflow_id: str, *, superstep: int | None = None) -> dict[str, Any]:
        """Get accumulated state through a superstep.

        State is computed by folding step values. superstep=None means latest.
        """
        ...

    @abstractmethod
    async def get_steps(self, workflow_id: str, *, superstep: int | None = None) -> list[StepRecord]:
        """Get step records through a superstep (None = all)."""
        ...

    async def get_checkpoint(self, workflow_id: str, *, superstep: int | None = None) -> Checkpoint:
        """Get a checkpoint for forking workflows.

        Default implementation calls get_state + get_steps.
        """
        values = await self.get_state(workflow_id, superstep=superstep)
        steps = await self.get_steps(workflow_id, superstep=superstep)
        return Checkpoint(values=values, steps=steps)

    @abstractmethod
    async def get_workflow(self, workflow_id: str) -> Workflow | None:
        """Get workflow metadata. Returns None if not found."""
        ...

    @abstractmethod
    async def list_workflows(self, *, status: WorkflowStatus | None = None, limit: int = 100) -> list[Workflow]:
        """List workflows, optionally filtered by status."""
        ...

    # === Lifecycle ===

    async def initialize(self) -> None:  # noqa: B027
        """Initialize the checkpointer (create tables, etc.)."""

    async def close(self) -> None:  # noqa: B027
        """Clean up resources (close connections, etc.)."""
