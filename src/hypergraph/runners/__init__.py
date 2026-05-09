"""Execution runners for hypergraph."""

from hypergraph.runners._shared.handles import AsyncMapHandle, AsyncRunHandle, SyncMapHandle, SyncRunHandle
from hypergraph.runners._shared.inspect import FailureCase, RunView
from hypergraph.runners._shared.types import (
    ErrorHandling,
    GraphState,
    MapLog,
    MapResult,
    NodeExecution,
    NodeRecord,
    NodeStats,
    PauseExecution,
    PauseInfo,
    RunLog,
    RunnerCapabilities,
    RunResult,
    RunStatus,
)
from hypergraph.runners.async_ import AsyncRunner
from hypergraph.runners.base import BaseRunner
from hypergraph.runners.daft import DaftRunner
from hypergraph.runners.sync import SyncRunner

__all__ = [
    # Core types
    "ErrorHandling",
    "RunStatus",
    "PauseExecution",
    "PauseInfo",
    "RunResult",
    "MapResult",
    "FailureCase",
    "RunView",
    "RunLog",
    "MapLog",
    "NodeRecord",
    "NodeStats",
    "RunnerCapabilities",
    "GraphState",
    "NodeExecution",
    # Runners
    "BaseRunner",
    "SyncRunner",
    "AsyncRunner",
    "DaftRunner",
    "SyncRunHandle",
    "AsyncRunHandle",
    "SyncMapHandle",
    "AsyncMapHandle",
]
