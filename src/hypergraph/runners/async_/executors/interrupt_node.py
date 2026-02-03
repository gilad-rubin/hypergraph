"""Async executor for InterruptNode."""

from __future__ import annotations

from inspect import isawaitable
from typing import TYPE_CHECKING, Any

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
        is_multi_output = node.is_multi_output
        is_multi_input = node.is_multi_input

        # Collect all input values
        input_values = {name: inputs[name] for name in node.inputs}

        # Resume path: ALL outputs already in state (provided via values dict)
        # Skip pause only if all values exist AND node hasn't executed yet this run
        # (in cycles, the node re-executes and should pause again)
        all_outputs_present = all(o in state.values for o in node.outputs)
        if all_outputs_present and node.name not in state.node_executions:
            return {o: state.values[o] for o in node.outputs}

        # Handler path: auto-resolve via node-attached handler
        if node.handler is not None:
            try:
                if is_multi_input:
                    response = node.handler(input_values)
                else:
                    response = node.handler(input_values[node.inputs[0]])
                if isawaitable(response):
                    response = await response
            except Exception as e:
                raise RuntimeError(
                    f"Handler for InterruptNode '{node.name}' failed: "
                    f"{type(e).__name__}: {e}"
                ) from e

            # Normalize response to dict
            if is_multi_output:
                if isinstance(response, dict):
                    return response
                # Single value returned for multi-output: assign to first output
                return {node.outputs[0]: response}
            return {node.outputs[0]: response}

        # Pause path: no handler, no resume value
        raise PauseExecution(
            PauseInfo(
                node_name=node.name,
                output_param=node.outputs[0],
                value=input_values[node.inputs[0]],
                output_params=node.outputs if is_multi_output else None,
                values=input_values if is_multi_input else None,
            )
        )
