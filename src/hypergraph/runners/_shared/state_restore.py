"""Execution-state initialization, checkpoint replay, and workflow identity."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from hypergraph.checkpointers.types import StepRecord, StepStatus
from hypergraph.nodes.base import HyperNode
from hypergraph.runners._shared.state import GraphState, NodeExecution

if TYPE_CHECKING:
    from hypergraph.checkpointers.types import Checkpoint
    from hypergraph.graph import Graph
    from hypergraph.nodes.graph_node import GraphNode


def initialize_state(
    graph: Graph,
    values: dict[str, Any],
    *,
    checkpoint: Checkpoint | None = None,
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


def _extract_model_type(hint: Any) -> type | None:
    """Extract a Pydantic BaseModel or dataclass type from a type hint.

    Handles: Model, Optional[Model], Model | None, list[Model].
    Returns the concrete model class (or the full list[Model] hint), else None.

    The parameterized-generic branches are checked *before* the bare
    ``isinstance(hint, type)`` branch on purpose: on Python 3.10
    ``isinstance(list[X], type)`` is True (it became False in 3.11), so a leading
    scalar check would misclassify ``list[Model]`` and return None there.
    """
    import types as _types
    from typing import Union, get_args, get_origin

    if hint is None:
        return None

    origin = get_origin(hint)

    # list[Model] — unwrap and check element type
    if origin is list:
        args = get_args(hint)
        if args and isinstance(args[0], type) and _is_model_class(args[0]):
            return hint  # return the full list[Model] hint
        return None

    # tuple[...] — JSON persistence round-trips tuples as lists, so any
    # parameterized tuple annotation is worth reconstructing on restore.
    if origin is tuple:
        return hint

    # Optional[Model] / Union[Model, None] / Model | None (PEP 604)
    if origin is Union or isinstance(hint, _types.UnionType):
        args = [a for a in get_args(hint) if a is not type(None)]
        if len(args) == 1 and isinstance(args[0], type) and _is_model_class(args[0]):
            return args[0]
        return None

    # Bare model class — guarded by `origin is None` so parameterized generics
    # (e.g. list[Model], which is `isinstance(..., type)` on 3.10) never land here.
    if origin is None and isinstance(hint, type) and _is_model_class(hint):
        return hint

    return None


def _is_model_class(cls: type) -> bool:
    """Check if cls is a Pydantic BaseModel or a dataclass."""
    import dataclasses

    if hasattr(cls, "model_validate"):
        return True
    return bool(dataclasses.is_dataclass(cls))


def _coerce_value(value: Any, hint: Any) -> Any:
    """Reconstruct a typed value from a deserialized dict/list."""
    from typing import get_args, get_origin

    if value is None:
        return None

    origin = get_origin(hint)

    # list[Model] — coerce each element
    if origin is list:
        if not isinstance(value, list):
            return value
        args = get_args(hint)
        if not args:
            return value
        elem_type = args[0]
        return [_coerce_single(item, elem_type) for item in value]

    # tuple[...] — rebuild the tuple, coercing model elements where annotated
    if origin is tuple:
        return _coerce_tuple(value, hint)

    # Scalar model
    if isinstance(hint, type):
        return _coerce_single(value, hint)

    return value


def _coerce_tuple(value: Any, hint: Any) -> Any:
    """Reconstruct an annotated tuple from a JSON-deserialized list."""
    from typing import get_args

    if not isinstance(value, (list, tuple)):
        return value
    args = get_args(hint)
    if len(args) == 2 and args[1] is Ellipsis:
        elem = args[0]
        if isinstance(elem, type) and _is_model_class(elem):
            return tuple(_coerce_single(item, elem) for item in value)
        return tuple(value)
    if args and len(args) == len(value):
        return tuple(
            _coerce_single(item, elem) if isinstance(elem, type) and _is_model_class(elem) else item for item, elem in zip(value, args, strict=False)
        )
    return tuple(value)


def _coerce_single(value: Any, model: type) -> Any:
    """Coerce a single value to a model type. Returns value unchanged on failure."""
    import dataclasses

    if isinstance(value, model):
        return value
    if not isinstance(value, dict):
        return value
    try:
        if hasattr(model, "model_validate"):
            return model.model_validate(value)
        if dataclasses.is_dataclass(model):
            return model(**{k: v for k, v in value.items() if k in {f.name for f in dataclasses.fields(model)}})
    except Exception:
        return value
    return value


def _build_output_type_map(graph: Graph) -> dict[str, Any]:
    """Build a map from value name to its annotated type hint.

    Checks both output annotations (from producers) and input annotations
    (from consumers) so that fork/checkpoint-restore works even when the
    forked graph doesn't contain the original producer node.
    """
    type_map: dict[str, Any] = {}
    for node_obj in graph._nodes.values():
        # Output types from the producing node
        for output_name in node_obj.data_outputs:
            hint = node_obj.get_output_type(output_name)
            if hint is not None:
                resolved = _extract_model_type(hint)
                if resolved is not None:
                    type_map[output_name] = resolved
        # Input types from consuming nodes
        for input_name in node_obj.inputs:
            if input_name in type_map:
                continue
            hint = node_obj.get_input_type(input_name)
            if hint is not None:
                resolved = _extract_model_type(hint)
                if resolved is not None:
                    type_map[input_name] = resolved
    return type_map


def coerce_checkpoint_values(
    graph: Graph,
    values: dict[str, Any],
) -> dict[str, Any]:
    """Reconstruct typed models from deserialized checkpoint dicts.

    Walks the graph's output type annotations and converts plain dicts
    back into Pydantic models or dataclasses where the type is known.
    """
    type_map = _build_output_type_map(graph)
    coerced = dict(values)
    for name, value in coerced.items():
        hint = type_map.get(name)
        if hint is not None:
            coerced[name] = _coerce_value(value, hint)
    return coerced


def initialize_state_with_checkpoint(
    *,
    graph: Graph,
    checkpoint_values: dict[str, Any],
    runtime_values: dict[str, Any],
    steps: list[StepRecord],
) -> GraphState:
    """Restore GraphState from checkpoint state with one ordered step replay."""
    from hypergraph.nodes.gate import END as _END
    from hypergraph.nodes.gate import IfElseNode, RouteNode

    state = GraphState()
    state.values = coerce_checkpoint_values(graph, checkpoint_values)

    graph_input_names = set(graph.inputs.all)
    bound_names = set(graph.inputs.bound)
    seeded_inputs = {name for name in checkpoint_values if name in graph_input_names and name not in bound_names}
    versions = {name: 1 for name in seeded_inputs}
    replay_versions = {name: 1 for name in seeded_inputs}

    completed_steps = sorted(
        (step for step in steps if step.status == StepStatus.COMPLETED),
        key=lambda step: (step.superstep, step.index),
    )
    for step in completed_steps:
        input_versions = dict(step.input_versions or {})
        step_values = dict(step.values or {})
        for input_name, consumed_version in input_versions.items():
            versions[input_name] = max(versions.get(input_name, 0), int(consumed_version))

        output_versions: dict[str, int] = {}
        for output_name in step_values:
            versions[output_name] = versions.get(output_name, 0) + 1
            replay_versions[output_name] = replay_versions.get(output_name, 0) + 1
            output_versions[output_name] = replay_versions[output_name]

        state.node_executions[step.node_name] = NodeExecution(
            node_name=step.node_name,
            input_versions=input_versions,
            outputs=step_values,
            output_versions=output_versions,
            duration_ms=step.duration_ms,
            cached=step.cached,
            sequence=step.index,
        )
        if step.decision is not None:
            decision = step.decision
            if decision == "END":
                decision = _END
            elif isinstance(decision, list):
                decision = [_END if target == "END" else target for target in decision]
            state.routing_decisions[step.node_name] = decision

    state.versions = versions

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

    type_map = _build_output_type_map(graph)
    for name in list(runtime_values):
        hint = type_map.get(name)
        if hint is not None:
            runtime_values[name] = _coerce_value(runtime_values[name], hint)
        state.update_value(name, runtime_values[name])
    state.resume_values = frozenset(runtime_values) if is_interrupt_resume_payload(graph, runtime_values) else frozenset()

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

    Interrupt outputs are projected through GraphNode boundary maps the same
    way ordinary outputs are.
    """
    from hypergraph.nodes.graph_node import GraphNode

    allowed_outputs: set[str] = set()
    for node in graph.iter_nodes():
        if node.is_interrupt:
            allowed_outputs.update(f"{prefix}{output}" for output in node.data_outputs)
            continue
        if isinstance(node, GraphNode):
            nested_keys = _collect_interrupt_resume_keys_from_nodes(node.iter_active_inner_nodes())
            allowed_outputs.update(f"{prefix}{node.map_resume_key_from_original(key)}" for key in nested_keys)
    return allowed_outputs


def _collect_interrupt_resume_keys_from_nodes(
    nodes: tuple[HyperNode, ...],
    *,
    prefix: str = "",
) -> set[str]:
    """Collect interrupt resume keys from an explicit active node scope."""
    from hypergraph.nodes.graph_node import GraphNode

    allowed_outputs: set[str] = set()
    for node in nodes:
        if node.is_interrupt:
            allowed_outputs.update(f"{prefix}{output}" for output in node.data_outputs)
            continue
        if isinstance(node, GraphNode):
            nested_keys = _collect_interrupt_resume_keys_from_nodes(node.iter_active_inner_nodes())
            allowed_outputs.update(f"{prefix}{node.map_resume_key_from_original(key)}" for key in nested_keys)
    return allowed_outputs


def graphnode_child_workflow_id(
    workflow_id: str | None,
    node_name: str,
    state: GraphState,
) -> str | None:
    """Return a child workflow id for a GraphNode execution.

    The first execution keeps the historical ``parent/node`` form so paused
    nested workflows can resume with the same id. Re-executions append a stable
    suffix derived from the previous execution's versions so checkpointed outer
    cycles don't collide with completed child runs.
    """
    if workflow_id is None:
        return None

    base = f"{workflow_id}/{node_name}"
    execution = state.node_executions.get(node_name)
    if execution is None:
        return base

    # output_versions are recorded after the previous execution completed, so
    # they already reflect the suffix we want for the next child run. For nodes
    # without recorded outputs, input_versions reflect the pre-execution state,
    # so advance one step beyond the highest seen value.
    iteration = max(execution.output_versions.values()) if execution.output_versions else max(execution.input_versions.values(), default=0) + 1
    return f"{base}/{iteration}"


def restore_completed_child_outputs(
    node: GraphNode,
    child_values: dict[str, Any],
) -> dict[str, Any]:
    """Project a terminal COMPLETED child workflow's persisted state onto the parent.

    Crash-window recovery: the child workflow committed COMPLETED, but the
    parent crashed before writing its GraphNode StepRecord. On resume the
    parent must not re-invoke the terminal child (that raises
    ``WorkflowAlreadyCompletedError``); instead it restores the child's
    persisted outputs so the missing parent step commits with truthful values.

    Mirrors the normal execution path: values are coerced with the child
    graph's type map (as a real child resume would), filtered to the child
    graph's outputs, and projected through the GraphNode boundary map.
    """
    from hypergraph.runners._shared.outputs import filter_outputs

    restored = GraphState(values=coerce_checkpoint_values(node.graph, child_values))
    return node.map_outputs_from_original(filter_outputs(restored, node.graph))


def validate_workflow_id(workflow_id: str | None, parent_run_id: str | None) -> None:
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
