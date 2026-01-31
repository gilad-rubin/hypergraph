"""Asynchronous runner for graph execution."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Literal

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
from hypergraph.runners._shared.protocols import AsyncNodeExecutor
from hypergraph.runners._shared.types import (
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
from hypergraph.runners.async_.executors import (
    AsyncFunctionNodeExecutor,
    AsyncGraphNodeExecutor,
    AsyncIfElseNodeExecutor,
    AsyncRouteNodeExecutor,
)
from hypergraph.runners.async_.superstep import (
    get_concurrency_limiter,
    reset_concurrency_limiter,
    run_superstep_async,
    set_concurrency_limiter,
)
from hypergraph.runners.base import BaseRunner

if TYPE_CHECKING:
    from hypergraph.events.dispatcher import EventDispatcher
    from hypergraph.events.processor import EventProcessor
    from hypergraph.graph import Graph

# Default max iterations for cyclic graphs
DEFAULT_MAX_ITERATIONS = 1000


class AsyncRunner(BaseRunner):
    """Asynchronous runner for graph execution.

    Executes graphs asynchronously with support for:
    - Async nodes (coroutines, async generators)
    - Concurrent execution within supersteps
    - Concurrency limiting via max_concurrency

    Features:
    - Supports cyclic graphs with max_iterations limit
    - Concurrent execution of independent nodes
    - Configurable concurrency limit
    - Supports both sync and async nodes

    Example:
        >>> from hypergraph import Graph, node, AsyncRunner
        >>> @node(output_name="doubled")
        ... async def double(x: int) -> int:
        ...     return x * 2
        >>> graph = Graph([double])
        >>> runner = AsyncRunner()
        >>> result = await runner.run(graph, {"x": 5})
        >>> result["doubled"]
        10
    """

    def __init__(self):
        """Initialize AsyncRunner with its node executors."""
        self._executors: dict[type[HyperNode], AsyncNodeExecutor] = {
            FunctionNode: AsyncFunctionNodeExecutor(),
            GraphNode: AsyncGraphNodeExecutor(self),
            IfElseNode: AsyncIfElseNodeExecutor(),
            RouteNode: AsyncRouteNodeExecutor(),
        }

    @property
    def capabilities(self) -> RunnerCapabilities:
        """AsyncRunner capabilities."""
        return RunnerCapabilities(
            supports_cycles=True,
            supports_async_nodes=True,
            supports_streaming=False,  # Phase 2
            returns_coroutine=True,
        )

    @property
    def supported_node_types(self) -> set[type[HyperNode]]:
        """Node types this runner can execute."""
        return set(self._executors.keys())

    async def run(
        self,
        graph: "Graph",
        values: dict[str, Any],
        *,
        select: list[str] | None = None,
        max_iterations: int | None = None,
        max_concurrency: int | None = None,
        event_processors: list[EventProcessor] | None = None,
        _parent_span_id: str | None = None,
    ) -> RunResult:
        """Execute a graph asynchronously.

        Args:
            graph: The graph to execute
            values: Input values for graph parameters
            select: Optional list of output names to include in result
            max_iterations: Max supersteps for cyclic graphs (default: 1000)
            max_concurrency: Max parallel node executions (None = unlimited)
            event_processors: Optional list of event processors for execution events
            _parent_span_id: Internal. Span ID of parent scope for nested runs.

        Returns:
            RunResult containing output values and execution status

        Raises:
            MissingInputError: If required inputs not provided
            InfiniteLoopError: If max_iterations exceeded
        """
        # Validate
        validate_runner_compatibility(graph, self.capabilities)
        validate_node_types(graph, self.supported_node_types)
        validate_inputs(graph, values)

        max_iter = max_iterations or DEFAULT_MAX_ITERATIONS

        # Set up event dispatcher
        dispatcher = _create_dispatcher(event_processors)
        run_id, run_span_id = await _emit_run_start(dispatcher, graph, _parent_span_id)
        start_time = time.time()

        try:
            state = await self._execute_graph(
                graph, values, max_iter, max_concurrency,
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
            await _emit_run_end(
                dispatcher, run_id, run_span_id, graph, start_time, _parent_span_id,
            )
            return result
        except Exception as e:
            await _emit_run_end(
                dispatcher, run_id, run_span_id, graph, start_time, _parent_span_id,
                error=e,
            )
            return RunResult(
                values={},
                status=RunStatus.FAILED,
                run_id=run_id,
                error=e,
            )
        finally:
            # Only shut down dispatcher if we own it (top-level call)
            if _parent_span_id is None and dispatcher.active:
                await dispatcher.shutdown_async()

    async def _execute_graph(
        self,
        graph: "Graph",
        values: dict[str, Any],
        max_iterations: int,
        max_concurrency: int | None,
        *,
        dispatcher: "EventDispatcher",
        run_id: str,
        run_span_id: str,
        event_processors: list[EventProcessor] | None = None,
    ) -> GraphState:
        """Execute graph until no more ready nodes or max_iterations reached."""
        state = initialize_state(graph, values)

        # Set up concurrency limiter only at top level (when none exists)
        # Nested graphs inherit the parent's semaphore via ContextVar
        existing_limiter = get_concurrency_limiter()
        if existing_limiter is None and max_concurrency is not None:
            semaphore = asyncio.Semaphore(max_concurrency)
            token = set_concurrency_limiter(semaphore)
        else:
            token = None

        try:
            for iteration in range(max_iterations):
                ready_nodes = get_ready_nodes(graph, state)

                if not ready_nodes:
                    break  # No more nodes to execute

                # Execute all ready nodes concurrently
                # Concurrency controlled by shared semaphore in ContextVar
                state = await run_superstep_async(
                    graph,
                    state,
                    ready_nodes,
                    values,
                    self._make_execute_node(event_processors),
                    max_concurrency,
                    dispatcher=dispatcher,
                    run_id=run_id,
                    run_span_id=run_span_id,
                )

            else:
                # Loop completed without break = hit max_iterations
                if get_ready_nodes(graph, state):
                    raise InfiniteLoopError(max_iterations)

        finally:
            # Reset concurrency limiter only if we set it
            if token is not None:
                reset_concurrency_limiter(token)

        return state

    def _make_execute_node(
        self, event_processors: list[EventProcessor] | None
    ) -> AsyncNodeExecutor:
        """Create an async node executor closure that carries event context.

        The superstep calls execute_node(node, state, inputs). For GraphNode
        executors, we need to pass event_processors and parent_span_id so
        nested graphs propagate events. This closure captures that context.

        The superstep sets ``execute_node.current_node_span_id`` before each
        call so that nested graph runs know their parent span.
        """
        current_span_id: list[str | None] = [None]

        async def execute_node(
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
            if isinstance(executor, AsyncGraphNodeExecutor):
                return await executor(
                    node, state, inputs,
                    event_processors=event_processors,
                    parent_span_id=current_span_id[0],
                )

            return await executor(node, state, inputs)

        # Expose mutable span_id holder so superstep can set it per-node
        execute_node.current_span_id = current_span_id  # type: ignore[attr-defined]
        return execute_node

    async def map(
        self,
        graph: "Graph",
        values: dict[str, Any],
        *,
        map_over: str | list[str],
        map_mode: Literal["zip", "product"] = "zip",
        select: list[str] | None = None,
        max_concurrency: int | None = None,
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
            max_concurrency: Max concurrent operations (shared across all items)
            event_processors: Optional list of event processors for execution events
            _parent_span_id: Internal. Span ID of parent scope for nested map runs.

        Returns:
            List of RunResult, one per iteration
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
        map_run_id, map_span_id = await _emit_run_start(
            dispatcher, graph, _parent_span_id,
            is_map=True, map_size=len(input_variations),
        )
        start_time = time.time()

        # Set up shared concurrency limiter at map level (if not already set)
        # All run() calls and their nested operations share this semaphore
        existing_limiter = get_concurrency_limiter()
        if existing_limiter is None and max_concurrency is not None:
            semaphore = asyncio.Semaphore(max_concurrency)
            token = set_concurrency_limiter(semaphore)
        else:
            token = None

        try:
            if max_concurrency is None:
                # Execute all variations concurrently
                tasks = [
                    self.run(
                        graph, v, select=select, max_concurrency=max_concurrency,
                        event_processors=event_processors,
                        _parent_span_id=map_span_id,
                    )
                    for v in input_variations
                ]
                results = list(await asyncio.gather(*tasks))
            else:
                # Worker pool: fixed number of workers pull from a queue
                results_list: list[RunResult] = []
                queue: asyncio.Queue[tuple[int, dict[str, Any]]] = asyncio.Queue()
                for idx, v in enumerate(input_variations):
                    queue.put_nowait((idx, v))

                order: list[int] = []

                async def _worker() -> None:
                    while True:
                        try:
                            idx, v = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        result = await self.run(
                            graph, v, select=select, max_concurrency=max_concurrency,
                            event_processors=event_processors,
                            _parent_span_id=map_span_id,
                        )
                        results_list.append(result)
                        order.append(idx)

                num_workers = min(max_concurrency, len(input_variations))
                workers = [asyncio.create_task(_worker()) for _ in range(num_workers)]
                await asyncio.gather(*workers)
                # Restore original input order
                results = [r for _, r in sorted(zip(order, results_list))]

            await _emit_run_end(
                dispatcher, map_run_id, map_span_id, graph, start_time, _parent_span_id,
            )
            return results
        except Exception as e:
            await _emit_run_end(
                dispatcher, map_run_id, map_span_id, graph, start_time, _parent_span_id,
                error=e,
            )
            raise
        finally:
            if token is not None:
                reset_concurrency_limiter(token)
            if _parent_span_id is None and dispatcher.active:
                await dispatcher.shutdown_async()


# ------------------------------------------------------------------
# Event helpers (module-level to keep the class focused)
# ------------------------------------------------------------------


def _create_dispatcher(
    processors: list[EventProcessor] | None,
) -> "EventDispatcher":
    """Create an EventDispatcher from processor list."""
    from hypergraph.events.dispatcher import EventDispatcher

    return EventDispatcher(processors)


async def _emit_run_start(
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

    await dispatcher.emit_async(RunStartEvent(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        graph_name=graph.name,
        is_map=is_map,
        map_size=map_size,
    ))
    return run_id, span_id


async def _emit_run_end(
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
    await dispatcher.emit_async(RunEndEvent(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        graph_name=graph.name,
        status="failed" if error else "completed",
        error=str(error) if error else None,
        duration_ms=duration_ms,
    ))
