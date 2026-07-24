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
    RerunError,
    WorkerLockError,
    WorkflowIdConflictError,
)
from hypergraph.host.home import RunHome
from hypergraph.host.host import Host, SubmitReceipt, serve
from hypergraph.host.refs import BatchCommandReceipt, BatchRef, BatchSubmitReceipt, CommandReceipt, RunRef
from hypergraph.host.views import BatchUpdate, BatchView, RunQuery, RunUpdate, RunView, WaitingCondition

__all__ = [
    "AlreadyTerminalError",
    "BatchCommandReceipt",
    "BatchRef",
    "BatchSubmitReceipt",
    "BatchTolerance",
    "BatchUpdate",
    "BatchView",
    "CommandReceipt",
    "DefinitionId",
    "ForkCompatibilityError",
    "Host",
    "HostError",
    "RerunError",
    "RunHome",
    "RunHomeClient",
    "RunQuery",
    "RunRef",
    "RunUpdate",
    "RunView",
    "SubmitReceipt",
    "WaitingCondition",
    "WorkerLockError",
    "WorkflowIdConflictError",
    "serve",
]
