"""Validation helpers for runner execution."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import get_close_matches
from typing import TYPE_CHECKING, Any, Literal

from hypergraph.exceptions import IncompatibleRunnerError, MissingInputError
from hypergraph.runners._shared.types import RunnerCapabilities

if TYPE_CHECKING:
    from hypergraph.graph import Graph
    from hypergraph.graph.input_spec import InputSpec
    from hypergraph.nodes.base import HyperNode

_VALID_ON_INTERNAL_OVERRIDE = ("ignore", "warn", "error")


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
    on_internal_override: Literal["ignore", "warn", "error"] = "warn",
) -> None:
    """Per-item value validation using pre-computed context.

    Validates a single set of input values against the pre-computed
    graph context.  Handles bound/provided merge, internal override
    detection, bypass detection, cycle entry, and completeness checks.
    """
    _validate_on_internal_override(on_internal_override)
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
            raise ValueError("Cannot determine whether to run or skip node(s):\n" + "\n".join(conflict_errors))
        raise ValueError(
            _build_internal_override_message(
                internal_edge=internal_edge,
                unknown=set(),
                expected_inputs=expected_inputs,
                active_nodes=ctx.active_nodes,
            )
        )

    if unknown:
        _handle_internal_override_policy(
            on_internal_override=on_internal_override,
            internal_edge=set(),
            unknown=unknown,
            expected_inputs=expected_inputs,
            active_nodes=ctx.active_nodes,
        )

    # Step 4: Completeness check for required inputs
    required = set(inputs_spec.required)
    missing_required = required - provided

    if not missing_required:
        return

    all_inputs = set(inputs_spec.all)
    suggestions = _get_suggestions(sorted(missing_required), all_inputs, provided)
    message = _build_missing_input_message(
        missing=sorted(missing_required),
        provided=list(provided),
        suggestions=suggestions,
        entrypoints=inputs_spec.entrypoints,
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
    on_internal_override: Literal["ignore", "warn", "error"] = "warn",
) -> None:
    """Validate that all required inputs are provided.

    Convenience wrapper that pre-computes graph context and validates
    a single set of values.  For batch validation (e.g., ``map()``), use
    ``precompute_input_validation`` + ``validate_item_inputs`` directly.
    """
    ctx = precompute_input_validation(graph, entrypoint=entrypoint, selected=selected)
    validate_item_inputs(ctx, values, on_internal_override=on_internal_override)


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
) -> str:
    """Build a helpful error message for missing inputs."""
    missing_str = ", ".join(f"'{m}'" for m in sorted(missing))
    msg = f"Missing required inputs: {missing_str}"

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

    # Check interrupts
    if graph.has_interrupts and not capabilities.supports_interrupts:
        interrupt_names = [node.name for node in graph._nodes.values() if node.is_interrupt]
        raise IncompatibleRunnerError(
            f"Graph contains InterruptNode(s) but runner doesn't support interrupts: {', '.join(interrupt_names)}. Use AsyncRunner instead.",
            node_name=interrupt_names[0] if interrupt_names else None,
            capability="supports_interrupts",
        )


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


def _validate_on_internal_override(policy: str) -> None:
    """Validate on_internal_override parameter."""
    if policy not in _VALID_ON_INTERNAL_OVERRIDE:
        valid = ", ".join(_VALID_ON_INTERNAL_OVERRIDE)
        raise ValueError(f"Invalid on_internal_override={policy!r}. Expected one of: {valid}")


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


def _get_interrupt_outputs(nodes: dict[str, HyperNode]) -> set[str]:
    """Get output names produced by interrupt nodes in the active scope."""
    return {output for n in nodes.values() if n.is_interrupt for output in n.outputs}


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


def _handle_internal_override_policy(
    *,
    on_internal_override: Literal["ignore", "warn", "error"],
    internal_edge: set[str],
    unknown: set[str],
    expected_inputs: set[str],
    active_nodes: dict[str, HyperNode],
) -> None:
    """Apply policy for non-conflicting internal/unknown parameters."""
    if not internal_edge and not unknown:
        return
    if on_internal_override == "ignore":
        return

    message = _build_internal_override_message(
        internal_edge=internal_edge,
        unknown=unknown,
        expected_inputs=expected_inputs,
        active_nodes=active_nodes,
    )

    if on_internal_override == "warn":
        import warnings

        warnings.warn(message, UserWarning, stacklevel=4)
        return
    raise ValueError(message)


def _build_internal_override_message(
    *,
    internal_edge: set[str],
    unknown: set[str],
    expected_inputs: set[str],
    active_nodes: dict[str, HyperNode],
) -> str:
    """Create a clear internal override message with producer mapping."""
    unexpected = sorted(internal_edge | unknown)
    message = [f"Providing values for internal parameters: {unexpected}."]

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
    """Find inputs that belong to nodes fully bypassed by intermediate injection.

    A node is bypassed ONLY if ALL of its outputs that are consumed downstream
    are provided by the user. If any consumed output is missing, the node must
    still run to produce it.

    When a node is bypassed, its exclusive inputs (not needed by other nodes)
    can be removed from the required set.

    Note: This iterates ``graph._nodes`` (the full graph), not just the active
    subgraph. This is safe because bypassed inputs are subtracted from
    ``inputs_spec.required``, which is already scoped to active nodes.
    Extra bypassed inputs from inactive nodes are no-ops.
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

    # Cycle entry point params: providing these means bootstrapping a cycle,
    # NOT bypassing the producer node. Exclude from bypass check.
    cycle_ep_params = {p for params in inputs_spec.entrypoints.values() for p in params}

    # A node is bypassed only if ALL its non-cycle consumed outputs are provided
    bypassed_nodes: set[str] = set()
    for node in graph._nodes.values():
        node_consumed_outputs = (set(node.outputs) & consumed_outputs) - cycle_ep_params
        if node_consumed_outputs and node_consumed_outputs <= provided:
            # All consumed outputs are provided — node is bypassed
            bypassed_nodes.add(node.name)

    if not bypassed_nodes:
        return set()

    # Also mark nodes as bypassed if ALL their non-cycle outputs are provided.
    # Unlike the first pass (consumed only), this catches nodes whose outputs
    # aren't consumed downstream but are still fully overridden by user values.
    # Cycle entry point params are excluded: providing them means bootstrapping
    # the cycle, NOT bypassing the producer.
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
