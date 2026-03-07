"""Async executor for GraphNode."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.helpers import collect_as_lists, map_inputs_to_func_params
from hypergraph.runners._shared.types import PauseExecution, PauseInfo, RunResult, RunStatus

if TYPE_CHECKING:
    from hypergraph.events.processor import EventProcessor
    from hypergraph.nodes.graph_node import GraphNode
    from hypergraph.runners._shared.types import GraphState
    from hypergraph.runners.async_.runner import AsyncRunner


class AsyncGraphNodeExecutor:
    """Executes GraphNode asynchronously by delegating to runner.

    Handles:
    - Simple nested graph execution
    - Map-over execution (delegates to runner.map())

    Nested graphs inherit the parent's concurrency limiter via ContextVar,
    so max_concurrency is shared across all levels of execution.
    """

    def __init__(self, runner: AsyncRunner):
        """Initialize with reference to parent runner.

        Args:
            runner: The AsyncRunner that owns this executor
        """
        self.runner = runner
        self._last_inner_logs: ContextVar[tuple] = ContextVar(
            "async_graph_node_executor_last_inner_logs",
            default=(),
        )

    @property
    def last_inner_logs(self) -> tuple:
        """Latest nested logs for the current task context."""
        return self._last_inner_logs.get()

    def consume_last_inner_logs(self) -> tuple:
        """Read and clear nested logs for the current task context."""
        logs = self._last_inner_logs.get()
        self._last_inner_logs.set(())
        return logs

    async def __call__(
        self,
        node: GraphNode,
        state: GraphState,
        inputs: dict[str, Any],
        *,
        event_processors: list[EventProcessor] | None = None,
        parent_span_id: str | None = None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute a GraphNode by running its inner graph.

        Inherits the parent's concurrency limiter via ContextVar.
        All nested operations share the same global concurrency budget.

        Args:
            node: The GraphNode to execute
            state: Current graph execution state (unused directly)
            inputs: Input values for the nested graph
            event_processors: Processors to propagate to nested runs
            parent_span_id: Span ID of the parent node for event linking
            workflow_id: Parent workflow ID for hierarchical checkpointing

        Returns:
            Dict mapping output names to their values
        """
        # Translate renamed input keys back to original inner graph names
        inner_inputs = map_inputs_to_func_params(node, inputs)
        child_workflow_id = f"{workflow_id}/{node.name}" if workflow_id else None

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
            if child_workflow_id is not None and self.runner._checkpointer is not None:
                existing_child_run = await self.runner._checkpointer.get_run_async(child_workflow_id)
                if existing_child_run is not None:
                    inner_inputs = {}
            prefix = f"{node.name}."
            for key in state.values:
                if key.startswith(prefix):
                    inner_inputs[key[len(prefix) :]] = state.values[key]

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
                event_processors=event_processors,
                workflow_id=child_workflow_id,
                _parent_span_id=parent_span_id,
                _parent_run_id=workflow_id,
            )
            self._last_inner_logs.set(tuple(r.log for r in results if r.log is not None))
            return collect_as_lists(results, node, error_handling)

        result = await self.runner.run(
            node.graph,
            inner_inputs,
            event_processors=event_processors,
            workflow_id=child_workflow_id,
            _parent_span_id=parent_span_id,
            _parent_run_id=workflow_id,
        )
        self._last_inner_logs.set((result.log,) if result.log is not None else ())
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
