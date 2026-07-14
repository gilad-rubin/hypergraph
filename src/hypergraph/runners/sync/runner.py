"""Synchronous runner for graph execution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from hypergraph.exceptions import ExecutionError, InfiniteLoopError
from hypergraph.nodes.base import HyperNode
from hypergraph.nodes.function import FunctionNode
from hypergraph.nodes.gate import IfElseNode, RouteNode
from hypergraph.nodes.graph_node import GraphNode
from hypergraph.runners._shared.event_metadata import (
    DEFAULT_RUN_CONTEXT,
    DEFAULT_RUN_LINEAGE,
    BatchSummary,
    RunContext,
    RunLineage,
)
from hypergraph.runners._shared.handles import SyncHandle, _launch_sync_execution
from hypergraph.runners._shared.input_normalization import runner_option_names
from hypergraph.runners._shared.outputs import SELECT_UNSET
from hypergraph.runners._shared.protocols import NodeExecutor
from hypergraph.runners._shared.results import MapResult, RunResult
from hypergraph.runners._shared.scheduling import ExecutionFrontier, compute_execution_scope
from hypergraph.runners._shared.state import ExecutionContext, GraphState, RunnerCapabilities
from hypergraph.runners._shared.state_restore import graphnode_child_workflow_id, initialize_state
from hypergraph.runners._shared.stop import _ActiveWorkflows, get_stop_signal
from hypergraph.runners._shared.template_sync import SyncRunnerTemplate
from hypergraph.runners._shared.validation import reject_background_runner_options
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
    from hypergraph.checkpointers.types import Checkpoint
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
        show_progress: bool = False,
    ):
        """Initialize SyncRunner with its node executors.

        Args:
            cache: Optional cache backend for node result caching.
                Nodes opt in with ``cache=True``.
            checkpointer: Optional checkpointer for workflow persistence.
                Must implement SyncCheckpointerProtocol (e.g. SqliteCheckpointer).
                Pass a workflow_id to run() to activate persistence.
            show_progress: If True, automatically add a RichProgressProcessor
                to every run() and map() call. Can be overridden per-call.
        """
        self._cache = cache
        self._checkpointer_instance = checkpointer
        self._show_progress = show_progress
        self._active_workflows = _ActiveWorkflows()
        self._executors: dict[type[HyperNode], NodeExecutor] = {
            FunctionNode: SyncFunctionNodeExecutor(),
            GraphNode: SyncGraphNodeExecutor(self),
            IfElseNode: SyncIfElseNodeExecutor(),
            RouteNode: SyncRouteNodeExecutor(),
        }

    def stop(self, workflow_id: str, *, info: Any = None) -> None:
        """Request cooperative stop for an active run or map.

        No-op if the workflow_id is not currently running.
        Thread-safe: uses threading.Event internally for sync runner.

        Args:
            workflow_id: The active workflow execution to stop.
            info: Optional metadata attached to the stop signal.
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
        inspect: bool = False,
        event_processors: list[EventProcessor] | None = None,
        show_progress: bool | None = None,
        checkpoint: Checkpoint | None = None,
        workflow_id: str | None = None,
        **input_values: Any,
    ) -> SyncHandle[RunResult]:
        """Start one graph execution in the background.

        Args:
            graph: The graph to execute.
            values: Optional graph inputs as a dictionary.
            select: Which outputs to return.
            on_missing: How to handle missing selected outputs.
            entrypoint: Optional explicit cycle entrypoint.
            max_iterations: Maximum iterations for cyclic graphs.
            inspect: Capture node inputs/outputs for live and settled inspection.
            event_processors: Optional processors for execution events.
            show_progress: Override runner-level progress display.
            checkpoint: Optional checkpoint from which to resume.
            workflow_id: Optional workflow identifier. With a checkpointer,
                omission assigns a generated workflow ID to the settled result;
                with ``inspect=True``, inspection binds it before restored state
                or node evidence is published.
            **input_values: Graph input shorthand.

        Returns:
            A process-local handle for the live execution.
        """
        reject_background_runner_options(
            input_values,
            start_method="SyncRunner.start_run",
            reserved_option_names=runner_option_names(
                self.run,
                include_private=True,
            )
            | runner_option_names(self.map, include_private=True),
        )
        inspection_session = None
        inspection_transport = None
        if inspect is True:
            from hypergraph.runners._shared._inspect import InspectionSession
            from hypergraph.runners._shared._inspect_transport import open_notebook_inspection_transport

            inspection_session = InspectionSession(
                graph_name=graph.name or "",
                workflow_id=workflow_id,
                item_index=None,
            )
            try:
                inspection_transport = open_notebook_inspection_transport(
                    inspection_session.snapshot(),
                    require_cross_thread=True,
                )
                if inspection_transport is not None:
                    inspection_transport.attach(inspection_session)
            except Exception:
                inspection_transport = None

        try:
            reservation = self._active_workflows.reserve(workflow_id)

            def execute() -> RunResult:
                try:
                    return self.run(
                        graph,
                        values,
                        select=select,
                        on_missing=on_missing,
                        entrypoint=entrypoint,
                        max_iterations=max_iterations,
                        inspect=inspect,
                        error_handling="continue",
                        event_processors=event_processors,
                        show_progress=show_progress,
                        checkpoint=checkpoint,
                        workflow_id=workflow_id,
                        _reservation=reservation,
                        _inspection_session=inspection_session,
                        _inspection_transport=inspection_transport,
                        **input_values,
                    )
                except BaseException as error:
                    if inspection_transport is not None:
                        inspection_transport.fail_to_start(error)
                    raise

            return _launch_sync_execution(execute, reservation)
        except BaseException as error:
            if inspection_transport is not None:
                inspection_transport.fail_to_start(error)
            raise

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
        inspect: bool = False,
        event_processors: list[EventProcessor] | None = None,
        show_progress: bool | None = None,
        workflow_id: str | None = None,
        **input_values: Any,
    ) -> SyncHandle[MapResult]:
        """Start a settled map execution in the background."""
        reject_background_runner_options(
            input_values,
            start_method="SyncRunner.start_map",
            reserved_option_names=runner_option_names(
                self.run,
                include_private=True,
            )
            | runner_option_names(self.map, include_private=True),
        )
        inspection_transport = None
        if inspect is True:
            from hypergraph.runners._shared._inspect import MapInspection
            from hypergraph.runners._shared._inspect_transport import open_notebook_inspection_transport

            pending = MapInspection(
                run_id="pending",
                graph_name=graph.name or "",
                workflow_id=workflow_id,
                status="running",
                map_over=(map_over,) if isinstance(map_over, str) else tuple(map_over) if isinstance(map_over, list) else (),
                map_mode=map_mode,
                requested_count=0,
                items=(),
                unstarted_item_indexes=(),
                total_duration_ms=0.0,
                captured=True,
                terminal=False,
            )
            try:
                inspection_transport = open_notebook_inspection_transport(
                    pending,
                    require_cross_thread=True,
                )
            except Exception:
                inspection_transport = None

        try:
            reservation = self._active_workflows.reserve(workflow_id)

            def execute() -> MapResult:
                try:
                    return self.map(
                        graph,
                        values,
                        map_over=map_over,
                        map_mode=map_mode,
                        clone=clone,
                        select=select,
                        on_missing=on_missing,
                        entrypoint=entrypoint,
                        inspect=inspect,
                        error_handling="continue",
                        event_processors=event_processors,
                        show_progress=show_progress,
                        workflow_id=workflow_id,
                        _reservation=reservation,
                        _inspection_transport=inspection_transport,
                        **input_values,
                    )
                except BaseException as error:
                    if inspection_transport is not None:
                        inspection_transport.fail_to_start(error)
                    raise

            return _launch_sync_execution(execute, reservation)
        except BaseException as error:
            if inspection_transport is not None:
                inspection_transport.fail_to_start(error)
            raise

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
            supports_streaming=True,
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
        checkpoint: Checkpoint | None = None,
        step_buffer: list[Any] | None = None,
        _complete_on_stop: bool = False,
        item_index: int | None = None,
    ) -> GraphState:
        """Execute graph until no more ready nodes or max_iterations reached.

        On failure, raises ExecutionError wrapping the cause and partial state.
        """
        state = initialize_state(graph, values, checkpoint=checkpoint)
        scope = compute_execution_scope(graph)

        # Checkpointer setup — template already validated the protocol,
        # so we just check if checkpointing is active for this run
        sync_cp = self._checkpointer_instance if (self._checkpointer_instance and workflow_id) else None
        # When resuming, offset counters so new steps don't overwrite prior ones
        from hypergraph.runners._shared.checkpoint_helpers import checkpoint_offsets

        superstep_offset, step_counter = checkpoint_offsets(checkpoint)
        node_order = {name: i for i, name in enumerate(graph._nodes)} if sync_cp else {}

        signal = get_stop_signal()
        assert signal is not None, "run template must install a workflow stop signal"

        superstep_idx = 0
        frontier = ExecutionFrontier.from_scope(scope, max_iterations)
        ctx_base = ExecutionContext(
            event_processors=event_processors,
            # Always False: processors already merged above; prevents GraphNode
            # sub-runners from double-adding RichProgressProcessor.
            show_progress=False,
            workflow_id=workflow_id,
            item_index=item_index,
            run_id=run_id,
            provided_values=values,
            emit_fn=dispatcher.emit if dispatcher.active else None,
        )

        try:
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

                if dispatcher.active:
                    from hypergraph.events.types import SuperstepStartEvent, _generate_span_id

                    dispatcher.emit(
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

                # Track ready nodes and prior input_versions for checkpoint helpers
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
                    # Execute all ready nodes
                    state = run_superstep_sync(
                        graph,
                        state,
                        ready_nodes,
                        values,
                        self._executors,
                        ctx_base,
                        cache=self._cache,
                        dispatcher=dispatcher,
                        run_id=run_id,
                        run_span_id=run_span_id,
                        superstep_idx=superstep_idx,
                    )
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
                if sync_cp:
                    step_counter = _save_superstep_sync(
                        sync_cp,
                        workflow_id,
                        superstep_idx + superstep_offset,
                        state,
                        ready_node_names,
                        prev_input_versions,
                        node_order,
                        step_counter,
                        step_buffer,
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
        finally:
            # Expose cooperative-stop truth even when execution exits early.
            state.stopped = signal.is_set
            state.stop_info = signal.info

        return state

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
        context: RunContext = DEFAULT_RUN_CONTEXT,
        is_map: bool = False,
        map_size: int | None = None,
        lineage: RunLineage = DEFAULT_RUN_LINEAGE,
    ) -> tuple[str, str]:
        """Emit run-start event via sync helper."""
        return _emit_run_start(
            dispatcher,
            graph,
            parent_span_id,
            context=context,
            is_map=is_map,
            map_size=map_size,
            lineage=lineage,
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
        context: RunContext = DEFAULT_RUN_CONTEXT,
        status: str | None = None,
        error: BaseException | None = None,
        batch_summary: BatchSummary | None = None,
    ) -> None:
        """Emit run-end event via sync helper."""
        _emit_run_end(
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
    stopped: bool = False,
    child_run_ids: dict[str, str | None] | None = None,
    attempted_node_names: tuple[str, ...] | set[str] | None = None,
    node_errors: dict[str, BaseException] | None = None,
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
        stopped=stopped,
        child_run_ids=child_run_ids,
        attempted_node_names=attempted_node_names,
        node_errors=node_errors,
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

    dispatcher.emit(event)
    return run_id, span_id


def _emit_run_end(
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

    dispatcher.emit(
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
