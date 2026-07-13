"""Shared sync runner lifecycle template."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from hypergraph.checkpointers.types import StepStatus
from hypergraph.exceptions import (
    ExecutionError,
    MissingInputError,
    WorkflowAlreadyRunningError,
    _failure_evidence_context,
    _NodeExecutionError,
)
from hypergraph.runners._shared._inspect import (
    InspectionSession,
    MapInspection,
    MapInspectionSession,
    RunInspection,
    inspection_scope,
)
from hypergraph.runners._shared.event_metadata import (
    DEFAULT_RUN_CONTEXT,
    DEFAULT_RUN_LINEAGE,
    BatchSummary,
    RunContext,
    RunLineage,
)
from hypergraph.runners._shared.input_normalization import (
    normalize_inputs,
    runner_option_names,
)
from hypergraph.runners._shared.lineage import (
    ResumeAction,
    plan_lineage,
    resolve_existing_run,
    validate_lineage_request,
)
from hypergraph.runners._shared.map_inputs import generate_map_inputs
from hypergraph.runners._shared.map_resume import (
    MAP_SIGNATURE_CONFIG_KEY,
    claim_completed_child_run_id,
    compute_map_item_signature,
    index_completed_child_runs,
)
from hypergraph.runners._shared.outputs import (
    SELECT_UNSET,
    filter_outputs,
    validate_error_handling,
    validate_on_missing,
)
from hypergraph.runners._shared.readiness import find_missing_resume_seed_inputs
from hypergraph.runners._shared.results import (
    ErrorHandling,
    MapResult,
    RunResult,
    RunStatus,
    build_failed_run_result,
    build_paused_run_result,
    build_pre_run_failed_result,
    build_restored_run_result,
    build_terminal_run_result,
)
from hypergraph.runners._shared.run_log import RunLogCollector
from hypergraph.runners._shared.scheduling import compute_execution_scope
from hypergraph.runners._shared.state import GraphState, PauseExecution
from hypergraph.runners._shared.state_restore import (
    generate_workflow_id,
    initialize_state,
    validate_workflow_id,
)
from hypergraph.runners._shared.stop import (
    _WorkflowReservation,
    get_stop_signal,
    reset_stop_signal,
    set_stop_signal,
)
from hypergraph.runners._shared.validation import (
    precompute_input_validation,
    resolve_runtime_selected,
    validate_delegated_runners,
    validate_item_inputs,
    validate_node_types,
    validate_runner_compatibility,
)
from hypergraph.runners._shared.value_resolution import (
    build_resume_validation_values,
    warn_on_bind_overrides,
)
from hypergraph.runners.base import BaseRunner

if TYPE_CHECKING:
    from hypergraph.checkpointers.types import Checkpoint
    from hypergraph.events.dispatcher import EventDispatcher
    from hypergraph.events.processor import EventProcessor
    from hypergraph.graph import Graph
    from hypergraph.nodes.base import HyperNode
    from hypergraph.runners._shared._inspect_transport import NotebookInspectionTransport
    from hypergraph.runners._shared.validation import _InputValidationContext


class SyncRunnerTemplate(BaseRunner, ABC):
    """Template implementation for sync run/map lifecycle."""

    @property
    @abstractmethod
    def supported_node_types(self) -> set[type[HyperNode]]:
        """Node types supported by this runner."""
        ...

    @property
    @abstractmethod
    def default_max_iterations(self) -> int:
        """Default max iterations for cyclic graphs."""
        ...

    @property
    def _checkpointer(self) -> Any:
        """Override to provide a checkpointer. Returns None by default."""
        return None

    @abstractmethod
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
        """Execute graph and return final state."""
        ...

    @abstractmethod
    def _create_dispatcher(
        self,
        processors: list[EventProcessor] | None,
    ) -> EventDispatcher:
        """Create event dispatcher."""
        ...

    @abstractmethod
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
        """Emit run-start event."""
        ...

    @abstractmethod
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
        """Emit run-end event."""
        ...

    @abstractmethod
    def _shutdown_dispatcher_sync(
        self,
        dispatcher: EventDispatcher,
    ) -> None:
        """Shut down dispatcher."""
        ...

    def _get_sync_checkpointer(self, workflow_id: str | None) -> Any:
        """Return sync checkpointer if workflow_id is provided, else None.

        Validates that the checkpointer supports sync writes via the
        SyncCheckpointerProtocol.
        """
        checkpointer = self._checkpointer
        if checkpointer is None or workflow_id is None:
            return None

        from hypergraph.checkpointers.protocols import SyncCheckpointerProtocol

        if not isinstance(checkpointer, SyncCheckpointerProtocol):
            raise TypeError(
                f"{type(checkpointer).__name__} does not support sync writes "
                f"(missing SyncCheckpointerProtocol). SyncRunner requires a checkpointer "
                f"that implements create_run_sync/save_step_sync/update_run_status_sync. "
                f"SqliteCheckpointer supports this."
            )
        return checkpointer

    def run(
        self,
        graph: Graph,
        values: dict[str, Any] | None = None,
        *,
        select: str | list[str] = SELECT_UNSET,
        on_missing: Literal["ignore", "warn", "error"] = "ignore",
        entrypoint: str | None = None,
        max_iterations: int | None = None,
        inspect: bool = False,
        error_handling: ErrorHandling = "raise",
        event_processors: list[EventProcessor] | None = None,
        show_progress: bool | None = None,
        checkpoint: Checkpoint | None = None,
        workflow_id: str | None = None,
        override_workflow: bool = False,
        fork_from: str | None = None,
        retry_from: str | None = None,
        _parent_span_id: str | None = None,
        _parent_run_id: str | None = None,
        _validation_ctx: _InputValidationContext | None = None,
        _run_config: dict[str, Any] | None = None,
        _complete_on_stop: bool = False,
        _item_index: int | None = None,
        _reservation: _WorkflowReservation | None = None,
        _inspection_session: InspectionSession | None = None,
        _inspection_transport: NotebookInspectionTransport | None = None,
        _inspection_path: tuple[str, ...] = (),
        **input_values: Any,
    ) -> RunResult:
        """Execute a graph once."""
        if not isinstance(inspect, bool):
            raise TypeError(
                f"inspect must be a bool, got {type(inspect).__name__}.\n\n"
                "How to fix: Pass inspect=True to capture node values or "
                "inspect=False to keep only always-on run facts."
            )
        top_level_inspection = inspect and _parent_span_id is None and _parent_run_id is None and _item_index is None
        inspection_transport = _inspection_transport
        if top_level_inspection and inspection_transport is None:
            try:
                from hypergraph.runners._shared._inspect_transport import open_notebook_inspection_transport

                inspection_transport = open_notebook_inspection_transport(
                    RunInspection(
                        run_id="pending",
                        graph_name=graph.name or "",
                        workflow_id=workflow_id,
                        item_index=None,
                        status="running",
                        nodes=(),
                        failures=(),
                        total_duration_ms=0.0,
                        captured=True,
                        terminal=False,
                    )
                )
            except Exception:
                inspection_transport = None
        try:
            run_option_names = runner_option_names(self.run)
            map_option_names = runner_option_names(self.map)
            validation_ctx = _validation_ctx
            if validation_ctx is None:
                validate_on_missing(on_missing)
                validate_error_handling(error_handling)
                validate_workflow_id(workflow_id, _parent_run_id)
                effective_selected = resolve_runtime_selected(select, graph)
                validation_ctx = precompute_input_validation(graph, entrypoint=entrypoint, selected=effective_selected)
            normalized_values = normalize_inputs(
                values,
                input_values,
                reserved_option_names=run_option_names | map_option_names,
                other_option_names=map_option_names - run_option_names,
                other_call_name="runner.map()",
                call_name="runner.run()",
                graph=graph,
                validation_ctx=validation_ctx,
            )
            # Only fire override warning at the user-initiated outer run; nested
            # GraphNode delegations propagate the same value and would re-warn.
            if _parent_span_id is None and _parent_run_id is None:
                warn_on_bind_overrides(graph, normalized_values)

            # Structural validation (doesn't depend on values)
            if _validation_ctx is None:
                validate_runner_compatibility(graph, self.capabilities)
                validate_node_types(graph, self.supported_node_types)
                validate_delegated_runners(graph, self.capabilities)
        except BaseException as error:
            if inspection_transport is not None:
                inspection_transport.fail_to_start(error)
            raise

        try:
            if self._checkpointer is not None and _validation_ctx is None and workflow_id is None and fork_from is None:
                workflow_id = generate_workflow_id()
            sync_checkpointer_key = workflow_id
            if sync_checkpointer_key is None:
                sync_checkpointer_key = fork_from if fork_from is not None else retry_from
            sync_cp = self._get_sync_checkpointer(sync_checkpointer_key)
            if _validation_ctx is None and (fork_from is not None or retry_from is not None) and sync_cp is None:
                raise ValueError("fork_from/retry_from require a checkpointer and workflow persistence to be enabled.")
        except BaseException as error:
            if inspection_transport is not None:
                inspection_transport.fail_to_start(error)
            raise
        resume_checkpoint = None
        resume_action = ResumeAction.START_NEW
        skip_missing_input_validation = False
        try:
            if sync_cp is not None and _validation_ctx is None:
                validate_lineage_request(
                    checkpoint=checkpoint,
                    fork_from=fork_from,
                    retry_from=retry_from,
                )
                candidate_checkpoint = checkpoint
                if fork_from is not None:
                    workflow_id, resume_checkpoint = sync_cp.fork_workflow(fork_from, workflow_id=workflow_id)
                    candidate_checkpoint = resume_checkpoint
                elif retry_from is not None:
                    workflow_id, resume_checkpoint = sync_cp.retry_workflow(retry_from, workflow_id=workflow_id)
                    candidate_checkpoint = resume_checkpoint

                existing_run = sync_cp.get_run(workflow_id)
                resume_action = resolve_existing_run(
                    existing_run=existing_run,
                    checkpoint=candidate_checkpoint,
                    override_workflow=override_workflow,
                    workflow_id=workflow_id,
                    graph_hash=graph.structural_hash,
                    graph=graph,
                    resume_values=normalized_values,
                )
                if resume_action is ResumeAction.USE_CHECKPOINT:
                    resume_checkpoint = candidate_checkpoint
                elif resume_action is ResumeAction.FORK_EXISTING:
                    # Ergonomic shortcut: same workflow_id + override => auto-fork.
                    workflow_id, resume_checkpoint = sync_cp.fork_workflow(workflow_id)
                elif resume_action is ResumeAction.RESUME_EXISTING:
                    resume_checkpoint = sync_cp.checkpoint(workflow_id)
                if resume_checkpoint is not None:
                    # Runs that start from checkpoint state (resume, fork, retry)
                    # should not re-require original graph inputs that were already
                    # consumed by upstream completed steps.
                    skip_missing_input_validation = True
        except BaseException as error:
            if inspection_transport is not None:
                inspection_transport.fail_to_start(error)
            raise

        run_context = RunContext(workflow_id=workflow_id, item_index=_item_index)
        run_lineage = plan_lineage(
            parent_workflow_id=_parent_run_id,
            checkpoint=resume_checkpoint,
            action=resume_action,
        )

        validation_values = build_resume_validation_values(graph, normalized_values, resume_checkpoint)

        # Value validation (after merge so checkpoint-provided params are visible)
        try:
            if _validation_ctx is None:
                validate_item_inputs(
                    validation_ctx,
                    validation_values,
                    skip_missing_required=skip_missing_input_validation,
                )
            else:
                validate_item_inputs(validation_ctx, validation_values)
        except BaseException as error:
            if inspection_transport is not None:
                inspection_transport.fail_to_start(error)
            raise

        try:
            if resume_checkpoint is not None and skip_missing_input_validation:
                resume_state = initialize_state(graph, normalized_values, checkpoint=resume_checkpoint)
                scope = compute_execution_scope(graph)
                missing_seed_inputs = sorted(
                    find_missing_resume_seed_inputs(
                        graph,
                        resume_state,
                        active_nodes=scope.active_nodes,
                        startup_predecessors=scope.startup_predecessors,
                    )
                )
                if missing_seed_inputs:
                    raise MissingInputError(
                        missing=missing_seed_inputs,
                        provided=sorted(normalized_values),
                        message=(
                            "Checkpoint resume is missing required seed inputs: "
                            + ", ".join(repr(name) for name in missing_seed_inputs)
                            + ". The restored checkpoint state leaves at least one active branch unrunnable."
                        ),
                    )
        except BaseException as error:
            if inspection_transport is not None:
                inspection_transport.fail_to_start(error)
            raise

        max_iter = max_iterations or self.default_max_iterations
        inspection_session = _inspection_session
        owns_inspection = inspect
        if owns_inspection and inspection_session is None:
            inspection_session = InspectionSession(
                graph_name=graph.name or "",
                workflow_id=workflow_id,
                item_index=_item_index,
            )
        if top_level_inspection and inspection_transport is not None and _inspection_session is None and inspection_session is not None:
            try:
                inspection_transport.attach(inspection_session)
            except Exception:
                inspection_transport = None
        try:
            effective_show_progress = show_progress if show_progress is not None else getattr(self, "_show_progress", False)
            if effective_show_progress:
                from hypergraph.runners._shared.scheduling import ensure_progress_processor

                event_processors = ensure_progress_processor(event_processors)
            if graph.default_event_processors:
                # Graph-carried processors merge in front of call-site ones — never
                # replace, never dedup. Reassigning event_processors also forwards
                # them into nested GraphNode sub-runs, exactly like call-site
                # processors.
                event_processors = [*graph.default_event_processors, *(event_processors or [])]
        except BaseException as error:
            if owns_inspection and inspection_session is not None:
                inspection_session.finish(
                    status=RunStatus.FAILED.value,
                    total_duration_ms=0.0,
                    error=error,
                )
            elif inspection_transport is not None:
                inspection_transport.fail_to_start(error)
            raise
        try:
            reservation = _reservation or self._active_workflows.reserve(workflow_id)
        except BaseException as error:
            if owns_inspection and inspection_session is not None:
                inspection_session.finish(
                    status=RunStatus.FAILED.value,
                    total_duration_ms=0.0,
                    error=error,
                )
            elif inspection_transport is not None:
                inspection_transport.fail_to_start(error)
            raise
        inspection_started_at = time.time()
        dispatcher = None
        signal_token = None
        try:
            reservation.bind(workflow_id)
            signal_token = set_stop_signal(reservation.signal)
            collector = RunLogCollector()
            all_processors = [collector] + (event_processors or [])
            dispatcher = self._create_dispatcher(all_processors)
            run_id, run_span_id = self._emit_run_start_sync(
                dispatcher,
                graph,
                _parent_span_id,
                context=run_context,
                lineage=run_lineage,
            )
            if owns_inspection:
                assert inspection_session is not None
                inspection_session.bind_run(run_id)
            if inspection_session is not None and resume_checkpoint is not None:
                for step in sorted(resume_checkpoint.steps, key=lambda item: (item.superstep, item.index)):
                    if step.status is StepStatus.COMPLETED:
                        qualified_name = "/".join((*_inspection_path, step.node_name))
                        inspection_session.restore_node(
                            run_id=step.run_id,
                            span_id=f"restored:{step.run_id}:{step.superstep}:{step.index}:{qualified_name}",
                            node_name=step.node_name,
                            qualified_name=qualified_name,
                            graph_name=graph.name or "",
                            item_index=_item_index,
                            superstep=step.superstep,
                            duration_ms=step.duration_ms,
                            cached=step.cached,
                        )
            start_time = time.time()

            # Checkpointer lifecycle — upsert run record
            if sync_cp is not None:
                run_config = {
                    "graph_struct_hash": graph.structural_hash,
                    "graph_code_hash": graph.code_hash,
                }
                if _run_config:
                    run_config.update(_run_config)
                sync_cp.create_run_sync(
                    workflow_id,
                    graph_name=graph.name,
                    parent_run_id=_parent_run_id,
                    forked_from=run_lineage.forked_from,
                    fork_superstep=run_lineage.fork_superstep,
                    retry_of=run_lineage.retry_of,
                    retry_index=run_lineage.retry_index,
                    config=run_config,
                )

            step_buffer: list[Any] = []
        except BaseException as error:
            try:
                try:
                    if dispatcher is not None and _parent_span_id is None and dispatcher.active:
                        self._shutdown_dispatcher_sync(dispatcher)
                finally:
                    if signal_token is not None:
                        reset_stop_signal(signal_token)
                    reservation.release()
            except BaseException as final_error:
                if owns_inspection and inspection_session is not None and not inspection_session.snapshot().terminal:
                    inspection_session.finish(
                        status=RunStatus.FAILED.value,
                        total_duration_ms=(time.time() - inspection_started_at) * 1000,
                        error=final_error,
                    )
                elif inspection_transport is not None:
                    inspection_transport.fail_to_start(final_error)
                raise
            if owns_inspection and inspection_session is not None and not inspection_session.snapshot().terminal:
                inspection_session.finish(
                    status=RunStatus.FAILED.value,
                    total_duration_ms=(time.time() - inspection_started_at) * 1000,
                    error=error,
                )
            raise

        terminal_error: BaseException | None = None
        try:
            with inspection_scope(inspection_session, _inspection_path):
                state = self._execute_graph_impl(
                    graph,
                    normalized_values,
                    max_iter,
                    dispatcher=dispatcher,
                    run_id=run_id,
                    run_span_id=run_span_id,
                    event_processors=event_processors,
                    workflow_id=workflow_id,
                    checkpoint=resume_checkpoint,
                    step_buffer=step_buffer,
                    _complete_on_stop=_complete_on_stop,
                    item_index=_item_index,
                )
            output_values = filter_outputs(state, graph, select, on_missing)
            total_duration_ms = (time.time() - start_time) * 1000
            was_stopped = state.stopped
            status = RunStatus.STOPPED if was_stopped else RunStatus.COMPLETED

            # Emit StopRequestedEvent if stopped
            if was_stopped and dispatcher.active:
                from hypergraph.events.types import StopRequestedEvent

                stop_info = state.stop_info
                dispatcher.emit(
                    StopRequestedEvent(
                        run_id=run_id,
                        span_id=run_span_id,
                        parent_span_id=_parent_span_id,
                        workflow_id=workflow_id,
                        item_index=_item_index,
                        graph_name=graph.name,
                        info=stop_info,
                    )
                )

            self._emit_run_end_sync(
                dispatcher,
                run_id,
                run_span_id,
                graph,
                start_time,
                _parent_span_id,
                context=run_context,
                status=status.value,
            )
            # Flush buffered steps and mark run completed
            if sync_cp is not None:
                from hypergraph.checkpointers.types import WorkflowStatus
                from hypergraph.runners._shared.checkpoint_helpers import checkpoint_offsets

                _, _step_offset = checkpoint_offsets(resume_checkpoint)
                _flush_and_complete(
                    sync_cp,
                    workflow_id,
                    step_buffer,
                    collector,
                    total_duration_ms,
                    step_offset=_step_offset,
                    status=WorkflowStatus.STOPPED if status == RunStatus.STOPPED else WorkflowStatus.COMPLETED,
                )
            if _parent_span_id is None:
                self._shutdown_dispatcher_sync(dispatcher)
                dispatcher = None
            if signal_token is not None:
                reset_stop_signal(signal_token)
                signal_token = None
            reservation.release()
            inspection = (
                inspection_session.finish(
                    status=status.value,
                    total_duration_ms=total_duration_ms,
                )
                if owns_inspection and inspection_session is not None
                else None
            )
            return build_terminal_run_result(
                values=output_values,
                status=status,
                run_id=run_id,
                workflow_id=workflow_id,
                log=collector.build(graph.name, run_id, total_duration_ms),
                inspection=inspection,
            )
        except PauseExecution as pause:
            partial_state = pause.partial_state
            partial_values = filter_outputs(partial_state, graph, select) if partial_state is not None else {}
            total_duration_ms = (time.time() - start_time) * 1000
            try:
                if dispatcher.active:
                    from hypergraph.events.types import InterruptEvent

                    dispatcher.emit(
                        InterruptEvent(
                            run_id=run_id,
                            span_id=pause.span_id or run_span_id,
                            parent_span_id=run_span_id,
                            workflow_id=workflow_id,
                            item_index=_item_index,
                            node_name=pause.pause_info.node_name,
                            graph_name=graph.name,
                            value=pause.pause_info.value,
                            response_param=pause.pause_info.output_param,
                        )
                    )
                    self._emit_run_end_sync(
                        dispatcher,
                        run_id,
                        run_span_id,
                        graph,
                        start_time,
                        _parent_span_id,
                        context=run_context,
                        status=RunStatus.PAUSED.value,
                    )
                if sync_cp is not None:
                    from hypergraph.checkpointers.types import WorkflowStatus
                    from hypergraph.runners._shared.checkpoint_helpers import checkpoint_offsets

                    for record in step_buffer:
                        sync_cp.save_step_sync(record)
                    _, step_offset = checkpoint_offsets(resume_checkpoint)
                    step_count = step_offset + collector.step_count
                    error_count = collector.failed_step_count
                    sync_cp.update_run_status_sync(
                        workflow_id,
                        WorkflowStatus.PAUSED,
                        duration_ms=total_duration_ms,
                        node_count=step_count,
                        error_count=error_count,
                    )
                if dispatcher is not None and _parent_span_id is None:
                    self._shutdown_dispatcher_sync(dispatcher)
                    dispatcher = None
                if signal_token is not None:
                    reset_stop_signal(signal_token)
                    signal_token = None
                reservation.release()
            except BaseException as final_error:
                terminal_error = final_error
                raise
            inspection = (
                inspection_session.finish(
                    status=RunStatus.PAUSED.value,
                    total_duration_ms=total_duration_ms,
                )
                if owns_inspection and inspection_session is not None
                else None
            )
            return build_paused_run_result(
                values=partial_values,
                run_id=run_id,
                workflow_id=workflow_id,
                pause=pause.pause_info,
                log=collector.build(graph.name, run_id, total_duration_ms),
                inspection=inspection,
            )
        except Exception as e:
            error = e
            partial_state = None
            node_failures = ()
            if isinstance(e, ExecutionError):
                error = e.__cause__ or e
                partial_state = e.partial_state
                if isinstance(e, _NodeExecutionError):
                    node_failures = e.node_failures

            try:
                if dispatcher is not None:
                    self._emit_run_end_sync(
                        dispatcher,
                        run_id,
                        run_span_id,
                        graph,
                        start_time,
                        _parent_span_id,
                        context=run_context,
                        error=error,
                    )

                # Flush buffered steps and mark run failed.
                # A bare WorkflowAlreadyRunningError is a pre-flight rejection of a
                # DUPLICATE start: this call never owned the run row, and the
                # original run is still executing — never touch its persisted
                # status. (Wrapped in ExecutionError it came from a node, so the
                # run genuinely failed and the write below is correct.)
                if sync_cp is not None and not isinstance(e, WorkflowAlreadyRunningError):
                    from hypergraph.runners._shared.checkpoint_helpers import checkpoint_offsets as _cp_offsets

                    _, _step_off = _cp_offsets(resume_checkpoint)
                    _flush_and_fail(sync_cp, workflow_id, step_buffer, collector, start_time, step_offset=_step_off)
                if dispatcher is not None and _parent_span_id is None:
                    self._shutdown_dispatcher_sync(dispatcher)
                    dispatcher = None
                if signal_token is not None:
                    reset_stop_signal(signal_token)
                    signal_token = None
                reservation.release()
            except BaseException as final_error:
                terminal_error = final_error
                raise

            total_duration_ms = (time.time() - start_time) * 1000
            inspection = (
                inspection_session.finish(
                    status=RunStatus.FAILED.value,
                    total_duration_ms=total_duration_ms,
                    failures=tuple(node_failures),
                    error=error,
                )
                if owns_inspection and inspection_session is not None
                else None
            )
            if error_handling == "raise":
                raise error from None

            partial_values = filter_outputs(partial_state, graph, select) if partial_state is not None else {}
            return build_failed_run_result(
                values=partial_values,
                run_id=run_id,
                workflow_id=workflow_id,
                error=error,
                node_failures=node_failures,
                log=collector.build(graph.name, run_id, total_duration_ms),
                inspection=inspection,
            )
        except BaseException as error:
            terminal_error = error
            raise
        finally:
            try:
                try:
                    if dispatcher is not None and _parent_span_id is None:
                        self._shutdown_dispatcher_sync(dispatcher)
                finally:
                    if signal_token is not None:
                        reset_stop_signal(signal_token)
                    reservation.release()
            except BaseException as final_error:
                if owns_inspection and inspection_session is not None and not inspection_session.snapshot().terminal:
                    inspection_session.finish(
                        status=RunStatus.FAILED.value,
                        total_duration_ms=(time.time() - inspection_started_at) * 1000,
                        error=final_error,
                    )
                raise
            if terminal_error is not None and owns_inspection and inspection_session is not None and not inspection_session.snapshot().terminal:
                inspection_session.finish(
                    status=RunStatus.FAILED.value,
                    total_duration_ms=(time.time() - inspection_started_at) * 1000,
                    error=terminal_error,
                )

    def map(
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
        error_handling: ErrorHandling = "raise",
        event_processors: list[EventProcessor] | None = None,
        show_progress: bool | None = None,
        workflow_id: str | None = None,
        _parent_span_id: str | None = None,
        _parent_run_id: str | None = None,
        _item_index: int | None = None,
        _reservation: _WorkflowReservation | None = None,
        _inspection_transport: NotebookInspectionTransport | None = None,
        **input_values: Any,
    ) -> MapResult:
        """Execute a graph multiple times with different inputs."""
        if not isinstance(inspect, bool):
            raise TypeError(
                f"inspect must be a bool, got {type(inspect).__name__}.\n\n"
                "How to fix: Pass inspect=True to capture map item values or "
                "inspect=False to keep only always-on batch facts."
            )
        top_level_inspection = inspect and _parent_span_id is None and _parent_run_id is None and _item_index is None
        inspection_transport = _inspection_transport
        if top_level_inspection and inspection_transport is None:
            try:
                from hypergraph.runners._shared._inspect_transport import open_notebook_inspection_transport

                pending_map_over = (map_over,) if isinstance(map_over, str) else tuple(map_over) if isinstance(map_over, list) else ()
                inspection_transport = open_notebook_inspection_transport(
                    MapInspection(
                        run_id="pending",
                        graph_name=graph.name or "",
                        workflow_id=workflow_id,
                        status="running",
                        map_over=pending_map_over,
                        map_mode=map_mode,
                        requested_count=0,
                        items=(),
                        unstarted_item_indexes=(),
                        total_duration_ms=0.0,
                        captured=True,
                        terminal=False,
                    )
                )
            except Exception:
                inspection_transport = None
        try:
            run_option_names = runner_option_names(self.run)
            map_option_names = runner_option_names(self.map)
            validate_error_handling(error_handling)
            validate_workflow_id(workflow_id, _parent_run_id)
            validate_on_missing(on_missing)
            effective_selected = resolve_runtime_selected(select, graph)
            ctx = precompute_input_validation(graph, entrypoint=entrypoint, selected=effective_selected)
            normalized_values = normalize_inputs(
                values,
                input_values,
                reserved_option_names=run_option_names | map_option_names,
                other_option_names=run_option_names - map_option_names,
                other_call_name="runner.run()",
                call_name="runner.map()",
                graph=graph,
                validation_ctx=ctx,
            )
            # Same parity as run(): only fire override warning at the user-initiated
            # outer call; nested delegations would re-warn for the propagated value.
            if _parent_span_id is None and _parent_run_id is None:
                warn_on_bind_overrides(graph, normalized_values)

            # Resolve show_progress and merge processors
            effective_show_progress = show_progress if show_progress is not None else getattr(self, "_show_progress", False)
            if effective_show_progress:
                from hypergraph.runners._shared.scheduling import ensure_progress_processor

                event_processors = ensure_progress_processor(event_processors)

            # One-time graph-structural validation
            validate_runner_compatibility(graph, self.capabilities)
            validate_node_types(graph, self.supported_node_types)
            validate_delegated_runners(graph, self.capabilities)
        except BaseException as error:
            if inspection_transport is not None:
                inspection_transport.fail_to_start(error)
            raise

        try:
            map_over_list = [map_over] if isinstance(map_over, str) else list(map_over)
            input_variations = list(generate_map_inputs(normalized_values, map_over_list, map_mode, clone))
        except BaseException as error:
            if inspection_transport is not None:
                inspection_transport.fail_to_start(error)
            raise
        map_inspection_session = (
            MapInspectionSession(
                graph_name=graph.name or "",
                workflow_id=workflow_id,
                requested_count=len(input_variations),
                map_over=tuple(map_over_list),
                map_mode=map_mode,
            )
            if inspect
            else None
        )
        if map_inspection_session is not None and top_level_inspection:
            try:
                if inspection_transport is not None:
                    inspection_transport.attach(map_inspection_session)
            except Exception:
                inspection_transport = None
        map_inspection_started_at = time.time()
        if not input_variations:
            map_result = MapResult(
                results=(),
                run_id=None,
                total_duration_ms=0,
                map_over=tuple(map_over_list),
                map_mode=map_mode,
                graph_name=graph.name or "",
            )
            if map_inspection_session is not None:
                map_inspection_session.bind_run(None)
                map_result = replace(
                    map_result,
                    _inspection=map_inspection_session.finish(
                        status=map_result.status.value,
                        total_duration_ms=0.0,
                    ),
                )
            return map_result

        try:
            reservation = _reservation or self._active_workflows.reserve(workflow_id)
        except BaseException as error:
            if map_inspection_session is not None:
                map_inspection_session.finish(
                    status=RunStatus.FAILED.value,
                    total_duration_ms=(time.time() - map_inspection_started_at) * 1000,
                    unstarted_item_indexes=tuple(range(len(input_variations))),
                    error=error,
                )
            elif inspection_transport is not None:
                inspection_transport.fail_to_start(error)
            raise
        dispatcher = None
        signal_token = None
        try:
            reservation.bind(workflow_id)
            signal_token = set_stop_signal(reservation.signal)

            # Graph-carried processors merge into the top-level map dispatcher only;
            # the per-item self.run(...) calls below re-merge them per item, so
            # forwarding the merged list would double-deliver every item event.
            dispatcher = self._create_dispatcher([*graph.default_event_processors, *(event_processors or [])])
            map_run_id, map_span_id = self._emit_run_start_sync(
                dispatcher,
                graph,
                _parent_span_id,
                context=RunContext(workflow_id=workflow_id, item_index=_item_index),
                is_map=True,
                map_size=len(input_variations),
                lineage=RunLineage(parent_workflow_id=_parent_run_id),
            )
            if map_inspection_session is not None:
                map_inspection_session.bind_run(map_run_id)
            start_time = time.time()

            # Create parent batch run if checkpointing
            sync_cp = self._get_sync_checkpointer(workflow_id)
            if sync_cp is not None:
                sync_cp.create_run_sync(
                    workflow_id,
                    graph_name=graph.name,
                    parent_run_id=_parent_run_id,
                    config={
                        "graph_struct_hash": graph.structural_hash,
                        "graph_code_hash": graph.code_hash,
                    },
                )

            # Resume: find completed child runs to skip by stable input signature.
            completed_runs = _get_completed_child_runs_sync(sync_cp, workflow_id)
            completed_by_signature, completed_by_index = index_completed_child_runs(completed_runs, workflow_id)
            map_stop_signal = get_stop_signal()
            claimed_indexes: set[int] = set()
        except BaseException as error:
            try:
                try:
                    if dispatcher is not None and _parent_span_id is None and dispatcher.active:
                        self._shutdown_dispatcher_sync(dispatcher)
                finally:
                    if signal_token is not None:
                        reset_stop_signal(signal_token)
                    reservation.release()
            except BaseException as final_error:
                if map_inspection_session is not None and not map_inspection_session.snapshot().terminal:
                    map_inspection_session.finish(
                        status=RunStatus.FAILED.value,
                        total_duration_ms=(time.time() - map_inspection_started_at) * 1000,
                        unstarted_item_indexes=tuple(range(len(input_variations))),
                        error=final_error,
                    )
                elif inspection_transport is not None:
                    inspection_transport.fail_to_start(final_error)
                raise
            if map_inspection_session is not None and not map_inspection_session.snapshot().terminal:
                map_inspection_session.finish(
                    status=RunStatus.FAILED.value,
                    total_duration_ms=(time.time() - map_inspection_started_at) * 1000,
                    unstarted_item_indexes=tuple(range(len(input_variations))),
                    error=error,
                )
            raise

        terminal_error: BaseException | None = None
        try:
            results = []
            for idx, variation_inputs in enumerate(input_variations):
                if map_stop_signal is not None and map_stop_signal.is_set:
                    break
                claimed_indexes.add(idx)
                child_workflow_id = f"{workflow_id}/{idx}" if workflow_id else None
                child_inspection_session = (
                    map_inspection_session.claim_item(
                        item_index=idx,
                        requested_inputs=variation_inputs,
                        workflow_id=child_workflow_id,
                    )
                    if map_inspection_session is not None
                    else None
                )
                item_signature = compute_map_item_signature(variation_inputs, map_over_list, map_mode) if sync_cp is not None else None

                # Skip completed items — restore result from checkpoint
                restore_run_id = (
                    claim_completed_child_run_id(
                        idx=idx,
                        signature=item_signature,
                        by_signature=completed_by_signature,
                        by_index=completed_by_index,
                    )
                    if item_signature is not None
                    else None
                )
                if restore_run_id is not None and sync_cp is not None:
                    state = sync_cp.state(restore_run_id)
                    restored_state = GraphState(values=dict(state))
                    restored_values = filter_outputs(restored_state, graph, select, on_missing)
                    result = build_restored_run_result(
                        values=restored_values,
                        graph_name=graph.name or "",
                        run_id=restore_run_id,
                    )
                    results.append(result)
                    if map_inspection_session is not None:
                        map_inspection_session.settle_item(
                            item_index=idx,
                            result=result,
                        )
                    continue

                try:
                    result = self.run(
                        graph,
                        variation_inputs,
                        select=select,
                        on_missing=on_missing,
                        entrypoint=entrypoint,
                        inspect=inspect,
                        error_handling="continue",
                        event_processors=event_processors,
                        show_progress=False,
                        workflow_id=child_workflow_id,
                        _parent_span_id=map_span_id,
                        _parent_run_id=workflow_id,
                        _validation_ctx=ctx,
                        _run_config=({MAP_SIGNATURE_CONFIG_KEY: item_signature} if item_signature is not None else None),
                        _item_index=idx,
                        _inspection_session=child_inspection_session,
                    )
                except Exception as e:
                    # Catch validation errors (e.g., MissingInputError) that raise
                    # before run()'s execution try block — parity with async map
                    # and both map_iter variants.
                    result = build_pre_run_failed_result(e)
                results.append(result)
                if map_inspection_session is not None:
                    map_inspection_session.settle_item(
                        item_index=idx,
                        result=result,
                    )
                if error_handling == "raise" and result.status == RunStatus.FAILED:
                    error = result.error
                    assert error is not None, "FAILED status requires an error"
                    with _failure_evidence_context(error, result.node_failures):
                        raise error from None

            total_duration_ms = (time.time() - start_time) * 1000
            unstarted_item_indexes = (
                tuple(idx for idx in range(len(input_variations)) if idx not in claimed_indexes)
                if map_stop_signal is not None and map_stop_signal.is_set
                else ()
            )
            map_result = MapResult(
                results=tuple(results),
                run_id=map_run_id,
                total_duration_ms=total_duration_ms,
                map_over=tuple(map_over_list),
                map_mode=map_mode,
                graph_name=graph.name or "",
                unstarted_item_indexes=unstarted_item_indexes,
            )
            batch_summary = BatchSummary.from_map_result(map_result)

            if map_stop_signal is not None and map_stop_signal.is_set and dispatcher.active:
                from hypergraph.events.types import StopRequestedEvent

                dispatcher.emit(
                    StopRequestedEvent(
                        run_id=map_run_id,
                        span_id=map_span_id,
                        parent_span_id=_parent_span_id,
                        workflow_id=workflow_id,
                        item_index=_item_index,
                        graph_name=graph.name,
                        info=map_stop_signal.info,
                    )
                )

            self._emit_run_end_sync(
                dispatcher,
                map_run_id,
                map_span_id,
                graph,
                start_time,
                _parent_span_id,
                context=RunContext(workflow_id=workflow_id, item_index=_item_index),
                status=batch_summary.event_status_value,
                batch_summary=batch_summary,
            )

            # Persist parent batch run status
            if sync_cp is not None:
                from hypergraph.checkpointers.types import WorkflowStatus

                error_count = sum(1 for r in results if r.status == RunStatus.FAILED)
                persisted_status = WorkflowStatus(batch_summary.workflow_status_value)
                sync_cp.update_run_status_sync(
                    workflow_id,
                    persisted_status,
                    duration_ms=total_duration_ms,
                    node_count=len(results),
                    error_count=error_count,
                )

            if _parent_span_id is None:
                self._shutdown_dispatcher_sync(dispatcher)
                dispatcher = None
            if signal_token is not None:
                reset_stop_signal(signal_token)
                signal_token = None
            reservation.release()

            if map_inspection_session is not None:
                map_result = replace(
                    map_result,
                    _inspection=map_inspection_session.finish(
                        status=map_result.status.value,
                        total_duration_ms=total_duration_ms,
                        unstarted_item_indexes=unstarted_item_indexes,
                    ),
                )

            return map_result
        except Exception as e:
            total_ms = (time.time() - start_time) * 1000
            try:
                if dispatcher is not None:
                    self._emit_run_end_sync(
                        dispatcher,
                        map_run_id,
                        map_span_id,
                        graph,
                        start_time,
                        _parent_span_id,
                        error=e,
                    )
                # Mark parent batch run as failed
                if sync_cp is not None:
                    from hypergraph.checkpointers.types import WorkflowStatus as _WS

                    error_count = sum(1 for r in results if r.status == RunStatus.FAILED)
                    sync_cp.update_run_status_sync(
                        workflow_id,
                        _WS.FAILED,
                        duration_ms=total_ms,
                        node_count=len(results),
                        error_count=error_count,
                    )
                if dispatcher is not None and _parent_span_id is None:
                    self._shutdown_dispatcher_sync(dispatcher)
                    dispatcher = None
                if signal_token is not None:
                    reset_stop_signal(signal_token)
                    signal_token = None
                reservation.release()
            except BaseException as final_error:
                terminal_error = final_error
                raise
            if map_inspection_session is not None:
                unstarted_item_indexes = tuple(idx for idx in range(len(input_variations)) if idx not in claimed_indexes)
                batch_error = None if any(result.error is e for result in results) else e
                map_inspection_session.finish(
                    status=RunStatus.FAILED.value,
                    total_duration_ms=total_ms,
                    unstarted_item_indexes=unstarted_item_indexes,
                    error=batch_error,
                )
            raise
        except BaseException as error:
            terminal_error = error
            raise
        finally:
            try:
                try:
                    if dispatcher is not None and _parent_span_id is None:
                        self._shutdown_dispatcher_sync(dispatcher)
                finally:
                    if signal_token is not None:
                        reset_stop_signal(signal_token)
                    reservation.release()
            except BaseException as final_error:
                if map_inspection_session is not None and not map_inspection_session.snapshot().terminal:
                    map_inspection_session.finish(
                        status=RunStatus.FAILED.value,
                        total_duration_ms=(time.time() - map_inspection_started_at) * 1000,
                        unstarted_item_indexes=tuple(idx for idx in range(len(input_variations)) if idx not in claimed_indexes),
                        error=final_error,
                    )
                raise
            if terminal_error is not None and map_inspection_session is not None and not map_inspection_session.snapshot().terminal:
                map_inspection_session.finish(
                    status=RunStatus.FAILED.value,
                    total_duration_ms=(time.time() - map_inspection_started_at) * 1000,
                    unstarted_item_indexes=tuple(idx for idx in range(len(input_variations)) if idx not in claimed_indexes),
                    error=terminal_error,
                )

    def map_iter(
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
        error_handling: ErrorHandling = "raise",
        **input_values: Any,
    ) -> Iterator[tuple[int, RunResult]]:
        """Stream ``(index, RunResult)`` pairs as each mapped item completes.

        Like :meth:`map`, but yields incrementally instead of buffering a
        ``MapResult`` — bounding memory to one item at a time. ``index`` is the
        input item's position, so a consumer can correlate a result with its
        source item regardless of arrival order. ``error_handling="raise"``
        re-raises when a failed item is reached; ``"continue"`` yields the failed
        ``RunResult`` and keeps going.
        """
        run_option_names = runner_option_names(self.run)
        map_option_names = runner_option_names(self.map)
        validate_error_handling(error_handling)
        validate_on_missing(on_missing)
        effective_selected = resolve_runtime_selected(select, graph)
        ctx = precompute_input_validation(graph, entrypoint=entrypoint, selected=effective_selected)
        normalized_values = normalize_inputs(
            values,
            input_values,
            reserved_option_names=run_option_names | map_option_names,
            other_option_names=run_option_names - map_option_names,
            other_call_name="runner.run()",
            call_name="runner.map_iter()",
            graph=graph,
            validation_ctx=ctx,
        )

        validate_runner_compatibility(graph, self.capabilities)
        validate_node_types(graph, self.supported_node_types)
        validate_delegated_runners(graph, self.capabilities)

        map_over_list = [map_over] if isinstance(map_over, str) else list(map_over)

        # Lazy: pull one input variation at a time so peak memory stays bounded
        # by a single item, not the whole batch.
        for idx, variation_inputs in enumerate(generate_map_inputs(normalized_values, map_over_list, map_mode, clone)):
            try:
                result = self.run(
                    graph,
                    variation_inputs,
                    select=select,
                    on_missing=on_missing,
                    entrypoint=entrypoint,
                    error_handling="continue",
                    show_progress=False,
                    _validation_ctx=ctx,
                    _item_index=idx,
                )
            except Exception as e:  # per-item validation error (e.g. missing input) → failed row
                result = build_pre_run_failed_result(e)
            if error_handling == "raise" and result.status == RunStatus.FAILED:
                error = result.error
                assert error is not None, "FAILED status requires an error"
                with _failure_evidence_context(error, result.node_failures):
                    raise error from None
            yield idx, result


def _flush_and_complete(
    sync_cp: Any,
    workflow_id: str,
    step_buffer: list,
    collector: RunLogCollector,
    total_duration_ms: float,
    *,
    step_offset: int = 0,
    status: Any,
) -> None:
    """Flush buffered steps and mark a terminal run status."""
    for record in step_buffer:
        sync_cp.save_step_sync(record)

    step_count = step_offset + collector.step_count
    error_count = collector.failed_step_count
    sync_cp.update_run_status_sync(
        workflow_id,
        status,
        duration_ms=total_duration_ms,
        node_count=step_count,
        error_count=error_count,
    )


def _flush_and_fail(
    sync_cp: Any, workflow_id: str, step_buffer: list, collector: RunLogCollector, start_time: float, *, step_offset: int = 0
) -> None:
    """Flush buffered steps and mark run failed."""
    for record in step_buffer:
        sync_cp.save_step_sync(record)
    from hypergraph.checkpointers.types import WorkflowStatus as _WS

    total_ms = (time.time() - start_time) * 1000
    fail_count = step_offset + collector.step_count
    err_count = collector.failed_step_count
    sync_cp.update_run_status_sync(
        workflow_id,
        _WS.FAILED,
        duration_ms=total_ms,
        node_count=fail_count,
        error_count=err_count,
    )


def _get_completed_child_runs_sync(
    sync_cp: Any,
    workflow_id: str | None,
) -> list[Any]:
    """Return completed child runs for a batch workflow (sync)."""
    if sync_cp is None or workflow_id is None:
        return []

    from hypergraph.checkpointers.types import WorkflowStatus

    child_runs = sync_cp.runs(parent_run_id=workflow_id)
    return [run for run in child_runs if run.status == WorkflowStatus.COMPLETED]
