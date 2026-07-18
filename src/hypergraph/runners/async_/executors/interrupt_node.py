"""Async executor for InterruptNode."""

from __future__ import annotations

from inspect import isawaitable
from typing import TYPE_CHECKING, Any

from hypergraph.nodes.base import _EMIT_SENTINEL
from hypergraph.runners._shared.results import PauseInfo
from hypergraph.runners._shared.state import PauseExecution

if TYPE_CHECKING:
    from hypergraph.nodes.interrupt import InterruptNode
    from hypergraph.runners._shared.state import ExecutionContext, GraphState


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

        # Answer-supplied path. Checkpointer-free runs deliberately set
        # is_resuming=True so an answer supplied up front skips the question;
        # checkpointed fresh runs reserve this path for an actual resume.
        # Checked BEFORE input validation: checkpoint state folds step outputs
        # only, so run-provided question inputs are absent on resume — and the
        # answer path never calls the handler, so it doesn't need them (#316).
        # After consuming, we pop the keys so a second cycle iteration
        # through this node correctly invokes the handler and pauses.
        if ctx.is_resuming:
            pv = ctx.provided_values
            all_outputs_provided = all(o in pv for o in data_outputs)
            if all_outputs_provided:
                result = {o: pv.pop(o) for o in data_outputs}
                return _add_emit_sentinels(result, node)

        # Question path requires every input for the handler call.
        input_values = {}
        for name in node.inputs:
            if name not in inputs:
                raise KeyError(
                    f"InterruptNode '{node.name}' requires input '{name}' but it was not provided. Available inputs: {list(inputs.keys())}"
                )
            input_values[name] = inputs[name]

        # Handler path: invoke the function
        try:
            params = node.map_inputs_to_params(input_values)
            response = node.func(**params)
            if isawaitable(response):
                response = await response
        except Exception as e:
            raise RuntimeError(f"Handler for InterruptNode '{node.name}' failed: {type(e).__name__}: {e}") from e

        if response is None:
            raise RuntimeError(
                f"InterruptNode '{node.name}' returned None, but an interrupt handler must return the question payload\n\n"
                f"How to fix: Return an ask-like object with prompt, options, and evidence fields."
            )
        _validate_ask_payload(node, response)
        raise PauseExecution(
            PauseInfo(
                node_name=node.name,
                value=response,
                response_key=data_outputs[0],
            )
        )


def _validate_ask_payload(node: InterruptNode, payload: Any) -> None:
    """Validate the runtime half of the engine's structural ask seam."""
    prompt = getattr(payload, "prompt", None)
    has_options = hasattr(payload, "options")
    options = getattr(payload, "options", None)
    evidence = getattr(payload, "evidence", None)

    if not isinstance(prompt, str):
        raise RuntimeError(
            f"InterruptNode '{node.name}' returned a question whose prompt is not a str\n\nHow to fix: Set question.prompt to a string."
        )
    if not has_options or (options is not None and (not isinstance(options, tuple) or not all(isinstance(option, str) for option in options))):
        raise RuntimeError(
            f"InterruptNode '{node.name}' returned a question whose options must be tuple[str, ...] or None\n\n"
            f"How to fix: Set question.options to a tuple of strings, or None."
        )
    if not isinstance(evidence, tuple):
        raise RuntimeError(
            f"InterruptNode '{node.name}' returned a question whose evidence is not a tuple\n\n"
            f"How to fix: Set question.evidence to a tuple (use () when empty)."
        )


def _add_emit_sentinels(result: dict[str, Any], node: InterruptNode) -> dict[str, Any]:
    """Add emit sentinel values to the result dict."""
    emit_outputs = node.outputs[len(node.data_outputs) :]
    for name in emit_outputs:
        result[name] = _EMIT_SENTINEL
    return result
