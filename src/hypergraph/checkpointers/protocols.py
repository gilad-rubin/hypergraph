"""Protocols for checkpointer capabilities.

Separates optional capabilities out of the async Checkpointer ABC so it stays
lean (ISP-compliant) and async-only implementations are not forced to stub out
methods they can't provide.

The protocols here are NOT interchangeable in how a caller may treat a failed
probe, so read the seam before adding one:

- **Required capability** — :class:`SyncCheckpointerProtocol`. ``SyncRunner``
  demands it: ``template_sync._get_sync_checkpointer`` raises ``TypeError``
  when a checkpointer with a ``workflow_id`` does not satisfy it. Failing the
  probe is a hard error, not a degraded mode.
- **Optional capability** — :class:`PendingNodeProtocol` /
  :class:`SyncPendingNodeProtocol`. Runners probe and simply skip the feature
  when it is absent, so a third-party checkpointer keeps working.

``runtime_checkable`` matches on attribute PRESENCE only — never on
signatures. Probes for optional seams therefore live in
``runners/_shared/pending_boundaries.py``, which additionally requires every
method of the seam to exist and be callable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from hypergraph.checkpointers.types import (
        Checkpoint,
        NodeBoundary,
        PendingNode,
        Run,
        StepRecord,
        WorkflowStatus,
    )


@runtime_checkable
class SyncCheckpointerProtocol(Protocol):
    """Sync write operations for checkpointers used with SyncRunner.

    SqliteCheckpointer implements this via its _sync_db() connection.
    The SyncRunnerTemplate checks isinstance(checkpointer, SyncCheckpointerProtocol)
    at run() entry when workflow_id is provided.
    """

    def create_run_sync(
        self,
        run_id: str,
        *,
        graph_name: str | None = None,
        parent_run_id: str | None = None,
        forked_from: str | None = None,
        fork_superstep: int | None = None,
        retry_of: str | None = None,
        retry_index: int | None = None,
        config: dict | None = None,
    ) -> Run: ...

    def save_step_sync(self, record: StepRecord) -> None: ...

    def update_run_status_sync(
        self,
        run_id: str,
        status: WorkflowStatus,
        *,
        duration_ms: float | None = None,
        node_count: int | None = None,
        error_count: int | None = None,
    ) -> None: ...

    def fork_workflow(
        self,
        source_run_id: str,
        *,
        workflow_id: str | None = None,
        superstep: int | None = None,
    ) -> tuple[str, Checkpoint]: ...

    def retry_workflow(
        self,
        source_run_id: str,
        *,
        workflow_id: str | None = None,
        superstep: int | None = None,
    ) -> tuple[str, Checkpoint]: ...


@runtime_checkable
class PendingNodeProtocol(Protocol):
    """Async durable pending-node boundary seam (PRD 0013) — optional.

    Runners probe for this before writing boundaries and skip the feature
    when it is absent, so a third-party checkpointer without the seam keeps
    working instead of hard-failing. Both methods belong to the seam: writing
    intent nobody can read back is not a usable capability.
    """

    async def record_pending_nodes(self, boundaries: Sequence[PendingNode]) -> None: ...

    async def get_node_boundaries(self, run_id: str) -> list[NodeBoundary]: ...


@runtime_checkable
class SyncPendingNodeProtocol(Protocol):
    """Sync mirror of :class:`PendingNodeProtocol` for ``SyncRunner``."""

    def record_pending_nodes_sync(self, boundaries: Sequence[PendingNode]) -> None: ...

    def get_node_boundaries_sync(self, run_id: str) -> list[NodeBoundary]: ...
