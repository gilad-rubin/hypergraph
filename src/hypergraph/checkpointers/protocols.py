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
signatures. Every optional seam is therefore probed through :func:`probe_seam`,
which additionally requires every method of the seam to exist and be callable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from hypergraph.checkpointers.types import NO_RUN_TOTALS, RunTotals

if TYPE_CHECKING:
    from hypergraph.checkpointers.types import (
        Checkpoint,
        NodeBoundary,
        PauseSlot,
        PendingNode,
        Run,
        StepRecord,
        WorkflowStatus,
    )


def probe_seam(checkpointer: object | None, protocol: type, methods: Sequence[str]) -> bool:
    """Whether ``checkpointer`` really implements one optional seam.

    ``None`` (no checkpointer) answers False, so callers need no null guard.
    Beyond the ``runtime_checkable`` isinstance check — which matches on
    attribute PRESENCE only, so a checkpointer carrying an unrelated
    attribute of the same name would probe true and then fail mid-run — every
    named method must exist AND be callable. Demanding the whole seam makes
    that false positive structurally hard rather than merely unlikely.

    Seam-specific preconditions (``pending_boundaries`` excludes
    ``durability="exit"``) belong at the call site, not here: they are facts
    about one feature, not about whether the seam is implemented.
    """
    if checkpointer is None:
        return False
    if not isinstance(checkpointer, protocol):
        return False
    return all(callable(getattr(checkpointer, name, None)) for name in methods)


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
        inputs: dict[str, Any] | None = None,
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


@runtime_checkable
class PauseSlotProtocol(Protocol):
    """Async durable pause-slot seam (PRD 0010) — optional.

    ``record_pause`` is atomic by contract: everything it is handed — the
    slot, any buffered step records, and the run's transition to ``PAUSED``
    — commits as one unit, and it is never called before the paused
    StepRecord's own write. Runners probe for the whole seam before using it
    and fall back to the plain save-steps-then-set-status path when it is
    absent, so a third-party checkpointer keeps working.
    """

    async def record_pause(
        self,
        slot: PauseSlot,
        *,
        step_records: Sequence[StepRecord] = (),
        totals: RunTotals = NO_RUN_TOTALS,
    ) -> None: ...

    async def get_pause_slot(self, run_id: str, *, pause_id: str | None = None) -> PauseSlot | None: ...

    async def settle_pause(self, run_id: str, *, pause_id: str | None = None, value: Any) -> PauseSlot: ...


@runtime_checkable
class SyncPauseSlotProtocol(Protocol):
    """Sync mirror of :class:`PauseSlotProtocol`."""

    def record_pause_sync(
        self,
        slot: PauseSlot,
        *,
        step_records: Sequence[StepRecord] = (),
        totals: RunTotals = NO_RUN_TOTALS,
    ) -> None: ...

    def get_pause_slot_sync(self, run_id: str, *, pause_id: str | None = None) -> PauseSlot | None: ...

    def settle_pause_sync(self, run_id: str, *, pause_id: str | None = None, value: Any) -> PauseSlot: ...
