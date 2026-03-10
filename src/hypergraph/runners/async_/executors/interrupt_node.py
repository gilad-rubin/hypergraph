"""Async executor for InterruptNode."""

from __future__ import annotations

from inspect import isawaitable
from typing import TYPE_CHECKING, Any

from hypergraph.nodes.base import _EMIT_SENTINEL
from hypergraph.runners._shared.types import PauseExecution, PauseInfo

if TYPE_CHECKING:
    from hypergraph.nodes.interrupt import InterruptNode
    from hypergraph.runners._shared.types import ExecutionContext, GraphState


class AsyncInterruptNodeExecutor:
    """Executes InterruptNode: checks resume state, invokes handler, or pauses."""

    async def __call__(
        self,
        node: InterruptNode,
        state: GraphState,
        inputs: dict[str, Any],
        ctx: ExecutionContext,
    ) -> dict[str, Any]:
        data_outputs = node.data_outputs

        # Collect all input values with validation
        input_values = {}
        for name in node.inputs:
            if name not in inputs:
                raise KeyError(
                    f"InterruptNode '{node.name}' requires input '{name}' but it was not provided. Available inputs: {list(inputs.keys())}"
                )
            input_values[name] = inputs[name]

        # Resume path: only auto-resolve when the runner is actually resuming
        # a paused workflow AND all data outputs are in provided_values.
        # Without the is_resuming guard, output names that happen to match
        # regular graph inputs (e.g. user_input used by both add_user_message
        # and the interrupt) would cause the interrupt to skip its handler
        # on a fresh run instead of pausing.
        # After consuming, we pop the keys so a second cycle iteration
        # through this node correctly invokes the handler and pauses.
        if ctx.is_resuming:
            pv = ctx.provided_values
            all_outputs_provided = all(o in pv for o in data_outputs)
            if all_outputs_provided:
                result = {o: pv.pop(o) for o in data_outputs}
                return _add_emit_sentinels(result, node)

        # Handler path: invoke the function
        try:
            params = node.map_inputs_to_params(input_values)
            response = node.func(**params)
            if isawaitable(response):
                response = await response
        except Exception as e:
            raise RuntimeError(f"Handler for InterruptNode '{node.name}' failed: {type(e).__name__}: {e}") from e

        # None return means "pause"
        if response is not None:
            result = _normalize_response(node, response, data_outputs)
            return _add_emit_sentinels(result, node)

        # Pause path: handler returned None
        if not data_outputs:
            raise RuntimeError(f"InterruptNode '{node.name}' returned None (pause) but has no data outputs to resume into")
        raise PauseExecution(
            PauseInfo(
                node_name=node.name,
                output_param=data_outputs[0],
                value=input_values[node.inputs[0]] if node.inputs else None,
                output_params=data_outputs if len(data_outputs) > 1 else None,
                values=input_values if len(node.inputs) > 1 else None,
            )
        )


def _normalize_response(
    node: InterruptNode,
    response: Any,
    data_outputs: tuple[str, ...],
) -> dict[str, Any]:
    """Normalize handler response to output dict (data outputs only)."""
    if not data_outputs:
        return {}
    if len(data_outputs) > 1 and isinstance(response, dict):
        expected_keys = set(data_outputs)
        actual_keys = set(response.keys())
        if actual_keys != expected_keys:
            missing = expected_keys - actual_keys
            extra = actual_keys - expected_keys
            raise ValueError(
                f"Handler for InterruptNode '{node.name}' returned dict "
                f"with incorrect keys. Expected: {sorted(expected_keys)}, "
                f"Got: {sorted(actual_keys)}. " + (f"Missing: {sorted(missing)}. " if missing else "") + (f"Extra: {sorted(extra)}." if extra else "")
            )
        return response
    # Single value (or single value for multi-output): assign to first output
    return {data_outputs[0]: response}


def _add_emit_sentinels(result: dict[str, Any], node: InterruptNode) -> dict[str, Any]:
    """Add emit sentinel values to the result dict."""
    emit_outputs = node.outputs[len(node.data_outputs) :]
    for name in emit_outputs:
        result[name] = _EMIT_SENTINEL
    return result
