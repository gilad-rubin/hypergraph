"""Shared async runner lifecycle template."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from hypergraph.exceptions import (
    ExecutionError,
    MissingInputError,
    WorkflowAlreadyRunningError,
    _failure_evidence_context,
    _NodeExecutionError,
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
from hypergraph.runners._shared.state import CheckpointErrorSink, GraphState, PauseExecution
from hypergraph.runners._shared.state_restore import (
    generate_workflow_id,
    initialize_state,
    validate_workflow_id,
)
from hypergraph.runners._shared.stop import get_stop_signal
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
    from hypergraph.runners._shared.validation import _InputValidationContext


MAX_UNBOUNDED_MAP_TASKS = 10_000
# Default streaming concurrency for map_iter when the caller doesn't pass one.
# Unlike map(), map_iter is bounded by design, so an absent limit means a modest
# bounded pool (tune via max_concurrency), never unbounded fan-out.
_DEFAULT_STREAM_CONCURRENCY = 16


class AsyncRunnerTemplate(BaseRunner, ABC):
    """Template implementation for async run/map lifecycle."""

    _accepts_checkpoint_error_sink: ClassVar[Literal[True]] = True

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
        """Execute graph and return final state.

        ``checkpoint_save_errors`` is a caller-owned sink: implementations
        append string reprs of background step-save failures (durability
        "async") so the template can surface them on the RunResult.
        """
        ...

    @abstractmethod
    def _create_dispatcher(
        self,
        processors: list[EventProcessor] | None,
    ) -> EventDispatcher:
        """Create event dispatcher."""
        ...

    @abstractmethod
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
        """Emit run-start event."""
        ...

    @abstractmethod
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
        """Emit run-end event."""
        ...

    @abstractmethod
    async def _shutdown_dispatcher_async(
        self,
        dispatcher: EventDispatcher,
    ) -> None:
        """Shut down dispatcher."""
        ...

    @abstractmethod
    def _get_concurrency_limiter(self) -> Any:
        """Get current shared concurrency limiter."""
        ...

    @abstractmethod
    def _set_concurrency_limiter(self, max_concurrency: int) -> Any:
        """Set shared concurrency limiter and return reset token."""
        ...

    @abstractmethod
    def _reset_concurrency_limiter(self, token: Any) -> None:
        """Reset shared concurrency limiter using token."""
        ...

    async def run(
        self,
        graph: Graph,
        values: dict[str, Any] | None = None,
        *,
        select: str | list[str] = SELECT_UNSET,
        on_missing: Literal["ignore", "warn", "error"] = "ignore",
        entrypoint: str | None = None,
        max_iterations: int | None = None,
        max_concurrency: int | None = None,
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
        _checkpoint_error_sink: CheckpointErrorSink | None = None,
        **input_values: Any,
    ) -> RunResult:
        """Execute a graph once."""
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

        checkpointer = self._checkpointer
        if _validation_ctx is None and (fork_from is not None or retry_from is not None) and checkpointer is None:
            raise ValueError("fork_from/retry_from require a checkpointer and workflow persistence to be enabled.")
        resume_checkpoint = None
        resume_action = ResumeAction.START_NEW
        skip_missing_input_validation = False
        if checkpointer is not None and _validation_ctx is None:
            if workflow_id is None and fork_from is None:
                workflow_id = generate_workflow_id()
            validate_lineage_request(
                checkpoint=checkpoint,
                fork_from=fork_from,
                retry_from=retry_from,
            )
            candidate_checkpoint = checkpoint
            if fork_from is not None:
                workflow_id, resume_checkpoint = await checkpointer.fork_workflow_async(fork_from, workflow_id=workflow_id)
                candidate_checkpoint = resume_checkpoint
            elif retry_from is not None:
                workflow_id, resume_checkpoint = await checkpointer.retry_workflow_async(retry_from, workflow_id=workflow_id)
                candidate_checkpoint = resume_checkpoint

            existing_run = await checkpointer.get_run_async(workflow_id)
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
                workflow_id, resume_checkpoint = await checkpointer.fork_workflow_async(workflow_id)
            elif resume_action is ResumeAction.RESUME_EXISTING:
                resume_checkpoint = await checkpointer.get_checkpoint(workflow_id)
            if resume_checkpoint is not None:
                # Runs that start from checkpoint state (resume, fork, retry)
                # should not re-require original graph inputs that were already
                # consumed by upstream completed steps.
                skip_missing_input_validation = True

        has_checkpointer = checkpointer is not None and workflow_id is not None
        run_context = RunContext(workflow_id=workflow_id, item_index=_item_index)
        run_lineage = plan_lineage(
            parent_workflow_id=_parent_run_id,
            checkpoint=resume_checkpoint,
            action=resume_action,
        )

        validation_values = build_resume_validation_values(graph, normalized_values, resume_checkpoint)

        # Value validation (after merge so checkpoint-provided params are visible)
        if _validation_ctx is None:
            validate_item_inputs(
                validation_ctx,
                validation_values,
                skip_missing_required=skip_missing_input_validation,
            )
        else:
            validate_item_inputs(validation_ctx, validation_values)

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

        max_iter = max_iterations or self.default_max_iterations
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
        collector = RunLogCollector()
        all_processors = [collector] + (event_processors or [])
        dispatcher = self._create_dispatcher(all_processors)
        run_id, run_span_id = await self._emit_run_start_async(
            dispatcher,
            graph,
            _parent_span_id,
            context=run_context,
            lineage=run_lineage,
        )
        start_time = time.time()

        # Checkpointer lifecycle — upsert run record
        if has_checkpointer:
            run_config = {
                "graph_struct_hash": graph.structural_hash,
                "graph_code_hash": graph.code_hash,
            }
            if _run_config:
                run_config.update(_run_config)
            await checkpointer.create_run(
                workflow_id,
                graph_name=graph.name,
                parent_run_id=_parent_run_id,
                forked_from=run_lineage.forked_from,
                fork_superstep=run_lineage.fork_superstep,
                retry_of=run_lineage.retry_of,
                retry_index=run_lineage.retry_index,
                config=run_config,
            )

        # Step buffer for "exit" durability — records are flushed after run completes
        step_buffer: list[Any] = []
        # Sink for background step-save failures ("async" durability) —
        # surfaced as result.checkpoint_ok / result.checkpoint_errors.
        checkpoint_save_errors: list[str] = []

        try:
            state = await self._execute_graph_impl_async(
                graph,
                normalized_values,
                max_iter,
                max_concurrency,
                dispatcher=dispatcher,
                run_id=run_id,
                run_span_id=run_span_id,
                event_processors=event_processors,
                workflow_id=workflow_id,
                checkpoint=resume_checkpoint,
                step_buffer=step_buffer,
                checkpoint_save_errors=checkpoint_save_errors,
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
                await dispatcher.emit_async(
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

            result = build_terminal_run_result(
                values=output_values,
                status=status,
                run_id=run_id,
                workflow_id=workflow_id,
                log=collector.build(graph.name, run_id, total_duration_ms),
                checkpoint_errors=checkpoint_save_errors,
            )
            await self._emit_run_end_async(
                dispatcher,
                run_id,
                run_span_id,
                graph,
                start_time,
                _parent_span_id,
                context=run_context,
                status=status.value,
            )
            # Flush buffered steps ("exit" mode) and mark run completed
            if has_checkpointer:
                from hypergraph.checkpointers.types import WorkflowStatus

                for record in step_buffer:
                    await checkpointer.save_step(record)
                from hypergraph.runners._shared.checkpoint_helpers import checkpoint_offsets

                _, step_offset = checkpoint_offsets(resume_checkpoint)
                step_count = step_offset + collector.step_count
                error_count = collector.failed_step_count
                await checkpointer.update_run_status(
                    workflow_id,
                    WorkflowStatus.STOPPED if status == RunStatus.STOPPED else WorkflowStatus.COMPLETED,
                    duration_ms=total_duration_ms,
                    node_count=step_count,
                    error_count=error_count,
                )
            return result
        except PauseExecution as pause:
            partial_state = pause.partial_state
            partial_values = filter_outputs(partial_state, graph, select) if partial_state is not None else {}
            total_duration_ms = (time.time() - start_time) * 1000
            if dispatcher.active:
                from hypergraph.events.types import InterruptEvent

                await dispatcher.emit_async(
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
                await self._emit_run_end_async(
                    dispatcher,
                    run_id,
                    run_span_id,
                    graph,
                    start_time,
                    _parent_span_id,
                    context=run_context,
                    status=RunStatus.PAUSED.value,
                )
            if has_checkpointer:
                for record in step_buffer:
                    await checkpointer.save_step(record)
                from hypergraph.checkpointers.types import WorkflowStatus
                from hypergraph.runners._shared.checkpoint_helpers import checkpoint_offsets

                _, step_offset = checkpoint_offsets(resume_checkpoint)
                step_count = step_offset + collector.step_count
                error_count = collector.failed_step_count
                await checkpointer.update_run_status(
                    workflow_id,
                    WorkflowStatus.PAUSED,
                    duration_ms=total_duration_ms,
                    node_count=step_count,
                    error_count=error_count,
                )
            return build_paused_run_result(
                values=partial_values,
                run_id=run_id,
                workflow_id=workflow_id,
                pause=pause.pause_info,
                log=collector.build(graph.name, run_id, total_duration_ms),
                checkpoint_errors=checkpoint_save_errors,
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

            await self._emit_run_end_async(
                dispatcher,
                run_id,
                run_span_id,
                graph,
                start_time,
                _parent_span_id,
                context=run_context,
                error=error,
            )

            # A bare WorkflowAlreadyRunningError is a pre-flight rejection of a
            # DUPLICATE start: this call never owned the run row, and the
            # original run is still executing — never touch its persisted
            # status. (Wrapped in ExecutionError it came from a node, so the
            # run genuinely failed and the write below is correct.)
            if has_checkpointer and not isinstance(e, WorkflowAlreadyRunningError):
                from hypergraph.checkpointers.types import WorkflowStatus as _WS

                # Flush buffered steps so partial execution is preserved on failure
                for record in step_buffer:
                    await checkpointer.save_step(record)
                total_duration_ms_fail = (time.time() - start_time) * 1000
                from hypergraph.runners._shared.checkpoint_helpers import checkpoint_offsets as _cp_offsets

                _, _step_offset = _cp_offsets(resume_checkpoint)
                fail_count = _step_offset + collector.step_count
                err_count = collector.failed_step_count
                await checkpointer.update_run_status(
                    workflow_id,
                    _WS.FAILED,
                    duration_ms=total_duration_ms_fail,
                    node_count=fail_count,
                    error_count=err_count,
                )

            if error_handling == "raise":
                raise error from None

            total_duration_ms = (time.time() - start_time) * 1000
            partial_values = filter_outputs(partial_state, graph, select) if partial_state is not None else {}
            return build_failed_run_result(
                values=partial_values,
                run_id=run_id,
                workflow_id=workflow_id,
                error=error,
                node_failures=node_failures,
                log=collector.build(graph.name, run_id, total_duration_ms),
                checkpoint_errors=checkpoint_save_errors,
            )
        finally:
            if _checkpoint_error_sink is not None:
                for checkpoint_error in checkpoint_save_errors:
                    _checkpoint_error_sink(checkpoint_error)
            if _parent_span_id is None and dispatcher.active:
                await self._shutdown_dispatcher_async(dispatcher)

    async def map(
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
        error_handling: ErrorHandling = "raise",
        event_processors: list[EventProcessor] | None = None,
        show_progress: bool | None = None,
        workflow_id: str | None = None,
        _parent_span_id: str | None = None,
        _parent_run_id: str | None = None,
        _item_index: int | None = None,
        _checkpoint_error_sink: CheckpointErrorSink | None = None,
        **input_values: Any,
    ) -> MapResult:
        """Execute a graph multiple times with different inputs."""
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

        map_over_list = [map_over] if isinstance(map_over, str) else list(map_over)
        input_variations = list(generate_map_inputs(normalized_values, map_over_list, map_mode, clone))
        if not input_variations:
            return MapResult(
                results=(),
                run_id=None,
                total_duration_ms=0,
                map_over=tuple(map_over_list),
                map_mode=map_mode,
                graph_name=graph.name or "",
            )
        if max_concurrency is None and len(input_variations) > MAX_UNBOUNDED_MAP_TASKS:
            raise ValueError(
                f"Too many map tasks without a concurrency limit: {len(input_variations)}. "
                f"Set max_concurrency or keep inputs at <= {MAX_UNBOUNDED_MAP_TASKS}."
            )
        item_checkpoint_errors: list[list[str]] = [[] for _ in input_variations]

        # Graph-carried processors merge into the top-level map dispatcher only;
        # the per-item self.run(...) calls below re-merge them per item, so
        # forwarding the merged list would double-deliver every item event.
        dispatcher = self._create_dispatcher([*graph.default_event_processors, *(event_processors or [])])
        map_run_id, map_span_id = await self._emit_run_start_async(
            dispatcher,
            graph,
            _parent_span_id,
            context=RunContext(workflow_id=workflow_id, item_index=_item_index),
            is_map=True,
            map_size=len(input_variations),
            lineage=RunLineage(parent_workflow_id=_parent_run_id),
        )
        start_time = time.time()

        # Create parent batch run if checkpointing
        checkpointer = self._checkpointer
        has_checkpointer = checkpointer is not None and workflow_id is not None
        if has_checkpointer:
            await checkpointer.create_run(
                workflow_id,
                graph_name=graph.name,
                parent_run_id=_parent_run_id,
                config={
                    "graph_struct_hash": graph.structural_hash,
                    "graph_code_hash": graph.code_hash,
                },
            )

        # Resume: find completed child runs to skip by stable input signature.
        completed_runs = await _get_completed_child_runs(checkpointer, workflow_id)
        completed_by_signature, completed_by_index = index_completed_child_runs(completed_runs, workflow_id)

        existing_limiter = self._get_concurrency_limiter()
        token = self._set_concurrency_limiter(max_concurrency) if existing_limiter is None and max_concurrency is not None else None
        map_stop_signal = get_stop_signal()
        claimed_indexes: set[int] = set()

        async def _run_map_item(idx: int, variation_inputs: dict[str, Any]) -> RunResult:
            """Execute one map variation, or restore from checkpoint if completed."""
            child_workflow_id = f"{workflow_id}/{idx}" if workflow_id else None
            item_signature = compute_map_item_signature(variation_inputs, map_over_list, map_mode)

            # Skip completed items — restore result from checkpoint.
            restore_run_id = claim_completed_child_run_id(
                idx=idx,
                signature=item_signature,
                by_signature=completed_by_signature,
                by_index=completed_by_index,
            )
            if restore_run_id is not None and has_checkpointer:
                state = await checkpointer.get_state(restore_run_id)
                restored_state = GraphState(values=dict(state))
                restored_values = filter_outputs(restored_state, graph, select, on_missing)
                return build_restored_run_result(
                    values=restored_values,
                    graph_name=graph.name or "",
                    run_id=restore_run_id,
                )

            try:
                return await self.run(
                    graph,
                    variation_inputs,
                    select=select,
                    on_missing=on_missing,
                    entrypoint=entrypoint,
                    max_concurrency=max_concurrency,
                    error_handling="continue",
                    event_processors=event_processors,
                    show_progress=False,
                    workflow_id=child_workflow_id,
                    _parent_span_id=map_span_id,
                    _parent_run_id=workflow_id,
                    _validation_ctx=ctx,
                    _run_config={MAP_SIGNATURE_CONFIG_KEY: item_signature},
                    _item_index=idx,
                    _checkpoint_error_sink=(item_checkpoint_errors[idx].append if _checkpoint_error_sink is not None else None),
                )
            except Exception as e:
                # Catch validation errors (e.g., MissingInputError) that raise
                # before run()'s execution try block
                return build_pre_run_failed_result(e)

        try:
            if max_concurrency is None:
                results: list[RunResult] = []
                if map_stop_signal is None or not map_stop_signal.is_set:
                    claimed_indexes.update(range(len(input_variations)))
                    tasks = [_run_map_item(idx, v) for idx, v in enumerate(input_variations)]
                    gathered = await asyncio.gather(*tasks, return_exceptions=True)
                    for item in gathered:
                        if isinstance(item, BaseException):
                            raise item
                        results.append(item)
                if error_handling == "raise":
                    for result in results:
                        if result.status == RunStatus.FAILED:
                            error = result.error
                            assert error is not None, "FAILED status requires an error"
                            with _failure_evidence_context(error, result.node_failures):
                                raise error from None
            else:
                results_list: list[RunResult] = []
                queue: asyncio.Queue[tuple[int, dict[str, Any]]] = asyncio.Queue()
                for idx, v in enumerate(input_variations):
                    queue.put_nowait((idx, v))

                order: list[int] = []
                stop_event = asyncio.Event()

                async def _worker() -> None:
                    """Consume queue items and execute map variations."""
                    while not stop_event.is_set():
                        if map_stop_signal is not None and map_stop_signal.is_set:
                            return
                        try:
                            idx, v = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        claimed_indexes.add(idx)
                        result = await _run_map_item(idx, v)
                        results_list.append(result)
                        order.append(idx)
                        if error_handling == "raise" and result.status == RunStatus.FAILED:
                            stop_event.set()

                num_workers = min(max_concurrency, len(input_variations))
                workers = [asyncio.create_task(_worker()) for _ in range(num_workers)]
                try:
                    await asyncio.gather(*workers)
                except Exception:
                    # Let the outer error handler emit map-level failure events.
                    raise
                results = [r for _, r in sorted(zip(order, results_list, strict=False))]
                if error_handling == "raise":
                    for result in results:
                        if result.status == RunStatus.FAILED:
                            error = result.error
                            assert error is not None, "FAILED status requires an error"
                            with _failure_evidence_context(error, result.node_failures):
                                raise error from None

            batch_summary = BatchSummary.from_results(results)

            await self._emit_run_end_async(
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
            total_duration_ms = (time.time() - start_time) * 1000

            # Persist parent batch run status
            if has_checkpointer:
                from hypergraph.checkpointers.types import WorkflowStatus

                error_count = sum(1 for r in results if r.status == RunStatus.FAILED)
                persisted_status = WorkflowStatus(batch_summary.workflow_status_value)
                await checkpointer.update_run_status(
                    workflow_id,
                    persisted_status,
                    duration_ms=total_duration_ms,
                    node_count=len(results),
                    error_count=error_count,
                )

            return MapResult(
                results=tuple(results),
                run_id=map_run_id,
                total_duration_ms=total_duration_ms,
                map_over=tuple(map_over_list),
                map_mode=map_mode,
                graph_name=graph.name or "",
                unstarted_item_indexes=(
                    tuple(idx for idx in range(len(input_variations)) if idx not in claimed_indexes)
                    if map_stop_signal is not None and map_stop_signal.is_set
                    else ()
                ),
            )
        except Exception as e:
            await self._emit_run_end_async(
                dispatcher,
                map_run_id,
                map_span_id,
                graph,
                start_time,
                _parent_span_id,
                error=e,
            )
            # Mark parent batch run as failed
            if has_checkpointer:
                from hypergraph.checkpointers.types import WorkflowStatus as _WS

                total_ms = (time.time() - start_time) * 1000
                error_count = sum(1 for r in results if r.status == RunStatus.FAILED)
                await checkpointer.update_run_status(
                    workflow_id,
                    _WS.FAILED,
                    duration_ms=total_ms,
                    node_count=len(results),
                    error_count=error_count,
                )
            raise
        finally:
            if _checkpoint_error_sink is not None:
                for checkpoint_errors in item_checkpoint_errors:
                    for checkpoint_error in checkpoint_errors:
                        _checkpoint_error_sink(checkpoint_error)
            if token is not None:
                self._reset_concurrency_limiter(token)
            if _parent_span_id is None and dispatcher.active:
                await self._shutdown_dispatcher_async(dispatcher)

    async def map_iter(
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
        error_handling: ErrorHandling = "raise",
        **input_values: Any,
    ) -> AsyncIterator[tuple[int, RunResult]]:
        """Stream ``(index, RunResult)`` pairs as each mapped item completes.

        Concurrent and backpressured: at most ``max_concurrency`` items run at
        once, and a bounded internal buffer means a slow consumer pauses
        production instead of materializing the whole batch — so peak memory is
        bounded, not proportional to the input size. ``index`` is the input
        item's position; results arrive in completion order. ``error_handling``
        matches :meth:`map`: ``"raise"`` re-raises when a failed item is reached,
        ``"continue"`` yields the failed ``RunResult`` and keeps going.
        """
        run_option_names = runner_option_names(self.run)
        map_option_names = runner_option_names(self.map)
        validate_error_handling(error_handling)
        validate_on_missing(on_missing)
        if max_concurrency is not None and max_concurrency < 1:
            raise ValueError(f"max_concurrency must be >= 1, got {max_concurrency}")
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

        # Always stream lazily: workers pull input variations on demand, so peak
        # memory is bounded by the worker pool + result buffer, never the input
        # size. map_iter is backpressured by default (its whole purpose), so an
        # absent max_concurrency means a bounded default pool, not map()'s
        # unbounded fan-out — and there is no whole-batch materialization or cap.
        concurrency = max_concurrency if max_concurrency is not None else _DEFAULT_STREAM_CONCURRENCY
        input_source = enumerate(generate_map_inputs(normalized_values, map_over_list, map_mode, clone))

        # Share the concurrency limiter across every item run and its internal
        # nodes/nested graphs, so max_concurrency is a global budget — matching
        # map()'s invariant rather than only limiting the worker count.
        existing_limiter = self._get_concurrency_limiter()
        token = self._set_concurrency_limiter(max_concurrency) if existing_limiter is None and max_concurrency is not None else None

        # maxsize bounds buffered completed results; a full queue blocks workers
        # on put() — that is the backpressure that pauses production.
        out_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=concurrency)
        done_sentinel = object()
        worker_failures: list[BaseException] = []
        stop_requested = False  # raise-mode: a sibling already produced a failure

        async def _worker() -> None:
            # Shared iterator: next() runs between awaits, so no two workers ever
            # claim the same index. Anything that escapes this coroutine — a lazy
            # input-generation error (e.g. zip mismatch) or a BaseException such
            # as a node CancelledError — is collected by the gather below and
            # re-raised to the consumer, never silently dropped.
            nonlocal stop_requested
            for i, variation_inputs in input_source:
                if stop_requested:
                    break  # raise-mode: don't start new items after a failure
                try:
                    result = await self.run(
                        graph,
                        variation_inputs,
                        select=select,
                        on_missing=on_missing,
                        entrypoint=entrypoint,
                        max_concurrency=max_concurrency,
                        error_handling="continue",
                        show_progress=False,
                        _validation_ctx=ctx,
                        _item_index=i,
                    )
                except Exception as e:  # node/validation error during a single run → failed row
                    result = build_pre_run_failed_result(e)
                if error_handling == "raise" and result.status == RunStatus.FAILED:
                    stop_requested = True
                await out_queue.put((i, result))

        workers = [asyncio.create_task(_worker()) for _ in range(concurrency)]

        async def _signal_done() -> None:
            outcomes = await asyncio.gather(*workers, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    worker_failures.append(outcome)
            await out_queue.put(done_sentinel)

        closer = asyncio.create_task(_signal_done())
        try:
            while True:
                item = await out_queue.get()
                if item is done_sentinel:
                    if worker_failures:
                        raise worker_failures[0]
                    break
                i, result = item
                if error_handling == "raise" and result.status == RunStatus.FAILED:
                    error = result.error
                    assert error is not None, "FAILED status requires an error"
                    with _failure_evidence_context(error, result.node_failures):
                        raise error from None
                yield i, result
        finally:
            for w in workers:
                w.cancel()
            closer.cancel()
            await asyncio.gather(*workers, closer, return_exceptions=True)
            if token is not None:
                self._reset_concurrency_limiter(token)


async def _get_completed_child_runs(
    checkpointer: Any,
    workflow_id: str | None,
) -> list[Any]:
    """Return completed child runs for a batch workflow."""
    if checkpointer is None or workflow_id is None:
        return []

    from hypergraph.checkpointers.types import WorkflowStatus

    child_runs = await checkpointer.list_runs(parent_run_id=workflow_id)
    return [run for run in child_runs if run.status == WorkflowStatus.COMPLETED]
