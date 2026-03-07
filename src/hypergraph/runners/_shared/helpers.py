"""Shared helper functions for runners."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from hypergraph.graph.validation import GraphConfigError
from hypergraph.nodes.base import HyperNode
from hypergraph.runners._shared.types import (
    ErrorHandling,
    GraphState,
    NodeExecution,
    RunResult,
    RunStatus,
)

if TYPE_CHECKING:
    from hypergraph.graph import Graph
    from hypergraph.nodes.graph_node import GraphNode


@dataclass(frozen=True)
class ExecutionScope:
    """Resolved execution scope shared by scheduler and validation."""

    active_nodes: frozenset[str] | None
    startup_predecessors: dict[str, frozenset[str]]


def compute_execution_scope(graph: Graph) -> ExecutionScope:
    """Resolve active nodes and startup predecessors from graph configuration.

    Scope is computed from graph-level entrypoint/select settings (no runtime
    overrides). Startup predecessors include DATA + ORDERING edges and exclude
    CONTROL edges (gate activation is handled separately by routing logic).
    """
    from hypergraph.graph.input_spec import _compute_active_scope

    if graph.entrypoints_config is None and graph.selected is None:
        active_nodes: set[str] | None = None
        active_subgraph = graph._nx_graph
    else:
        active_nodes_dict, active_subgraph = _compute_active_scope(
            graph._nodes,
            graph._nx_graph,
            entrypoints=graph.entrypoints_config,
            selected=graph.selected,
        )
        active_nodes = frozenset(active_nodes_dict)

    predecessors: dict[str, set[str]] = {}
    for src, dst, data in active_subgraph.edges(data=True):
        if src == dst:
            continue
        if data.get("edge_type") == "control":
            continue
        predecessors.setdefault(dst, set()).add(src)

    return ExecutionScope(
        active_nodes=active_nodes,
        startup_predecessors={name: frozenset(preds) for name, preds in predecessors.items()},
    )


class ValueSource(Enum):
    """Source of a parameter's value during graph execution."""

    EDGE = "edge"  # From upstream node output
    PROVIDED = "provided"  # From run() call
    BOUND = "bound"  # From graph.bind() - NEVER copy
    DEFAULT = "default"  # From function signature - MUST copy


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

    # 1. Edge value (from upstream node output)
    if param in state.values:
        return (ValueSource.EDGE, state.values[param])

    # 2. Input value (from run() call)
    if param in provided_values:
        return (ValueSource.PROVIDED, provided_values[param])

    # 3. Bound value (from graph.bind()) - check both graph and GraphNode
    if param in graph.inputs.bound:
        return (ValueSource.BOUND, graph.inputs.bound[param])

    # 3b. For GraphNode: check if inner graph has it bound
    if isinstance(node, GraphNode):
        original_param = node._resolve_original_input_name(param)
        if original_param in node._graph.inputs.bound:
            return (ValueSource.BOUND, node._graph.inputs.bound[original_param])

    # 4. Function default (from signature)
    if node.has_signature_default_for(param):
        default = node.get_signature_default_for(param)
        return (ValueSource.DEFAULT, default)

    # No value found - this shouldn't happen if validation passed
    raise KeyError(f"No value for input '{param}'")


def get_ready_nodes(
    graph: Graph,
    state: GraphState,
    *,
    active_nodes: set[str] | None = None,
    startup_predecessors: dict[str, frozenset[str]] | None = None,
) -> list[HyperNode]:
    """Find nodes whose inputs are all satisfied and not stale.

    A node is ready when:
    1. All its inputs have values in state (or have defaults/bounds)
    2. The node hasn't been executed yet, OR
    3. The node was executed but its inputs have changed (stale)
    4. If controlled by a gate, the gate has routed to this node
    5. If active_nodes is set, the node must be in the active set

    Args:
        graph: The graph being executed
        state: Current execution state
        active_nodes: Optional set of node names to restrict scheduling to.
            When set (from with_entrypoint), nodes outside this set are never
            scheduled even if their inputs are available.
        startup_predecessors: Optional map of node -> startup predecessors in
            the active scope. If omitted, computed from the graph.

    Returns:
        List of nodes ready to execute
    """
    # First, identify which nodes are activated by gates
    activated_nodes = _get_activated_nodes(graph, state)
    if startup_predecessors is None:
        startup_predecessors = _compute_startup_predecessors(graph, active_nodes=active_nodes)

    ready = []
    for node in graph._nodes.values():
        if active_nodes is not None and node.name not in active_nodes:
            continue
        if _is_node_ready(node, graph, state, activated_nodes, startup_predecessors=startup_predecessors):
            ready.append(node)

    # If a gate is ready, its routing decision should apply before targets run.
    # Block targets of ready gates for this superstep so decisions take effect
    # on the next iteration.
    from hypergraph.nodes.gate import END, GateNode

    ready_gate_names = {n.name for n in ready if isinstance(n, GateNode)}
    if ready_gate_names:
        blocked_targets: set[str] = set()
        for gate_name in ready_gate_names:
            gate = graph._nodes.get(gate_name)
            if gate is None:
                continue
            for target in gate.targets:
                if target is END:
                    continue
                if target == gate_name:
                    continue
                blocked_targets.add(target)
        if blocked_targets:
            ready = [n for n in ready if n.name not in blocked_targets]

    # Defer wait_for consumers whose producers are also ready this superstep
    ready = _defer_wait_for_nodes(ready, graph, state)

    return ready


def find_missing_resume_seed_inputs(
    graph: Graph,
    state: GraphState,
    *,
    active_nodes: set[str] | None = None,
    startup_predecessors: dict[str, frozenset[str]] | None = None,
) -> set[str]:
    """Return graph inputs that block checkpoint-started execution from resuming.

    This is used after restoring checkpoint state. If no nodes are ready, but an
    activated node would become runnable once a graph-level seed input existed,
    returning that input lets callers raise a clear error instead of silently
    no-op completing the run.
    """
    activated_nodes = _get_activated_nodes(graph, state)
    if startup_predecessors is None:
        startup_predecessors = _compute_startup_predecessors(graph, active_nodes=active_nodes)

    missing: set[str] = set()
    graph_inputs = set(graph.inputs.all)

    for node in graph._nodes.values():
        if active_nodes is not None and node.name not in active_nodes:
            continue
        if node.name not in activated_nodes:
            continue
        if node.is_interrupt and all(output in state.values for output in node.data_outputs):
            continue
        if not _startup_predecessors_satisfied(
            node,
            graph,
            state,
            activated_nodes=activated_nodes,
            startup_predecessors=startup_predecessors,
        ):
            continue
        if not _wait_for_satisfied(node, state):
            continue
        if not _needs_execution(node, graph, state):
            continue

        for param in node.inputs:
            if param in graph_inputs and not _has_input(param, node, graph, state):
                missing.add(param)

    return missing


def _compute_startup_predecessors(
    graph: Graph,
    *,
    active_nodes: set[str] | None,
) -> dict[str, frozenset[str]]:
    """Compute startup predecessors from graph topology.

    Uses DATA + ORDERING edges and excludes CONTROL edges.
    """
    predecessors: dict[str, set[str]] = {}
    for src, dst, data in graph._nx_graph.edges(data=True):
        if src == dst:
            continue
        if active_nodes is not None and (src not in active_nodes or dst not in active_nodes):
            continue
        if data.get("edge_type") == "control":
            continue
        predecessors.setdefault(dst, set()).add(src)
    return {name: frozenset(preds) for name, preds in predecessors.items()}


def _get_activated_nodes(graph: Graph, state: GraphState) -> set[str]:
    """Get all nodes that have been activated by gate routing decisions.

    A node is activated if:
    - It has no controlling gates, OR
    - At least one controlling gate has routed to it

    Returns:
        Set of activated node names
    """
    # Clear stale gate decisions: if a gate will re-execute (inputs changed),
    # its previous routing decision is outdated and must not activate targets
    _clear_stale_gate_decisions(graph, state)

    activated = set()

    # Use cached map of node -> controlling gates
    for node_name in graph._nodes:
        gates = graph.controlled_by.get(node_name, [])
        if not gates:
            # No controlling gates - always activated
            activated.add(node_name)
        else:
            # Check if any controlling gate has routed to this node
            for gate_name in gates:
                decision = state.routing_decisions.get(gate_name)
                if decision is None:
                    if gate_name not in state.node_executions:
                        gate = graph._nodes.get(gate_name)
                        if gate is None:
                            continue
                        default_open = getattr(gate, "default_open", True)
                        if default_open and node_name not in state.node_executions:
                            # Gate has never executed, target has never executed —
                            # allow first-pass startup.  Once the target has executed,
                            # it must wait for the gate's actual routing decision
                            # before re-firing (prevents mid-pipeline re-trigger
                            # when shared params cause staleness).
                            #
                            # Exception: when entrypoints are configured, non-entrypoint
                            # gate targets must wait for the gate to actually route to
                            # them.  Without this, an inputless gate target (e.g. an
                            # interrupt node) would fire before the gate on the first pass.
                            entrypoints = graph.entrypoints_config
                            if entrypoints and node_name not in entrypoints:
                                continue
                            activated.add(node_name)
                            break
                        continue
                    continue  # Gate executed before but decision was cleared (stale)

                # Check if this node was activated by the decision
                if _is_node_activated_by_decision(node_name, decision):
                    activated.add(node_name)
                    break

    return activated


def _clear_stale_gate_decisions(graph: Graph, state: GraphState) -> None:
    """Clear routing decisions for gates that will re-execute.

    If a gate's inputs have changed since its last execution, its previous
    routing decision is stale. Keeping it would let targets activate before
    the gate re-evaluates — causing off-by-one iterations in cycles.
    """
    from hypergraph.nodes.gate import END, GateNode

    for node in graph._nodes.values():
        if isinstance(node, GateNode) and node.name in state.routing_decisions:
            # END is terminal — never clear it, even if inputs changed
            if state.routing_decisions[node.name] is END:
                continue
            if _needs_execution(node, graph, state):
                del state.routing_decisions[node.name]


def _is_node_activated_by_decision(node_name: str, decision: Any) -> bool:
    """Check if a routing decision activates a specific node."""
    from hypergraph.nodes.gate import END

    if decision is END:
        return False
    if decision is None:
        return False
    if isinstance(decision, list):
        return node_name in decision
    return decision == node_name


def _is_node_ready(
    node: HyperNode,
    graph: Graph,
    state: GraphState,
    activated_nodes: set[str],
    *,
    startup_predecessors: dict[str, frozenset[str]] | None = None,
) -> bool:
    """Check if a single node is ready to execute."""
    # Check if node is activated (not blocked by gate decisions)
    if node.name not in activated_nodes:
        return False

    # Startup is predecessor-driven in both implicit and explicit modes.
    if not _startup_predecessors_satisfied(node, graph, state, activated_nodes=activated_nodes, startup_predecessors=startup_predecessors):
        return False

    # Check if all inputs are available
    if not _has_all_inputs(node, graph, state):
        return False

    # Check wait_for satisfaction (ordering-only inputs)
    if not _wait_for_satisfied(node, state):
        return False

    # Check if node needs execution (not executed or stale)
    return _needs_execution(node, graph, state)


def _startup_predecessors_satisfied(
    node: HyperNode,
    graph: Graph,
    state: GraphState,
    *,
    activated_nodes: set[str] | None = None,
    startup_predecessors: dict[str, frozenset[str]] | None = None,
) -> bool:
    """Whether startup predecessor constraints allow node execution.

    Only gates on predecessors that are activated (reachable via routing).
    Non-activated predecessors (e.g., on the non-selected branch of a route)
    are excluded to prevent deadlocks in mutually exclusive routes.
    """
    predecessors = (startup_predecessors or {}).get(node.name, frozenset())
    if not predecessors:
        return True

    entrypoints = graph.entrypoints_config or ()
    if node.name in entrypoints and node.name not in state.node_executions:
        return True

    # Only require predecessors that are activated or already executed
    relevant = frozenset(pred for pred in predecessors if (activated_nodes is not None and pred in activated_nodes) or pred in state.node_executions)
    return all(pred in state.node_executions for pred in relevant)


def _is_controlled_by_gate(node: HyperNode, graph: Graph) -> bool:
    """Check if a node is controlled by any gate (O(1) via cached map)."""
    return bool(graph.controlled_by.get(node.name))


def _wait_for_satisfied(node: HyperNode, state: GraphState) -> bool:
    """Check if all wait_for ordering dependencies are satisfied.

    A wait_for name must:
    - Exist in state (value has been produced)
    - On re-execution: have a fresh version since last consumed
    """
    if not node.wait_for:
        return True

    last_exec = state.node_executions.get(node.name)

    for name in node.wait_for:
        if name not in state.values:
            return False
        # On re-execution, check freshness
        if last_exec is not None:
            current_version = state.get_version(name)
            consumed_version = last_exec.wait_for_versions.get(name, 0)
            if current_version <= consumed_version:
                return False
    return True


def _defer_wait_for_nodes(
    ready: list[HyperNode],
    graph: Graph,
    state: GraphState,
) -> list[HyperNode]:
    """If a producer and its wait_for consumer are both ready, defer the consumer.

    This handles the first-superstep edge case: both nodes have all inputs
    satisfied, but the consumer should wait for the producer to run first.
    """
    if not ready:
        return ready

    # Collect all outputs that will be produced this superstep
    ready_outputs: set[str] = set()
    for node in ready:
        ready_outputs.update(node.outputs)

    # Defer nodes whose wait_for includes an output from a co-ready node
    deferred: set[str] = set()
    for node in ready:
        if not node.wait_for:
            continue
        # Only defer on first execution. Once the consumer has executed at least
        # once, perpetual deferral can starve cyclic wait_for patterns.
        if node.name in state.node_executions:
            continue
        for name in node.wait_for:
            if name in ready_outputs:
                # Check the producer is a different node
                for other in ready:
                    if other.name != node.name and name in other.outputs:
                        deferred.add(node.name)
                        break
            if node.name in deferred:
                break

    if not deferred:
        return ready
    return [n for n in ready if n.name not in deferred]


def _has_all_inputs(node: HyperNode, graph: Graph, state: GraphState) -> bool:
    """Check if all inputs for a node are available."""
    return all(_has_input(param, node, graph, state) for param in node.inputs)


def _has_input(param: str, node: HyperNode, graph: Graph, state: GraphState) -> bool:
    """Check if a single input parameter is available."""
    # Value in state (from edge or initial input)
    if param in state.values:
        return True

    # Bound value in graph
    if param in graph.inputs.bound:
        return True

    # Node has default for this parameter
    return bool(node.has_default_for(param))


def _needs_execution(node: HyperNode, graph: Graph, state: GraphState) -> bool:
    """Check if node needs (re-)execution."""
    if node.name not in state.node_executions:
        return True  # Never executed

    # A pending routing decision targeting this node is an explicit re-trigger.
    # Without this, inputless gate targets (e.g. interrupt nodes) would never
    # be considered stale and would not re-execute on subsequent cycles.
    if _has_pending_activation(node, graph, state):
        return True

    # Check if any input has changed since last execution
    last_exec = state.node_executions[node.name]
    return _is_stale(node, graph, state, last_exec)


def _has_pending_activation(node: HyperNode, graph: Graph, state: GraphState) -> bool:
    """Check if a gate routing decision is actively targeting this node."""
    for gate_name in graph.controlled_by.get(node.name, []):
        decision = state.routing_decisions.get(gate_name)
        if decision is not None and _is_node_activated_by_decision(node.name, decision):
            return True
    return False


def _is_stale(
    node: HyperNode,
    graph: Graph,
    state: GraphState,
    last_exec: NodeExecution,
) -> bool:
    """Check if node inputs have changed since last execution.

    Implements two staleness-skip rules for non-gated nodes:

    1. **Sole Producer Rule** — skip if this node itself produces the param.
       Prevents ``add_response(messages) -> messages`` from re-triggering
       infinitely.

    2. **Descendant Producer Rule** (DAGs only) — skip if ALL producers of
       the param are descendants of this node.  Prevents downstream writes
       from triggering upstream re-execution, e.g. an interrupt node that
       consumes ``messages`` while a downstream accumulator produces it.

    EXCEPTION: For gate-controlled nodes, neither rule applies.  Gates
    explicitly drive cycle re-execution.
    """
    # wait_for freshness is an explicit re-trigger signal.
    for wait_name in node.wait_for:
        current_wait_version = state.get_version(wait_name)
        consumed_wait_version = last_exec.wait_for_versions.get(wait_name, 0)
        if current_wait_version > consumed_wait_version:
            return True

    self_producers = graph.self_producers
    is_gated = _is_controlled_by_gate(node, graph)
    downstream = graph.downstream_produced.get(node.name, frozenset())
    input_data_producers = graph.input_data_producers.get(node.name, {})

    for param in node.inputs:
        if not is_gated:
            # Sole Producer Rule: node produces this param itself
            if node.name in self_producers.get(param, set()):
                continue
            # Descendant Producer Rule: all producers are downstream (DAGs only)
            if param in downstream:
                continue
        current_version = state.get_version(param)
        consumed_version = last_exec.input_versions.get(param, 0)
        if current_version == consumed_version:
            continue

        # Only producers wired to this node/input should trigger staleness.
        upstream = input_data_producers.get(param, frozenset())
        if upstream and _latest_upstream_output_version(param, upstream, state) <= consumed_version:
            continue
        return True
    return False


def _latest_upstream_output_version(
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
    for param in node.inputs:
        inputs[param] = _resolve_input(param, node, graph, state, provided_values)
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


def map_inputs_to_func_params(node: HyperNode, inputs: dict[str, Any]) -> dict[str, Any]:
    """Map renamed input names back to original function parameter names.

    Delegates to node.map_inputs_to_params() for polymorphic behavior.
    Each node type handles its own rename mapping logic.

    Args:
        node: The node with potential renames
        inputs: Dict with renamed input names as keys

    Returns:
        Dict with original function parameter names as keys
    """
    return node.map_inputs_to_params(inputs)


def wrap_outputs(node: HyperNode, result: Any) -> dict[str, Any]:
    """Wrap execution result in a dict mapping output names to values.

    Uses node.data_outputs for unpacking the function return value, then
    auto-produces _EMIT_SENTINEL for each emit output.
    """
    from hypergraph.nodes.base import _EMIT_SENTINEL

    data_outputs = node.data_outputs
    emit_outputs = node.outputs[len(data_outputs) :]  # emit portion

    # Wrap data outputs
    if not data_outputs:
        wrapped = {}
    elif len(data_outputs) == 1:
        wrapped = {data_outputs[0]: result}
    else:
        if len(data_outputs) != len(result):
            raise ValueError(f"Node '{node.name}' has {len(data_outputs)} data outputs but returned {len(result)} values")
        wrapped = dict(zip(data_outputs, result, strict=True))

    # Auto-produce sentinel for each emit output
    for name in emit_outputs:
        wrapped[name] = _EMIT_SENTINEL

    return wrapped


def initialize_state(
    graph: Graph,
    values: dict[str, Any],
    *,
    checkpoint: Any | None = None,
) -> GraphState:
    """Initialize execution state with provided input values.

    Args:
        graph: The graph being executed
        values: Input values provided to runner.run()

    Returns:
        Initial GraphState with input values set
    """
    if checkpoint is None:
        state = GraphState()
        # Set initial values for all provided inputs
        for name, value in values.items():
            state.update_value(name, value)
        return state

    return initialize_state_with_checkpoint(
        graph=graph,
        checkpoint_values=checkpoint.values,
        runtime_values=values,
        steps=checkpoint.steps,
    )


def initialize_state_with_checkpoint(
    *,
    graph: Graph,
    checkpoint_values: dict[str, Any],
    runtime_values: dict[str, Any],
    steps: list[Any],
) -> GraphState:
    """Restore GraphState from checkpoint snapshot and append runtime overrides.

    Restores exact version counters via step replay so mid-cycle resumes keep
    staleness semantics correct (no remap-to-1).
    """
    from hypergraph.nodes.gate import END as _END
    from hypergraph.nodes.gate import IfElseNode, RouteNode

    state = GraphState()
    state.values = dict(checkpoint_values)

    versions: dict[str, int] = {}
    graph_input_names = set(graph.inputs.all)
    bound_names = set(graph.inputs.bound)
    for name in checkpoint_values:
        # Runtime-provided graph inputs start at version 1 (set by update_value
        # during initialize_state). Bound values start at version 0 (resolved
        # lazily via collect_inputs_for_node, never entering state.versions).
        if name in graph_input_names and name not in bound_names:
            versions[name] = 1

    completed_steps = [s for s in steps if getattr(s, "status", None) is not None and s.status.value == "completed"]
    completed_steps.sort(key=lambda s: (s.superstep, s.index))

    for step in completed_steps:
        for input_name, consumed_version in (step.input_versions or {}).items():
            versions[input_name] = max(versions.get(input_name, 0), int(consumed_version))
        if step.values:
            for out_name in step.values:
                versions[out_name] = versions.get(out_name, 0) + 1

    state.versions = versions

    for step in completed_steps:
        state.node_executions[step.node_name] = NodeExecution(
            node_name=step.node_name,
            input_versions=dict(step.input_versions or {}),
            outputs=dict(step.values or {}),
            output_versions={},
            duration_ms=step.duration_ms,
            cached=step.cached,
        )
        if step.decision is not None:
            decision = step.decision
            if decision == "END":
                decision = _END
            elif isinstance(decision, list):
                decision = [_END if d == "END" else d for d in decision]
            state.routing_decisions[step.node_name] = decision

    # Gate routing is derivable from internal gate output values.
    for node in graph._nodes.values():
        gate_out = f"_{node.name}"
        if gate_out not in state.values:
            continue
        if isinstance(node, IfElseNode):
            state.routing_decisions[node.name] = node.when_true if bool(state.values[gate_out]) else node.when_false
        elif isinstance(node, RouteNode):
            routed = state.values[gate_out]
            state.routing_decisions[node.name] = _END if routed == "END" else routed

    for name, value in runtime_values.items():
        state.update_value(name, value)

    # Reconstruct per-step output versions so staleness checks can attribute
    # changes to explicit upstream producers after checkpoint restore.
    replay_versions: dict[str, int] = {}
    for name in checkpoint_values:
        if name in graph_input_names and name not in bound_names:
            replay_versions[name] = 1
    for step in completed_steps:
        output_versions: dict[str, int] = {}
        for out_name in step.values or {}:
            replay_versions[out_name] = replay_versions.get(out_name, 0) + 1
            output_versions[out_name] = replay_versions[out_name]
        execution = state.node_executions.get(step.node_name)
        if execution is not None:
            state.node_executions[step.node_name] = replace(execution, output_versions=output_versions)

    return state


def generate_workflow_id() -> str:
    """Create a compact auto-generated workflow id."""
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"run-{day}-{uuid.uuid4().hex[:6]}"


def is_interrupt_resume_payload(
    graph: Graph,
    values: dict[str, Any],
) -> bool:
    """Return True when runtime values only provide interrupt response outputs.

    This enables paused workflow continuation with the same ``workflow_id``
    while keeping strict no-override lineage semantics for normal inputs.
    """
    if not values:
        return False

    allowed_outputs = _collect_interrupt_resume_keys(graph)

    return bool(allowed_outputs) and set(values).issubset(allowed_outputs)


def _collect_interrupt_resume_keys(
    graph: Graph,
    *,
    prefix: str = "",
) -> set[str]:
    """Collect valid interrupt resume keys for this graph scope.

    Top-level interrupt outputs are exposed directly (``decision``).
    Nested graph interrupts are exposed with dotted graph-node prefixes
    (``inner.decision`` / ``outer.inner.decision``), matching
    ``PauseInfo.response_key``.
    """
    from hypergraph.nodes.graph_node import GraphNode

    allowed_outputs: set[str] = set()
    for node in graph.iter_nodes():
        if node.is_interrupt:
            allowed_outputs.update(f"{prefix}{output}" for output in node.data_outputs)
            continue
        if isinstance(node, GraphNode):
            nested_prefix = f"{prefix}{node.name}."
            allowed_outputs.update(_collect_interrupt_resume_keys(node.graph, prefix=nested_prefix))
    return allowed_outputs


_UNSET_SELECT: Any = object()
"""Sentinel distinguishing 'user didn't pass select' from explicit '**'."""


def filter_outputs(
    state: GraphState,
    graph: Graph,
    select: str | list[str] | Any = _UNSET_SELECT,
    on_missing: str = "ignore",
) -> dict[str, Any]:
    """Filter state values to only include requested outputs.

    Excludes emit sentinel values from the output — they are internal
    ordering signals and should never be visible to the user.

    Args:
        state: Final execution state
        graph: The executed graph
        select: Which outputs to return. ``"**"`` = all outputs (default).
            A list of exact names returns only those. Unset = check
            ``graph.selected`` first, then fall back to ``"**"``.
        on_missing: How to handle missing selected outputs.
            ``"ignore"`` (default) = silently omit.
            ``"warn"`` = warn and return available.
            ``"error"`` = raise ValueError.

    Returns:
        Dict of output values
    """
    if on_missing not in _VALID_ON_MISSING:
        raise ValueError(f"Invalid on_missing={on_missing!r}. Expected one of: {', '.join(_VALID_ON_MISSING)}")

    from hypergraph.nodes.base import _EMIT_SENTINEL

    effective = _resolve_select(select, graph)

    if effective == "**":
        return _collect_all_outputs(state, graph, _EMIT_SENTINEL)

    names = [effective] if isinstance(effective, str) else effective
    return _collect_selected_outputs(state, names, _EMIT_SENTINEL, on_missing)


def _resolve_select(select: Any, graph: Graph) -> str | list[str]:
    """Resolve effective select: unset → graph.selected → '**'."""
    if select is _UNSET_SELECT:
        return list(graph.selected) if graph.selected is not None else "**"
    return select


def _collect_all_outputs(
    state: GraphState,
    graph: Graph,
    sentinel: Any,
) -> dict[str, Any]:
    """Return all graph outputs present in state, excluding emit sentinels."""
    return {k: state.values[k] for k in graph.outputs if k in state.values and state.values[k] is not sentinel}


def _collect_selected_outputs(
    state: GraphState,
    names: list[str],
    sentinel: Any,
    on_missing: str,
) -> dict[str, Any]:
    """Return selected outputs, handling missing per on_missing policy."""
    result = {}
    missing = []
    for k in names:
        if k in state.values and state.values[k] is not sentinel:
            result[k] = state.values[k]
        elif k not in state.values:
            missing.append(k)

    if missing:
        _handle_missing_outputs(missing, state, sentinel, on_missing)

    return result


_VALID_ON_MISSING = ("ignore", "warn", "error")


def _validate_on_missing(on_missing: str) -> None:
    """Validate on_missing parameter eagerly (before execution)."""
    if on_missing not in _VALID_ON_MISSING:
        raise ValueError(f"Invalid on_missing={on_missing!r}. Expected one of: {', '.join(_VALID_ON_MISSING)}")


_VALID_ERROR_HANDLING = ("raise", "continue")


def _validate_error_handling(error_handling: str) -> None:
    """Validate error_handling parameter eagerly (before execution)."""
    if error_handling not in _VALID_ERROR_HANDLING:
        valid = ", ".join(repr(v) for v in _VALID_ERROR_HANDLING)
        raise ValueError(
            f"Invalid error_handling={error_handling!r}.\n\n"
            f"Valid options: {valid}\n\n"
            f"How to fix: Pass error_handling='raise' or error_handling='continue'."
        )


def _validate_workflow_id(workflow_id: str | None, parent_run_id: str | None) -> None:
    """Reject user-provided workflow_id containing '/' (reserved for hierarchy).

    Only validates user-initiated calls (parent_run_id is None). Internal child
    calls from GraphNode executors legitimately use '/' in hierarchical IDs.
    """
    if workflow_id and "/" in workflow_id and parent_run_id is None:
        raise ValueError(
            f"workflow_id cannot contain '/': {workflow_id!r}. "
            "The '/' character is reserved for hierarchical run IDs "
            "(nested graphs, map items). Choose a different workflow_id."
        )


def _handle_missing_outputs(
    missing: list[str],
    state: GraphState,
    sentinel: Any,
    on_missing: str,
) -> None:
    """Handle missing outputs per policy: ignore, warn, or error."""
    if on_missing not in _VALID_ON_MISSING:
        raise ValueError(f"Invalid on_missing={on_missing!r}. Expected one of: {', '.join(_VALID_ON_MISSING)}")
    if on_missing == "ignore":
        return

    available = [k for k in state.values if state.values[k] is not sentinel]
    msg = f"Requested outputs not found: {missing}. Available outputs: {available}"

    if on_missing == "warn":
        import warnings

        # stacklevel=6: _handle → _collect_selected → filter_outputs → run → user
        warnings.warn(msg, UserWarning, stacklevel=6)
    elif on_missing == "error":
        raise ValueError(msg)


def _clone_value(value: Any, param_name: str) -> Any:
    """Deep-copy a value for clone, with clone-specific error."""
    try:
        return copy.deepcopy(value)
    except (TypeError, copy.Error) as e:
        raise GraphConfigError(
            f"Parameter '{param_name}' cannot be deep-copied for clone.\n\n"
            f"Options:\n"
            f"  1. Use clone=[...] to clone only specific params\n"
            f"  2. Use .bind({param_name}=...) on the inner graph to share it\n"
            f"     (bind values bypass clone entirely)\n\n"
            f"Technical details: {e}"
        ) from e


def _maybe_clone_broadcast(
    broadcast_values: dict[str, Any],
    clone: bool | list[str],
) -> dict[str, Any]:
    """Clone broadcast values based on clone config."""
    if clone is False:
        return broadcast_values
    if clone is True:
        return {k: _clone_value(v, k) for k, v in broadcast_values.items()}
    # clone is a list of param names
    return {k: _clone_value(v, k) if k in clone else v for k, v in broadcast_values.items()}


def generate_map_inputs(
    values: dict[str, Any],
    map_over: list[str],
    map_mode: str,
    clone: bool | list[str] = False,
) -> Iterator[dict[str, Any]]:
    """Generate input dicts for each map iteration.

    Args:
        values: Input values dict
        map_over: Parameter names to iterate over
        map_mode: "zip" for parallel iteration, "product" for cartesian product
        clone: Deep-copy broadcast values per iteration.
            False = share by reference (default).
            True = deep-copy all broadcast values.
            list[str] = deep-copy only named params.

    Yields:
        Input dict for each iteration

    Raises:
        ValueError: If zip mode with unequal lengths
    """
    mapped_values = {k: values[k] for k in map_over}
    broadcast_values = {k: v for k, v in values.items() if k not in map_over}

    if map_mode == "zip":
        yield from _generate_zip_inputs(mapped_values, broadcast_values, clone)
    elif map_mode == "product":
        yield from _generate_product_inputs(mapped_values, broadcast_values, clone)
    else:
        raise ValueError(f"Unknown map_mode: {map_mode}")


def _generate_zip_inputs(
    mapped_values: dict[str, list],
    broadcast_values: dict[str, Any],
    clone: bool | list[str] = False,
) -> Iterator[dict[str, Any]]:
    """Generate inputs for zip mode (parallel iteration)."""
    if not mapped_values:
        yield dict(broadcast_values)
        return

    lengths = [len(v) for v in mapped_values.values()]
    if len(set(lengths)) > 1:
        raise ValueError(
            f"map_over parameters must have equal lengths in zip mode. Got lengths: {dict(zip(mapped_values.keys(), lengths, strict=False))}"
        )

    if not lengths:
        return

    for i in range(lengths[0]):
        yield {
            **_maybe_clone_broadcast(broadcast_values, clone),
            **{k: v[i] for k, v in mapped_values.items()},
        }


def _generate_product_inputs(
    mapped_values: dict[str, list],
    broadcast_values: dict[str, Any],
    clone: bool | list[str] = False,
) -> Iterator[dict[str, Any]]:
    """Generate inputs for product mode (cartesian product)."""
    from itertools import product as iter_product

    if not mapped_values:
        yield dict(broadcast_values)
        return

    keys = list(mapped_values.keys())
    value_lists = [mapped_values[k] for k in keys]

    for combo in iter_product(*value_lists):
        yield {
            **_maybe_clone_broadcast(broadcast_values, clone),
            **dict(zip(keys, combo, strict=False)),
        }


def collect_as_lists(
    results: Sequence[RunResult],
    node: GraphNode,
    error_handling: ErrorHandling = "raise",
) -> dict[str, list]:
    """Collect multiple RunResults into lists per output.

    Handles output name translation: inner graph produces original names,
    but we need to return renamed names to match the GraphNode's interface.

    Args:
        results: List of RunResult from runner.map()
        node: The GraphNode (used for output name translation)
        error_handling: How to handle failed results. "raise" raises on first
            failure, "continue" uses None placeholders to preserve list length.

    Returns:
        Dict mapping renamed output names to lists of values
    """
    collected: dict[str, list] = {name: [] for name in node.outputs}
    for result in results:
        if result.status == RunStatus.FAILED:
            if error_handling == "raise":
                raise result.error  # type: ignore[misc]
            # Continue mode: use None placeholders to preserve list length
            for name in node.outputs:
                collected[name].append(None)
            continue
        # Translate original output names to renamed names
        renamed_values = node.map_outputs_from_original(result.values)
        for name in node.outputs:
            if name in renamed_values:
                collected[name].append(renamed_values[name])
    return collected
