"""Inspection adapters for querying persisted runs.

The core ``Checkpointer`` contract is intentionally small and runner-focused.
Inspection surfaces such as the CLI should depend on a dedicated adapter
instead of reaching into a concrete backend's convenience API directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from hypergraph.checkpointers.sqlite import SqliteCheckpointer
    from hypergraph.checkpointers.types import Checkpoint, LineageView, Run, StepRecord, WorkflowStatus

_UNSET = object()


class RunInspector(Protocol):
    """Backend-neutral sync inspection interface for persisted workflow runs."""

    @property
    def db_path(self) -> str: ...

    def get_run(self, run_id: str) -> Run | None: ...

    def runs(
        self,
        *,
        status: WorkflowStatus | None = None,
        graph_name: str | None = None,
        since: datetime | None = None,
        parent_run_id: str | None | object = _UNSET,
        limit: int | None = 100,
    ) -> list[Run]: ...

    def steps(self, run_id: str, *, superstep: int | None = None) -> list[StepRecord]: ...

    def state(self, run_id: str, *, superstep: int | None = None) -> dict[str, Any]: ...

    def search(self, query: str, *, field: str | None = None, limit: int = 20) -> list[StepRecord]: ...

    def stats(self, run_id: str) -> dict[str, Any]: ...

    def checkpoint(self, run_id: str, *, superstep: int | None = None) -> Checkpoint: ...

    def lineage(self, workflow_id: str, *, include_steps: bool = True, max_runs: int = 200) -> LineageView: ...


@dataclass(frozen=True)
class SqliteRunInspector:
    """Inspection adapter over ``SqliteCheckpointer`` sync query helpers."""

    checkpointer: SqliteCheckpointer

    @property
    def db_path(self) -> str:
        return self.checkpointer._path

    def get_run(self, run_id: str) -> Run | None:
        return self.checkpointer.get_run(run_id)

    def runs(
        self,
        *,
        status: WorkflowStatus | None = None,
        graph_name: str | None = None,
        since: datetime | None = None,
        parent_run_id: str | None | object = _UNSET,
        limit: int | None = 100,
    ) -> list[Run]:
        kwargs: dict[str, Any] = {
            "status": status,
            "graph_name": graph_name,
            "since": since,
            "limit": limit,
        }
        if parent_run_id is not _UNSET:
            kwargs["parent_run_id"] = parent_run_id
        return self.checkpointer.runs(**kwargs)

    def steps(self, run_id: str, *, superstep: int | None = None) -> list[StepRecord]:
        return self.checkpointer.steps(run_id, superstep=superstep)

    def state(self, run_id: str, *, superstep: int | None = None) -> dict[str, Any]:
        return self.checkpointer.state(run_id, superstep=superstep)

    def search(self, query: str, *, field: str | None = None, limit: int = 20) -> list[StepRecord]:
        return self.checkpointer.search(query, field=field, limit=limit)

    def stats(self, run_id: str) -> dict[str, Any]:
        return self.checkpointer.stats(run_id)

    def checkpoint(self, run_id: str, *, superstep: int | None = None) -> Checkpoint:
        return self.checkpointer.checkpoint(run_id, superstep=superstep)

    def lineage(self, workflow_id: str, *, include_steps: bool = True, max_runs: int = 200) -> LineageView:
        return self.checkpointer.lineage(workflow_id, include_steps=include_steps, max_runs=max_runs)
