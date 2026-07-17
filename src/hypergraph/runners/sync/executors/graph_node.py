"""Sync executor for GraphNode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.checkpointers.types import WorkflowStatus
from hypergraph.runners._shared._inspect import current_inspection
from hypergraph.runners._shared.outputs import collect_as_lists
from hypergraph.runners._shared.state_restore import (
    graphnode_child_workflow_id,
    restore_completed_child_outputs,
)

if TYPE_CHECKING:
    from hypergraph.nodes.graph_node import GraphNode
    from hypergraph.runners._shared.state import ExecutionContext, GraphState
    from hypergraph.runners.sync.runner import SyncRunner


class SyncGraphNodeExecutor:
    """Executes GraphNode by delegating to runner.

    Handles:
    - Simple nested graph execution
    - Map-over execution (delegates to runner.map())
    """

    def __init__(self, runner: SyncRunner):
        """Initialize with reference to parent runner."""
        self.runner = runner

    def __call__(
        self,
        node: GraphNode,
        state: GraphState,
        inputs: dict[str, Any],
        ctx: ExecutionContext,
    ) -> dict[str, Any]:
        """Execute a GraphNode by running its inner graph.

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
        inner_inputs = node.map_inputs_to_params(inputs)
        child_workflow_id = graphnode_child_workflow_id(ctx.workflow_id, node.name, state)
        map_config = node.map_config

        # Resolve the effective runner before any persistence reads: a
        # delegated runner (runner_override) owns the child workflow's
        # persistence boundary, while the parent's own receipts stay in the
        # parent runner's checkpointer.
        runner = node.runner_override or self.runner
        parent_cp = self.runner._get_sync_checkpointer(ctx.workflow_id)
        child_cp = runner._get_sync_checkpointer(child_workflow_id) if hasattr(runner, "_get_sync_checkpointer") else None

        # Route interrupt resume values into the inner graph. The parent sees
        # the GraphNode's resolved output address ("decision", "review.verdict",
        # etc.); the child run resumes with the inner graph's local output name.
        # When resuming a persisted child workflow, do not re-send normal child
        # inputs like "draft" — those are already captured by the child
        # checkpoint and would be treated as illegal overrides.
        # Only inject on first execution: on cycle loop-back the node has already
        # executed so the inner interrupt should fire again, not skip.
        if node.name not in state.node_executions:
            prefix = f"{node.name}."
            # Skip values that match this node's public inputs; those are
            # user-provided GraphNode inputs already routed via inner_inputs.
            node_input_set = set(node.inputs)
            resume_values = {}
            if not node.namespaced:
                resume_values.update(
                    {
                        node.resolve_original_output_name(suffix): value
                        for key, value in state.values.items()
                        if key.startswith(prefix)
                        for suffix in [key[len(prefix) :]]
                        if suffix not in node_input_set
                    }
                )
            resume_values.update(
                {
                    node.resolve_original_output_name(key): value
                    for key, value in state.values.items()
                    if key in node.outputs and key not in node_input_set
                }
            )
            child_fork_from: str | None = None
            child_retry_from: str | None = None

            if map_config is None and child_workflow_id is not None and parent_cp is not None and child_cp is not None:
                existing_child_run = child_cp.get_run(child_workflow_id)
                if existing_child_run is not None:
                    if existing_child_run.status is WorkflowStatus.COMPLETED:
                        # Crash-window recovery: the child committed COMPLETED but
                        # this parent step was never persisted. Restore the child's
                        # outputs instead of re-invoking the terminal child (which
                        # would raise WorkflowAlreadyCompletedError). Terminal
                        # FAILED children fall through to the resume path below so
                        # their failure resurfaces — never restored-as-success.
                        return restore_completed_child_outputs(node, child_cp.state(child_workflow_id))
                    inner_inputs = {}
                elif resume_values:
                    current_parent_run = parent_cp.get_run(ctx.workflow_id) if ctx.workflow_id else None
                    source_parent_run_id = None
                    if current_parent_run is not None:
                        source_parent_run_id = current_parent_run.retry_of or current_parent_run.forked_from
                    if source_parent_run_id is not None:
                        source_child_run_id = graphnode_child_workflow_id(source_parent_run_id, node.name, state)
                        source_child_run = child_cp.get_run(source_child_run_id) if source_child_run_id is not None else None
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
                "_item_index": ctx.item_index,
            }
            if node.runner_override is None:
                inspection_context = current_inspection()
                if inspection_context is not None:
                    inspection_session, inspection_path = inspection_context
                    map_kwargs["_inspection_session"] = inspection_session
                    map_kwargs["_inspection_path"] = (*inspection_path, node.name)
            if getattr(node, "_complete_on_stop", False):
                map_kwargs["_complete_on_stop"] = True
            results = runner.map(node.graph, inner_inputs, **map_kwargs)
            if ctx.on_inner_log:
                for result in results:
                    if result.log is not None:
                        ctx.on_inner_log(result.log)
            return collect_as_lists(results, node, error_handling)

        run_kwargs: dict[str, Any] = {
            "event_processors": ctx.event_processors,
            "show_progress": ctx.show_progress,
            "workflow_id": child_workflow_id,
            "_parent_span_id": ctx.parent_span_id,
            "_parent_run_id": ctx.workflow_id,
            "_item_index": ctx.item_index,
        }
        if node.runner_override is None:
            inspection_context = current_inspection()
            if inspection_context is not None:
                inspection_session, inspection_path = inspection_context
                run_kwargs["_inspection_session"] = inspection_session
                run_kwargs["_inspection_path"] = (*inspection_path, node.name)
        if runner.capabilities.supports_checkpointing:
            run_kwargs["fork_from"] = child_fork_from
            run_kwargs["retry_from"] = child_retry_from
        if getattr(node, "_complete_on_stop", False):
            run_kwargs["_complete_on_stop"] = True

        result = runner.run(node.graph, inner_inputs, **run_kwargs)
        if ctx.on_inner_log and result.log is not None:
            ctx.on_inner_log(result.log)
        return node.map_outputs_from_original(result.values)
