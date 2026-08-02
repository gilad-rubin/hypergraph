"""Durable host (Tier 1 local host) — submit, execute, and watch runs.

One machine, zero extra infrastructure: a SQLite Run Home, one
product-owned worker, and a backend-neutral client that needs no graph
code. Tier 0 (direct runner execution) is unchanged.
"""

from hypergraph.host.batch import BatchTolerance
from hypergraph.host.client import RunHomeClient
from hypergraph.host.definition import DefinitionId
from hypergraph.host.errors import (
    AlreadyTerminalError,
    ForkCompatibilityError,
    HostError,
    ItemKeyError,
    RerunError,
    UnservedGraphError,
    WorkerLockError,
    WorkflowIdConflictError,
)
from hypergraph.host.home import RunHome
from hypergraph.host.host import Host, SubmitReceipt, serve
from hypergraph.host.read_models import (
    RUN_READ_STATUS_VALUES,
    BatchItemReadModel,
    BatchReadModel,
    BatchSummaryReadModel,
    NodeTimingReadModel,
    NodeTimingsReadModel,
    PauseReadModel,
    RunHomeReadModel,
    RunReadModel,
    RunTimingReadModel,
    StepTimingReadModel,
)
from hypergraph.host.refs import BatchCommandReceipt, BatchRef, BatchSubmitReceipt, CommandReceipt, RunRef
from hypergraph.host.runtime import HostRuntime
from hypergraph.host.views import BatchItemView, BatchUpdate, BatchView, RunQuery, RunUpdate, RunView, WaitingCondition

__all__ = [
    "AlreadyTerminalError",
    "BatchCommandReceipt",
    "BatchItemView",
    "BatchItemReadModel",
    "BatchReadModel",
    "BatchRef",
    "BatchSubmitReceipt",
    "BatchSummaryReadModel",
    "BatchTolerance",
    "BatchUpdate",
    "BatchView",
    "CommandReceipt",
    "DefinitionId",
    "ForkCompatibilityError",
    "Host",
    "HostError",
    "HostRuntime",
    "ItemKeyError",
    "NodeTimingReadModel",
    "NodeTimingsReadModel",
    "PauseReadModel",
    "RUN_READ_STATUS_VALUES",
    "RerunError",
    "RunHome",
    "RunHomeClient",
    "RunHomeReadModel",
    "RunQuery",
    "RunReadModel",
    "RunRef",
    "RunTimingReadModel",
    "RunUpdate",
    "RunView",
    "StepTimingReadModel",
    "SubmitReceipt",
    "UnservedGraphError",
    "WaitingCondition",
    "WorkerLockError",
    "WorkflowIdConflictError",
    "serve",
]
