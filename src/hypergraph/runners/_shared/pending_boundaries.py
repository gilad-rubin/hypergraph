"""Durable pending node boundaries (PRD 0013).

A superstep's StepRecords are written when the superstep is over. A process
that dies after one sibling committed and before its siblings ran leaves a
journal gap: recovery would have to infer what was pending from an
incomplete record. This module closes that gap by persisting the superstep's
runnable node boundaries as *intent* before any sibling can cause external
work.

Sync and async runners share every decision here — the record shape, the
capability probe, and the write point — so the two paths cannot drift. Each
runner calls :func:`build_pending_nodes` and then its own thin dispatch
helper at the SAME place in the superstep loop: after the runnable batch is
fixed, before the first sibling dispatches.

A pending record is never execution truth. StepRecords remain the sole
execution journal; the boundary's state is derived by joining the two
(:class:`hypergraph.checkpointers.types.BoundaryState`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.checkpointers.protocols import PendingNodeProtocol, SyncPendingNodeProtocol
from hypergraph.checkpointers.types import PendingNode

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hypergraph.nodes.base import HyperNode


def supports_pending_boundaries(checkpointer: Any, *, sync: bool) -> bool:
    """Whether this checkpointer can and should persist node boundaries.

    Resolved once per run, not per superstep.

    ``durability="exit"`` is deliberately excluded: that mode buffers every
    StepRecord to run exit and advertises no mid-run recovery, so a boundary
    written mid-run could never be joined against a journal to classify it.
    The durable Host forbids ``"exit"`` outright, so the durable tier is
    unaffected.
    """
    if checkpointer is None:
        return False
    policy = getattr(checkpointer, "policy", None)
    if policy is not None and getattr(policy, "durability", None) == "exit":
        return False
    protocol = SyncPendingNodeProtocol if sync else PendingNodeProtocol
    return isinstance(checkpointer, protocol)


def build_pending_nodes(
    workflow_id: str,
    superstep_idx: int,
    ready_nodes: Sequence[HyperNode],
) -> list[PendingNode]:
    """Build one pending record per runnable sibling in this superstep.

    ``superstep_idx`` must be the resume-offset index the StepRecords will
    carry, so a boundary and its step share one address. A nested graph's
    delegating ``GraphNode`` is recorded under its parent-facing name here;
    the child run records its own boundaries under the child workflow id.
    """
    return [
        PendingNode(
            run_id=workflow_id,
            superstep=superstep_idx,
            node_name=node.name,
            node_type=type(node).__name__,
        )
        for node in ready_nodes
    ]


def record_pending_nodes_sync(
    checkpointer: Any,
    workflow_id: str,
    superstep_idx: int,
    ready_nodes: Sequence[HyperNode],
) -> None:
    """Persist this superstep's boundaries before any sibling dispatches."""
    checkpointer.record_pending_nodes_sync(build_pending_nodes(workflow_id, superstep_idx, ready_nodes))


async def record_pending_nodes_async(
    checkpointer: Any,
    workflow_id: str,
    superstep_idx: int,
    ready_nodes: Sequence[HyperNode],
) -> None:
    """Async mirror of :func:`record_pending_nodes_sync`."""
    await checkpointer.record_pending_nodes(build_pending_nodes(workflow_id, superstep_idx, ready_nodes))
