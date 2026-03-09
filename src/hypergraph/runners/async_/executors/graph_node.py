"""Async executor for GraphNode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.helpers import collect_as_lists, map_inputs_to_func_params
from hypergraph.runners._shared.types import PauseExecution, PauseInfo, RunResult, RunStatus

if TYPE_CHECKING:
    from hypergraph.nodes.graph_node import GraphNode
    from hypergraph.runners._shared.types import ExecutionContext, GraphState
    from hypergraph.runners.async_.runner import AsyncRunner


class AsyncGraphNodeExecutor:
    """Executes GraphNode asynchronously by delegating to runner.

    Handles:
    - Simple nested graph execution
    - Map-over execution (delegates to runner.map())

    Inner RunLogs are pushed to the caller via ctx.on_inner_log callback.
    Each concurrent GraphNode execution gets its own ctx with its own
    callback — no shared state, no ContextVar needed.

    Nested graphs inherit the parent's concurrency limiter via ContextVar,
    so max_concurrency is shared across all levels of execution.
    """

    def __init__(self, runner: AsyncRunner):
        self.runner = runner

    async def __call__(
        self,
        node: GraphNode,
        state: GraphState,
        inputs: dict[str, Any],
        ctx: ExecutionContext,
    ) -> dict[str, Any]:
        """Execute a GraphNode by running its inner graph.

        Inherits the parent's concurrency limiter via ContextVar.
        All nested operations share the same global concurrency budget.

        Args:
            node: The GraphNode to execute
            state: Current graph execution state
            inputs: Input values for the nested graph
            ctx: Execution context with event_processors, span IDs, workflow_id,
                 and on_inner_log callback for pushing nested RunLogs

        Returns:
            Dict mapping output names to their values
        """
        # Translate renamed input keys back to original inner graph names
        inner_inputs = map_inputs_to_func_params(node, inputs)

        # Route interrupt resume values into the inner graph.
        # On pause, _handle_nested_result prefixes the node_name ("ask_user/ask_slack")
        # and PauseInfo.response_key becomes "ask_user.user_input".  On resume the
        # caller puts that dotted key into the outer state — we strip the prefix here
        # so the inner graph sees the unprefixed key ("user_input").
        # Only inject on first execution: on cycle loop-back the node has already
        # executed so the inner interrupt should fire again, not skip.
        if node.name not in state.node_executions:
            prefix = f"{node.name}."
            for key in state.values:
                if key.startswith(prefix):
                    inner_inputs[key[len(prefix) :]] = state.values[key]

        child_workflow_id = f"{ctx.workflow_id}/{node.name}" if ctx.workflow_id else None

        map_config = node.map_config

        if map_config:
            _, mode, error_handling = map_config
            # Use original param names for map_over (inner graph expects these)
            original_params = node._original_map_params()
            results = await self.runner.map(
                node.graph,
                inner_inputs,
                map_over=original_params,
                map_mode=mode,
                clone=node._original_clone(),
                error_handling=error_handling,
                event_processors=ctx.event_processors,
                workflow_id=child_workflow_id,
                _parent_span_id=ctx.parent_span_id,
                _parent_run_id=ctx.workflow_id,
            )
            if ctx.on_inner_log:
                for r in results:
                    if r.log is not None:
                        ctx.on_inner_log(r.log)
            return collect_as_lists(results, node, error_handling)

        result = await self.runner.run(
            node.graph,
            inner_inputs,
            event_processors=ctx.event_processors,
            workflow_id=child_workflow_id,
            _parent_span_id=ctx.parent_span_id,
            _parent_run_id=ctx.workflow_id,
        )
        if ctx.on_inner_log and result.log is not None:
            ctx.on_inner_log(result.log)
        return self._handle_nested_result(node, result)

    def _handle_nested_result(self, node: GraphNode, result: RunResult) -> dict[str, Any]:
        """Handle result from nested graph, propagating pause if needed."""
        if result.status == RunStatus.PAUSED:
            assert result.pause is not None, "PAUSED status requires pause info"
            nested_pause = PauseInfo(
                node_name=f"{node.name}/{result.pause.node_name}",
                output_param=result.pause.output_param,
                value=result.pause.value,
                # Propagate multi-output fields (new in PR #40)
                output_params=result.pause.output_params,
                values=result.pause.values,
            )
            raise PauseExecution(nested_pause)
        return node.map_outputs_from_original(result.values)
