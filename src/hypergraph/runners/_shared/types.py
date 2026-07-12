"""Compatibility re-exports for the canonical runner result and state types."""

from __future__ import annotations

from hypergraph.runners._shared.results import (
    DURATION_PRECISION as DURATION_PRECISION,
)
from hypergraph.runners._shared.results import (
    ErrorHandling as ErrorHandling,
)
from hypergraph.runners._shared.results import (
    MapLog as MapLog,
)
from hypergraph.runners._shared.results import (
    MapResult as MapResult,
)
from hypergraph.runners._shared.results import (
    NodeRecord as NodeRecord,
)
from hypergraph.runners._shared.results import (
    NodeStats as NodeStats,
)
from hypergraph.runners._shared.results import (
    PauseInfo as PauseInfo,
)
from hypergraph.runners._shared.results import (
    RunLog as RunLog,
)
from hypergraph.runners._shared.results import (
    RunResult as RunResult,
)
from hypergraph.runners._shared.results import (
    RunStatus as RunStatus,
)
from hypergraph.runners._shared.results import (
    _build_map_item_placeholder_log as _build_map_item_placeholder_log,
)
from hypergraph.runners._shared.results import (
    _compute_node_stats as _compute_node_stats,
)
from hypergraph.runners._shared.results import (
    _format_duration as _format_duration,
)
from hypergraph.runners._shared.results import (
    _generate_run_id as _generate_run_id,
)
from hypergraph.runners._shared.results import (
    _has_timed_work as _has_timed_work,
)
from hypergraph.runners._shared.results import (
    aggregate_run_status as aggregate_run_status,
)
from hypergraph.runners._shared.results import (
    build_failed_run_result as build_failed_run_result,
)
from hypergraph.runners._shared.results import (
    build_paused_run_result as build_paused_run_result,
)
from hypergraph.runners._shared.results import (
    build_pre_run_failed_result as build_pre_run_failed_result,
)
from hypergraph.runners._shared.results import (
    build_restored_run_log as build_restored_run_log,
)
from hypergraph.runners._shared.results import (
    build_restored_run_result as build_restored_run_result,
)
from hypergraph.runners._shared.results import (
    build_terminal_run_result as build_terminal_run_result,
)
from hypergraph.runners._shared.results import (
    generate_run_id as generate_run_id,
)
from hypergraph.runners._shared.state import (
    CheckpointErrorSink as CheckpointErrorSink,
)
from hypergraph.runners._shared.state import (
    ExecutionContext as ExecutionContext,
)
from hypergraph.runners._shared.state import (
    GraphState as GraphState,
)
from hypergraph.runners._shared.state import (
    NodeExecution as NodeExecution,
)
from hypergraph.runners._shared.state import (
    PauseExecution as PauseExecution,
)
from hypergraph.runners._shared.state import (
    RunnerCapabilities as RunnerCapabilities,
)
