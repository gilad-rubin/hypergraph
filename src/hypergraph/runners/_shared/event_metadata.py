"""Shared internal metadata objects for execution events."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hypergraph.runners._shared.types import RunResult


@dataclass(frozen=True, slots=True)
class RunContext:
    """Stable execution context shared by run-level events."""

    workflow_id: str | None = None
    item_index: int | None = None


@dataclass(frozen=True, slots=True)
class RunLineage:
    """Lineage metadata exported on run start."""

    parent_workflow_id: str | None = None
    forked_from: str | None = None
    fork_superstep: int | None = None
    retry_of: str | None = None
    retry_index: int | None = None
    is_resume: bool = False


DEFAULT_RUN_CONTEXT = RunContext()
DEFAULT_RUN_LINEAGE = RunLineage()


@dataclass(frozen=True, slots=True)
class BatchSummary:
    """Bounded aggregate summary for parent map runs."""

    total_items: int
    completed_items: int
    failed_items: int
    paused_items: int
    stopped_items: int
    outcome: str

    @classmethod
    def from_results(cls, results: Sequence[RunResult]) -> BatchSummary:
        """Build a summary from mapped child run results."""
        from hypergraph.runners._shared.types import RunStatus

        total_items = len(results)
        completed_items = sum(1 for result in results if result.status == RunStatus.COMPLETED)
        failed_items = sum(1 for result in results if result.status == RunStatus.FAILED)
        paused_items = sum(1 for result in results if result.status == RunStatus.PAUSED)
        stopped_items = sum(1 for result in results if result.status == RunStatus.STOPPED)
        return cls(
            total_items=total_items,
            completed_items=completed_items,
            failed_items=failed_items,
            paused_items=paused_items,
            stopped_items=stopped_items,
            outcome=_batch_outcome(
                total_items=total_items,
                completed_items=completed_items,
                failed_items=failed_items,
                paused_items=paused_items,
                stopped_items=stopped_items,
            ),
        )

    @property
    def event_status_value(self) -> str:
        """Status value to export on the parent RunEndEvent."""
        if self.outcome == "failed":
            return "failed"
        if self.outcome == "partial":
            return "partial"
        if self.outcome == "paused":
            return "paused"
        if self.outcome == "stopped":
            return "stopped"
        return "completed"


def _batch_outcome(
    *,
    total_items: int,
    completed_items: int,
    failed_items: int,
    paused_items: int,
    stopped_items: int,
) -> str:
    """Summarize mapped child outcomes for export-oriented observability."""
    if total_items == 0:
        return "completed"
    if failed_items == total_items:
        return "failed"
    if failed_items > 0:
        return "partial"
    if paused_items > 0:
        return "paused"
    if stopped_items > 0:
        return "stopped"
    return "completed"
