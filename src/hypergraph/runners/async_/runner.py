"""Asynchronous runner for graph execution."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from hypergraph.exceptions import InfiniteLoopError
from hypergraph.nodes.base import HyperNode
from hypergraph.nodes.function import FunctionNode
from hypergraph.nodes.gate import IfElseNode, RouteNode
from hypergraph.nodes.interrupt import InterruptNode
from hypergraph.nodes.graph_node import GraphNode
from hypergraph.runners._shared.helpers import get_ready_nodes, initialize_state
from hypergraph.runners._shared.protocols import AsyncNodeExecutor
from hypergraph.runners._shared.template_async import AsyncRunnerTemplate
from hypergraph.runners._shared.types import (
    GraphState,
    PauseExecution,
    RunnerCapabilities,
    _generate_run_id,
)
from hypergraph.runners.async_.executors import (
    AsyncFunctionNodeExecutor,
    AsyncGraphNodeExecutor,
    AsyncIfElseNodeExecutor,
    AsyncInterruptNodeExecutor,
    AsyncRouteNodeExecutor,
)
from hypergraph.runners.async_.superstep import (
    get_concurrency_limiter,
    reset_concurrency_limiter,
    run_superstep_async,
    set_concurrency_limiter,
)

if TYPE_CHECKING:
    from hypergraph.cache import CacheBackend
    from hypergraph.events.dispatcher import EventDispatcher
    from hypergraph.events.processor import EventProcessor
    from hypergraph.graph import Graph

# Default max iterations for cyclic graphs
DEFAULT_MAX_ITERATIONS = 1000


class AsyncRunner(AsyncRunnerTemplate):
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
    - Human-in-the-loop via InterruptNode (pause and resume)

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

    def __init__(self, cache: "CacheBackend | None" = None):
        """Initialize AsyncRunner with its node executors.

        Args:
            cache: Optional cache backend for node result caching.
                Nodes opt in with ``cache=True``.
        """
        self._cache = cache
        self._executors: dict[type[HyperNode], AsyncNodeExecutor] = {
            FunctionNode: AsyncFunctionNodeExecutor(),
            GraphNode: AsyncGraphNodeExecutor(self),
            IfElseNode: AsyncIfElseNodeExecutor(),
            RouteNode: AsyncRouteNodeExecutor(),
            InterruptNode: AsyncInterruptNodeExecutor(),
        }

    @property
    def capabilities(self) -> RunnerCapabilities:
        """AsyncRunner capabilities."""
        return RunnerCapabilities(
            supports_cycles=True,
            supports_async_nodes=True,
            supports_streaming=False,  # Phase 2
            returns_coroutine=True,
            supports_interrupts=True,
        )

    @property
    def default_max_iterations(self) -> int:
        """Default iteration cap for cyclic graphs."""
        return DEFAULT_MAX_ITERATIONS

    @property
    def supported_node_types(self) -> set[type[HyperNode]]:
        """Node types this runner can execute."""
        return set(self._executors.keys())

    async def _execute_graph_impl_async(
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
        """Execute graph until no more ready nodes or max_iterations reached.

        On failure, attaches partial state to the exception as ``_partial_state``
        so the caller can extract values accumulated before the error.
        """
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
            for _ in range(max_iterations):
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
                    cache=self._cache,
                    dispatcher=dispatcher,
                    run_id=run_id,
                    run_span_id=run_span_id,
                )

            else:
                # Loop completed without break = hit max_iterations
                if get_ready_nodes(graph, state):
                    raise InfiniteLoopError(max_iterations)

        except PauseExecution as pause:
            pause._partial_state = state  # type: ignore[attr-defined]
            raise
        except Exception as e:
            if not hasattr(e, "_partial_state"):
                e._partial_state = state  # type: ignore[attr-defined]
            raise
        finally:
            # Reset concurrency limiter only if we set it
            if token is not None:
                reset_concurrency_limiter(token)

        return state

    def _make_execute_node(
        self,
        event_processors: list[EventProcessor] | None,
    ) -> AsyncNodeExecutor:
        """Create an async node executor closure that carries event context.

        The superstep calls execute_node(node, state, inputs). For GraphNode
        executors, we need to pass event_processors and parent_span_id so
        nested graphs propagate events. This closure captures that context.

        The superstep sets ``execute_node.current_span_id`` before each
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
                    node,
                    state,
                    inputs,
                    event_processors=event_processors,
                    parent_span_id=current_span_id[0],
                )

            return await executor(node, state, inputs)

        # Expose mutable span_id holder so superstep can set it per-node
        execute_node.current_span_id = current_span_id  # type: ignore[attr-defined]
        return execute_node

    # Template hook implementations

    def _create_dispatcher(
        self,
        processors: list[EventProcessor] | None,
    ) -> "EventDispatcher":
        return _create_dispatcher(processors)

    async def _emit_run_start_async(
        self,
        dispatcher: "EventDispatcher",
        graph: "Graph",
        parent_span_id: str | None,
        *,
        is_map: bool = False,
        map_size: int | None = None,
    ) -> tuple[str, str]:
        return await _emit_run_start(
            dispatcher,
            graph,
            parent_span_id,
            is_map=is_map,
            map_size=map_size,
        )

    async def _emit_run_end_async(
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
        await _emit_run_end(
            dispatcher,
            run_id,
            span_id,
            graph,
            start_time,
            parent_span_id,
            error=error,
        )

    async def _shutdown_dispatcher_async(self, dispatcher: "EventDispatcher") -> None:
        await dispatcher.shutdown_async()

    def _get_concurrency_limiter(self) -> Any:
        return get_concurrency_limiter()

    def _set_concurrency_limiter(self, max_concurrency: int) -> Any:
        semaphore = asyncio.Semaphore(max_concurrency)
        return set_concurrency_limiter(semaphore)

    def _reset_concurrency_limiter(self, token: Any) -> None:
        reset_concurrency_limiter(token)


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
