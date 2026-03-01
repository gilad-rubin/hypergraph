"""Protocols for checkpointer capabilities.

Separates sync write operations into a runtime-checkable Protocol
so that the async Checkpointer ABC stays lean (ISP-compliant).
Only checkpointers that support sync writes (like SqliteCheckpointer)
implement this protocol; async-only implementations are not forced
to stub out methods they can't provide.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from hypergraph.checkpointers.types import Run, StepRecord, WorkflowStatus


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
