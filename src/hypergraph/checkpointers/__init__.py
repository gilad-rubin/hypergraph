"""Checkpointer package for run persistence.

Provides the ``Checkpointer`` ABC, ``SqliteCheckpointer`` implementation,
and supporting types for durable workflow execution.
"""

from hypergraph.checkpointers.base import Checkpointer, CheckpointPolicy
from hypergraph.checkpointers.protocols import SyncCheckpointerProtocol
from hypergraph.checkpointers.serializers import JsonSerializer, PickleSerializer, Serializer
from hypergraph.checkpointers.sqlite import SqliteCheckpointer
from hypergraph.checkpointers.types import (
    Checkpoint,
    LineageRow,
    LineageView,
    Run,
    RunTable,
    StepRecord,
    StepStatus,
    StepTable,
    WorkflowStatus,
)

__all__ = [
    "Checkpointer",
    "CheckpointPolicy",
    "Checkpoint",
    "JsonSerializer",
    "LineageRow",
    "LineageView",
    "PickleSerializer",
    "Run",
    "RunTable",
    "Serializer",
    "SqliteCheckpointer",
    "StepRecord",
    "StepStatus",
    "StepTable",
    "SyncCheckpointerProtocol",
    "WorkflowStatus",
]
