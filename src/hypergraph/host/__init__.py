"""Durable host (Tier 1 local host) — submit, execute, and watch runs.

One machine, zero extra infrastructure: a SQLite Run Home, one
product-owned worker, and a backend-neutral client that needs no graph
code. Tier 0 (direct runner execution) is unchanged.
"""

from hypergraph.host.batch import BatchTolerance
from hypergraph.host.client import RunHomeClient
from hypergraph.host.definition import DefinitionId, definition_struct_hash
from hypergraph.host.errors import (
    AlreadyTerminalError,
    BuilderIdentityError,
    FollowDeadlineExpired,
    ForkCompatibilityError,
    HostError,
    ItemKeyError,
    NoServingWorkerError,
    RerunError,
    ReservedFactKindError,
    UnservedGraphError,
    WorkerLockError,
    WorkflowIdConflictError,
)
from hypergraph.host.home import RunHome, WorkerCoverage
from hypergraph.host.host import GraphBuilder, Host, SubmitReceipt, serve
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
from hypergraph.host.watch import (
    Attention,
    ConsolePanel,
    ItemProgress,
    LogPanel,
    SubmissionProgress,
    SubmissionWatcher,
    WatchSnapshot,
    render_snapshot,
    snapshot_line,
    watch_snapshot,
    watch_submissions,
)

__all__ = [
    "Attention",
    "ConsolePanel",
    "ItemProgress",
    "LogPanel",
    "SubmissionProgress",
    "SubmissionWatcher",
    "WatchSnapshot",
    "render_snapshot",
    "snapshot_line",
    "watch_snapshot",
    "watch_submissions",
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
    "BuilderIdentityError",
    "CommandReceipt",
    "DefinitionId",
    "definition_struct_hash",
    "FollowDeadlineExpired",
    "ForkCompatibilityError",
    "GraphBuilder",
    "Host",
    "HostError",
    "HostRuntime",
    "ItemKeyError",
    "NodeTimingReadModel",
    "NoServingWorkerError",
    "NodeTimingsReadModel",
    "PauseReadModel",
    "RUN_READ_STATUS_VALUES",
    "RerunError",
    "ReservedFactKindError",
    "RunHome",
    "WorkerCoverage",
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
