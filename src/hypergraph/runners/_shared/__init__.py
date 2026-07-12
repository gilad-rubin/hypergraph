"""Shared utilities for runners."""

from hypergraph.runners._shared.input_normalization import (
    merge_with_duplicate_check,
    normalize_inputs,
    runner_option_names,
)
from hypergraph.runners._shared.map_inputs import generate_map_inputs
from hypergraph.runners._shared.outputs import filter_outputs, wrap_outputs
from hypergraph.runners._shared.protocols import (
    AsyncNodeExecutor,
    NodeExecutor,
)
from hypergraph.runners._shared.readiness import get_ready_nodes
from hypergraph.runners._shared.results import PauseInfo, RunResult, RunStatus
from hypergraph.runners._shared.state import (
    GraphState,
    NodeExecution,
    PauseExecution,
    RunnerCapabilities,
)
from hypergraph.runners._shared.state_restore import initialize_state
from hypergraph.runners._shared.validation import (
    validate_inputs,
    validate_node_types,
    validate_runner_compatibility,
)
from hypergraph.runners._shared.value_resolution import collect_inputs_for_node

__all__ = [
    # Types
    "GraphState",
    "NodeExecution",
    "RunnerCapabilities",
    "PauseExecution",
    "PauseInfo",
    "RunResult",
    "RunStatus",
    # Protocols
    "NodeExecutor",
    "AsyncNodeExecutor",
    # Helpers
    "collect_inputs_for_node",
    "wrap_outputs",
    "initialize_state",
    "filter_outputs",
    "generate_map_inputs",
    "get_ready_nodes",
    # Validation
    "validate_inputs",
    "validate_runner_compatibility",
    "validate_node_types",
    # Input normalization
    "merge_with_duplicate_check",
    "normalize_inputs",
    "runner_option_names",
]
