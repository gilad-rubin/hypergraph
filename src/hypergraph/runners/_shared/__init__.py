"""Shared utilities for runners."""

from hypergraph.runners._shared.types import (
    GraphState,
    NodeExecution,
    RunnerCapabilities,
    PauseExecution,
    PauseInfo,
    RunResult,
    RunStatus,
)
from hypergraph.runners._shared.protocols import (
    NodeExecutor,
    AsyncNodeExecutor,
)
from hypergraph.runners._shared.helpers import (
    collect_inputs_for_node,
    map_inputs_to_func_params,
    wrap_outputs,
    initialize_state,
    filter_outputs,
    generate_map_inputs,
    get_ready_nodes,
)
from hypergraph.runners._shared.input_normalization import (
    ASYNC_MAP_RESERVED_OPTION_NAMES,
    ASYNC_RUN_RESERVED_OPTION_NAMES,
    MAP_RESERVED_OPTION_NAMES,
    RUN_RESERVED_OPTION_NAMES,
    merge_with_duplicate_check,
    normalize_inputs,
)
from hypergraph.runners._shared.validation import (
    validate_inputs,
    validate_runner_compatibility,
    validate_map_compatible,
    validate_node_types,
)

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
    "map_inputs_to_func_params",
    "wrap_outputs",
    "initialize_state",
    "filter_outputs",
    "generate_map_inputs",
    "get_ready_nodes",
    # Validation
    "validate_inputs",
    "validate_runner_compatibility",
    "validate_map_compatible",
    "validate_node_types",
    # Input normalization
    "RUN_RESERVED_OPTION_NAMES",
    "ASYNC_RUN_RESERVED_OPTION_NAMES",
    "MAP_RESERVED_OPTION_NAMES",
    "ASYNC_MAP_RESERVED_OPTION_NAMES",
    "merge_with_duplicate_check",
    "normalize_inputs",
]
