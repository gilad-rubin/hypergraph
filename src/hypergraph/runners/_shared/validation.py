"""Validation helpers for runner execution."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import get_close_matches
from typing import TYPE_CHECKING, Any

from hypergraph.exceptions import IncompatibleRunnerError, MissingInputError
from hypergraph.runners._shared.types import RunnerCapabilities

if TYPE_CHECKING:
    from hypergraph.graph import Graph
    from hypergraph.graph.input_spec import InputSpec
    from hypergraph.nodes.base import HyperNode


@dataclass(frozen=True, slots=True)
class _InputValidationContext:
    """Pre-computed graph-structural context for input validation.

    Holds everything derived from graph topology that doesn't depend
    on per-item values.  Compute once via ``precompute_input_validation``,
    then validate each item with ``validate_item_inputs``.
    """

    graph: Graph
    active_nodes: dict[str, HyperNode]
    active_subgraph: Any  # nx DiGraph
    input_spec: InputSpec
    edge_produced: set[str]
    interrupt_outputs: set[str]
    cycle_ep_params: set[str]


def precompute_input_validation(
    graph: Graph,
    *,
    entrypoint: str | None = None,
    selected: tuple[str, ...] | None = None,
) -> _InputValidationContext:
    """Graph-structural validation — call once, reuse across items."""
    from hypergraph.graph._helpers import get_edge_produced_values

    if entrypoint is not None:
        raise ValueError(
            "Runtime entrypoint overrides are no longer supported. "
            "Configure entrypoints on the graph via Graph(..., entrypoint=...) "
            "or graph.with_entrypoint(...)."
        )
    effective_selected = graph.selected if selected is None else selected
    if effective_selected != graph.selected:
        raise ValueError("Runtime select overrides are no longer supported. Configure output scope on the graph via graph.select(...).")
    active_nodes, active_subgraph = _resolve_active_scope(graph, graph.selected)
    input_spec = graph.inputs
    cycle_ep_params = {p for params in input_spec.entrypoints.values() for p in params}
    edge_produced = get_edge_produced_values(active_subgraph)
    interrupt_outputs = _get_interrupt_outputs(active_nodes)

    return _InputValidationContext(
        graph=graph,
        active_nodes=active_nodes,
        active_subgraph=active_subgraph,
        input_spec=input_spec,
        edge_produced=edge_produced,
        interrupt_outputs=interrupt_outputs,
        cycle_ep_params=cycle_ep_params,
    )


def validate_item_inputs(
    ctx: _InputValidationContext,
    values: dict[str, Any],
    *,
    skip_missing_required: bool = False,
) -> None:
    """Per-item value validation using pre-computed context.

    Validates a single set of input values against the pre-computed
    graph context.  Handles bound/provided merge, internal override
    detection, bypass detection, cycle entry, and completeness checks.
    """
    inputs_spec = ctx.input_spec

    # Step 1: Merge bound + provided
    merged = {**inputs_spec.bound, **values}
    provided = set(merged.keys())

    # Step 2/3: Internal/unknown parameter handling
    # Bound values are always expected — the user explicitly set them via bind().
    # InputSpec.all excludes bound params (they're "satisfied"), but since we
    # merge them back into `provided` above, we must also include them here.
    expected_inputs = set(inputs_spec.all) | set(inputs_spec.bound)
    unexpected = provided - expected_inputs - ctx.interrupt_outputs
    internal_edge = unexpected & ctx.edge_produced
    unknown = unexpected - ctx.edge_produced

    if internal_edge:
        conflict_errors = _find_internal_override_conflicts(
            active_nodes=ctx.active_nodes,
            provided=provided,
            internal_edge=internal_edge,
            cycle_ep_params=ctx.cycle_ep_params,
        )
        if conflict_errors:
            scope = f" in graph '{ctx.graph.name}'" if ctx.graph.name else ""
            raise ValueError(f"Cannot determine whether to run or skip node(s){scope}:\n" + "\n".join(conflict_errors))
        raise ValueError(
            _build_internal_override_message(
                internal_edge=internal_edge,
                unknown=set(),
                expected_inputs=expected_inputs,
                active_nodes=ctx.active_nodes,
                graph_name=ctx.graph.name,
            )
        )

    if unknown:
        _warn_on_unrecognized_inputs(
            unknown=unknown,
            expected_inputs=expected_inputs,
            active_nodes=ctx.active_nodes,
            graph_name=ctx.graph.name,
        )

    # Step 4: Completeness check for required inputs
    required = set(inputs_spec.required)
    missing_required = required - provided

    if skip_missing_required or not missing_required:
        return

    all_inputs = set(inputs_spec.all)
    suggestions = _get_suggestions(sorted(missing_required), all_inputs, provided)
    consumers = {m: _find_consumer_paths(ctx.active_nodes, m) for m in sorted(missing_required)}
    message = _build_missing_input_message(
        missing=sorted(missing_required),
        provided=list(provided),
        suggestions=suggestions,
        entrypoints=inputs_spec.entrypoints,
        consumers=consumers,
        graph_name=ctx.graph.name,
    )

    raise MissingInputError(
        missing=sorted(missing_required),
        provided=list(provided),
        message=message,
    )


def validate_inputs(
    graph: Graph,
    values: dict[str, Any],
    *,
    entrypoint: str | None = None,
    selected: tuple[str, ...] | None = None,
    skip_missing_required: bool = False,
) -> None:
    """Validate that all required inputs are provided.

    Convenience wrapper that pre-computes graph context and validates
    a single set of values.  For batch validation (e.g., ``map()``), use
    ``precompute_input_validation`` + ``validate_item_inputs`` directly.
    """
    ctx = precompute_input_validation(graph, entrypoint=entrypoint, selected=selected)
    validate_item_inputs(
        ctx,
        values,
        skip_missing_required=skip_missing_required,
    )


def _get_suggestions(
    missing: list[str],
    all_inputs: set[str],
    provided: set[str],
) -> dict[str, list[str]]:
    """Find similar names to suggest for typos.

    Checks provided values first (for typo detection), then falls back to
    valid inputs that weren't provided (for discoverability).
    """
    suggestions: dict[str, list[str]] = {}
    for m in missing:
        # Check provided values first (typo detection)
        matches = get_close_matches(m, provided, n=1, cutoff=0.6)
        # Fall back to valid inputs that weren't provided
        # Exclude the missing parameter itself to avoid suggesting "embedder -> embedder?"
        if not matches:
            matches = get_close_matches(m, all_inputs - provided - {m}, n=1, cutoff=0.6)
        if matches:
            suggestions[m] = matches
    return suggestions


def _build_missing_input_message(
    missing: list[str],
    provided: list[str],
    suggestions: dict[str, list[str]],
    entrypoints: dict[str, tuple[str, ...]] | None = None,
    consumers: dict[str, list[str]] | None = None,
    graph_name: str | None = None,
) -> str:
    """Build a helpful error message for missing inputs."""
    missing_str = ", ".join(f"'{m}'" for m in sorted(missing))
    scope = f" in graph '{graph_name}'" if graph_name else ""
    msg = f"Missing required inputs{scope}: {missing_str}"

    if consumers and any(consumers.values()):
        msg += "\n\nNeeded by:"
        for m in sorted(missing):
            paths = consumers.get(m) or []
            if paths:
                msg += f"\n  - '{m}' <- {', '.join(paths)}"
            else:
                msg += f"\n  - '{m}'"

    if provided:
        msg += f"\n\nProvided: {', '.join(f'{p!r}' for p in sorted(provided))}"

    if entrypoints:
        msg += "\n\nCycle entry points:"
        for name, params in sorted(entrypoints.items()):
            params_str = ", ".join(params) if params else "(no params needed)"
            msg += f"\n  {name} → {params_str}"

    if suggestions:
        msg += "\n\nDid you mean:"
        for m, sugg in suggestions.items():
            msg += f"\n  - '{m}' -> '{sugg[0]}'?"

    msg += "\n\nHint: If you used graph.bind(), remember it returns a NEW graph."
    msg += "\n  ❌ graph.bind(x=10)           # Result discarded!"
    msg += "\n  ✅ graph = graph.bind(x=10)   # Correct"

    return msg


def build_resume_seed_message(
    graph: Graph,
    missing: list[str],
    active_node_names: set[str] | frozenset[str] | None,
) -> str:
    """Build the message body for a missing-seed-inputs resume failure.

    The active scope here may be a name-set (from ``compute_execution_scope``)
    rather than the full HyperNode mapping, so we look the nodes back up before
    walking nested consumers.
    """
    if active_node_names is None:
        active_nodes_dict = dict(graph._nodes)
    else:
        active_nodes_dict = {name: graph._nodes[name] for name in active_node_names if name in graph._nodes}

    scope = f" in graph '{graph.name}'" if graph.name else ""
    msg = f"Checkpoint resume is missing required seed inputs{scope}: " + ", ".join(repr(name) for name in missing) + "."

    consumer_lines: list[str] = []
    for name in missing:
        paths = _find_consumer_paths(active_nodes_dict, name)
        if paths:
            consumer_lines.append(f"  - '{name}' <- {', '.join(paths)}")
    if consumer_lines:
        msg += "\nNeeded by:\n" + "\n".join(consumer_lines)

    msg += "\nThe restored checkpoint state does not make any pending nodes runnable."
    return msg


def _find_consumer_paths(active_nodes: dict[str, HyperNode], param: str) -> list[str]:
    """Return dot-paths for nodes that consume ``param``, descending into nested graphs.

    A flat-graph node consuming the param yields its name. A GraphNode that
    exposes the param returns paths into whichever inner node ultimately reads
    it, so callers can pinpoint the deeply nested consumer.
    """
    from hypergraph.nodes.graph_node import GraphNode

    paths: list[str] = []
    for node in active_nodes.values():
        if param not in node.inputs:
            continue
        if isinstance(node, GraphNode):
            original = node._resolve_original_input_name(param)
            inner_active = {n.name: n for n in node.iter_active_inner_nodes()}
            inner_paths = _find_consumer_paths(inner_active, original)
            if inner_paths:
                paths.extend(f"{node.name}.{p}" for p in inner_paths)
            else:
                paths.append(node.name)
        else:
            paths.append(node.name)
    return paths


def validate_runner_compatibility(
    graph: Graph,
    capabilities: RunnerCapabilities,
) -> None:
    """Validate that a runner can execute a graph.

    Checks:
    - If graph has async nodes, runner must support them
    - If graph has cycles, runner must support them (currently all do)

    Args:
        graph: The graph to validate
        capabilities: The runner's capabilities

    Raises:
        IncompatibleRunnerError: If runner can't handle graph features
    """
    # Check async nodes
    if graph.has_async_nodes and not capabilities.supports_async_nodes:
        # Find the async node(s) for a helpful error message
        async_nodes = [node.name for node in graph._nodes.values() if node.is_async]
        raise IncompatibleRunnerError(
            f"Graph contains async node(s) but runner doesn't support async: {', '.join(async_nodes)}. Use AsyncRunner instead.",
            node_name=async_nodes[0] if async_nodes else None,
            capability="supports_async_nodes",
        )

    # Check cycles (all current runners support cycles, but future-proofing)
    if graph.has_cycles and not capabilities.supports_cycles:
        raise IncompatibleRunnerError(
            "Graph contains cycles but runner doesn't support cycles.",
            capability="supports_cycles",
        )

    # Check gates
    if graph.has_gates and not capabilities.supports_gates:
        gate_names = [node.name for node in graph._nodes.values() if node.is_gate]
        raise IncompatibleRunnerError(
            f"Graph has gates (@route/@branch) but runner doesn't support gates: {', '.join(gate_names)}.",
            capability="supports_gates",
        )

    # Check interrupts
    if graph.has_interrupts and not capabilities.supports_interrupts:
        interrupt_names = [node.name for node in graph._nodes.values() if node.is_interrupt]
        raise IncompatibleRunnerError(
            f"Graph contains InterruptNode(s) but runner doesn't support interrupts: {', '.join(interrupt_names)}. Use AsyncRunner instead.",
            node_name=interrupt_names[0] if interrupt_names else None,
            capability="supports_interrupts",
        )

    # Recurse into nested GraphNodes
    from hypergraph.nodes.graph_node import GraphNode

    for node in graph._nodes.values():
        if isinstance(node, GraphNode):
            validate_runner_compatibility(node.graph, capabilities)


def validate_map_compatible(graph: Graph) -> None:
    """Validate that a graph can be used with map().

    Checks that graphs with interrupts are not used with map().

    Args:
        graph: The graph to validate

    Raises:
        IncompatibleRunnerError: If graph contains InterruptNodes
    """
    if graph.has_interrupts:
        raise IncompatibleRunnerError(
            "Graph contains InterruptNode(s) which are incompatible with .map(). Use .run() for graphs with interrupts.",
            capability="supports_interrupts",
        )


def _resolve_active_scope(
    graph: Graph,
    selected: tuple[str, ...] | None,
) -> tuple[dict[str, HyperNode], Any]:
    """Resolve active nodes and active subgraph for current entrypoint/selection."""
    from hypergraph.graph.input_spec import _compute_active_scope

    return _compute_active_scope(
        graph._nodes,
        graph._nx_graph,
        entrypoints=graph._entrypoints,
        selected=selected,
    )


def _get_interrupt_outputs(
    nodes: dict[str, HyperNode],
    *,
    prefix: str = "",
) -> set[str]:
    """Get interrupt resume keys in the active scope, including nested GraphNodes."""
    from hypergraph.nodes.graph_node import GraphNode

    outputs: set[str] = set()
    for node in nodes.values():
        if node.is_interrupt:
            outputs.update(f"{prefix}{output}" for output in node.data_outputs)
            continue
        if isinstance(node, GraphNode):
            nested_outputs = _get_interrupt_outputs({inner.name: inner for inner in node.iter_active_inner_nodes()})
            outputs.update(f"{prefix}{node.name}.{node.map_resume_key_from_original(output)}" for output in nested_outputs)
    return outputs


def _find_internal_override_conflicts(
    *,
    active_nodes: dict[str, HyperNode],
    provided: set[str],
    internal_edge: set[str],
    cycle_ep_params: set[str],
) -> list[str]:
    """Find hard conflicts where compute and inject modes are mixed."""
    if not internal_edge:
        return []

    downstream_consumed = {param for node in active_nodes.values() for param in node.inputs}
    conflicts: list[str] = []

    for node in active_nodes.values():
        node_outputs = set(node.outputs) - cycle_ep_params
        injected_outputs = sorted(node_outputs & internal_edge)
        if not injected_outputs:
            continue

        # Compute+inject: node can run but user also provides its outputs
        if _node_is_runnable_from_seed_values(node, provided):
            seeded_inputs = sorted(set(node.inputs) & provided)
            if seeded_inputs:
                conflicts.append(
                    f"- '{node.name}' conflict — you provided both its inputs "
                    f"{seeded_inputs} and its outputs {injected_outputs}.\n"
                    f"    To skip '{node.name}': remove {seeded_inputs}\n"
                    f"    To run  '{node.name}': remove {injected_outputs}"
                )
            else:
                defaults = sorted([p for p in node.inputs if node.has_default_for(p)])
                consumed_for_node = (node_outputs & downstream_consumed) - cycle_ep_params
                all_needed = sorted(consumed_for_node)
                conflicts.append(
                    f"- '{node.name}' conflict — you provided its outputs "
                    f"{injected_outputs}, but defaults {defaults} also make it runnable.\n"
                    f"    To skip '{node.name}': also provide {sorted(set(all_needed) - set(injected_outputs))}\n"
                    f"    To run  '{node.name}': remove {injected_outputs}"
                )
            continue

        # Partial inject: some outputs provided but downstream needs others
        consumed_for_node = (node_outputs & downstream_consumed) - cycle_ep_params
        missing_consumed = sorted(consumed_for_node - provided)
        if missing_consumed:
            all_needed = sorted(consumed_for_node)
            conflicts.append(
                f"- '{node.name}' conflict — you provided {injected_outputs} "
                f"but it also produces {missing_consumed} which downstream nodes need.\n"
                f"    To skip '{node.name}': also provide {missing_consumed}\n"
                f"    To run  '{node.name}': remove {injected_outputs}"
            )

    return conflicts


def _node_is_runnable_from_seed_values(node: HyperNode, provided: set[str]) -> bool:
    """True when node can run from provided/bound/default values before execution."""
    return all(param in provided or node.has_default_for(param) for param in node.inputs)


def _warn_on_unrecognized_inputs(
    *,
    unknown: set[str],
    expected_inputs: set[str],
    active_nodes: dict[str, HyperNode],
    graph_name: str | None = None,
) -> None:
    """Warn when extra provided keys are outside the active graph scope."""
    if not unknown:
        return

    import warnings

    warnings.warn(
        _build_internal_override_message(
            internal_edge=set(),
            unknown=unknown,
            expected_inputs=expected_inputs,
            active_nodes=active_nodes,
            graph_name=graph_name,
        ),
        UserWarning,
        stacklevel=4,
    )


def _build_internal_override_message(
    *,
    internal_edge: set[str],
    unknown: set[str],
    expected_inputs: set[str],
    active_nodes: dict[str, HyperNode],
    graph_name: str | None = None,
) -> str:
    """Create a clear internal override message with producer mapping."""
    unexpected = sorted(internal_edge | unknown)
    scope = f" in graph '{graph_name}'" if graph_name else ""
    message = [f"Providing values for internal parameters{scope}: {unexpected}."]

    if internal_edge:
        producers: list[str] = []
        for output_name in sorted(internal_edge):
            source_nodes = sorted(node.name for node in active_nodes.values() if output_name in node.outputs)
            if source_nodes:
                producers.append(f"{output_name} <- {', '.join(source_nodes)}")
            else:
                producers.append(f"{output_name} <- <unknown producer>")
        message.append(f"Produced by active graph edges: {', '.join(producers)}.")

    if unknown:
        message.append(f"Not recognized in active graph scope: {sorted(unknown)}.")

    message.append(f"Expected inputs: {sorted(expected_inputs)}")
    return " ".join(message)


def _find_bypassed_inputs(graph: Graph, provided: set[str], inputs_spec: InputSpec) -> set[str]:
    """Legacy helper for classifying impossible compute-vs-inject mixes.

    The runtime no longer supports supplying active internal edge-produced
    values to skip execution. This helper remains as topology analysis for
    conflict reporting, describing which inputs would become irrelevant if a
    user tries to provide all outputs of some active producer nodes.
    """
    # Build output -> producer nodes mapping (handle mutex branches)
    output_to_nodes: dict[str, list[str]] = {}
    for node in graph._nodes.values():
        for output in node.outputs:
            output_to_nodes.setdefault(output, []).append(node.name)

    # Find which outputs are consumed by downstream nodes
    consumed_outputs: set[str] = set()
    for node in graph._nodes.values():
        consumed_outputs.update(node.inputs)

    # Cycle entry point params bootstrap a cycle and do not imply that an
    # active producer has been replaced.
    cycle_ep_params = {p for params in inputs_spec.entrypoints.values() for p in params}

    # A node is considered fully replaced only if ALL its non-cycle consumed
    # outputs are provided by the user.
    bypassed_nodes: set[str] = set()
    for node in graph._nodes.values():
        node_consumed_outputs = (set(node.outputs) & consumed_outputs) - cycle_ep_params
        if node_consumed_outputs and node_consumed_outputs <= provided:
            # All consumed outputs are provided — treat the node as replaced for
            # conflict analysis.
            bypassed_nodes.add(node.name)

    if not bypassed_nodes:
        return set()

    # Also mark nodes as replaced if ALL their non-cycle outputs are provided.
    # Unlike the first pass (consumed only), this catches nodes whose outputs
    # are not consumed downstream but are still being fully supplied by the
    # user in a contradictory request.
    for node in graph._nodes.values():
        non_cycle_outputs = set(node.outputs) - cycle_ep_params
        if non_cycle_outputs and non_cycle_outputs <= provided:
            bypassed_nodes.add(node.name)

    # Collect inputs exclusively needed by bypassed nodes
    bypassed_inputs: set[str] = set()
    for node_name in bypassed_nodes:
        node = graph._nodes[node_name]
        bypassed_inputs.update(node.inputs)

    # Remove inputs that are also consumed by non-bypassed nodes
    for node in graph._nodes.values():
        if node.name not in bypassed_nodes:
            bypassed_inputs -= set(node.inputs)

    return bypassed_inputs


def resolve_runtime_selected(
    select: Any,
    graph: Graph,
) -> tuple[str, ...] | None:
    """Resolve effective select under canonical graph scope semantics."""
    from hypergraph.runners._shared.helpers import _UNSET_SELECT

    if select is _UNSET_SELECT:
        return graph.selected

    raise ValueError("Runtime select overrides are no longer supported. Configure output scope on the graph via graph.select(...).")


def _resolve_effective_input_spec(
    graph: Graph,
    selected: tuple[str, ...] | None,
    *,
    active_scope: tuple[dict[str, HyperNode], Any] | None = None,
) -> InputSpec:
    """Return canonical graph InputSpec (runtime selection overrides removed)."""
    effective_selected = graph.selected if selected is None else selected
    if effective_selected != graph.selected:
        raise ValueError("Runtime select overrides are no longer supported. Configure output scope on the graph via graph.select(...).")
    return graph.inputs


def validate_delegated_runners(
    graph: Graph,
    parent_capabilities: RunnerCapabilities,
) -> None:
    """Validate runner_override on GraphNodes within the graph.

    Checks two things for each GraphNode with a runner_override:
    1. The delegated runner can handle the subgraph's features
       (async nodes, cycles, interrupts).
    2. The parent→child direction is compatible — a sync parent
       cannot delegate to a runner whose run() returns a coroutine.

    Args:
        graph: The graph to scan for delegated GraphNodes
        parent_capabilities: The parent runner's capabilities

    Raises:
        IncompatibleRunnerError: If delegation is invalid
    """
    from hypergraph.nodes.graph_node import GraphNode

    for node in graph._nodes.values():
        if not isinstance(node, GraphNode):
            continue
        override = node.runner_override
        if override is None:
            continue

        child_caps = override.capabilities

        # Check subgraph compatibility with the delegated runner
        validate_runner_compatibility(node.graph, child_caps)

        # Sync parent cannot delegate to an async-returning runner
        if not parent_capabilities.returns_coroutine and child_caps.returns_coroutine:
            raise IncompatibleRunnerError(
                f"GraphNode '{node.name}' has runner_override that returns a coroutine, "
                f"but the parent runner is synchronous. "
                f"Use AsyncRunner as the parent, or choose a sync-compatible runner for '{node.name}'.",
                node_name=node.name,
                capability="returns_coroutine",
            )


def validate_node_types(
    graph: Graph,
    supported_types: set[type[HyperNode]],
) -> None:
    """Validate that all nodes in graph have registered executors.

    Args:
        graph: The graph to validate
        supported_types: Set of node types the runner supports

    Raises:
        TypeError: If a node type is not supported by the runner
    """
    for node in graph._nodes.values():
        node_type = type(node)
        if node_type not in supported_types:
            supported_names = [t.__name__ for t in supported_types]
            raise TypeError(f"Runner does not support node type '{node_type.__name__}'. Supported types: {supported_names}")
