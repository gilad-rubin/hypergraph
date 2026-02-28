"""Asynchronous runner for graph execution."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from hypergraph.exceptions import ExecutionError, InfiniteLoopError
from hypergraph.nodes.base import HyperNode
from hypergraph.nodes.function import FunctionNode
from hypergraph.nodes.gate import IfElseNode, RouteNode
from hypergraph.nodes.graph_node import GraphNode
from hypergraph.nodes.interrupt import InterruptNode
from hypergraph.runners._shared.helpers import compute_active_node_set, get_ready_nodes, initialize_state
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
    from hypergraph.checkpointers.base import Checkpointer
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

    def __init__(
        self,
        cache: CacheBackend | None = None,
        checkpointer: Checkpointer | None = None,
    ):
        """Initialize AsyncRunner with its node executors.

        Args:
            cache: Optional cache backend for node result caching.
                Nodes opt in with ``cache=True``.
            checkpointer: Optional checkpointer for workflow persistence.
                Enables save/resume, crash recovery, and cross-process queries.
                Pass a workflow_id to run() to activate persistence.
        """
        self._cache = cache
        self._checkpointer_instance = checkpointer
        self._executors: dict[type[HyperNode], AsyncNodeExecutor] = {
            FunctionNode: AsyncFunctionNodeExecutor(),
            GraphNode: AsyncGraphNodeExecutor(self),
            IfElseNode: AsyncIfElseNodeExecutor(),
            RouteNode: AsyncRouteNodeExecutor(),
            InterruptNode: AsyncInterruptNodeExecutor(),
        }

    @property
    def _checkpointer(self) -> Checkpointer | None:
        """Checkpointer for workflow persistence."""
        return self._checkpointer_instance

    @property
    def capabilities(self) -> RunnerCapabilities:
        """AsyncRunner capabilities."""
        return RunnerCapabilities(
            supports_cycles=True,
            supports_async_nodes=True,
            supports_streaming=False,  # Phase 2
            returns_coroutine=True,
            supports_interrupts=True,
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

    async def _execute_graph_impl_async(
        self,
        graph: Graph,
        values: dict[str, Any],
        max_iterations: int,
        max_concurrency: int | None,
        *,
        dispatcher: EventDispatcher,
        run_id: str,
        run_span_id: str,
        event_processors: list[EventProcessor] | None = None,
        workflow_id: str | None = None,
        step_buffer: list[Any] | None = None,
    ) -> GraphState:
        """Execute graph until no more ready nodes or max_iterations reached.

        On failure, raises ExecutionError wrapping the cause and partial state.
        """
        state = initialize_state(graph, values)
        active_nodes = compute_active_node_set(graph)

        # Set up concurrency limiter only at top level (when none exists)
        # Nested graphs inherit the parent's semaphore via ContextVar
        existing_limiter = get_concurrency_limiter()
        if existing_limiter is None and max_concurrency is not None:
            semaphore = asyncio.Semaphore(max_concurrency)
            token = set_concurrency_limiter(semaphore)
        else:
            token = None

        # Checkpointer setup — deterministic node ordering for index assignment
        checkpointer = self._checkpointer_instance
        has_checkpointer = checkpointer is not None and workflow_id is not None
        step_counter = 0
        node_order = {name: i for i, name in enumerate(graph._nodes)} if has_checkpointer else {}
        save_tasks: list[asyncio.Task[None]] = []

        try:
            for superstep_idx in range(max_iterations):
                ready_nodes = get_ready_nodes(graph, state, active_nodes=active_nodes)

                if not ready_nodes:
                    break  # No more nodes to execute

                if dispatcher.active:
                    from hypergraph.events.types import SuperstepStartEvent, _generate_span_id

                    await dispatcher.emit_async(
                        SuperstepStartEvent(
                            run_id=run_id,
                            span_id=_generate_span_id(),
                            parent_span_id=run_span_id,
                            graph_name=graph.name,
                            superstep=superstep_idx,
                        )
                    )

                # Track ready nodes and their prior input_versions for
                # detecting re-executions (cycles) and failures
                ready_node_names = [n.name for n in ready_nodes]
                prev_input_versions = {
                    name: dict(state.node_executions[name].input_versions) for name in ready_node_names if name in state.node_executions
                }

                superstep_error: BaseException | None = None
                try:
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
                except ExecutionError as e:
                    superstep_error = e
                    state = e.partial_state  # type: ignore[assignment]
                except Exception as e:
                    superstep_error = ExecutionError(e, state)

                # Save step records for executed nodes (even on failure)
                if has_checkpointer:
                    step_counter = await self._save_superstep_records(
                        checkpointer,
                        workflow_id,
                        superstep_idx,
                        state,
                        ready_node_names,
                        prev_input_versions,
                        node_order,
                        step_counter,
                        step_buffer,
                        save_tasks,
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

        except PauseExecution as pause:
            pause._partial_state = state  # type: ignore[attr-defined]
            raise
        finally:
            # Await any background save tasks before returning
            if save_tasks:
                results = await asyncio.gather(*save_tasks, return_exceptions=True)
                failures = [r for r in results if isinstance(r, BaseException)]
                if failures:
                    import logging

                    logger = logging.getLogger("hypergraph.checkpointers")
                    if len(failures) == len(results):
                        logger.error(
                            "All %d async step saves failed for workflow %s — no steps persisted: %s",
                            len(failures),
                            workflow_id,
                            failures[0],
                        )
                    else:
                        for f in failures:
                            logger.warning("Async step save failed: %s", f)
            # Reset concurrency limiter only if we set it
            if token is not None:
                reset_concurrency_limiter(token)

        return state

    async def _save_superstep_records(
        self,
        checkpointer: Checkpointer,
        workflow_id: str,
        superstep_idx: int,
        state: GraphState,
        ready_node_names: list[str],
        prev_input_versions: dict[str, dict[str, int]],
        node_order: dict[str, int],
        step_counter: int,
        step_buffer: list[Any] | None,
        save_tasks: list[asyncio.Task[None]],
        graph: Graph,
        superstep_error: BaseException | None = None,
    ) -> int:
        """Build StepRecords for nodes scheduled in this superstep.

        Uses ready_node_names (not set-diff on node_executions keys) so that
        cyclic re-executions are captured. Distinguishes fresh vs stale entries
        via input_versions comparison to correctly mark failed nodes.
        """
        from hypergraph.checkpointers.types import StepRecord, StepStatus, _utcnow

        sorted_names = sorted(ready_node_names, key=lambda name: node_order.get(name, 0))

        for name in sorted_names:
            execution = state.node_executions.get(name)
            now = _utcnow()
            node_type = type(graph._nodes[name]).__name__ if graph and name in graph._nodes else None

            if execution is not None:
                # Check if this is a fresh execution or a stale copy from a prior superstep.
                # A cyclic node is "ready" because ≥1 input version changed, so fresh
                # re-executions always have different input_versions than the stale copy.
                is_fresh = name not in prev_input_versions or execution.input_versions != prev_input_versions[name]
                if is_fresh:
                    record = StepRecord(
                        run_id=workflow_id,
                        superstep=superstep_idx,
                        node_name=name,
                        index=step_counter,
                        status=StepStatus.COMPLETED,
                        input_versions=execution.input_versions,
                        values=execution.outputs,
                        duration_ms=execution.duration_ms,
                        cached=execution.cached,
                        decision=_normalize_decision(state.routing_decisions.get(name)),
                        node_type=node_type,
                        created_at=now,
                        completed_at=now,
                    )
                elif superstep_error is not None:
                    # Stale entry — node was scheduled but failed during re-execution
                    record = StepRecord(
                        run_id=workflow_id,
                        superstep=superstep_idx,
                        node_name=name,
                        index=step_counter,
                        status=StepStatus.FAILED,
                        input_versions=execution.input_versions,
                        error=_extract_error_message(superstep_error),
                        node_type=node_type,
                        created_at=now,
                    )
                else:
                    continue
            elif superstep_error is not None:
                # No prior execution — node failed on first attempt
                record = StepRecord(
                    run_id=workflow_id,
                    superstep=superstep_idx,
                    node_name=name,
                    index=step_counter,
                    status=StepStatus.FAILED,
                    input_versions={},
                    error=_extract_error_message(superstep_error),
                    node_type=node_type,
                    created_at=now,
                )
            else:
                continue

            step_counter += 1
            durability = checkpointer.policy.durability
            if durability == "sync":
                await checkpointer.save_step(record)
            elif durability == "async":
                save_tasks.append(asyncio.create_task(checkpointer.save_step(record)))
            elif step_buffer is not None:
                # "exit" mode — buffer for flushing after run completes
                step_buffer.append(record)

        return step_counter

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
        last_inner_logs: list[tuple] = [()]

        async def execute_node(
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
            if isinstance(executor, AsyncGraphNodeExecutor):
                result = await executor(
                    node,
                    state,
                    inputs,
                    event_processors=event_processors,
                    parent_span_id=current_span_id[0],
                )
                last_inner_logs[0] = executor.last_inner_logs
                return result

            last_inner_logs[0] = ()
            return await executor(node, state, inputs)

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

    async def _emit_run_start_async(
        self,
        dispatcher: EventDispatcher,
        graph: Graph,
        parent_span_id: str | None,
        *,
        is_map: bool = False,
        map_size: int | None = None,
    ) -> tuple[str, str]:
        """Emit run-start event via async helper."""
        return await _emit_run_start(
            dispatcher,
            graph,
            parent_span_id,
            is_map=is_map,
            map_size=map_size,
        )

    async def _emit_run_end_async(
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
        """Emit run-end event via async helper."""
        await _emit_run_end(
            dispatcher,
            run_id,
            span_id,
            graph,
            start_time,
            parent_span_id,
            error=error,
        )

    async def _shutdown_dispatcher_async(self, dispatcher: EventDispatcher) -> None:
        """Shut down dispatcher for top-level async runs."""
        await dispatcher.shutdown_async()

    def _get_concurrency_limiter(self) -> Any:
        """Return currently active shared concurrency limiter, if any."""
        return get_concurrency_limiter()

    def _set_concurrency_limiter(self, max_concurrency: int) -> Any:
        """Create and register a shared semaphore for nested async execution."""
        semaphore = asyncio.Semaphore(max_concurrency)
        return set_concurrency_limiter(semaphore)

    def _reset_concurrency_limiter(self, token: Any) -> None:
        """Reset shared concurrency limiter using context token."""
        reset_concurrency_limiter(token)


# ------------------------------------------------------------------
# Helpers (module-level to keep the class focused)
# ------------------------------------------------------------------


def _extract_error_message(error: BaseException) -> str:
    """Extract a human-readable error message from a (possibly wrapped) exception."""
    cause = error.__cause__ if error.__cause__ is not None else error
    return str(cause)


def _normalize_decision(decision: Any) -> str | list[str] | None:
    """Convert routing decision to a JSON-serializable form.

    Gate nodes store the END sentinel (a class) as a decision value.
    This converts it to the string "END" for persistence.
    """
    if decision is None:
        return None
    from hypergraph.nodes.gate import END as _END

    if decision is _END:
        return "END"
    if isinstance(decision, list):
        return [("END" if d is _END else d) for d in decision]
    return decision


# ------------------------------------------------------------------
# Event helpers
# ------------------------------------------------------------------


def _create_dispatcher(
    processors: list[EventProcessor] | None,
) -> EventDispatcher:
    """Create an EventDispatcher from processor list."""
    from hypergraph.events.dispatcher import EventDispatcher

    return EventDispatcher(processors)


async def _emit_run_start(
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

    await dispatcher.emit_async(
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


async def _emit_run_end(
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
    await dispatcher.emit_async(
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
