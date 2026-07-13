"""Asynchronous runner for graph execution."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

from hypergraph.exceptions import ExecutionError, InfiniteLoopError
from hypergraph.nodes.base import HyperNode
from hypergraph.nodes.function import FunctionNode
from hypergraph.nodes.gate import IfElseNode, RouteNode
from hypergraph.nodes.graph_node import GraphNode
from hypergraph.nodes.interrupt import InterruptNode
from hypergraph.runners._shared.event_metadata import (
    DEFAULT_RUN_CONTEXT,
    DEFAULT_RUN_LINEAGE,
    BatchSummary,
    RunContext,
    RunLineage,
)
from hypergraph.runners._shared.handles import AsyncHandle, _launch_async_execution
from hypergraph.runners._shared.outputs import SELECT_UNSET
from hypergraph.runners._shared.protocols import AsyncNodeExecutor
from hypergraph.runners._shared.results import MapResult, RunResult
from hypergraph.runners._shared.scheduling import (
    ExecutionFrontier,
    compute_execution_scope,
    plan_interrupt_batch,
)
from hypergraph.runners._shared.state import (
    ExecutionContext,
    GraphState,
    PauseExecution,
    RunnerCapabilities,
)
from hypergraph.runners._shared.state_restore import graphnode_child_workflow_id, initialize_state
from hypergraph.runners._shared.stop import _ActiveWorkflows, get_stop_signal
from hypergraph.runners._shared.template_async import AsyncRunnerTemplate
from hypergraph.runners._shared.validation import (
    reject_background_error_handling_option,
    reject_background_lineage_options,
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
    from hypergraph.checkpointers.types import Checkpoint
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
        show_progress: bool = False,
    ):
        """Initialize AsyncRunner with its node executors.

        Args:
            cache: Optional cache backend for node result caching.
                Nodes opt in with ``cache=True``.
            checkpointer: Optional checkpointer for workflow persistence.
                Enables save/resume, crash recovery, and cross-process queries.
                Pass a workflow_id to run() to activate persistence.
            show_progress: If True, automatically add a RichProgressProcessor
                to every run() and map() call. Can be overridden per-call.
        """
        self._cache = cache
        self._checkpointer_instance = checkpointer
        self._show_progress = show_progress
        self._active_workflows = _ActiveWorkflows()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._executors: dict[type[HyperNode], AsyncNodeExecutor] = {
            FunctionNode: AsyncFunctionNodeExecutor(),
            GraphNode: AsyncGraphNodeExecutor(self),
            IfElseNode: AsyncIfElseNodeExecutor(),
            RouteNode: AsyncRouteNodeExecutor(),
            InterruptNode: AsyncInterruptNodeExecutor(),
        }

    def stop(self, workflow_id: str, *, info: Any = None) -> None:
        """Request cooperative stop for an active run or map.

        No-op if the workflow_id is not currently running.
        Thread-safe: can be called from any thread or coroutine.

        Args:
            workflow_id: The active workflow execution to stop.
            info: Optional metadata attached to the stop signal.
                  Accessible via ``StopRequestedEvent.info``.
        """
        self._active_workflows.stop(workflow_id, info=info)

    def start_run(
        self,
        graph: Graph,
        values: dict[str, Any] | None = None,
        *,
        select: str | list[str] = SELECT_UNSET,
        on_missing: Literal["ignore", "warn", "error"] = "ignore",
        entrypoint: str | None = None,
        max_iterations: int | None = None,
        max_concurrency: int | None = None,
        event_processors: list[EventProcessor] | None = None,
        show_progress: bool | None = None,
        checkpoint: Checkpoint | None = None,
        workflow_id: str | None = None,
        **input_values: Any,
    ) -> AsyncHandle[RunResult]:
        """Start one graph execution in the background.

        Args:
            graph: The graph to execute.
            values: Optional graph inputs as a dictionary.
            select: Which outputs to return.
            on_missing: How to handle missing selected outputs.
            entrypoint: Optional explicit cycle entrypoint.
            max_iterations: Maximum iterations for cyclic graphs.
            max_concurrency: Maximum number of nodes executing concurrently.
            event_processors: Optional processors for execution events.
            show_progress: Override runner-level progress display.
            checkpoint: Optional checkpoint from which to resume.
            workflow_id: Optional workflow identifier.
            **input_values: Graph input shorthand.

        Returns:
            A process-local handle for the live execution.

        Raises:
            RuntimeError: If called without a running event loop.
        """
        reject_background_error_handling_option(
            input_values,
            start_method="AsyncRunner.start_run",
        )
        reject_background_lineage_options(
            input_values,
            start_method="AsyncRunner.start_run",
        )
        loop = asyncio.get_running_loop()
        reservation = self._active_workflows.reserve(workflow_id)
        return _launch_async_execution(
            loop,
            lambda: self.run(
                graph,
                values,
                select=select,
                on_missing=on_missing,
                entrypoint=entrypoint,
                max_iterations=max_iterations,
                max_concurrency=max_concurrency,
                error_handling="continue",
                event_processors=event_processors,
                show_progress=show_progress,
                checkpoint=checkpoint,
                workflow_id=workflow_id,
                _reservation=reservation,
                **input_values,
            ),
            reservation,
            self._background_tasks,
        )

    def start_map(
        self,
        graph: Graph,
        values: dict[str, Any] | None = None,
        *,
        map_over: str | list[str],
        map_mode: Literal["zip", "product"] = "zip",
        clone: bool | list[str] = False,
        select: str | list[str] = SELECT_UNSET,
        on_missing: Literal["ignore", "warn", "error"] = "ignore",
        entrypoint: str | None = None,
        max_concurrency: int | None = None,
        event_processors: list[EventProcessor] | None = None,
        show_progress: bool | None = None,
        workflow_id: str | None = None,
        **input_values: Any,
    ) -> AsyncHandle[MapResult]:
        """Start a settled map execution in the background."""
        reject_background_error_handling_option(
            input_values,
            start_method="AsyncRunner.start_map",
        )
        loop = asyncio.get_running_loop()
        reservation = self._active_workflows.reserve(workflow_id)
        return _launch_async_execution(
            loop,
            lambda: self.map(
                graph,
                values,
                map_over=map_over,
                map_mode=map_mode,
                clone=clone,
                select=select,
                on_missing=on_missing,
                entrypoint=entrypoint,
                max_concurrency=max_concurrency,
                error_handling="continue",
                event_processors=event_processors,
                show_progress=show_progress,
                workflow_id=workflow_id,
                _reservation=reservation,
                **input_values,
            ),
            reservation,
            self._background_tasks,
        )

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
            supports_streaming=True,
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
        checkpoint: Checkpoint | None = None,
        step_buffer: list[Any] | None = None,
        checkpoint_save_errors: list[str] | None = None,
        _complete_on_stop: bool = False,
        item_index: int | None = None,
    ) -> GraphState:
        """Execute graph until no more ready nodes or max_iterations reached.

        On failure, raises ExecutionError wrapping the cause and partial state.
        Background step-save failures (durability "async") are appended to
        ``checkpoint_save_errors`` as string reprs for the template to surface.
        """
        state = initialize_state(graph, values, checkpoint=checkpoint)
        scope = compute_execution_scope(graph)

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
        # When resuming, offset counters so new steps don't overwrite prior ones
        from hypergraph.runners._shared.checkpoint_helpers import checkpoint_offsets

        superstep_offset, step_counter = checkpoint_offsets(checkpoint)
        node_order = {name: i for i, name in enumerate(graph._nodes)} if has_checkpointer else {}
        save_tasks: list[asyncio.Task[None]] = []

        signal = get_stop_signal()
        assert signal is not None, "run template must install a workflow stop signal"

        try:
            superstep_idx = 0
            frontier = ExecutionFrontier.from_scope(scope, max_iterations)
            ctx_base = ExecutionContext(
                event_processors=event_processors,
                # Always False: processors already merged above; prevents GraphNode
                # sub-runners from double-adding RichProgressProcessor.
                show_progress=False,
                workflow_id=workflow_id,
                run_id=run_id,
                item_index=item_index,
                provided_values=values,
                is_resuming=(checkpoint is not None if self._checkpointer_instance is not None else True),
                checkpoint_error_sink=checkpoint_save_errors.append if checkpoint_save_errors is not None else None,
                emit_fn=dispatcher.emit if dispatcher.active else None,
            )

            while frontier.has_pending_components():
                # Check stop signal at superstep boundary.
                # When complete_on_stop is True, nodes still see stop_requested
                # but the runner continues until all ready nodes are done.
                if signal.is_set and not _complete_on_stop:
                    break
                try:
                    ready_nodes = frontier.next_ready_batch(
                        graph,
                        state,
                        active_nodes=scope.active_nodes,
                        startup_predecessors=scope.startup_predecessors,
                    )
                except InfiniteLoopError as e:
                    raise ExecutionError(e, state) from e

                if not ready_nodes:
                    continue

                # Isolate interrupts BEFORE ready_node_names is captured below,
                # so checkpoint metadata records exactly the executed batch.
                ready_nodes = plan_interrupt_batch(ready_nodes)

                if dispatcher.active:
                    from hypergraph.events.types import SuperstepStartEvent, _generate_span_id

                    await dispatcher.emit_async(
                        SuperstepStartEvent(
                            run_id=run_id,
                            span_id=_generate_span_id(),
                            parent_span_id=run_span_id,
                            workflow_id=workflow_id,
                            item_index=ctx_base.item_index,
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
                child_run_ids = {
                    name: graphnode_child_workflow_id(workflow_id, name, state)
                    for name in ready_node_names
                    if isinstance(graph._nodes.get(name), GraphNode)
                }

                superstep_error: BaseException | None = None
                attempted_node_names: tuple[str, ...] | None = None
                node_errors: dict[str, BaseException] | None = None
                try:
                    # Execute all ready nodes concurrently
                    # Concurrency controlled by shared semaphore in ContextVar
                    state = await run_superstep_async(
                        graph,
                        state,
                        ready_nodes,
                        values,
                        self._executors,
                        ctx_base,
                        max_concurrency,
                        cache=self._cache,
                        dispatcher=dispatcher,
                        run_id=run_id,
                        run_span_id=run_span_id,
                        superstep_idx=superstep_idx,
                    )
                except PauseExecution as pause:
                    if pause.partial_state is not None:
                        state = pause.partial_state
                    # Save step records before propagating the pause.
                    # The interrupt node gets a "paused" status record.
                    if has_checkpointer:
                        step_counter = await self._save_superstep_records(
                            checkpointer,
                            workflow_id,
                            superstep_idx + superstep_offset,
                            state,
                            ready_node_names,
                            prev_input_versions,
                            node_order,
                            step_counter,
                            step_buffer,
                            save_tasks,
                            graph,
                            superstep_error=None,
                            is_pause=True,
                            stopped=signal.is_set,
                            child_run_ids=child_run_ids,
                        )
                    raise
                except ExecutionError as e:
                    superstep_error = e
                    state = e.partial_state
                    attempted_node_names = e.attempted_node_names
                    node_errors = e.node_errors
                except Exception as e:
                    superstep_error = ExecutionError(e, state)
                    attempted_node_names = ()
                    node_errors = {}

                # Save step records for executed nodes (even on failure)
                if has_checkpointer:
                    step_counter = await self._save_superstep_records(
                        checkpointer,
                        workflow_id,
                        superstep_idx + superstep_offset,
                        state,
                        ready_node_names,
                        prev_input_versions,
                        node_order,
                        step_counter,
                        step_buffer,
                        save_tasks,
                        graph,
                        superstep_error,
                        stopped=signal.is_set,
                        child_run_ids=child_run_ids,
                        attempted_node_names=attempted_node_names,
                        node_errors=node_errors,
                    )

                if superstep_error is not None:
                    raise superstep_error

                superstep_idx += 1

        except PauseExecution as pause:
            pause.partial_state = state
            pause.stopped = signal.is_set
            raise
        finally:
            # Await any background save tasks before returning
            if save_tasks:
                results = await asyncio.gather(*save_tasks, return_exceptions=True)
                failures = [r for r in results if isinstance(r, BaseException)]
                if failures:
                    if checkpoint_save_errors is not None:
                        checkpoint_save_errors.extend(repr(f) for f in failures)
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

        # Propagate stopped flag to the template layer
        state.stopped = signal.is_set
        state.stop_info = signal.info
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
        is_pause: bool = False,
        stopped: bool = False,
        child_run_ids: dict[str, str | None] | None = None,
        attempted_node_names: tuple[str, ...] | set[str] | None = None,
        node_errors: dict[str, BaseException] | None = None,
    ) -> int:
        """Build StepRecords and dispatch to the appropriate durability mode."""
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
            is_pause=is_pause,
            stopped=stopped,
            child_run_ids=child_run_ids,
            attempted_node_names=attempted_node_names,
            node_errors=node_errors,
        )

        durability = checkpointer.policy.durability
        for record in records:
            if durability == "sync":
                await checkpointer.save_step(record)
            elif durability == "async":
                save_tasks.append(asyncio.create_task(checkpointer.save_step(record)))
            elif step_buffer is not None:
                step_buffer.append(record)

        return step_counter

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
        context: RunContext = DEFAULT_RUN_CONTEXT,
        is_map: bool = False,
        map_size: int | None = None,
        lineage: RunLineage = DEFAULT_RUN_LINEAGE,
    ) -> tuple[str, str]:
        """Emit run-start event via async helper."""
        return await _emit_run_start(
            dispatcher,
            graph,
            parent_span_id,
            context=context,
            is_map=is_map,
            map_size=map_size,
            lineage=lineage,
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
        context: RunContext = DEFAULT_RUN_CONTEXT,
        status: str | None = None,
        error: BaseException | None = None,
        batch_summary: BatchSummary | None = None,
    ) -> None:
        """Emit run-end event via async helper."""
        await _emit_run_end(
            dispatcher,
            run_id,
            span_id,
            graph,
            start_time,
            parent_span_id,
            context=context,
            status=status,
            error=error,
            batch_summary=batch_summary,
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
    context: RunContext = DEFAULT_RUN_CONTEXT,
    is_map: bool = False,
    map_size: int | None = None,
    lineage: RunLineage = DEFAULT_RUN_LINEAGE,
) -> tuple[str, str]:
    """Emit RunStartEvent and return (run_id, span_id)."""
    from hypergraph.runners._shared.event_helpers import build_run_start_event

    run_id, span_id, event = build_run_start_event(
        graph,
        parent_span_id,
        context=context,
        is_map=is_map,
        map_size=map_size,
        lineage=lineage,
    )

    if not dispatcher.active:
        return run_id, span_id

    await dispatcher.emit_async(event)
    return run_id, span_id


async def _emit_run_end(
    dispatcher: EventDispatcher,
    run_id: str,
    span_id: str,
    graph: Graph,
    start_time: float,
    parent_span_id: str | None,
    *,
    context: RunContext = DEFAULT_RUN_CONTEXT,
    status: str | None = None,
    error: BaseException | None = None,
    batch_summary: BatchSummary | None = None,
) -> None:
    """Emit RunEndEvent."""
    if not dispatcher.active:
        return

    from hypergraph.runners._shared.event_helpers import build_run_end_event

    await dispatcher.emit_async(
        build_run_end_event(
            run_id,
            span_id,
            graph,
            start_time,
            parent_span_id,
            context=context,
            status=status,
            error=error,
            batch_summary=batch_summary,
        )
    )
