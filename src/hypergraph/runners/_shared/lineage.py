"""Pure workflow lineage decisions shared by runner templates."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

from hypergraph.checkpointers.types import Checkpoint, Run
from hypergraph.exceptions import (
    GraphChangedError,
    InputOverrideRequiresForkError,
    WorkflowAlreadyCompletedError,
    WorkflowForkError,
    WorkflowStoppedError,
)
from hypergraph.runners._shared.event_metadata import RunLineage
from hypergraph.runners._shared.state_restore import is_interrupt_resume_payload

if TYPE_CHECKING:
    from hypergraph.graph import Graph


class ResumeAction(Enum):
    """I/O action selected by the existing-run policy."""

    START_NEW = "start_new"
    USE_CHECKPOINT = "use_checkpoint"
    FORK_EXISTING = "fork_existing"
    RESUME_EXISTING = "resume_existing"


def validate_lineage_request(
    *,
    checkpoint: Checkpoint | None,
    fork_from: str | None,
    retry_from: str | None,
) -> None:
    """Reject conflicting checkpoint lineage mechanisms before any I/O."""
    if fork_from is not None and retry_from is not None:
        raise ValueError("Cannot pass both fork_from and retry_from. Choose one lineage source.")
    if checkpoint is not None and (fork_from is not None or retry_from is not None):
        raise ValueError("Cannot combine checkpoint with fork_from/retry_from. Use one forking mechanism.")


def resolve_existing_run(
    *,
    existing_run: Run | None,
    checkpoint: Checkpoint | None,
    override_workflow: bool,
    workflow_id: str,
    graph_hash: str,
    graph: Graph,
    resume_values: dict[str, Any],
) -> ResumeAction:
    """Select the checkpoint action while preserving rejection precedence."""
    if checkpoint is not None:
        if existing_run is not None:
            raise WorkflowForkError(f"Cannot fork into existing workflow '{workflow_id}'. Use a new workflow_id.")
        return ResumeAction.USE_CHECKPOINT

    if existing_run is None:
        return ResumeAction.START_NEW

    if override_workflow:
        return ResumeAction.FORK_EXISTING

    previous_hash = (existing_run.config or {}).get("graph_struct_hash")
    if previous_hash is not None and previous_hash != graph_hash:
        raise GraphChangedError(workflow_id)
    if existing_run.status.value == "stopped":
        if not resume_values:
            raise WorkflowStoppedError(workflow_id)
    elif resume_values and not is_interrupt_resume_payload(graph, resume_values):
        raise InputOverrideRequiresForkError(workflow_id)
    if existing_run.status.value == "completed":
        raise WorkflowAlreadyCompletedError(workflow_id)
    return ResumeAction.RESUME_EXISTING


def plan_lineage(
    *,
    parent_workflow_id: str | None,
    checkpoint: Checkpoint | None,
    action: ResumeAction,
) -> RunLineage:
    """Project the selected resume action into run-start lineage metadata."""
    forked_from: str | None = None
    fork_superstep: int | None = None
    retry_of: str | None = None
    retry_index: int | None = None
    if checkpoint is not None and action is not ResumeAction.RESUME_EXISTING:
        forked_from = checkpoint.source_run_id
        fork_superstep = checkpoint.source_superstep
        retry_of = checkpoint.retry_of
        retry_index = checkpoint.retry_index
    is_resume = checkpoint is not None and forked_from is None and retry_of is None
    return RunLineage(
        parent_workflow_id=parent_workflow_id,
        forked_from=forked_from,
        fork_superstep=fork_superstep,
        retry_of=retry_of,
        retry_index=retry_index,
        is_resume=is_resume,
    )
