"""Runner input addressing, availability, and value resolution."""

from __future__ import annotations

import copy
import warnings
from enum import Enum
from typing import TYPE_CHECKING, Any

from hypergraph.graph.validation import GraphConfigError
from hypergraph.nodes.base import HyperNode
from hypergraph.runners._shared.types import GraphState

if TYPE_CHECKING:
    from hypergraph.checkpointers.types import Checkpoint
    from hypergraph.graph import Graph


class ValueSource(Enum):
    """Source of a parameter's value during graph execution."""

    EDGE = "edge"  # From upstream node output
    PROVIDED = "provided"  # From run() call
    BOUND = "bound"  # From graph.bind() - NEVER copy
    DEFAULT = "default"  # From function signature - MUST copy


def address_for_node_input(
    node: HyperNode,
    param: str,
) -> str:
    """Return the resolved graph-scope key for an input parameter.

    GraphNode boundary projection happens before runner execution, so
    ``node.inputs`` already contains parent-facing addresses. The node argument
    is kept so call sites read as "address for this node input" while the
    current implementation simply returns the already-resolved address.
    """
    return param


_SHORT_REPR_MAX_LEN = 40


def short_value_repr(value: Any, max_len: int = _SHORT_REPR_MAX_LEN) -> str | None:
    """Short repr for primitive values; None for everything else.

    Single source of truth for inlining user-supplied values into error and
    warning text. Returns a length-capped repr for bool/int/float/str/bytes/None,
    and None for everything else (DataFrames, numpy arrays, custom classes) so
    callers can fall back to a generic message rather than dumping or hitting an
    expensive __repr__.
    """
    if value is None or isinstance(value, (bool, int, float, bytes, str)):
        rep = repr(value)
        return rep if len(rep) <= max_len else None
    return None


def warn_on_bind_overrides(graph: Graph, provided_values: dict[str, Any]) -> None:
    """Emit a UserWarning for each provided value that overrides a bound value.

    Fires uniformly regardless of whether the override address is flat or
    dotted -- both surfaces share the same canonical form on graph.inputs.bound
    and provided_values. For primitive bound/provided pairs the warning shows
    both values; otherwise it stays generic. Dot-pathed addresses are annotated
    with their owning subgraph for clarity.
    """
    from hypergraph.graph._helpers import describe_addressed_input

    bound = graph.inputs.bound
    for key, new_value in provided_values.items():
        if key not in bound:
            continue
        old_value = bound[key]
        if old_value is new_value:
            continue
        described = describe_addressed_input(key)
        old_repr = short_value_repr(old_value)
        new_repr = short_value_repr(new_value)
        if old_repr is not None and new_repr is not None:
            msg = f"Run value overrides bound value for {described} (address {key!r}): {old_repr} -> {new_repr}"
        else:
            msg = f"Run value overrides bound value for {described} (address {key!r})"
        warnings.warn(msg, UserWarning, stacklevel=4)


def _safe_deepcopy(value: Any, param_name: str = "<unknown>") -> Any:
    """Deep-copy a value, falling back gracefully for non-copyable objects.

    Some objects (locks, file handles, C extensions) cannot be deep-copied.
    For these, we raise a clear error explaining the issue.

    Args:
        value: The value to deep-copy
        param_name: Name of the parameter (for error messages)

    Raises:
        GraphConfigError: If value cannot be deep-copied
    """
    try:
        return copy.deepcopy(value)
    except (TypeError, copy.Error) as e:
        # Clear, human-friendly explanation
        raise GraphConfigError(
            f"Parameter '{param_name}' has a default value that cannot be safely copied.\n\n"
            f"Why copying is needed:\n"
            f"  Default values in Python are shared across function calls. If your\n"
            f"  default is mutable (like a list, dict, or object), changes in one run\n"
            f"  would affect future runs unless we make a fresh copy each time.\n\n"
            f"Why this default can't be copied:\n"
            f"  The {type(value).__name__} object contains thread locks or other system\n"
            f"  resources that cannot be duplicated.\n\n"
            f"Solution:\n"
            f"  Use .bind() to provide this value at the graph level instead:\n\n"
            f"    graph = Graph([...]).bind({param_name}=your_{type(value).__name__.lower()}_instance)\n\n"
            f"  This way the object is shared intentionally, and you control its lifecycle.\n\n"
            f"Technical details: {e}"
        ) from e


def get_value_source(
    param: str,
    node: HyperNode,
    graph: Graph,
    state: GraphState,
    provided_values: dict[str, Any],
) -> tuple[ValueSource, Any]:
    """Determine where a parameter's value comes from.

    Returns:
        (ValueSource, value) tuple indicating the source and the actual value.

    Resolution order (first match wins):
        1. EDGE - From upstream node output (state.values)
        2. PROVIDED - From run() call (provided_values)
        3. BOUND - From graph.bind() (never copied)
        4. DEFAULT - From function signature (must be deep-copied)

    Raises:
        KeyError: If no value source is found for the parameter.
    """
    from hypergraph.nodes.graph_node import GraphNode

    # 1. Edge / restored-state value. For a GraphNode, a checkpoint restores
    # inputs under the parent-facing address; resolve to the canonical key so
    # the lookup matches the readiness/staleness checks.
    state_addr = address_for_node_input(node, param)
    if state_addr in state.values and _state_value_satisfies_input(param, node, graph, state, state_addr):
        return (ValueSource.EDGE, state.values[state_addr])

    # 2. Input value (from run() call). The address may differ from the local
    # name when the input is projected through a GraphNode boundary.
    provided_addr = address_for_node_input(node, param)
    if provided_addr in provided_values:
        return (ValueSource.PROVIDED, provided_values[provided_addr])

    # 3. Bound value resolved at this graph boundary. Same addressing applies
    # for graph.inputs.bound.
    bound_addr = address_for_node_input(node, param)
    if bound_addr in graph.inputs.bound:
        return (ValueSource.BOUND, graph.inputs.bound[bound_addr])

    # 3b. For GraphNode: check if inner graph has it bound
    if isinstance(node, GraphNode):
        for local_param in node._local_inputs_for_address(param):
            original_param = node._resolve_original_input_name(local_param)
            if original_param in node._graph.inputs.bound:
                return (ValueSource.BOUND, node._graph.inputs.bound[original_param])

    # 4. Function default (from signature)
    if node.has_signature_default_for(param):
        default = node.get_signature_default_for(param)
        return (ValueSource.DEFAULT, default)

    # No value found - this shouldn't happen if validation passed
    raise KeyError(f"No value for input '{param}'")


def has_all_inputs(node: HyperNode, graph: Graph, state: GraphState) -> bool:
    """Check if all inputs for a node are available."""
    if graphnode_has_resume_values(node, state):
        return True
    return all(has_input(param, node, graph, state) for param in node.inputs)


def graphnode_has_resume_values(node: HyperNode, state: GraphState) -> bool:
    """Return whether state carries a GraphNode interrupt resume payload."""
    from hypergraph.nodes.graph_node import GraphNode

    if not isinstance(node, GraphNode):
        return False

    resume_values = state.resume_values
    if not resume_values:
        return False

    node_input_set = set(node.inputs)
    if any(key in resume_values for key in node.outputs if key not in node_input_set):
        return True

    if node.namespaced:
        return False

    prefix = f"{node.name}."
    return any(key.startswith(prefix) and key[len(prefix) :] not in node_input_set for key in resume_values)


def has_input(param: str, node: HyperNode, graph: Graph, state: GraphState) -> bool:
    """Check if a single input parameter is available."""
    addr = address_for_node_input(node, param)
    if addr in state.values and _state_value_satisfies_input(param, node, graph, state, addr):
        return True
    if addr in graph.inputs.bound:
        return True
    return bool(node.has_default_for(param))


def _state_value_satisfies_input(
    param: str,
    node: HyperNode,
    graph: Graph,
    state: GraphState,
    state_addr: str | None = None,
) -> bool:
    """Whether the current state value is a valid source for this node input.

    For inputs with declared data producers, the current value must either be
    the original graph input seed or the current output from one of those
    producers. This prevents explicit-edge graphs from consuming an identically
    named value produced by an undeclared branch.
    """
    producers = graph.input_data_producers.get(node.name, {}).get(param)
    if not producers:
        return True

    value_addr = state_addr or param
    current_version = state.versions.get(value_addr, 0)
    if _version_produced_by(param, current_version, producers, state):
        return True

    if not _version_produced_by_any_node(param, current_version, state):
        return value_addr in graph.inputs.all or param in graph.inputs.all or not any(producer in state.node_executions for producer in producers)

    return False


def _version_produced_by(
    param: str,
    version: int,
    producers: frozenset[str],
    state: GraphState,
) -> bool:
    """Return whether ``version`` of ``param`` came from an eligible producer."""
    for producer in producers:
        execution = state.node_executions.get(producer)
        if execution is None:
            continue
        if execution.output_versions.get(param) == version:
            return True
        if _unversioned_execution_can_own_value(param, producer, state):
            return True
    return False


def _version_produced_by_any_node(
    param: str,
    version: int,
    state: GraphState,
) -> bool:
    """Return whether ``version`` of ``param`` was written by any executed node."""
    return any(
        execution.output_versions.get(param) == version or _unversioned_execution_can_own_value(param, node_name, state)
        for node_name, execution in state.node_executions.items()
    )


def _unversioned_execution_can_own_value(param: str, node_name: str, state: GraphState) -> bool:
    """Backward-compat ownership check for executions without output_versions."""
    execution = state.node_executions.get(node_name)
    if execution is None or param not in execution.outputs or param in execution.output_versions:
        return False

    candidates = [
        (candidate.sequence, position, executed_name)
        for position, (executed_name, candidate) in enumerate(state.node_executions.items())
        if param in candidate.outputs
    ]
    sequenced = [candidate for candidate in candidates if candidate[0] >= 0]
    owner = max(sequenced, key=lambda candidate: (candidate[0], candidate[1]))[2] if sequenced else candidates[-1][2]
    return owner == node_name


def latest_upstream_output_version(
    param: str,
    upstream: frozenset[str],
    state: GraphState,
) -> int:
    """Get newest known version of ``param`` produced by eligible upstream nodes."""
    latest = 0
    for producer in upstream:
        execution = state.node_executions.get(producer)
        if execution is None:
            continue

        produced_version = execution.output_versions.get(param)
        if produced_version is None:
            if param in execution.outputs:
                # Backward compat: treat missing output_versions as definitely
                # stale so the consumer re-executes after checkpoint restore.
                produced_version = state.get_version(param) + 1
            else:
                continue

        latest = max(latest, produced_version)

    return latest


def collect_inputs_for_node(
    node: HyperNode,
    graph: Graph,
    state: GraphState,
    provided_values: dict[str, Any],
) -> dict[str, Any]:
    """Gather inputs for a node following value resolution order.

    Resolution order (first wins):
    1. Edge value (from state.values, produced by upstream node)
    2. Input value (from provided_values dict)
    3. Bound value (from graph.bind())
    4. Function default

    Args:
        node: The node to collect inputs for
        graph: The graph being executed
        state: Current execution state
        provided_values: Values provided to runner.run()

    Returns:
        Dict mapping input names to their values
    """
    inputs = {}
    graphnode_resume = graphnode_has_resume_values(node, state)
    for param in node.inputs:
        try:
            inputs[param] = _resolve_input(param, node, graph, state, provided_values)
        except KeyError:
            if graphnode_resume:
                continue
            raise
    return inputs


def _resolve_input(
    param: str,
    node: HyperNode,
    graph: Graph,
    state: GraphState,
    provided_values: dict[str, Any],
) -> Any:
    """Resolve a single input value following the precedence order.

    Uses get_value_source() to determine where the value comes from,
    then applies deep-copy ONLY for signature defaults (never for bound values).
    """
    source, value = get_value_source(param, node, graph, state, provided_values)

    # Deep-copy ONLY signature defaults to prevent mutable default mutation
    if source == ValueSource.DEFAULT:
        return _safe_deepcopy(value, param_name=param)

    # All other sources: return as-is (no copying)
    return value


def build_resume_validation_values(
    graph: Graph,
    normalized_values: dict[str, Any],
    resume_checkpoint: Checkpoint | None,
) -> dict[str, Any]:
    """Build canonical validation inputs for resume/fork/retry runs."""
    validation_values = dict(normalized_values)
    if resume_checkpoint is None:
        return validation_values

    for input_name in graph.inputs.all:
        if input_name in validation_values:
            continue
        if input_name in resume_checkpoint.values:
            validation_values[input_name] = resume_checkpoint.values[input_name]
            continue
        if any(input_name in (step.input_versions or {}) for step in resume_checkpoint.steps):
            # The checkpoint may omit original graph inputs once downstream
            # state is sufficient to resume. A prior consumed version is enough
            # to satisfy canonical key-presence validation.
            validation_values[input_name] = None

    return validation_values
