"""Checkpointer package for run persistence.

Provides the ``Checkpointer`` ABC, ``SqliteCheckpointer`` implementation,
and supporting types for durable workflow execution.
"""

from hypergraph.checkpointers.base import Checkpointer, CheckpointPolicy
from hypergraph.checkpointers.inspection import RunInspector, SqliteRunInspector
from hypergraph.checkpointers.memory import MemoryCheckpointer
from hypergraph.checkpointers.protocols import SyncCheckpointerProtocol
from hypergraph.checkpointers.serializers import JsonSerializer, PickleSerializer, Serializer
from hypergraph.checkpointers.sqlite import SqliteCheckpointer
from hypergraph.checkpointers.types import (
    AttemptError,
    AttemptLedgerError,
    AttemptRecord,
    AttemptSeries,
    AttemptStatus,
    BoundaryState,
    Checkpoint,
    LineageRow,
    LineageView,
    NodeBoundary,
    PendingNode,
    Run,
    RunTable,
    StepRecord,
    StepStatus,
    StepTable,
    WorkflowStatus,
    node_address,
)

__all__ = [
    "AttemptError",
    "AttemptLedgerError",
    "AttemptRecord",
    "AttemptSeries",
    "AttemptStatus",
    "BoundaryState",
    "Checkpointer",
    "CheckpointPolicy",
    "Checkpoint",
    "JsonSerializer",
    "LineageRow",
    "LineageView",
    "MemoryCheckpointer",
    "NodeBoundary",
    "PendingNode",
    "PickleSerializer",
    "Run",
    "RunInspector",
    "RunTable",
    "Serializer",
    "SqliteCheckpointer",
    "SqliteRunInspector",
    "StepRecord",
    "StepStatus",
    "StepTable",
    "SyncCheckpointerProtocol",
    "WorkflowStatus",
    "node_address",
]
