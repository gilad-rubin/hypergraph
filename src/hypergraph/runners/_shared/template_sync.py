"""Shared sync runner lifecycle template."""

from __future__ import annotations

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
    WorkflowAlreadyCompletedError,
    WorkflowForkError,
)
from hypergraph.runners._shared.helpers import (
    _UNSET_SELECT,
    _validate_error_handling,
    _validate_on_missing,
    _validate_workflow_id,
    filter_outputs,
    generate_map_inputs,
    generate_workflow_id,
    is_interrupt_resume_payload,
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


_MAP_SIGNATURE_CONFIG_KEY = "map_item_signature"


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
        checkpoint: Any | None = None,
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
        checkpoint: Any | None = None,
        workflow_id: str | None = None,
        override_workflow: bool = False,
        fork_from: str | None = None,
        retry_from: str | None = None,
        _parent_span_id: str | None = None,
        _parent_run_id: str | None = None,
        _validation_ctx: _InputValidationContext | None = None,
        _run_config: dict[str, Any] | None = None,
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

        if self._checkpointer is not None and _validation_ctx is None and workflow_id is None:
            workflow_id = generate_workflow_id()
        sync_cp = self._get_sync_checkpointer(workflow_id)
        if _validation_ctx is None and (fork_from is not None or retry_from is not None) and sync_cp is None:
            raise ValueError("fork_from/retry_from require a checkpointer and workflow persistence to be enabled.")
        resume_checkpoint = None
        if sync_cp is not None and _validation_ctx is None:
            if fork_from is not None and retry_from is not None:
                raise ValueError("Cannot pass both fork_from and retry_from. Choose one lineage source.")
            if checkpoint is not None and (fork_from is not None or retry_from is not None):
                raise ValueError("Cannot combine checkpoint with fork_from/retry_from. Use one forking mechanism.")
            if fork_from is not None:
                workflow_id, resume_checkpoint = sync_cp.fork_workflow(fork_from, workflow_id=workflow_id)
                checkpoint = resume_checkpoint
            elif retry_from is not None:
                workflow_id, resume_checkpoint = sync_cp.retry_workflow(retry_from, workflow_id=workflow_id)
                checkpoint = resume_checkpoint

            existing_run = sync_cp.get_run(workflow_id)
            graph_hash = graph.structural_hash
            if checkpoint is not None:
                if existing_run is not None:
                    raise WorkflowForkError(f"Cannot fork into existing workflow '{workflow_id}'. Use a new workflow_id.")
                resume_checkpoint = checkpoint
            elif existing_run is not None:
                if override_workflow:
                    # Ergonomic shortcut: same workflow_id + override => auto-fork.
                    workflow_id, resume_checkpoint = sync_cp.fork_workflow(workflow_id)
                    checkpoint = resume_checkpoint
                else:
                    previous_hash = (existing_run.config or {}).get("graph_struct_hash")
                    if previous_hash is not None and previous_hash != graph_hash:
                        raise GraphChangedError(workflow_id)
                    if normalized_values and not is_interrupt_resume_payload(graph, normalized_values):
                        raise InputOverrideRequiresForkError(workflow_id)
                    if existing_run.status.value == "completed":
                        raise WorkflowAlreadyCompletedError(workflow_id)
                    resume_checkpoint = sync_cp.checkpoint(workflow_id)

        forked_from: str | None = None
        fork_superstep: int | None = None
        retry_of: str | None = None
        retry_index: int | None = None
        if checkpoint is not None and resume_checkpoint is not None:
            forked_from = getattr(resume_checkpoint, "source_run_id", None)
            fork_superstep = getattr(resume_checkpoint, "source_superstep", None)
            retry_of = getattr(resume_checkpoint, "retry_of", None)
            retry_index = getattr(resume_checkpoint, "retry_index", None)

        validation_values = normalized_values
        if resume_checkpoint is not None:
            # Validate only canonical graph inputs from checkpoint state.
            # Internal produced values are not user inputs in the canonical model.
            validation_values = dict(normalized_values)
            for input_name in graph.inputs.all:
                if input_name not in validation_values and input_name in resume_checkpoint.values:
                    validation_values[input_name] = resume_checkpoint.values[input_name]
                elif input_name not in validation_values and any(
                    input_name in (step.input_versions or {}) for step in getattr(resume_checkpoint, "steps", ())
                ):
                    # Resume checkpoints may omit original graph inputs from
                    # checkpoint.values once downstream state is sufficient to
                    # continue. Presence in prior step inputs is enough to
                    # satisfy canonical input validation for resume/retry.
                    validation_values[input_name] = None

        # Value validation (after merge so checkpoint-provided params are visible)
        if _validation_ctx is None:
            effective_selected = resolve_runtime_selected(select, graph)
            validate_inputs(
                graph,
                validation_values,
                entrypoint=entrypoint,
                selected=effective_selected,
                on_internal_override=on_internal_override,
            )
        else:
            validate_item_inputs(_validation_ctx, validation_values, on_internal_override=on_internal_override)

        max_iter = max_iterations or self.default_max_iterations
        collector = RunLogCollector()
        all_processors = [collector] + (event_processors or [])
        dispatcher = self._create_dispatcher(all_processors)
        run_id, run_span_id = self._emit_run_start_sync(dispatcher, graph, _parent_span_id)
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
                forked_from=forked_from,
                fork_superstep=fork_superstep,
                retry_of=retry_of,
                retry_index=retry_index,
                config=run_config,
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
                checkpoint=resume_checkpoint,
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
                from hypergraph.runners._shared.checkpoint_helpers import checkpoint_offsets

                _, _step_offset = checkpoint_offsets(resume_checkpoint)
                _flush_and_complete(sync_cp, workflow_id, step_buffer, collector, total_duration_ms, step_offset=_step_offset)
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
                from hypergraph.runners._shared.checkpoint_helpers import checkpoint_offsets as _cp_offsets

                _, _step_off = _cp_offsets(resume_checkpoint)
                _flush_and_fail(sync_cp, workflow_id, step_buffer, collector, start_time, step_offset=_step_off)

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
                config={
                    "graph_struct_hash": graph.structural_hash,
                    "graph_code_hash": graph.code_hash,
                },
            )

        # Resume: find completed child runs to skip by stable input signature.
        completed_runs = _get_completed_child_runs_sync(sync_cp, workflow_id)
        completed_by_signature, completed_by_index = _index_completed_child_runs_sync(completed_runs, workflow_id)

        try:
            results = []
            for idx, variation_inputs in enumerate(input_variations):
                child_workflow_id = f"{workflow_id}/{idx}" if workflow_id else None
                item_signature = _compute_map_item_signature_sync(variation_inputs, map_over_list, map_mode)

                # Skip completed items — restore result from checkpoint
                restore_run_id = _claim_completed_child_run_id_sync(
                    idx=idx,
                    signature=item_signature,
                    by_signature=completed_by_signature,
                    by_index=completed_by_index,
                )
                if restore_run_id is not None and sync_cp is not None:
                    state = sync_cp.state(restore_run_id)
                    restored_state = GraphState(values=dict(state))
                    restored_values = filter_outputs(restored_state, graph, select, on_missing)
                    results.append(
                        RunResult(
                            values=restored_values,
                            status=RunStatus.COMPLETED,
                            run_id=restore_run_id,
                            workflow_id=restore_run_id,
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
                    _run_config={_MAP_SIGNATURE_CONFIG_KEY: item_signature},
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


def _flush_and_complete(
    sync_cp: Any, workflow_id: str, step_buffer: list, collector: RunLogCollector, total_duration_ms: float, *, step_offset: int = 0
) -> None:
    """Flush buffered steps and mark run completed."""
    for record in step_buffer:
        sync_cp.save_step_sync(record)
    from hypergraph.checkpointers.types import WorkflowStatus

    step_count = step_offset + len(collector._records)
    error_count = sum(1 for r in collector._records if r.status == "failed")
    sync_cp.update_run_status_sync(
        workflow_id,
        WorkflowStatus.COMPLETED,
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
    fail_count = step_offset + len(collector._records)
    err_count = sum(1 for r in collector._records if r.status == "failed")
    sync_cp.update_run_status_sync(
        workflow_id,
        _WS.FAILED,
        duration_ms=total_ms,
        node_count=fail_count,
        error_count=err_count,
    )


def _normalize_signature_value_sync(value: Any) -> Any:
    """Normalize map inputs into a JSON-stable structure for hashing."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _normalize_signature_value_sync(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize_signature_value_sync(v) for v in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_normalize_signature_value_sync(v) for v in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        )
    return {"__type__": type(value).__name__, "__repr__": repr(value)}


def _compute_map_item_signature_sync(
    variation_inputs: dict[str, Any],
    map_over: list[str],
    map_mode: str,
) -> str:
    """Compute a stable signature for one mapped item input payload."""
    payload = {
        "map_mode": map_mode,
        "map_over": map_over,
        "inputs": _normalize_signature_value_sync(variation_inputs),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


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


def _index_completed_child_runs_sync(
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


def _claim_completed_child_run_id_sync(
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
