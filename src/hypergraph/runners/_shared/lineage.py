"""Pure workflow lineage decisions shared by runner templates."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

from hypergraph.checkpointers.types import Checkpoint, Run
from hypergraph.exceptions import (
    CompactedRetentionError,
    GraphChangedError,
    InputOverrideRequiresForkError,
    RetryPolicyChangedError,
    WorkflowAlreadyCompletedError,
    WorkflowForkError,
    WorkflowStoppedError,
)
from hypergraph.runners._shared.event_metadata import RunLineage
from hypergraph.runners._shared.policy_manifest import RetryPolicyManifest, diff_policy_manifests
from hypergraph.runners._shared.state_restore import (
    is_interrupt_resume_payload,
    is_retention_baseline,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hypergraph.checkpointers.base import CheckpointPolicy
    from hypergraph.checkpointers.types import StepRecord
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


def _validate_run_identity(
    *,
    existing_run: Run,
    workflow_id: str,
    graph_hash: str,
    graph: Graph,
) -> None:
    """Reject a same-lineage resume whose graph or retry policy changed.

    Graph identity is checked before policy identity: a structural change is
    the coarser fact, and reporting a policy diff for a graph the workflow
    never ran would be noise.
    """
    previous_hash = (existing_run.config or {}).get("graph_struct_hash")
    if previous_hash is not None and previous_hash != graph_hash:
        raise GraphChangedError(workflow_id)
    # Policy compatibility (#232): validated here — before checkpoint
    # restoration and before create_run() can overwrite the stored config.
    # A missing manifest is a legacy config; the attempt ledger's
    # begin_attempt() fingerprint check remains its durable backstop.
    stored_manifest = RetryPolicyManifest.from_config(existing_run.config)
    if stored_manifest is not None:
        changes = diff_policy_manifests(stored_manifest, RetryPolicyManifest.from_graph(graph))
        if changes:
            raise RetryPolicyChangedError(workflow_id, changes)


def validate_map_parent_identity(
    *,
    existing_run: Run | None,
    workflow_id: str,
    graph_hash: str,
    graph: Graph,
) -> None:
    """Gate a crash-resumed ``map()`` at the parent batch boundary (#309).

    ``map()`` persists its parent row with an upsert, so the stored evidence
    of the previous batch is overwritten the moment that row is written. This
    reads that evidence first and applies the run path's identity rules to it
    — before any item executes and before the row can be rewritten.

    Identity only. A re-admitted batch is still allowed to be topped up, so
    the run path's non-identity rejections (already-completed, stopped,
    input-override-requires-fork) deliberately stay out of this boundary.
    """
    if existing_run is None:
        return
    _validate_run_identity(
        existing_run=existing_run,
        workflow_id=workflow_id,
        graph_hash=graph_hash,
        graph=graph,
    )


def find_compacted_producers(
    *,
    graph: Graph,
    steps: Sequence[StepRecord],
    active_nodes: frozenset[str] | set[str] | None,
) -> tuple[str, ...]:
    """Active nodes whose execution identity a retention baseline folded away.

    ``steps`` must be the source run's RAW history (``show_internal=True``):
    the baseline carrier is hidden from public step reads, and it is the only
    row that says which values outlived their producer's step record.

    A node qualifies when it is in the target's active scope, has no surviving
    row of its own at ANY status, and produces at least one value the baseline
    carries. Restore then sees a node with every input available and no
    execution on record — and runs it again.

    Status is deliberately not part of the test. A node whose newest row is
    PAUSED or FAILED is still on record: compaction pruned older attempts, not
    the node's identity, and re-executing it is the ordinary resume path.

    Node-set-conservative by construction: the baseline stores values without
    producer provenance, so a name collision between two producers of the same
    output can over-report. #277 makes it exact.
    """
    folded: set[str] = set()
    on_record: set[str] = set()
    for step in steps:
        if is_retention_baseline(step):
            folded.update(step.values or {})
        else:
            on_record.add(step.node_name)
    if not folded:
        return ()
    return tuple(
        sorted(
            name
            for name, node in graph._nodes.items()
            if name not in on_record and (active_nodes is None or name in active_nodes) and folded.intersection(node.outputs)
        )
    )


def validate_restorable_history(
    *,
    graph: Graph,
    steps: Sequence[StepRecord],
    active_nodes: frozenset[str] | set[str] | None,
    workflow_id: str | None,
    source_run_id: str,
    policy: CheckpointPolicy | None,
    is_retry: bool = False,
) -> None:
    """Refuse a fork/resume/retry that compaction would turn into re-execution (#239).

    State reconstruction and execution restoration are separate capabilities.
    Run inputs and folded step values rebuild the STATE of a compacted run
    fine; what compaction destroys is the EXECUTION identity of the nodes it
    pruned. Restoring anyway re-invokes them with their real side effects, and
    the run still reports COMPLETED — so the boundary refuses instead, before
    a single node executes.
    """
    pruned = find_compacted_producers(graph=graph, steps=steps, active_nodes=active_nodes)
    if not pruned:
        return
    raise CompactedRetentionError(
        workflow_id=workflow_id,
        source_run_id=source_run_id,
        pruned_nodes=pruned,
        retention=getattr(policy, "retention", None),
        window=getattr(policy, "window", None),
        is_retry=is_retry,
    )


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

    _validate_run_identity(
        existing_run=existing_run,
        workflow_id=workflow_id,
        graph_hash=graph_hash,
        graph=graph,
    )
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
