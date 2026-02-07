"""Synchronous runner for graph execution."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable

from hypergraph.exceptions import ExecutionError, InfiniteLoopError
from hypergraph.nodes.base import HyperNode
from hypergraph.nodes.function import FunctionNode
from hypergraph.nodes.gate import IfElseNode, RouteNode
from hypergraph.nodes.graph_node import GraphNode
from hypergraph.runners._shared.helpers import get_ready_nodes, initialize_state
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

    def __init__(self, cache: "CacheBackend | None" = None):
        """Initialize SyncRunner with its node executors.

        Args:
            cache: Optional cache backend for node result caching.
                Nodes opt in with ``cache=True``.
        """
        self._cache = cache
        self._executors: dict[type[HyperNode], NodeExecutor] = {
            FunctionNode: SyncFunctionNodeExecutor(),
            GraphNode: SyncGraphNodeExecutor(self),
            IfElseNode: SyncIfElseNodeExecutor(),
            RouteNode: SyncRouteNodeExecutor(),
        }

    @property
    def capabilities(self) -> RunnerCapabilities:
        """SyncRunner capabilities."""
        return RunnerCapabilities(
            supports_cycles=True,
            supports_async_nodes=False,
            supports_streaming=False,
            returns_coroutine=False,
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
        graph: "Graph",
        values: dict[str, Any],
        max_iterations: int,
        *,
        dispatcher: "EventDispatcher",
        run_id: str,
        run_span_id: str,
        event_processors: list[EventProcessor] | None = None,
    ) -> GraphState:
        """Execute graph until no more ready nodes or max_iterations reached.

        On failure, raises ExecutionError wrapping the cause and partial state.
        """
        state = initialize_state(graph, values)

        for _ in range(max_iterations):
            ready_nodes = get_ready_nodes(graph, state)

            if not ready_nodes:
                break  # No more nodes to execute

            try:
                # Execute all ready nodes
                state = run_superstep_sync(
                    graph,
                    state,
                    ready_nodes,
                    values,
                    self._make_execute_node(event_processors),
                    cache=self._cache,
                    dispatcher=dispatcher,
                    run_id=run_id,
                    run_span_id=run_span_id,
                )
            except ExecutionError:
                raise
            except Exception as e:
                raise ExecutionError(e, state) from e

        else:
            # Loop completed without break = hit max_iterations
            if get_ready_nodes(graph, state):
                raise ExecutionError(
                    InfiniteLoopError(max_iterations),
                    state,
                )

        return state

    def _make_execute_node(
        self,
        event_processors: list[EventProcessor] | None,
    ) -> Callable:
        """Create a node executor closure that carries event context.

        The superstep calls execute_node(node, state, inputs). For GraphNode
        executors, we need to pass event_processors and parent_span_id so
        nested graphs propagate events. This closure captures that context.

        The superstep sets ``execute_node.current_span_id`` before each
        call so that nested graph runs know their parent span.
        """
        current_span_id: list[str | None] = [None]

        def execute_node(
            node: HyperNode,
            state: GraphState,
            inputs: dict[str, Any],
        ) -> dict[str, Any]:
            """Execute one node with optional nested-graph context."""
            node_type = type(node)
            executor = self._executors.get(node_type)

            if executor is None:
                raise TypeError(
                    f"No executor registered for node type '{node_type.__name__}'"
                )

            # For GraphNodeExecutor, pass context as params (not mutable state)
            if isinstance(executor, SyncGraphNodeExecutor):
                return executor(
                    node,
                    state,
                    inputs,
                    event_processors=event_processors,
                    parent_span_id=current_span_id[0],
                )

            return executor(node, state, inputs)

        # Expose mutable span_id holder so superstep can set it per-node
        execute_node.current_span_id = current_span_id  # type: ignore[attr-defined]
        return execute_node

    # Template hook implementations

    def _create_dispatcher(
        self,
        processors: list[EventProcessor] | None,
    ) -> "EventDispatcher":
        """Create event dispatcher for this runner."""
        return _create_dispatcher(processors)

    def _emit_run_start_sync(
        self,
        dispatcher: "EventDispatcher",
        graph: "Graph",
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
        dispatcher: "EventDispatcher",
        run_id: str,
        span_id: str,
        graph: "Graph",
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

    def _shutdown_dispatcher_sync(self, dispatcher: "EventDispatcher") -> None:
        """Shut down dispatcher for top-level sync runs."""
        dispatcher.shutdown()


# ------------------------------------------------------------------
# Event helpers (module-level to keep the class focused)
# ------------------------------------------------------------------


def _create_dispatcher(
    processors: list[EventProcessor] | None,
) -> "EventDispatcher":
    """Create an EventDispatcher from processor list."""
    from hypergraph.events.dispatcher import EventDispatcher

    return EventDispatcher(processors)


def _emit_run_start(
    dispatcher: "EventDispatcher",
    graph: "Graph",
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

    dispatcher.emit(RunStartEvent(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        graph_name=graph.name,
        is_map=is_map,
        map_size=map_size,
    ))
    return run_id, span_id


def _emit_run_end(
    dispatcher: "EventDispatcher",
    run_id: str,
    span_id: str,
    graph: "Graph",
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
    dispatcher.emit(RunEndEvent(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        graph_name=graph.name,
        status="failed" if error else "completed",
        error=str(error) if error else None,
        duration_ms=duration_ms,
    ))
