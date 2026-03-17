"""Async executor for GraphNode."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.helpers import collect_as_lists, graphnode_child_workflow_id, map_inputs_to_func_params
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

    Nested graphs inherit the parent's concurrency limiter, so
    max_concurrency is shared across all levels of execution.
    """

    def __init__(self, runner: AsyncRunner):
        """Initialize with reference to parent runner."""
        self.runner = runner

    async def __call__(
        self,
        node: GraphNode,
        state: GraphState,
        inputs: dict[str, Any],
        ctx: ExecutionContext,
    ) -> dict[str, Any]:
        """Execute a GraphNode by running its inner graph.

        All nested operations share the same global concurrency budget.

        Args:
            node: The GraphNode to execute
            state: Current graph execution state (unused directly)
            inputs: Input values for the nested graph
            ctx: Execution context with event processors, workflow ID, and
                nested log sink

        Returns:
            Dict mapping output names to their values
        """
        # Translate renamed input keys back to original inner graph names
        inner_inputs = map_inputs_to_func_params(node, inputs)
        child_workflow_id = graphnode_child_workflow_id(ctx.workflow_id, node.name, state)
        map_config = node.map_config

        # Route interrupt resume values into the inner graph.
        # On pause, _handle_nested_result prefixes the node_name ("ask_user/ask_slack")
        # and PauseInfo.response_key becomes "ask_user.user_input".  On resume the
        # caller puts that dotted key into the outer state — we strip the prefix here
        # so the inner graph sees the unprefixed key ("user_input").
        # When resuming a persisted child workflow, do not re-send normal child
        # inputs like "draft" — those are already captured by the child
        # checkpoint and would be treated as illegal overrides.
        # Only inject on first execution: on cycle loop-back the node has already
        # executed so the inner interrupt should fire again, not skip.
        if node.name not in state.node_executions:
            child_fork_from: str | None = None
            child_retry_from: str | None = None
            prefix = f"{node.name}."
            resume_values = {
                node.resolve_original_output_name(key[len(prefix) :]): value for key, value in state.values.items() if key.startswith(prefix)
            }

            if map_config is None and child_workflow_id is not None and self.runner._checkpointer is not None:
                existing_child_run = await self.runner._checkpointer.get_run_async(child_workflow_id)
                if existing_child_run is not None:
                    inner_inputs = {}
                elif resume_values:
                    current_parent_run = await self.runner._checkpointer.get_run_async(ctx.workflow_id) if ctx.workflow_id else None
                    source_parent_run_id = None
                    if current_parent_run is not None:
                        source_parent_run_id = current_parent_run.retry_of or current_parent_run.forked_from
                    if source_parent_run_id is not None:
                        source_child_run_id = graphnode_child_workflow_id(source_parent_run_id, node.name, state)
                        source_child_run = (
                            await self.runner._checkpointer.get_run_async(source_child_run_id) if source_child_run_id is not None else None
                        )
                        if source_child_run is not None:
                            inner_inputs = {}
                            if current_parent_run is not None and current_parent_run.retry_of is not None:
                                child_retry_from = source_child_run_id
                            else:
                                child_fork_from = source_child_run_id

            inner_inputs.update(resume_values)
        else:
            child_fork_from = None
            child_retry_from = None

        # Use delegated runner if configured, otherwise inherit parent
        runner = node.runner_override or self.runner

        if map_config:
            _, mode, error_handling = map_config
            # Use original param names for map_over (inner graph expects these)
            original_params = node._original_map_params()
            map_kwargs: dict[str, Any] = {
                "map_over": original_params,
                "map_mode": mode,
                "clone": node._original_clone(),
                "error_handling": error_handling,
                "event_processors": ctx.event_processors,
                "show_progress": ctx.show_progress,
                "workflow_id": child_workflow_id,
                "_parent_span_id": ctx.parent_span_id,
                "_parent_run_id": ctx.workflow_id,
            }
            if getattr(node, "_complete_on_stop", False):
                map_kwargs["_complete_on_stop"] = True
            map_call = runner.map(node.graph, inner_inputs, **map_kwargs)
            # Delegated runner may be sync (e.g. DaftRunner) — await only if needed
            results = await map_call if inspect.isawaitable(map_call) else map_call
            if ctx.on_inner_log:
                for result in results:
                    if result.log is not None:
                        ctx.on_inner_log(result.log)
            return collect_as_lists(results, node, error_handling)

        # Only pass checkpoint kwargs if the delegated runner supports checkpointing
        run_kwargs: dict[str, Any] = {
            "event_processors": ctx.event_processors,
            "show_progress": ctx.show_progress,
            "workflow_id": child_workflow_id,
            "_parent_span_id": ctx.parent_span_id,
            "_parent_run_id": ctx.workflow_id,
        }
        if runner.capabilities.supports_checkpointing:
            run_kwargs["fork_from"] = child_fork_from
            run_kwargs["retry_from"] = child_retry_from
        if getattr(node, "_complete_on_stop", False):
            run_kwargs["_complete_on_stop"] = True

        run_call = runner.run(node.graph, inner_inputs, **run_kwargs)
        # Delegated runner may be sync (e.g. DaftRunner) — await only if needed
        result = await run_call if inspect.isawaitable(run_call) else run_call
        if ctx.on_inner_log and result.log is not None:
            ctx.on_inner_log(result.log)
        return self._handle_nested_result(node, result)

    def _handle_nested_result(self, node: GraphNode, result: RunResult) -> dict[str, Any]:
        """Handle result from nested graph, propagating pause if needed."""
        if result.status == RunStatus.PAUSED:
            assert result.pause is not None, "PAUSED status requires pause info"
            nested_pause = PauseInfo(
                node_name=f"{node.name}/{result.pause.node_name}",
                output_param=node.map_output_name_from_original(result.pause.output_param),
                value=result.pause.value,
                # Propagate multi-output fields (new in PR #40)
                output_params=(
                    tuple(node.map_output_name_from_original(name) for name in result.pause.output_params)
                    if result.pause.output_params is not None
                    else None
                ),
                values=result.pause.values,
            )
            raise PauseExecution(nested_pause)
        return node.map_outputs_from_original(result.values)
