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
        output_name = node.outputs[0]

        # Resume path: output already in state (provided via values dict)
        if output_name in state.values:
            return {output_name: state.values[output_name]}

        input_value = inputs[node.inputs[0]]

        # Handler path: auto-resolve via node-attached handler
        if node.handler is not None:
            try:
                response = node.handler(input_value)
                if isawaitable(response):
                    response = await response
            except Exception as e:
                raise RuntimeError(
                    f"Handler for InterruptNode \'{node.name}\' failed: "
                    f"{type(e).__name__}: {e}"
                ) from e
            return {output_name: response}

        # Pause path: no handler, no resume value
        raise PauseExecution(
            PauseInfo(
                node_name=node.name,
                output_param=output_name,
                value=input_value,
            )
        )
