"""Shared async runner lifecycle template."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Literal

from hypergraph.exceptions import (
    ExecutionError,
    GraphChangedError,
    InputOverrideRequiresForkError,
    MissingInputError,
    WorkflowAlreadyCompletedError,
    WorkflowForkError,
)
from hypergraph.runners._shared.event_metadata import (
    DEFAULT_RUN_CONTEXT,
    DEFAULT_RUN_LINEAGE,
    BatchSummary,
    RunContext,
    RunLineage,
)
from hypergraph.runners._shared.helpers import (
    _UNSET_SELECT,
    _validate_error_handling,
    _validate_on_missing,
    _validate_workflow_id,
    build_resume_validation_values,
    compute_execution_scope,
    filter_outputs,
    find_missing_resume_seed_inputs,
    generate_map_inputs,
    generate_workflow_id,
    get_ready_nodes,
    initialize_state,
    is_interrupt_resume_payload,
)
from hypergraph.runners._shared.input_normalization import (
    ASYNC_MAP_RESERVED_OPTION_NAMES,
    ASYNC_RUN_RESERVED_OPTION_NAMES,
    normalize_inputs,
)
from hypergraph.runners._shared.run_log import RunLogCollector
from hypergraph.runners._shared.types import (
    ErrorHandling,
    GraphState,
    MapResult,
    PauseExecution,
    RunResult,
    RunStatus,
    _generate_run_id,
)
from hypergraph.runners._shared.validation import (
    precompute_input_validation,
    resolve_runtime_selected,
    validate_delegated_runners,
    validate_inputs,
    validate_item_inputs,
    validate_map_compatible,
    validate_node_types,
    validate_runner_compatibility,
)
from hypergraph.runners.base import BaseRunner

if TYPE_CHECKING:
    from hypergraph.events.dispatcher import EventDispatcher
    from hypergraph.events.processor import EventProcessor
    from hypergraph.graph import Graph
    from hypergraph.nodes.base import HyperNode
    from hypergraph.runners._shared.validation import _InputValidationContext


MAX_UNBOUNDED_MAP_TASKS = 10_000
_MAP_SIGNATURE_CONFIG_KEY = "map_item_signature"


class AsyncRunnerTemplate(BaseRunner, ABC):
    """Template implementation for async run/map lifecycle."""

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
        checkpoint: Any | None = None,
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
        select: str | list[str] = _UNSET_SELECT,
        on_missing: Literal["ignore", "warn", "error"] = "ignore",
        entrypoint: str | None = None,
        max_iterations: int | None = None,
        max_concurrency: int | None = None,
        error_handling: ErrorHandling = "raise",
        event_processors: list[EventProcessor] | None = None,
        show_progress: bool | None = None,
        checkpoint: Any | None = None,
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
        **input_values: Any,
    ) -> RunResult:
        """Execute a graph once."""
        normalized_values = normalize_inputs(
            values,
            input_values,
            reserved_option_names=ASYNC_RUN_RESERVED_OPTION_NAMES,
        )

        # Structural validation (doesn't depend on values)
        if _validation_ctx is None:
            validate_runner_compatibility(graph, self.capabilities)
            validate_node_types(graph, self.supported_node_types)
            validate_delegated_runners(graph, self.capabilities)
            _validate_on_missing(on_missing)
            _validate_error_handling(error_handling)
            _validate_workflow_id(workflow_id, _parent_run_id)

        checkpointer = self._checkpointer
        if _validation_ctx is None and (fork_from is not None or retry_from is not None) and checkpointer is None:
            raise ValueError("fork_from/retry_from require a checkpointer and workflow persistence to be enabled.")
        resume_checkpoint = None
        skip_missing_input_validation = False
        if checkpointer is not None and _validation_ctx is None:
            if workflow_id is None:
                workflow_id = generate_workflow_id()
            if fork_from is not None and retry_from is not None:
                raise ValueError("Cannot pass both fork_from and retry_from. Choose one lineage source.")
            if checkpoint is not None and (fork_from is not None or retry_from is not None):
                raise ValueError("Cannot combine checkpoint with fork_from/retry_from. Use one forking mechanism.")
            if fork_from is not None:
                workflow_id, resume_checkpoint = await checkpointer.fork_workflow_async(fork_from, workflow_id=workflow_id)
                checkpoint = resume_checkpoint
            elif retry_from is not None:
                workflow_id, resume_checkpoint = await checkpointer.retry_workflow_async(retry_from, workflow_id=workflow_id)
                checkpoint = resume_checkpoint

            existing_run = await checkpointer.get_run_async(workflow_id)
            graph_hash = graph.structural_hash
            if checkpoint is not None:
                if existing_run is not None:
                    raise WorkflowForkError(f"Cannot fork into existing workflow '{workflow_id}'. Use a new workflow_id.")
                resume_checkpoint = checkpoint
            elif existing_run is not None:
                if override_workflow:
                    # Ergonomic shortcut: same workflow_id + override => auto-fork.
                    workflow_id, resume_checkpoint = await checkpointer.fork_workflow_async(workflow_id)
                    checkpoint = resume_checkpoint
                else:
                    previous_hash = (existing_run.config or {}).get("graph_struct_hash")
                    if previous_hash is not None and previous_hash != graph_hash:
                        raise GraphChangedError(workflow_id)
                    if normalized_values and not is_interrupt_resume_payload(graph, normalized_values):
                        raise InputOverrideRequiresForkError(workflow_id)
                    if existing_run.status.value == "completed":
                        raise WorkflowAlreadyCompletedError(workflow_id)
                    resume_checkpoint = await checkpointer.get_checkpoint(workflow_id)
            if resume_checkpoint is not None:
                # Runs that start from checkpoint state (resume, fork, retry)
                # should not re-require original graph inputs that were already
                # consumed by upstream completed steps.
                skip_missing_input_validation = True

        has_checkpointer = checkpointer is not None and workflow_id is not None
        forked_from: str | None = None
        fork_superstep: int | None = None
        retry_of: str | None = None
        retry_index: int | None = None
        if checkpoint is not None and resume_checkpoint is not None:
            forked_from = getattr(resume_checkpoint, "source_run_id", None)
            fork_superstep = getattr(resume_checkpoint, "source_superstep", None)
            retry_of = getattr(resume_checkpoint, "retry_of", None)
            retry_index = getattr(resume_checkpoint, "retry_index", None)
        is_resume = resume_checkpoint is not None and forked_from is None and retry_of is None
        run_context = RunContext(workflow_id=workflow_id, item_index=_item_index)
        run_lineage = RunLineage(
            parent_workflow_id=_parent_run_id,
            forked_from=forked_from,
            fork_superstep=fork_superstep,
            retry_of=retry_of,
            retry_index=retry_index,
            is_resume=is_resume,
        )

        validation_values = build_resume_validation_values(graph, normalized_values, resume_checkpoint)

        # Value validation (after merge so checkpoint-provided params are visible)
        if _validation_ctx is None:
            effective_selected = resolve_runtime_selected(select, graph)
            validate_inputs(
                graph,
                validation_values,
                entrypoint=entrypoint,
                selected=effective_selected,
                skip_missing_required=skip_missing_input_validation,
            )
        else:
            validate_item_inputs(_validation_ctx, validation_values)

        if resume_checkpoint is not None and skip_missing_input_validation:
            resume_state = initialize_state(graph, normalized_values, checkpoint=resume_checkpoint)
            scope = compute_execution_scope(graph)
            ready_nodes = get_ready_nodes(
                graph,
                resume_state,
                active_nodes=scope.active_nodes,
                startup_predecessors=scope.startup_predecessors,
            )
            if not ready_nodes:
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
                            + ". The restored checkpoint state does not make any pending nodes runnable."
                        ),
                    )

        max_iter = max_iterations or self.default_max_iterations
        effective_show_progress = show_progress if show_progress is not None else getattr(self, "_show_progress", False)
        if effective_show_progress:
            from hypergraph.runners._shared.helpers import ensure_progress_processor

            event_processors = ensure_progress_processor(event_processors)
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
                forked_from=forked_from,
                fork_superstep=fork_superstep,
                retry_of=retry_of,
                retry_index=retry_index,
                config=run_config,
            )

        # Step buffer for "exit" durability — records are flushed after run completes
        step_buffer: list[Any] = []

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
                _complete_on_stop=_complete_on_stop,
                item_index=_item_index,
            )
            output_values = filter_outputs(state, graph, select, on_missing)
            total_duration_ms = (time.time() - start_time) * 1000
            was_stopped = getattr(state, "_stopped", False)
            status = RunStatus.STOPPED if was_stopped else RunStatus.COMPLETED

            # Emit StopRequestedEvent if stopped
            if was_stopped and dispatcher.active:
                from hypergraph.events.types import StopRequestedEvent

                stop_info = getattr(state, "_stop_info", None)
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

            result = RunResult(
                values=output_values,
                status=status,
                run_id=run_id,
                workflow_id=workflow_id,
                log=collector.build(graph.name, run_id, total_duration_ms),
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
                step_count = step_offset + len(collector._records)
                error_count = sum(1 for r in collector._records if r.status == "failed")
                await checkpointer.update_run_status(
                    workflow_id,
                    WorkflowStatus.STOPPED if status == RunStatus.STOPPED else WorkflowStatus.COMPLETED,
                    duration_ms=total_duration_ms,
                    node_count=step_count,
                    error_count=error_count,
                )
            return result
        except PauseExecution as pause:
            partial_state = getattr(pause, "_partial_state", None)
            was_stopped = getattr(pause, "_stopped", False)
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
                step_count = step_offset + len(collector._records)
                error_count = sum(1 for r in collector._records if r.status == "failed")
                await checkpointer.update_run_status(
                    workflow_id,
                    WorkflowStatus.PAUSED,
                    duration_ms=total_duration_ms,
                    node_count=step_count,
                    error_count=error_count,
                )
            return RunResult(
                values=partial_values,
                status=RunStatus.PAUSED,
                run_id=run_id,
                workflow_id=workflow_id,
                pause=pause.pause_info,
                log=collector.build(graph.name, run_id, total_duration_ms),
            )
        except Exception as e:
            error = e
            partial_state = getattr(e, "_partial_state", None)
            if isinstance(e, ExecutionError):
                error = e.__cause__ or e
                partial_state = e.partial_state

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

            if has_checkpointer:
                from hypergraph.checkpointers.types import WorkflowStatus as _WS

                # Flush buffered steps so partial execution is preserved on failure
                for record in step_buffer:
                    await checkpointer.save_step(record)
                total_duration_ms_fail = (time.time() - start_time) * 1000
                from hypergraph.runners._shared.checkpoint_helpers import checkpoint_offsets as _cp_offsets

                _, _step_offset = _cp_offsets(resume_checkpoint)
                fail_count = _step_offset + len(collector._records)
                err_count = sum(1 for r in collector._records if r.status == "failed")
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
            return RunResult(
                values=partial_values,
                status=RunStatus.FAILED,
                run_id=run_id,
                workflow_id=workflow_id,
                error=error,
                log=collector.build(graph.name, run_id, total_duration_ms),
            )
        finally:
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
        select: str | list[str] = _UNSET_SELECT,
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
        **input_values: Any,
    ) -> MapResult:
        """Execute a graph multiple times with different inputs."""
        normalized_values = normalize_inputs(
            values,
            input_values,
            reserved_option_names=ASYNC_MAP_RESERVED_OPTION_NAMES,
        )

        # Resolve show_progress and merge processors
        effective_show_progress = show_progress if show_progress is not None else getattr(self, "_show_progress", False)
        if effective_show_progress:
            from hypergraph.runners._shared.helpers import ensure_progress_processor

            event_processors = ensure_progress_processor(event_processors)

        # One-time graph-structural validation
        validate_runner_compatibility(graph, self.capabilities)
        validate_node_types(graph, self.supported_node_types)
        validate_delegated_runners(graph, self.capabilities)
        validate_map_compatible(graph)
        _validate_error_handling(error_handling)
        _validate_workflow_id(workflow_id, _parent_run_id)
        effective_selected = resolve_runtime_selected(select, graph)
        _validate_on_missing(on_missing)
        ctx = precompute_input_validation(graph, entrypoint=entrypoint, selected=effective_selected)

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

        dispatcher = self._create_dispatcher(event_processors)
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
        completed_by_signature, completed_by_index = _index_completed_child_runs(completed_runs, workflow_id)

        existing_limiter = self._get_concurrency_limiter()
        token = self._set_concurrency_limiter(max_concurrency) if existing_limiter is None and max_concurrency is not None else None

        async def _run_map_item(idx: int, variation_inputs: dict[str, Any]) -> RunResult:
            """Execute one map variation, or restore from checkpoint if completed."""
            child_workflow_id = f"{workflow_id}/{idx}" if workflow_id else None
            item_signature = _compute_map_item_signature(variation_inputs, map_over_list, map_mode)

            # Skip completed items — restore result from checkpoint.
            restore_run_id = _claim_completed_child_run_id(
                idx=idx,
                signature=item_signature,
                by_signature=completed_by_signature,
                by_index=completed_by_index,
            )
            if restore_run_id is not None and has_checkpointer:
                state = await checkpointer.get_state(restore_run_id)
                restored_state = GraphState(values=dict(state))
                restored_values = filter_outputs(restored_state, graph, select, on_missing)
                return RunResult(
                    values=restored_values,
                    status=RunStatus.COMPLETED,
                    run_id=restore_run_id,
                    workflow_id=restore_run_id,
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
                    _run_config={_MAP_SIGNATURE_CONFIG_KEY: item_signature},
                    _item_index=idx,
                )
            except Exception as e:
                # Catch validation errors (e.g., MissingInputError) that raise
                # before run()'s execution try block
                return RunResult(
                    values={},
                    status=RunStatus.FAILED,
                    run_id=_generate_run_id(),
                    error=e,
                )

        try:
            if max_concurrency is None:
                tasks = [_run_map_item(idx, v) for idx, v in enumerate(input_variations)]
                gathered = await asyncio.gather(*tasks, return_exceptions=True)
                results: list[RunResult] = []
                for item in gathered:
                    if isinstance(item, BaseException):
                        raise item
                    results.append(item)
                if error_handling == "raise":
                    for result in results:
                        if result.status == RunStatus.FAILED:
                            raise result.error  # type: ignore[misc]
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
                        try:
                            idx, v = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return
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
                            raise result.error  # type: ignore[misc]

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
            if token is not None:
                self._reset_concurrency_limiter(token)
            if _parent_span_id is None and dispatcher.active:
                await self._shutdown_dispatcher_async(dispatcher)


def _normalize_signature_value(value: Any) -> Any:
    """Normalize map inputs into a JSON-stable structure for hashing."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _normalize_signature_value(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize_signature_value(v) for v in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_normalize_signature_value(v) for v in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        )
    return {"__type__": type(value).__name__, "__repr__": repr(value)}


def _compute_map_item_signature(
    variation_inputs: dict[str, Any],
    map_over: list[str],
    map_mode: str,
) -> str:
    """Compute a stable signature for one mapped item input payload."""
    payload = {
        "map_mode": map_mode,
        "map_over": map_over,
        "inputs": _normalize_signature_value(variation_inputs),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


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


def _index_completed_child_runs(
    child_runs: list[Any],
    workflow_id: str | None,
) -> tuple[dict[str, list[str]], dict[int, list[str]]]:
    """Index completed child runs by signature and by legacy index suffix."""
    by_signature: dict[str, list[str]] = defaultdict(list)
    by_index: dict[int, list[str]] = defaultdict(list)

    for run in child_runs:
        if isinstance(run.config, dict):
            signature = run.config.get(_MAP_SIGNATURE_CONFIG_KEY)
            if isinstance(signature, str):
                by_signature[signature].append(run.id)

        if workflow_id is None:
            continue
        suffix = run.id.removeprefix(f"{workflow_id}/")
        if suffix.isdigit():
            by_index[int(suffix)].append(run.id)

    for ids in by_signature.values():
        ids.sort()
    for ids in by_index.values():
        ids.sort()
    return by_signature, by_index


def _claim_completed_child_run_id(
    *,
    idx: int,
    signature: str,
    by_signature: dict[str, list[str]],
    by_index: dict[int, list[str]],
) -> str | None:
    """Claim a completed child run id for resume, preferring signature match."""
    by_sig = by_signature.get(signature)
    if by_sig:
        return by_sig.pop(0)

    by_idx = by_index.get(idx)
    if by_idx:
        return by_idx.pop(0)

    return None
