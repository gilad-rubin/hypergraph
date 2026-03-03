"""Synchronous runner for graph execution."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hypergraph.exceptions import ExecutionError, InfiniteLoopError
from hypergraph.nodes.base import HyperNode
from hypergraph.nodes.function import FunctionNode
from hypergraph.nodes.gate import IfElseNode, RouteNode
from hypergraph.nodes.graph_node import GraphNode
from hypergraph.runners._shared.helpers import compute_active_node_set, get_ready_nodes, initialize_state
from hypergraph.runners._shared.protocols import NodeExecutor
from hypergraph.runners._shared.template_sync import SyncRunnerTemplate
from hypergraph.runners._shared.types import GraphState, RunnerCapabilities, _generate_run_id
from hypergraph.runners.sync.executors import (
    SyncFunctionNodeExecutor,
    SyncGraphNodeExecutor,
    SyncIfElseNodeExecutor,
    SyncRouteNodeExecutor,
)
from hypergraph.runners.sync.superstep import run_superstep_sync

if TYPE_CHECKING:
    from hypergraph.cache import CacheBackend
    from hypergraph.checkpointers.base import Checkpointer
    from hypergraph.events.dispatcher import EventDispatcher
    from hypergraph.events.processor import EventProcessor
    from hypergraph.graph import Graph

# Default max iterations for cyclic graphs
DEFAULT_MAX_ITERATIONS = 1000


class SyncRunner(SyncRunnerTemplate):
    """Synchronous runner for graph execution.

    Executes graphs synchronously without async support.
    Nodes are executed sequentially within each superstep.

    Features:
    - Supports cyclic graphs with max_iterations limit
    - Sequential execution (no concurrency)
    - Does not support async nodes (use AsyncRunner instead)

    Example:
        >>> from hypergraph import Graph, node, SyncRunner
        >>> @node(output_name="doubled")
        ... def double(x: int) -> int:
        ...     return x * 2
        >>> graph = Graph([double])
        >>> runner = SyncRunner()
        >>> result = runner.run(graph, {"x": 5})
        >>> result["doubled"]
        10
    """

    def __init__(
        self,
        cache: CacheBackend | None = None,
        checkpointer: Checkpointer | None = None,
    ):
        """Initialize SyncRunner with its node executors.

        Args:
            cache: Optional cache backend for node result caching.
                Nodes opt in with ``cache=True``.
            checkpointer: Optional checkpointer for workflow persistence.
                Must implement SyncCheckpointerProtocol (e.g. SqliteCheckpointer).
                Pass a workflow_id to run() to activate persistence.
        """
        self._cache = cache
        self._checkpointer_instance = checkpointer
        self._executors: dict[type[HyperNode], NodeExecutor] = {
            FunctionNode: SyncFunctionNodeExecutor(),
            GraphNode: SyncGraphNodeExecutor(self),
            IfElseNode: SyncIfElseNodeExecutor(),
            RouteNode: SyncRouteNodeExecutor(),
        }

    @property
    def _checkpointer(self) -> Checkpointer | None:
        """Checkpointer for workflow persistence."""
        return self._checkpointer_instance

    @property
    def capabilities(self) -> RunnerCapabilities:
        """SyncRunner capabilities."""
        return RunnerCapabilities(
            supports_cycles=True,
            supports_async_nodes=False,
            supports_streaming=False,
            returns_coroutine=False,
            supports_checkpointing=self._checkpointer_instance is not None,
        )

    @property
    def default_max_iterations(self) -> int:
        """Default iteration cap for cyclic graphs."""
        return DEFAULT_MAX_ITERATIONS

    @property
    def supported_node_types(self) -> set[type[HyperNode]]:
        """Node types this runner can execute."""
        return set(self._executors.keys())

    def _execute_graph_impl(
        self,
        graph: Graph,
        values: dict[str, Any],
        max_iterations: int,
        *,
        dispatcher: EventDispatcher,
        run_id: str,
        run_span_id: str,
        event_processors: list[EventProcessor] | None = None,
        workflow_id: str | None = None,
        checkpoint: Any | None = None,
        step_buffer: list[Any] | None = None,
    ) -> GraphState:
        """Execute graph until no more ready nodes or max_iterations reached.

        On failure, raises ExecutionError wrapping the cause and partial state.
        """
        state = initialize_state(graph, values, checkpoint=checkpoint)
        active_nodes = compute_active_node_set(graph)

        # Checkpointer setup — template already validated the protocol,
        # so we just check if checkpointing is active for this run
        sync_cp = self._checkpointer_instance if (self._checkpointer_instance and workflow_id) else None
        step_counter = 0
        node_order = {name: i for i, name in enumerate(graph._nodes)} if sync_cp else {}

        for superstep_idx in range(max_iterations):
            ready_nodes = get_ready_nodes(graph, state, active_nodes=active_nodes)

            if not ready_nodes:
                break  # No more nodes to execute

            if dispatcher.active:
                from hypergraph.events.types import SuperstepStartEvent, _generate_span_id

                dispatcher.emit(
                    SuperstepStartEvent(
                        run_id=run_id,
                        span_id=_generate_span_id(),
                        parent_span_id=run_span_id,
                        graph_name=graph.name,
                        superstep=superstep_idx,
                    )
                )

            # Track ready nodes and prior input_versions for checkpoint helpers
            ready_node_names = [n.name for n in ready_nodes]
            prev_input_versions = {
                name: dict(state.node_executions[name].input_versions) for name in ready_node_names if name in state.node_executions
            }

            superstep_error: BaseException | None = None
            try:
                # Execute all ready nodes
                state = run_superstep_sync(
                    graph,
                    state,
                    ready_nodes,
                    values,
                    self._make_execute_node(event_processors, workflow_id=workflow_id),
                    cache=self._cache,
                    dispatcher=dispatcher,
                    run_id=run_id,
                    run_span_id=run_span_id,
                )
            except ExecutionError as e:
                superstep_error = e
                state = e.partial_state  # type: ignore[assignment]
            except Exception as e:
                superstep_error = ExecutionError(e, state)

            # Save step records for executed nodes (even on failure)
            if sync_cp:
                step_counter = _save_superstep_sync(
                    sync_cp,
                    workflow_id,
                    superstep_idx,
                    state,
                    ready_node_names,
                    prev_input_versions,
                    node_order,
                    step_counter,
                    step_buffer,
                    graph,
                    superstep_error,
                )

            if superstep_error is not None:
                raise superstep_error

        else:
            # Loop completed without break = hit max_iterations
            if get_ready_nodes(graph, state, active_nodes=active_nodes):
                raise ExecutionError(
                    InfiniteLoopError(max_iterations),
                    state,
                )

        return state

    def _make_execute_node(
        self,
        event_processors: list[EventProcessor] | None,
        workflow_id: str | None = None,
    ) -> Callable:
        """Create a node executor closure that carries event context.

        The superstep calls execute_node(node, state, inputs). For GraphNode
        executors, we need to pass event_processors, parent_span_id, and
        workflow_id so nested graphs propagate events and checkpointing.

        The superstep sets ``execute_node.current_span_id`` before each
        call so that nested graph runs know their parent span.
        """
        current_span_id: list[str | None] = [None]
        last_inner_logs: list[tuple] = [()]

        def execute_node(
            node: HyperNode,
            state: GraphState,
            inputs: dict[str, Any],
        ) -> dict[str, Any]:
            """Execute one node with optional nested-graph context."""
            node_type = type(node)
            executor = self._executors.get(node_type)

            if executor is None:
                raise TypeError(f"No executor registered for node type '{node_type.__name__}'")

            # For GraphNodeExecutor, pass context as params (not mutable state)
            if isinstance(executor, SyncGraphNodeExecutor):
                result = executor(
                    node,
                    state,
                    inputs,
                    event_processors=event_processors,
                    parent_span_id=current_span_id[0],
                    workflow_id=workflow_id,
                )
                last_inner_logs[0] = executor.last_inner_logs
                return result

            last_inner_logs[0] = ()
            return executor(node, state, inputs)

        # Expose mutable holders so superstep can read/set per-node
        execute_node.current_span_id = current_span_id  # type: ignore[attr-defined]
        execute_node.last_inner_logs = last_inner_logs  # type: ignore[attr-defined]
        return execute_node

    # Template hook implementations

    def _create_dispatcher(
        self,
        processors: list[EventProcessor] | None,
    ) -> EventDispatcher:
        """Create event dispatcher for this runner."""
        return _create_dispatcher(processors)

    def _emit_run_start_sync(
        self,
        dispatcher: EventDispatcher,
        graph: Graph,
        parent_span_id: str | None,
        *,
        is_map: bool = False,
        map_size: int | None = None,
    ) -> tuple[str, str]:
        """Emit run-start event via sync helper."""
        return _emit_run_start(
            dispatcher,
            graph,
            parent_span_id,
            is_map=is_map,
            map_size=map_size,
        )

    def _emit_run_end_sync(
        self,
        dispatcher: EventDispatcher,
        run_id: str,
        span_id: str,
        graph: Graph,
        start_time: float,
        parent_span_id: str | None,
        *,
        error: BaseException | None = None,
    ) -> None:
        """Emit run-end event via sync helper."""
        _emit_run_end(
            dispatcher,
            run_id,
            span_id,
            graph,
            start_time,
            parent_span_id,
            error=error,
        )

    def _shutdown_dispatcher_sync(self, dispatcher: EventDispatcher) -> None:
        """Shut down dispatcher for top-level sync runs."""
        dispatcher.shutdown()


# ------------------------------------------------------------------
# Checkpoint helpers (module-level to keep the class focused)
# ------------------------------------------------------------------


def _save_superstep_sync(
    sync_cp: Any,
    workflow_id: str,
    superstep_idx: int,
    state: GraphState,
    ready_node_names: list[str],
    prev_input_versions: dict[str, dict[str, int]],
    node_order: dict[str, int],
    step_counter: int,
    step_buffer: list[Any] | None,
    graph: Any,
    superstep_error: BaseException | None,
) -> int:
    """Build StepRecords and dispatch to sync durability mode."""
    from hypergraph.runners._shared.checkpoint_helpers import build_superstep_records

    records, step_counter = build_superstep_records(
        workflow_id=workflow_id,
        superstep_idx=superstep_idx,
        state=state,
        ready_node_names=ready_node_names,
        prev_input_versions=prev_input_versions,
        node_order=node_order,
        step_counter=step_counter,
        graph=graph,
        superstep_error=superstep_error,
    )

    # SyncRunner durability: "sync" and "async" both write immediately (no event loop).
    # "exit" buffers for flushing after run completes.
    durability = sync_cp.policy.durability
    for record in records:
        if durability == "exit" and step_buffer is not None:
            step_buffer.append(record)
        else:
            sync_cp.save_step_sync(record)

    return step_counter


# ------------------------------------------------------------------
# Event helpers (module-level to keep the class focused)
# ------------------------------------------------------------------


def _create_dispatcher(
    processors: list[EventProcessor] | None,
) -> EventDispatcher:
    """Create an EventDispatcher from processor list."""
    from hypergraph.events.dispatcher import EventDispatcher

    return EventDispatcher(processors)


def _emit_run_start(
    dispatcher: EventDispatcher,
    graph: Graph,
    parent_span_id: str | None,
    *,
    is_map: bool = False,
    map_size: int | None = None,
) -> tuple[str, str]:
    """Emit RunStartEvent and return (run_id, span_id)."""
    from hypergraph.events.types import _generate_span_id

    run_id = _generate_run_id()
    span_id = _generate_span_id()

    if not dispatcher.active:
        return run_id, span_id

    from hypergraph.events.types import RunStartEvent

    dispatcher.emit(
        RunStartEvent(
            run_id=run_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            graph_name=graph.name,
            is_map=is_map,
            map_size=map_size,
        )
    )
    return run_id, span_id


def _emit_run_end(
    dispatcher: EventDispatcher,
    run_id: str,
    span_id: str,
    graph: Graph,
    start_time: float,
    parent_span_id: str | None,
    *,
    error: BaseException | None = None,
) -> None:
    """Emit RunEndEvent."""
    if not dispatcher.active:
        return

    from hypergraph.events.types import RunEndEvent

    duration_ms = (time.time() - start_time) * 1000
    dispatcher.emit(
        RunEndEvent(
            run_id=run_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            graph_name=graph.name,
            status="failed" if error else "completed",
            error=str(error) if error else None,
            duration_ms=duration_ms,
        )
    )
