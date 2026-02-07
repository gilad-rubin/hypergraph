"""Async executor for InterruptNode."""

from __future__ import annotations

from inspect import isawaitable
from typing import TYPE_CHECKING, Any

from hypergraph.nodes.base import _EMIT_SENTINEL
from hypergraph.runners._shared.types import PauseExecution, PauseInfo

if TYPE_CHECKING:
    from hypergraph.nodes.interrupt import InterruptNode
    from hypergraph.runners._shared.types import GraphState


class AsyncInterruptNodeExecutor:
    """Executes InterruptNode: checks resume state, invokes handler, or pauses."""

    async def __call__(
        self,
        node: "InterruptNode",
        state: "GraphState",
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        data_outputs = node.data_outputs
        is_multi_output = len(data_outputs) > 1
        is_multi_input = node.is_multi_input

        # Collect all input values with validation
        input_values = {}
        for name in node.inputs:
            if name not in inputs:
                raise KeyError(
                    f"InterruptNode '{node.name}' requires input '{name}' "
                    f"but it was not provided. Available inputs: {list(inputs.keys())}"
                )
            input_values[name] = inputs[name]

        # Resume path: ALL data outputs already in state (provided via values dict)
        # Skip pause only if all values exist AND node hasn't executed yet this run
        # (in cycles, the node re-executes and should pause again)
        all_outputs_present = all(o in state.values for o in data_outputs)
        if all_outputs_present and node.name not in state.node_executions:
            result = {o: state.values[o] for o in data_outputs}
            return _add_emit_sentinels(result, node)

        # Handler path: invoke the function/handler
        if node.func is not None:
            try:
                response = _call_handler(node, input_values)
                if isawaitable(response):
                    response = await response
            except Exception as e:
                raise RuntimeError(
                    f"Handler for InterruptNode '{node.name}' failed: "
                    f"{type(e).__name__}: {e}"
                ) from e

            # None return means "pause" (Option E semantics)
            if response is not None:
                result = _normalize_response(node, response, data_outputs, is_multi_output)
                return _add_emit_sentinels(result, node)

        # Pause path: no handler, or handler returned None
        raise PauseExecution(
            PauseInfo(
                node_name=node.name,
                output_param=data_outputs[0],
                value=input_values[node.inputs[0]],
                output_params=data_outputs if is_multi_output else None,
                values=input_values if is_multi_input else None,
            )
        )


def _call_handler(node: "InterruptNode", input_values: dict[str, Any]) -> Any:
    """Call the handler with the appropriate calling convention."""
    # Func-based nodes: call with keyword arguments
    if node._use_kwargs:
        params = node.map_inputs_to_params(input_values)
        return node.func(**params)

    # Legacy class-constructor nodes: positional calling convention
    if node.is_multi_input:
        return node.func(input_values)
    return node.func(input_values[node.inputs[0]])


def _normalize_response(
    node: "InterruptNode",
    response: Any,
    data_outputs: tuple[str, ...],
    is_multi_output: bool,
) -> dict[str, Any]:
    """Normalize handler response to output dict (data outputs only)."""
    if is_multi_output:
        if isinstance(response, dict):
            expected_keys = set(data_outputs)
            actual_keys = set(response.keys())
            if actual_keys != expected_keys:
                missing = expected_keys - actual_keys
                extra = actual_keys - expected_keys
                raise ValueError(
                    f"Handler for InterruptNode '{node.name}' returned dict "
                    f"with incorrect keys. Expected: {sorted(expected_keys)}, "
                    f"Got: {sorted(actual_keys)}. "
                    + (f"Missing: {sorted(missing)}. " if missing else "")
                    + (f"Extra: {sorted(extra)}." if extra else "")
                )
            return response
        # Single value returned for multi-output: assign to first data output
        return {data_outputs[0]: response}
    return {data_outputs[0]: response}


def _add_emit_sentinels(result: dict[str, Any], node: "InterruptNode") -> dict[str, Any]:
    """Add emit sentinel values to the result dict."""
    emit_outputs = node.outputs[len(node.data_outputs):]
    for name in emit_outputs:
        result[name] = _EMIT_SENTINEL
    return result
