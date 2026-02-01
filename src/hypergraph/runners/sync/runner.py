"""Synchronous runner for graph execution."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable, Literal

from hypergraph.exceptions import InfiniteLoopError
from hypergraph.nodes.base import HyperNode
from hypergraph.nodes.function import FunctionNode
from hypergraph.nodes.gate import IfElseNode, RouteNode
from hypergraph.nodes.graph_node import GraphNode
from hypergraph.runners._shared.helpers import (
    filter_outputs,
    generate_map_inputs,
    get_ready_nodes,
    initialize_state,
)
from hypergraph.runners._shared.protocols import NodeExecutor
from hypergraph.runners._shared.types import (
    ErrorHandling,
    GraphState,
    RunnerCapabilities,
    RunResult,
    RunStatus,
    _generate_run_id,
)
from hypergraph.runners._shared.validation import (
    validate_inputs,
    validate_map_compatible,
    validate_node_types,
    validate_runner_compatibility,
)
from hypergraph.runners.base import BaseRunner
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


class SyncRunner(BaseRunner):
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
    def supported_node_types(self) -> set[type[HyperNode]]:
        """Node types this runner can execute."""
        return set(self._executors.keys())

    def run(
        self,
        graph: "Graph",
        values: dict[str, Any],
        *,
        select: list[str] | None = None,
        max_iterations: int | None = None,
        event_processors: list[EventProcessor] | None = None,
        _parent_span_id: str | None = None,
    ) -> RunResult:
        """Execute a graph synchronously.

        Args:
            graph: The graph to execute
            values: Input values for graph parameters
            select: Optional list of output names to include in result
            max_iterations: Max supersteps for cyclic graphs (default: 1000)
            event_processors: Optional list of event processors for execution events
            _parent_span_id: Internal. Span ID of parent scope for nested runs.

        Returns:
            RunResult containing output values and execution status

        Raises:
            MissingInputError: If required inputs not provided
            IncompatibleRunnerError: If graph has async nodes
            InfiniteLoopError: If max_iterations exceeded
        """
        # Validate
        validate_runner_compatibility(graph, self.capabilities)
        validate_node_types(graph, self.supported_node_types)
        validate_inputs(graph, values)

        max_iter = max_iterations or DEFAULT_MAX_ITERATIONS

        # Set up event dispatcher
        dispatcher = _create_dispatcher(event_processors)
        run_id, run_span_id = _emit_run_start(dispatcher, graph, _parent_span_id)
        start_time = time.time()

        try:
            state = self._execute_graph(
                graph, values, max_iter,
                dispatcher=dispatcher,
                run_id=run_id,
                run_span_id=run_span_id,
                event_processors=event_processors,
            )
            output_values = filter_outputs(state, graph, select)
            result = RunResult(
                values=output_values,
                status=RunStatus.COMPLETED,
                run_id=run_id,
            )
            _emit_run_end(dispatcher, run_id, run_span_id, graph, start_time, _parent_span_id)
            return result
        except Exception as e:
            _emit_run_end(
                dispatcher, run_id, run_span_id, graph, start_time, _parent_span_id,
                error=e,
            )
            partial_state = getattr(e, "_partial_state", None)
            partial_values = (
                filter_outputs(partial_state, graph, select)
                if partial_state is not None
                else {}
            )
            return RunResult(
                values=partial_values,
                status=RunStatus.FAILED,
                run_id=run_id,
                error=e,
            )
        finally:
            # Only shut down dispatcher if we own it (top-level call)
            if _parent_span_id is None and dispatcher.active:
                dispatcher.shutdown()

    def _execute_graph(
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

        On failure, attaches partial state to the exception as ``_partial_state``
        so the caller can extract values accumulated before the error.
        """
        state = initialize_state(graph, values)

        try:
            for iteration in range(max_iterations):
                ready_nodes = get_ready_nodes(graph, state)

                if not ready_nodes:
                    break  # No more nodes to execute

                # Execute all ready nodes
                state = run_superstep_sync(
                    graph, state, ready_nodes, values,
                    self._make_execute_node(event_processors),
                    cache=self._cache,
                    dispatcher=dispatcher,
                    run_id=run_id,
                    run_span_id=run_span_id,
                )

            else:
                # Loop completed without break = hit max_iterations
                if get_ready_nodes(graph, state):
                    raise InfiniteLoopError(max_iterations)
        except Exception as e:
            if not hasattr(e, "_partial_state"):
                e._partial_state = state  # type: ignore[attr-defined]
            raise

        return state

    def _make_execute_node(
        self, event_processors: list[EventProcessor] | None
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
            node_type = type(node)
            executor = self._executors.get(node_type)

            if executor is None:
                raise TypeError(
                    f"No executor registered for node type '{node_type.__name__}'"
                )

            # For GraphNodeExecutor, pass context as params (not mutable state)
            if isinstance(executor, SyncGraphNodeExecutor):
                return executor(
                    node, state, inputs,
                    event_processors=event_processors,
                    parent_span_id=current_span_id[0],
                )

            return executor(node, state, inputs)

        # Expose mutable span_id holder so superstep can set it per-node
        execute_node.current_span_id = current_span_id  # type: ignore[attr-defined]
        return execute_node

    def map(
        self,
        graph: "Graph",
        values: dict[str, Any],
        *,
        map_over: str | list[str],
        map_mode: Literal["zip", "product"] = "zip",
        select: list[str] | None = None,
        error_handling: ErrorHandling = "raise",
        event_processors: list[EventProcessor] | None = None,
        _parent_span_id: str | None = None,
    ) -> list[RunResult]:
        """Execute graph multiple times with different inputs.

        Args:
            graph: The graph to execute
            values: Input values (map_over params should be lists)
            map_over: Parameter name(s) to iterate over
            map_mode: "zip" for parallel iteration, "product" for cartesian
            select: Optional list of outputs to return
            error_handling: "raise" to stop on first failure, "continue" to
                collect all results including failures
            event_processors: Optional list of event processors for execution events
            _parent_span_id: Internal. Span ID of parent scope for nested map runs.

        Returns:
            List of RunResult, one per iteration

        Raises:
            Exception: The underlying error from the first failed item
                when ``error_handling="raise"``
        """
        # Validate
        validate_runner_compatibility(graph, self.capabilities)
        validate_node_types(graph, self.supported_node_types)
        validate_map_compatible(graph)

        # Normalize map_over to list
        map_over_list = [map_over] if isinstance(map_over, str) else list(map_over)

        # Generate input variations
        input_variations = list(generate_map_inputs(values, map_over_list, map_mode))

        if not input_variations:
            return []

        # Set up event dispatcher for map-level events
        dispatcher = _create_dispatcher(event_processors)
        map_run_id, map_span_id = _emit_run_start(
            dispatcher, graph, _parent_span_id,
            is_map=True, map_size=len(input_variations),
        )
        start_time = time.time()

        try:
            results = []
            for variation_inputs in input_variations:
                result = self.run(
                    graph, variation_inputs, select=select,
                    event_processors=event_processors,
                    _parent_span_id=map_span_id,
                )
                results.append(result)
                if error_handling == "raise" and result.status == RunStatus.FAILED:
                    raise result.error  # type: ignore[misc]

            _emit_run_end(dispatcher, map_run_id, map_span_id, graph, start_time, _parent_span_id)
            return results
        except Exception as e:
            _emit_run_end(
                dispatcher, map_run_id, map_span_id, graph, start_time, _parent_span_id,
                error=e,
            )
            raise
        finally:
            if _parent_span_id is None and dispatcher.active:
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
