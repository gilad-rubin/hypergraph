"""Sync executor for GraphNode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.helpers import collect_as_lists, graphnode_child_workflow_id, map_inputs_to_func_params

if TYPE_CHECKING:
    from hypergraph.events.processor import EventProcessor
    from hypergraph.nodes.graph_node import GraphNode
    from hypergraph.runners._shared.types import GraphState
    from hypergraph.runners.sync.runner import SyncRunner


class SyncGraphNodeExecutor:
    """Executes GraphNode by delegating to runner.

    Handles:
    - Simple nested graph execution
    - Map-over execution (delegates to runner.map())
    """

    def __init__(self, runner: SyncRunner):
        """Initialize with reference to parent runner.

        Args:
            runner: The SyncRunner that owns this executor
        """
        self.runner = runner
        self.last_inner_logs: tuple = ()

    def __call__(
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
        child_workflow_id = graphnode_child_workflow_id(workflow_id, node.name, state)
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
            sync_cp = self.runner._get_sync_checkpointer(child_workflow_id)
            if map_config is None and sync_cp is not None and sync_cp.get_run(child_workflow_id) is not None:
                inner_inputs = {}
            prefix = f"{node.name}."
            for key in state.values:
                if key.startswith(prefix):
                    inner_key = node.resolve_original_output_name(key[len(prefix) :])
                    inner_inputs[inner_key] = state.values[key]

        # Use delegated runner if configured, otherwise inherit parent
        runner = node.runner_override or self.runner

        if map_config:
            _, mode, error_handling = map_config
            # Use original param names for map_over (inner graph expects these)
            original_params = node._original_map_params()
            results = runner.map(
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
            self.last_inner_logs = tuple(r.log for r in results if r.log is not None)
            return collect_as_lists(results, node, error_handling)

        result = runner.run(
            node.graph,
            inner_inputs,
            event_processors=event_processors,
            workflow_id=child_workflow_id,
            _parent_span_id=parent_span_id,
            _parent_run_id=workflow_id,
        )
        self.last_inner_logs = (result.log,) if result.log is not None else ()
        return node.map_outputs_from_original(result.values)
