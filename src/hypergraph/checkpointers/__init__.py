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
    AnswerRejectedError,
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
    PauseAlreadySettledError,
    PauseSettlementError,
    PauseSlot,
    PendingNode,
    Run,
    RunTable,
    RunTotals,
    StalePauseError,
    StepRecord,
    StepStatus,
    StepTable,
    WorkflowStatus,
    node_address,
)

__all__ = [
    "AnswerRejectedError",
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
    "PauseAlreadySettledError",
    "PauseSettlementError",
    "PauseSlot",
    "PendingNode",
    "PickleSerializer",
    "Run",
    "RunInspector",
    "RunTable",
    "RunTotals",
    "Serializer",
    "SqliteCheckpointer",
    "SqliteRunInspector",
    "StalePauseError",
    "StepRecord",
    "StepStatus",
    "StepTable",
    "SyncCheckpointerProtocol",
    "WorkflowStatus",
    "node_address",
]
