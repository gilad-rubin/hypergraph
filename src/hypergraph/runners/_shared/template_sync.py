"""Shared sync runner lifecycle template."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

from hypergraph.exceptions import ExecutionError
from hypergraph.runners._shared.helpers import (
    _UNSET_SELECT,
    _validate_error_handling,
    _validate_on_missing,
    _validate_workflow_id,
    filter_outputs,
    generate_map_inputs,
)
from hypergraph.runners._shared.input_normalization import (
    ASYNC_MAP_RESERVED_OPTION_NAMES,
    ASYNC_RUN_RESERVED_OPTION_NAMES,
    normalize_inputs,
)
from hypergraph.runners._shared.run_log import RunLogCollector
from hypergraph.runners._shared.types import ErrorHandling, GraphState, MapResult, RunResult, RunStatus
from hypergraph.runners._shared.validation import (
    precompute_input_validation,
    resolve_runtime_selected,
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
        step_buffer: list[Any] | None = None,
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
        is_map: bool = False,
        map_size: int | None = None,
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
        error: BaseException | None = None,
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
        select: str | list[str] = _UNSET_SELECT,
        on_missing: Literal["ignore", "warn", "error"] = "ignore",
        on_internal_override: Literal["ignore", "warn", "error"] = "warn",
        entrypoint: str | None = None,
        max_iterations: int | None = None,
        error_handling: ErrorHandling = "raise",
        event_processors: list[EventProcessor] | None = None,
        workflow_id: str | None = None,
        _parent_span_id: str | None = None,
        _parent_run_id: str | None = None,
        _validation_ctx: _InputValidationContext | None = None,
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
            _validate_on_missing(on_missing)
            _validate_error_handling(error_handling)
            _validate_workflow_id(workflow_id, _parent_run_id)

        # Checkpoint resume: load prior state and merge graph-input values.
        # Only merge values the graph expects as inputs (required, optional, seeds) —
        # intermediate edge-produced values are NOT merged (they'll be re-computed).
        sync_cp = self._get_sync_checkpointer(workflow_id)
        if sync_cp is not None:
            checkpoint_state = sync_cp.state(workflow_id)
            if checkpoint_state:
                graph_input_names = set(graph.inputs.all)
                checkpoint_inputs = {k: v for k, v in checkpoint_state.items() if k in graph_input_names}
                if checkpoint_inputs:
                    normalized_values = {**checkpoint_inputs, **normalized_values}

        # Value validation (after merge so checkpoint-provided params are visible)
        if _validation_ctx is None:
            effective_selected = resolve_runtime_selected(select, graph)
            validate_inputs(
                graph,
                normalized_values,
                entrypoint=entrypoint,
                selected=effective_selected,
                on_internal_override=on_internal_override,
            )
        else:
            validate_item_inputs(_validation_ctx, normalized_values, on_internal_override=on_internal_override)

        max_iter = max_iterations or self.default_max_iterations
        collector = RunLogCollector()
        all_processors = [collector] + (event_processors or [])
        dispatcher = self._create_dispatcher(all_processors)
        run_id, run_span_id = self._emit_run_start_sync(dispatcher, graph, _parent_span_id)
        start_time = time.time()

        # Checkpointer lifecycle — upsert run record
        if sync_cp is not None:
            sync_cp.create_run_sync(
                workflow_id,
                graph_name=graph.name,
                parent_run_id=_parent_run_id,
            )

        step_buffer: list[Any] = []

        try:
            state = self._execute_graph_impl(
                graph,
                normalized_values,
                max_iter,
                dispatcher=dispatcher,
                run_id=run_id,
                run_span_id=run_span_id,
                event_processors=event_processors,
                workflow_id=workflow_id,
                step_buffer=step_buffer,
            )
            output_values = filter_outputs(state, graph, select, on_missing)
            total_duration_ms = (time.time() - start_time) * 1000
            result = RunResult(
                values=output_values,
                status=RunStatus.COMPLETED,
                run_id=run_id,
                workflow_id=workflow_id,
                log=collector.build(graph.name, run_id, total_duration_ms),
            )
            self._emit_run_end_sync(
                dispatcher,
                run_id,
                run_span_id,
                graph,
                start_time,
                _parent_span_id,
            )
            # Flush buffered steps and mark run completed
            if sync_cp is not None:
                _flush_and_complete(sync_cp, workflow_id, step_buffer, collector, total_duration_ms)
            return result
        except Exception as e:
            error = e
            partial_state = getattr(e, "_partial_state", None)
            if isinstance(e, ExecutionError):
                error = e.__cause__ or e
                partial_state = e.partial_state

            self._emit_run_end_sync(
                dispatcher,
                run_id,
                run_span_id,
                graph,
                start_time,
                _parent_span_id,
                error=error,
            )

            # Flush buffered steps and mark run failed
            if sync_cp is not None:
                _flush_and_fail(sync_cp, workflow_id, step_buffer, collector, start_time)

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
                self._shutdown_dispatcher_sync(dispatcher)

    def map(
        self,
        graph: Graph,
        values: dict[str, Any] | None = None,
        *,
        map_over: str | list[str],
        map_mode: Literal["zip", "product"] = "zip",
        clone: bool | list[str] = False,
        select: str | list[str] = _UNSET_SELECT,
        on_missing: Literal["ignore", "warn", "error"] = "ignore",
        on_internal_override: Literal["ignore", "warn", "error"] = "warn",
        entrypoint: str | None = None,
        error_handling: ErrorHandling = "raise",
        event_processors: list[EventProcessor] | None = None,
        workflow_id: str | None = None,
        _parent_span_id: str | None = None,
        _parent_run_id: str | None = None,
        **input_values: Any,
    ) -> MapResult:
        """Execute a graph multiple times with different inputs."""
        normalized_values = normalize_inputs(
            values,
            input_values,
            reserved_option_names=ASYNC_MAP_RESERVED_OPTION_NAMES,
        )

        # One-time graph-structural validation
        validate_runner_compatibility(graph, self.capabilities)
        validate_node_types(graph, self.supported_node_types)
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

        dispatcher = self._create_dispatcher(event_processors)
        map_run_id, map_span_id = self._emit_run_start_sync(
            dispatcher,
            graph,
            _parent_span_id,
            is_map=True,
            map_size=len(input_variations),
        )
        start_time = time.time()

        # Create parent batch run if checkpointing
        sync_cp = self._get_sync_checkpointer(workflow_id)
        if sync_cp is not None:
            sync_cp.create_run_sync(
                workflow_id,
                graph_name=graph.name,
                parent_run_id=_parent_run_id,
            )

        # Resume: find completed child runs to skip
        completed_indices = _get_completed_child_indices_sync(sync_cp, workflow_id)

        try:
            results = []
            for idx, variation_inputs in enumerate(input_variations):
                child_workflow_id = f"{workflow_id}/{idx}" if workflow_id else None

                # Skip completed items — restore result from checkpoint
                if idx in completed_indices and sync_cp is not None:
                    state = sync_cp.state(child_workflow_id)
                    results.append(
                        RunResult(
                            values=state,
                            status=RunStatus.COMPLETED,
                            workflow_id=child_workflow_id,
                        )
                    )
                    continue

                result = self.run(
                    graph,
                    variation_inputs,
                    select=select,
                    on_missing=on_missing,
                    on_internal_override=on_internal_override,
                    entrypoint=entrypoint,
                    error_handling="continue",
                    event_processors=event_processors,
                    workflow_id=child_workflow_id,
                    _parent_span_id=map_span_id,
                    _parent_run_id=workflow_id,
                    _validation_ctx=ctx,
                )
                results.append(result)
                if error_handling == "raise" and result.status == RunStatus.FAILED:
                    raise result.error  # type: ignore[misc]

            self._emit_run_end_sync(
                dispatcher,
                map_run_id,
                map_span_id,
                graph,
                start_time,
                _parent_span_id,
            )
            total_duration_ms = (time.time() - start_time) * 1000

            # Complete parent batch run
            if sync_cp is not None:
                from hypergraph.checkpointers.types import WorkflowStatus

                error_count = sum(1 for r in results if r.status == RunStatus.FAILED)
                sync_cp.update_run_status_sync(
                    workflow_id,
                    WorkflowStatus.COMPLETED,
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

                total_ms = (time.time() - start_time) * 1000
                error_count = sum(1 for r in results if r.status == RunStatus.FAILED)
                sync_cp.update_run_status_sync(
                    workflow_id,
                    _WS.FAILED,
                    duration_ms=total_ms,
                    node_count=len(results),
                    error_count=error_count,
                )
            raise
        finally:
            if _parent_span_id is None and dispatcher.active:
                self._shutdown_dispatcher_sync(dispatcher)


def _flush_and_complete(sync_cp: Any, workflow_id: str, step_buffer: list, collector: RunLogCollector, total_duration_ms: float) -> None:
    """Flush buffered steps and mark run completed."""
    for record in step_buffer:
        sync_cp.save_step_sync(record)
    from hypergraph.checkpointers.types import WorkflowStatus

    step_count = len(collector._records)
    error_count = sum(1 for r in collector._records if r.status == "failed")
    sync_cp.update_run_status_sync(
        workflow_id,
        WorkflowStatus.COMPLETED,
        duration_ms=total_duration_ms,
        node_count=step_count,
        error_count=error_count,
    )


def _flush_and_fail(sync_cp: Any, workflow_id: str, step_buffer: list, collector: RunLogCollector, start_time: float) -> None:
    """Flush buffered steps and mark run failed."""
    for record in step_buffer:
        sync_cp.save_step_sync(record)
    from hypergraph.checkpointers.types import WorkflowStatus as _WS

    total_ms = (time.time() - start_time) * 1000
    fail_count = len(collector._records)
    err_count = sum(1 for r in collector._records if r.status == "failed")
    sync_cp.update_run_status_sync(
        workflow_id,
        _WS.FAILED,
        duration_ms=total_ms,
        node_count=fail_count,
        error_count=err_count,
    )


def _get_completed_child_indices_sync(
    sync_cp: Any,
    workflow_id: str | None,
) -> set[int]:
    """Query checkpoint for completed child runs and return their indices (sync).

    Only COMPLETED items are skipped — FAILED and ACTIVE items are re-executed.
    """
    if sync_cp is None or workflow_id is None:
        return set()

    from hypergraph.checkpointers.types import WorkflowStatus

    child_runs = sync_cp.runs(parent_run_id=workflow_id)
    completed: set[int] = set()
    for run in child_runs:
        if run.status != WorkflowStatus.COMPLETED:
            continue
        suffix = run.id.removeprefix(f"{workflow_id}/")
        if suffix.isdigit():
            completed.add(int(suffix))
    return completed
