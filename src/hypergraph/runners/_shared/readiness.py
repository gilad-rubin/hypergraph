"""Node readiness, gate activation, and execution-state transitions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hypergraph.nodes.base import HyperNode
from hypergraph.runners._shared.scheduling import ExecutionComponent, compute_startup_predecessors
from hypergraph.runners._shared.state import GraphState, NodeExecution
from hypergraph.runners._shared.value_resolution import (
    address_for_node_input,
    graphnode_has_resume_values,
    has_all_inputs,
    has_input,
    latest_upstream_output_version,
)

if TYPE_CHECKING:
    from hypergraph.graph import Graph


def get_ready_nodes(
    graph: Graph,
    state: GraphState,
    *,
    active_nodes: set[str] | None = None,
    startup_predecessors: dict[str, frozenset[str]] | None = None,
    candidate_nodes: Sequence[str] | None = None,
    execution_order: Sequence[str] | None = None,
) -> list[HyperNode]:
    """Find nodes whose inputs are all satisfied and not stale.

    Suspension is a same-superstep ordering rule, never a reachability rule.
    If suspension alone empties the ready frontier, activation is recomputed
    once without it. The controller was not in the empty frontier and cannot
    fire, so gate-first ordering is vacuous and master reachability applies.

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
        candidate_nodes: Optional node names to restrict scheduling to.
            Used by the SCC executor to localize readiness to one component.
        execution_order: Optional stable ordering for node evaluation.

    Returns:
        List of nodes ready to execute
    """
    if startup_predecessors is None:
        startup_predecessors = compute_startup_predecessors(graph, active_nodes=active_nodes)

    candidate_set = set(candidate_nodes) if candidate_nodes is not None else None
    ordered_names = tuple(execution_order) if execution_order is not None else tuple(graph._nodes)

    from hypergraph.nodes.gate import END, GateNode

    activated_nodes = _get_activated_nodes(graph, state)
    suspension_lifted = False
    while True:
        ready = []
        for node_name in ordered_names:
            node = graph._nodes[node_name]
            if active_nodes is not None and node.name not in active_nodes:
                continue
            if candidate_set is not None and node.name not in candidate_set:
                continue
            if _is_node_ready(node, graph, state, activated_nodes, startup_predecessors=startup_predecessors):
                ready.append(node)

        # If a gate is ready, its routing decision should apply before targets
        # run. Block targets of ready gates for this superstep so decisions
        # take effect on the next iteration.
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

        # Defer wait_for consumers whose producers are also ready this
        # superstep.
        ready = _defer_wait_for_nodes(ready, graph, state)
        if ready or suspension_lifted:
            return ready

        unsuspended_nodes = _get_activated_nodes(graph, state, suspend_pending_decisions=False)
        if not unsuspended_nodes - activated_nodes:
            return ready
        activated_nodes = unsuspended_nodes
        suspension_lifted = True


def get_ready_nodes_in_component(
    graph: Graph,
    state: GraphState,
    *,
    component: ExecutionComponent,
    active_nodes: set[str] | frozenset[str] | None,
    startup_predecessors: dict[str, frozenset[str]],
) -> list[HyperNode]:
    """Find ready nodes inside a single execution component."""
    return get_ready_nodes(
        graph,
        state,
        active_nodes=active_nodes,
        startup_predecessors=startup_predecessors,
        candidate_nodes=component.node_names,
        execution_order=component.node_names,
    )


def find_missing_resume_seed_inputs(
    graph: Graph,
    state: GraphState,
    *,
    active_nodes: set[str] | frozenset[str] | None = None,
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
        startup_predecessors = compute_startup_predecessors(graph, active_nodes=active_nodes)

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
            if graphnode_has_resume_values(node, state):
                continue
            if param in graph_inputs and not has_input(param, node, graph, state):
                missing.add(param)

    return missing


def gate_permits_startup(
    node_name: str,
    *,
    decision: Any,
    gate_executed: bool,
    node_executed: bool,
    default_open: bool,
    entrypoints: tuple[str, ...] | None,
    gate_activated: bool = True,
) -> bool:
    """Return whether one controlling gate permits a target to start.

    The early returns mirror the canonical gate-activation decision table.
    Executed decisions take precedence over startup-only defaults.

    ``gate_activated`` says whether the controlling gate's authority is still
    causally live, and it scopes both branches (issue #220):

    - Decision branch: a pending decision activates its targets only while
      the deciding gate is not cut off upstream (see ``_compute_cut_gates``).
      An orphaned decision — the deciding gate's own control path was
      explicitly terminated or routed away — must not fire anything.
    - First-pass ``default_open`` branch: permission is only as alive as the
      gate handing it out; a gate whose own control path is already blocked
      can never fire, so its targets must not start on data readiness alone.
      Explicit entrypoints are exempt — the user asked to start there, and
      the controlling gate may be outside the active scope entirely.
    """
    if decision is not None:
        if not gate_activated:
            return False
        return _is_node_activated_by_decision(node_name, decision)
    if gate_executed:
        return False
    if node_executed:
        return False
    if not default_open:
        return False
    if entrypoints:
        return node_name in entrypoints
    return gate_activated


def _get_activated_nodes(
    graph: Graph,
    state: GraphState,
    *,
    suspend_pending_decisions: bool = True,
) -> set[str]:
    """Get all nodes activated by gate routing or first-pass startup.

    Computed as a shrinking fixpoint so blocking propagates through chained
    gates (``gate_a -> gate_b -> target``): when ``gate_a`` decides END or
    routes elsewhere, ``gate_b`` is deactivated, and ``target`` loses its
    first-pass ``default_open`` permission through ``gate_b``. Starting from
    "everything activated" (greatest fixpoint) preserves first-pass startup
    for undecided gate chains, including gates that control each other in a
    cycle. Live decision-based activation never depends on the fixpoint, so
    gate re-firing in cycles re-activates targets as before; orphaned
    decisions of cut-off gates are dropped first and activate nothing.

    Implemented as a worklist: a node is re-examined only when one of its
    controlling gates was just deactivated, keeping the total predicate-call
    count linear in graph size regardless of node declaration order.
    """
    from collections import deque

    # Stale decisions must be cleared before any activation is evaluated,
    # then orphaned decisions of cut-off gates dropped, then gates downstream
    # of a re-firing controller transiently suspended (issue #220).
    _clear_stale_gate_decisions(graph, state)
    controls = _build_controls_map(graph)
    cut_gates = _compute_cut_gates(graph, state, controls)
    _drop_orphaned_decisions(state, cut_gates)
    suspended_gates = _compute_suspended_gates(graph, state, controls) if suspend_pending_decisions else set()

    activated = set(graph._nodes)
    gated_names = [name for name in graph._nodes if graph.controlled_by.get(name)]
    queue = deque(gated_names)
    queued = set(gated_names)
    while queue:
        node_name = queue.popleft()
        queued.discard(node_name)
        if node_name not in activated:
            continue
        if _any_gate_permits_startup(node_name, graph, state, activated, cut_gates, suspended_gates):
            continue
        activated.discard(node_name)
        for dependent in controls.get(node_name, ()):
            if dependent in activated and dependent not in queued:
                queue.append(dependent)
                queued.add(dependent)

    return activated


def _any_gate_permits_startup(
    node_name: str,
    graph: Graph,
    state: GraphState,
    activated: set[str],
    cut_gates: set[str],
    suspended_gates: set[str],
) -> bool:
    """Whether at least one controlling gate permits this node to start."""
    for gate_name in graph.controlled_by.get(node_name, []):
        gate = graph._nodes.get(gate_name)
        if gate is None:
            continue
        decision = state.routing_decisions.get(gate_name)
        # The liveness signal matches the branch the predicate will take:
        # a pending decision is live while its gate is neither cut off nor
        # suspended behind a re-firing controller; first-pass default_open
        # permission is live while the gate is still activated.
        alive = (gate_name not in cut_gates and gate_name not in suspended_gates) if decision is not None else gate_name in activated
        if gate_permits_startup(
            node_name,
            decision=decision,
            gate_executed=gate_name in state.node_executions,
            node_executed=node_name in state.node_executions,
            default_open=getattr(gate, "default_open", True),
            entrypoints=graph.entrypoints_config,
            gate_activated=alive,
        ):
            return True
    return False


def _build_controls_map(graph: Graph) -> dict[str, list[str]]:
    """Invert ``controlled_by``: gate name -> nodes it controls."""
    controls: dict[str, list[str]] = {}
    for target, gates in graph.controlled_by.items():
        for gate_name in gates:
            controls.setdefault(gate_name, []).append(target)
    return controls


def _compute_cut_gates(
    graph: Graph,
    state: GraphState,
    controls: dict[str, list[str]],
) -> set[str]:
    """Gates whose control path is explicitly severed upstream (worklist).

    A gate is *cut off* when every controlling gate either currently holds an
    explicit decision that excludes it (END or routed elsewhere) or is itself
    cut off. A controller with no current decision — never fired, or its
    selection was already consumed — keeps its targets alive: it may still
    (re-)fire and route to them. That consumed-means-live rule is what keeps
    in-flight pending decisions working across cycle iterations.
    """
    from collections import deque

    from hypergraph.nodes.gate import GateNode

    controlled_gates = [name for name, node in graph._nodes.items() if isinstance(node, GateNode) and graph.controlled_by.get(name)]
    cut: set[str] = set()
    queue = deque(controlled_gates)
    queued = set(controlled_gates)
    while queue:
        gate_name = queue.popleft()
        queued.discard(gate_name)
        if gate_name in cut:
            continue
        if _any_controller_keeps_alive(gate_name, graph, state, cut):
            continue
        cut.add(gate_name)
        for dependent in controls.get(gate_name, ()):
            if isinstance(graph._nodes.get(dependent), GateNode) and dependent not in cut and dependent not in queued:
                queue.append(dependent)
                queued.add(dependent)

    return cut


def _any_controller_keeps_alive(
    gate_name: str,
    graph: Graph,
    state: GraphState,
    cut: set[str],
) -> bool:
    """Whether any controlling gate can still (re-)route to this gate."""
    for controller in graph.controlled_by.get(gate_name, []):
        if controller not in graph._nodes:
            continue
        if controller in cut:
            continue
        decision = state.routing_decisions.get(controller)
        if decision is None or _is_node_activated_by_decision(gate_name, decision):
            return True
    return False


def _compute_suspended_gates(
    graph: Graph,
    state: GraphState,
    controls: dict[str, list[str]],
) -> set[str]:
    """Gates whose pending decisions must wait for an upstream re-fire.

    When a controlling gate is scheduled to re-execute — the exact signal
    that already clears the gate's own stale decision — its verdict is
    pending, and chains below it must not act on previously-pending
    downstream decisions in the same superstep: the gate fires first, then
    its consequences propagate. Suspension spreads transitively down control
    edges (gates only) and is recomputed from current state on every
    evaluation, so nothing is persisted: it lifts as soon as the controller
    has re-fired. This is what distinguishes 'controller decision None
    because harmlessly consumed' (downstream decisions stay live) from
    'controller decision None and controller is stale' (verdict pending —
    wait one superstep). Independent nodes outside the re-firing gate's
    chain are unaffected.
    """
    from collections import deque

    from hypergraph.nodes.gate import GateNode

    refiring = [
        name
        for name, gate in graph._nodes.items()
        if isinstance(gate, GateNode) and name in state.node_executions and _needs_execution(gate, graph, state)
    ]
    suspended: set[str] = set()
    queue = deque(refiring)
    while queue:
        gate_name = queue.popleft()
        for dependent in controls.get(gate_name, ()):
            if isinstance(graph._nodes.get(dependent), GateNode) and dependent not in suspended:
                suspended.add(dependent)
                queue.append(dependent)

    return suspended


def _drop_orphaned_decisions(state: GraphState, cut_gates: set[str]) -> None:
    """Delete pending decisions made by cut-off gates.

    Once a gate's control path is explicitly severed, its in-flight selection
    is causally dead — deleting it (rather than suppressing it) prevents the
    decision from resurrecting later, e.g. after the upstream exclusion is
    itself consumed. END decisions are kept: they activate nothing and remain
    a truthful terminal marker.
    """
    from hypergraph.nodes.gate import END

    for gate_name in cut_gates:
        decision = state.routing_decisions.get(gate_name)
        if decision is not None and decision is not END:
            del state.routing_decisions[gate_name]


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


def apply_node_result(
    graph: Graph,
    state: GraphState,
    node: HyperNode,
    outputs: dict[str, Any],
    input_versions: dict[str, int],
    wait_for_versions: dict[str, int],
    *,
    duration_ms: float,
    cached: bool,
) -> None:
    """Apply node execution results to state in place."""
    for name, value in outputs.items():
        state.update_value(name, value)

    output_versions = {name: state.get_version(name) for name in outputs}
    sequence = (
        max(
            (execution.sequence for execution in state.node_executions.values() if execution.sequence >= 0),
            default=-1,
        )
        + 1
    )

    state.node_executions[node.name] = NodeExecution(
        node_name=node.name,
        input_versions=input_versions,
        outputs=outputs,
        output_versions=output_versions,
        wait_for_versions=wait_for_versions,
        duration_ms=duration_ms,
        cached=cached,
        sequence=sequence,
    )

    for gate_name in graph.controlled_by.get(node.name, []):
        decision = state.routing_decisions.get(gate_name)
        if decision is None:
            continue
        if isinstance(decision, list):
            remaining = [target for target in decision if target != node.name]
            if remaining:
                state.routing_decisions[gate_name] = remaining
            else:
                del state.routing_decisions[gate_name]
            continue
        if _is_node_activated_by_decision(node.name, decision):
            del state.routing_decisions[gate_name]


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
    if not has_all_inputs(node, graph, state):
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
        # Resolve the key the same way it was recorded at the graph boundary.
        addr = address_for_node_input(node, param)
        current_version = state.versions.get(addr, 0)
        consumed_version = last_exec.input_versions.get(addr, 0)
        if current_version == consumed_version:
            continue

        # Only producers wired to this node/input should trigger staleness.
        upstream = input_data_producers.get(param, frozenset())
        if upstream and latest_upstream_output_version(param, upstream, state) <= consumed_version:
            continue
        return True
    return False
