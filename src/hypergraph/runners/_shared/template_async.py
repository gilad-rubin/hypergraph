"""Shared async runner lifecycle template."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from hypergraph.checkpointers.types import RunTotals, StepStatus
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
    validate_map_parent_identity,
    validate_restorable_history,
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
from hypergraph.runners._shared.policy_manifest import RETRY_POLICY_CONFIG_KEY, RetryPolicyManifest
from hypergraph.runners._shared.readiness import find_missing_resume_seed_inputs
from hypergraph.runners._shared.results import (
    ErrorHandling,
    MapResult,
    PauseInfo,
    RunResult,
    RunStatus,
    build_failed_run_result,
    build_paused_run_result,
    build_pre_run_failed_result,
    build_restored_run_result,
    build_terminal_run_result,
)
from hypergraph.runners._shared.run_log import RunLogCollector
from hypergraph.runners._shared.run_teardown import AsyncRunTeardown, InspectionSettlement
from hypergraph.runners._shared.scheduling import compute_execution_scope
from hypergraph.runners._shared.state import CheckpointErrorSink, GraphState, PauseExecution
from hypergraph.runners._shared.state_restore import (
    generate_workflow_id,
    initialize_state,
    validate_workflow_id,
)
from hypergraph.runners._shared.stop import (
    _WorkflowReservation,
    get_stop_signal,
)
from hypergraph.runners._shared.validation import (
    precompute_input_validation,
    resolve_runtime_selected,
    validate_delegated_runners,
    validate_item_inputs,
    validate_max_concurrency,
    validate_node_types,
    validate_runner_compatibility,
)
from hypergraph.runners._shared.value_resolution import (
    build_resume_validation_values,
    collect_inputs_for_node,
    start_inputs_for_run,
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


# sync:skip-start: fan-out budgets — a sync map runs items one at a time, so neither cap exists there
MAX_UNBOUNDED_MAP_TASKS = 10_000
# Default streaming concurrency for map_iter when the caller doesn't pass one.
# Unlike map(), map_iter is bounded by design, so an absent limit means a modest
# bounded pool (tune via max_concurrency), never unbounded fan-out.
_DEFAULT_STREAM_CONCURRENCY = 16


# sync:skip-end
class AsyncRunnerTemplate(BaseRunner, ABC):
    """Template implementation for async run/map lifecycle."""

    _accepts_checkpoint_error_sink: ClassVar[Literal[True]] = True  # sync:skip: only the async engine saves steps in the background

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
        max_concurrency: int | None,  # sync:skip: a sync engine has no concurrency budget to spend
        *,
        dispatcher: EventDispatcher,
        run_id: str,
        run_span_id: str,
        event_processors: list[EventProcessor] | None = None,
        workflow_id: str | None = None,
        checkpoint: Checkpoint | None = None,
        step_buffer: list[Any] | None = None,
        checkpoint_save_errors: list[str] | None = None,  # sync:skip: only background step-saves can fail out of band
        _complete_on_stop: bool = False,
        item_index: int | None = None,
    ) -> GraphState:
        # sync:only-start: the sink the paragraph below documents does not exist in the sync signature
        # """Execute graph and return final state."""
        # sync:only-end
        # sync:skip-start: same, the async docstring documents an async-only parameter
        """Execute graph and return final state.

        ``checkpoint_save_errors`` is a caller-owned sink: implementations
        append string reprs of background step-save failures (durability
        "async") so the template can surface them on the RunResult.
        """
        # sync:skip-end
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
        status: RunStatus | None = None,
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

    # sync:skip-start: the shared limiter is an asyncio.Semaphore; sequential sync execution needs no budget
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

    # sync:skip-end
    # sync:only-start: a sync run must never require an event loop, so it needs the checkpointer's sync half
    # def _get_sync_checkpointer(self, workflow_id: str | None) -> Any:
    #     """Return sync checkpointer if workflow_id is provided, else None.
    #
    #     Validates that the checkpointer supports sync writes via the
    #     SyncCheckpointerProtocol.
    #     """
    #     checkpointer = self._checkpointer
    #     if checkpointer is None or workflow_id is None:
    #         return None
    #
    #     from hypergraph.checkpointers.protocols import SyncCheckpointerProtocol
    #
    #     if not isinstance(checkpointer, SyncCheckpointerProtocol):
    #         raise TypeError(
    #             f"{type(checkpointer).__name__} does not support sync writes "
    #             f"(missing SyncCheckpointerProtocol). SyncRunner requires a checkpointer "
    #             f"that implements create_run_sync/save_step_sync/update_run_status_sync. "
    #             f"SqliteCheckpointer supports this."
    #         )
    #     return checkpointer
    #
    # sync:only-end
    async def run(
        self,
        graph: Graph,
        values: dict[str, Any] | None = None,
        *,
        select: str | list[str] = SELECT_UNSET,
        on_missing: Literal["ignore", "warn", "error"] = "ignore",
        entrypoint: str | None = None,
        max_iterations: int | None = None,
        max_concurrency: int | None = None,  # sync:skip: sync runs one node at a time; there is no budget to set
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
        _resume_seed_values: dict[str, Any] | None = None,
        _complete_on_stop: bool = False,
        _item_index: int | None = None,
        _checkpoint_error_sink: CheckpointErrorSink | None = None,  # sync:skip: nothing saves steps off the calling thread in sync
        _reservation: _WorkflowReservation | None = None,
        _inspection_session: InspectionSession | None = None,
        _inspection_transport: NotebookInspectionTransport | None = None,
        _inspection_path: tuple[str, ...] = (),
        **input_values: Any,
    ) -> RunResult:
        """Execute a graph once."""
        # sync:skip-start: runner-level max_concurrency / event_processors defaults are an AsyncRunner constructor feature
        if max_concurrency is None:
            max_concurrency = getattr(self, "_max_concurrency", None)
        if _parent_span_id is None and _parent_run_id is None:
            event_processors = [*getattr(self, "_event_processors", ()), *(event_processors or [])]
        # sync:skip-end
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
                        _runner_kind="async",
                    )
                )
            except Exception:
                inspection_transport = None
        try:
            validate_max_concurrency(max_concurrency)  # sync:skip: no max_concurrency parameter to validate
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

        # sync:only-start: acquiring the checkpointer's sync half can raise TypeError, so it happens inside a try that reports to the transport
        # try:
        #     if self._checkpointer is not None and _validation_ctx is None and workflow_id is None and fork_from is None:
        #         workflow_id = generate_workflow_id()
        #     sync_checkpointer_key = workflow_id
        #     if sync_checkpointer_key is None:
        #         sync_checkpointer_key = fork_from if fork_from is not None else retry_from
        #     sync_cp = self._get_sync_checkpointer(sync_checkpointer_key)
        #     if _validation_ctx is None and (fork_from is not None or retry_from is not None) and sync_cp is None:
        #         raise ValueError("fork_from/retry_from require a checkpointer and workflow persistence to be enabled.")
        # except BaseException as error:
        #     if inspection_transport is not None:
        #         inspection_transport.fail_to_start(error)
        #     raise
        # resume_checkpoint = None
        # resume_action = ResumeAction.START_NEW
        # skip_missing_input_validation = False
        # try:
        #     if sync_cp is not None and _validation_ctx is None:
        # sync:only-end
        # sync:skip-start: the async half needs no protocol check, so one try covers acquisition and lineage
        try:
            checkpointer = self._checkpointer
            if _validation_ctx is None and (fork_from is not None or retry_from is not None) and checkpointer is None:
                raise ValueError("fork_from/retry_from require a checkpointer and workflow persistence to be enabled.")
            resume_checkpoint = None
            resume_action = ResumeAction.START_NEW
            skip_missing_input_validation = False
            if checkpointer is not None and _validation_ctx is None:
                if workflow_id is None and fork_from is None:
                    workflow_id = generate_workflow_id()
                # sync:skip-end
                validate_lineage_request(
                    checkpoint=checkpoint,
                    fork_from=fork_from,
                    retry_from=retry_from,
                )
                candidate_checkpoint = checkpoint
                if fork_from is not None:
                    workflow_id, resume_checkpoint = await checkpointer.fork_workflow_async(
                        fork_from,
                        workflow_id=workflow_id,
                    )
                    candidate_checkpoint = resume_checkpoint
                elif retry_from is not None:
                    workflow_id, resume_checkpoint = await checkpointer.retry_workflow_async(
                        retry_from,
                        workflow_id=workflow_id,
                    )
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
                    # Retention compaction is the only writer of a baseline
                    # carrier, so retention="full" pays nothing for this gate.
                    source_run_id = resume_checkpoint.source_run_id
                    retention_policy = getattr(checkpointer, "policy", None)
                    read_raw_steps = getattr(checkpointer, "get_steps", None)
                    if source_run_id is not None and getattr(retention_policy, "retention", "full") != "full" and callable(read_raw_steps):
                        validate_restorable_history(
                            graph=graph,
                            steps=await read_raw_steps(source_run_id, superstep=resume_checkpoint.source_superstep, show_internal=True),
                            active_nodes=compute_execution_scope(graph).active_nodes,
                            workflow_id=workflow_id,
                            source_run_id=source_run_id,
                            policy=retention_policy,
                            is_retry=resume_checkpoint.retry_of is not None,
                        )
        except BaseException as error:
            if inspection_transport is not None:
                inspection_transport.fail_to_start(error)
            raise

        if resume_checkpoint is not None and _resume_seed_values:
            # GraphNode may recover parent-owned seeds when a child paused or
            # failed before producing any durable value. Keep them out of the
            # runtime payload so strict resume/interrupt lineage stays intact.
            resume_checkpoint.values = {**_resume_seed_values, **resume_checkpoint.values}

        has_checkpointer = checkpointer is not None and workflow_id is not None
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
                runner_kind="async",
            )
        if top_level_inspection and inspection_transport is not None and _inspection_session is None and inspection_session is not None:
            try:
                inspection_transport.attach(inspection_session)
            except Exception:
                inspection_transport = None
        inspection_settlement = InspectionSettlement(
            inspection_session if owns_inspection else None,
            transport=inspection_transport,
        )
        try:
            effective_show_progress = show_progress if show_progress is not None else getattr(self, "_show_progress", False)
            if effective_show_progress:
                from hypergraph.runners._shared.scheduling import ensure_progress_processor

                # The auto default inspects the merged carried + call-site view:
                # an explicit Rich processor anywhere suppresses it (issue #207).
                event_processors = ensure_progress_processor(event_processors, carried=graph.default_event_processors)
            if graph.default_event_processors:
                # Graph-carried processors merge in front of call-site ones — never
                # replace, and user-declared processors are never deduped.
                # Reassigning event_processors also forwards them into nested
                # GraphNode sub-runs, exactly like call-site processors.
                event_processors = [*graph.default_event_processors, *(event_processors or [])]
        except BaseException as error:
            inspection_settlement.abort(error)
            raise
        try:
            reservation = _reservation or self._active_workflows.reserve(workflow_id)
        except BaseException as error:
            inspection_settlement.abort(error)
            raise
        inspection_settlement.start()
        dispatcher = None
        run_row_created = False
        step_buffer: list[Any] = []
        checkpoint_save_errors: list[str] = []  # sync:skip: a sync step-save raises inline, so there is nothing to collect

        async def settle_created_run_failed() -> None:
            if not run_row_created:
                return
            from hypergraph.checkpointers.types import WorkflowStatus
            from hypergraph.runners._shared.checkpoint_helpers import checkpoint_offsets

            for record in step_buffer:
                await checkpointer.save_step(record)
            _, step_offset = checkpoint_offsets(resume_checkpoint)
            await checkpointer.update_run_status(
                workflow_id,
                WorkflowStatus.FAILED,
                duration_ms=(time.time() - start_time) * 1000,
                node_count=step_offset + collector.step_count,
                error_count=collector.failed_step_count,
            )

        teardown = AsyncRunTeardown(
            reservation,
            parent_span_id=_parent_span_id,
            shutdown_dispatcher=self._shutdown_dispatcher_async,
            settle_run_row=settle_created_run_failed,
            checkpoint_error_sink=_checkpoint_error_sink,  # sync:skip: RunTeardown has no sink to forward
            checkpoint_errors=lambda: checkpoint_save_errors,  # sync:skip: RunTeardown has no sink to forward
        )

        try:
            teardown.arm(workflow_id)
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
            if owns_inspection:
                assert inspection_session is not None
                inspection_session.bind_run(run_id, workflow_id=workflow_id)
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
            if has_checkpointer:
                run_config = {
                    "graph_struct_hash": graph.structural_hash,
                    "graph_code_hash": graph.code_hash,
                    RETRY_POLICY_CONFIG_KEY: RetryPolicyManifest.from_graph(graph).to_config_value(),
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
                    inputs=start_inputs_for_run(graph, normalized_values, resume_checkpoint),
                )
                run_row_created = True
        except BaseException as error:
            try:
                await teardown.settle_completely(dispatcher, settle_run_row=True)
            except BaseException as final_error:
                inspection_settlement.abort(final_error)
                raise
            inspection_settlement.abort(error)
            raise

        terminal_error: BaseException | None = None
        try:
            with inspection_scope(inspection_session, _inspection_path):
                state = await self._execute_graph_impl_async(
                    graph,
                    normalized_values,
                    max_iter,
                    max_concurrency,  # sync:skip: the sync engine takes no concurrency budget
                    dispatcher=dispatcher,
                    run_id=run_id,
                    run_span_id=run_span_id,
                    event_processors=event_processors,
                    workflow_id=workflow_id,
                    checkpoint=resume_checkpoint,
                    step_buffer=step_buffer,
                    checkpoint_save_errors=checkpoint_save_errors,  # sync:skip: the sync engine has no background saves to report
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

            await self._emit_run_end_async(
                dispatcher,
                run_id,
                run_span_id,
                graph,
                start_time,
                _parent_span_id,
                context=run_context,
                status=status,
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
            try:
                dispatcher = await teardown.settle(dispatcher)
            except BaseException as final_error:
                terminal_error = final_error
                raise
            inspection = inspection_settlement.publish(
                status=status.value,
                total_duration_ms=total_duration_ms,
            )
            return build_terminal_run_result(
                values=output_values,
                status=status,
                run_id=run_id,
                workflow_id=workflow_id,
                log=collector.build(graph.name, run_id, total_duration_ms),
                checkpoint_errors=checkpoint_save_errors,  # sync:skip: no background saves, so no errors to surface
                inspection=inspection,
            )
        except PauseExecution as pause:
            partial_state = pause.partial_state
            partial_values = filter_outputs(partial_state, graph, select) if partial_state is not None else {}
            total_duration_ms = (time.time() - start_time) * 1000
            try:
                _validate_pause_options_have_routes(graph, pause.pause_info, partial_state)  # sync:skip: no sync interrupt executor exists to pause
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
                            response_param=pause.pause_info.response_key,
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
                        status=RunStatus.PAUSED,
                    )
                if has_checkpointer:
                    from hypergraph.runners._shared.checkpoint_helpers import checkpoint_offsets
                    from hypergraph.runners._shared.pause_slots import commit_pause_async

                    _, step_offset = checkpoint_offsets(resume_checkpoint)
                    step_count = step_offset + collector.step_count
                    error_count = collector.failed_step_count
                    # The durable pause slot, whatever step records are still
                    # buffered, and the PAUSED transition commit as ONE unit,
                    # and the paused step is never written after the slot: no
                    # reader ever sees a committed paused run whose question
                    # is missing (PRD 0010). The generated sync template commits
                    # the same way; no shipped sync runner declares
                    # ``supports_interrupts`` yet, so that half stays
                    # unexercised in-tree, and generation is what keeps it from
                    # drifting when one does.
                    await commit_pause_async(
                        checkpointer,
                        graph,
                        workflow_id,
                        pause,
                        step_buffer,
                        RunTotals(total_duration_ms, step_count, error_count),
                    )
                dispatcher = await teardown.settle(dispatcher)
            except BaseException as final_error:
                terminal_error = final_error
                raise
            inspection = inspection_settlement.publish(
                status=RunStatus.PAUSED.value,
                total_duration_ms=total_duration_ms,
            )
            return build_paused_run_result(
                values=partial_values,
                run_id=run_id,
                workflow_id=workflow_id,
                pause=pause.pause_info,
                log=collector.build(graph.name, run_id, total_duration_ms),
                checkpoint_errors=checkpoint_save_errors,  # sync:skip: no background saves, so no errors to surface
                inspection=inspection,
            )
        except Exception as e:
            if e is terminal_error:
                raise
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
                dispatcher = await teardown.settle(dispatcher)
            except BaseException as final_error:
                terminal_error = final_error
                raise

            total_duration_ms = (time.time() - start_time) * 1000
            inspection = inspection_settlement.publish(
                status=RunStatus.FAILED.value,
                total_duration_ms=total_duration_ms,
                failures=tuple(node_failures),
                error=error,
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
                checkpoint_errors=checkpoint_save_errors,  # sync:skip: no background saves, so no errors to surface
                inspection=inspection,
            )
        except BaseException as error:
            terminal_error = error
            raise
        finally:
            try:
                await teardown.settle_completely(dispatcher, settle_run_row=terminal_error is not None)
            except BaseException as final_error:
                inspection_settlement.abort(final_error)
                raise
            if terminal_error is not None:
                inspection_settlement.abort(terminal_error)

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
        max_concurrency: int | None = None,  # sync:skip: sync map is sequential; there is no budget to set
        inspect: bool = False,
        error_handling: ErrorHandling = "raise",
        event_processors: list[EventProcessor] | None = None,
        show_progress: bool | None = None,
        workflow_id: str | None = None,
        _parent_span_id: str | None = None,
        _parent_run_id: str | None = None,
        _item_index: int | None = None,
        _checkpoint_error_sink: CheckpointErrorSink | None = None,  # sync:skip: nothing saves steps off the calling thread in sync
        _reservation: _WorkflowReservation | None = None,
        _inspection_session: InspectionSession | None = None,
        _inspection_transport: NotebookInspectionTransport | None = None,
        _inspection_path: tuple[str, ...] = (),
        **input_values: Any,
    ) -> MapResult:
        """Execute a graph multiple times with different inputs."""
        # sync:skip-start: runner-level max_concurrency / event_processors defaults are an AsyncRunner constructor feature
        if max_concurrency is None:
            max_concurrency = getattr(self, "_max_concurrency", None)
        if _parent_span_id is None and _parent_run_id is None:
            event_processors = [*getattr(self, "_event_processors", ()), *(event_processors or [])]
        # sync:skip-end
        if not isinstance(inspect, bool):
            raise TypeError(
                f"inspect must be a bool, got {type(inspect).__name__}.\n\n"
                "How to fix: Pass inspect=True to capture map item values or "
                "inspect=False to keep only always-on batch facts."
            )
        owns_inspection = inspect and _inspection_session is None
        top_level_inspection = owns_inspection and _parent_span_id is None and _parent_run_id is None and _item_index is None
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
                        _runner_kind="async",
                    )
                )
            except Exception:
                inspection_transport = None
        try:
            validate_max_concurrency(max_concurrency)  # sync:skip: no max_concurrency parameter to validate
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

                # Inspect the merged carried + call-site view before synthesizing
                # a default; only the call-site list is returned — the map
                # dispatcher below merges carried processors itself (issue #207).
                event_processors = ensure_progress_processor(event_processors, carried=graph.default_event_processors)

            # One-time graph-structural validation
            validate_runner_compatibility(graph, self.capabilities)
            validate_node_types(graph, self.supported_node_types)
            validate_delegated_runners(graph, self.capabilities)
            # sync:only-start: the protocol check can raise TypeError, which belongs to this pre-flight try
            # sync_cp = self._get_sync_checkpointer(workflow_id)
            # sync:only-end
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
                runner_kind="async",
            )
            if owns_inspection
            else None
        )
        if map_inspection_session is not None and top_level_inspection:
            try:
                if inspection_transport is not None:
                    inspection_transport.attach(map_inspection_session)
            except Exception:
                inspection_transport = None
        inspection_settlement = InspectionSettlement(
            map_inspection_session,
            transport=inspection_transport,
        )
        inspection_settlement.start()
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
                    _inspection=inspection_settlement.publish(
                        status=map_result.status.value,
                        total_duration_ms=0.0,
                    ),
                )
            return map_result
        # sync:skip-start: nothing fans out in a sync map, so there is no unbounded fan-out to cap and no background saves to collect
        if max_concurrency is None and len(input_variations) > MAX_UNBOUNDED_MAP_TASKS:
            error = ValueError(
                f"Too many map tasks without a concurrency limit: {len(input_variations)}. "
                f"Set max_concurrency or keep inputs at <= {MAX_UNBOUNDED_MAP_TASKS}."
            )
            inspection_settlement.abort(error, unstarted_item_indexes=tuple(range(len(input_variations))))
            raise error
        item_checkpoint_errors: list[list[str]] = [[] for _ in input_variations]
        # sync:skip-end

        try:
            reservation = _reservation or self._active_workflows.reserve(workflow_id)
        except BaseException as error:
            inspection_settlement.abort(error, unstarted_item_indexes=tuple(range(len(input_variations))))
            raise
        dispatcher = None
        checkpointer = self._checkpointer  # sync:skip: the sync half was acquired in the pre-flight try above
        has_checkpointer = checkpointer is not None and workflow_id is not None
        parent_run_row_created = False
        claimed_indexes: set[int] = set()
        results: list[RunResult] = []

        async def settle_created_parent_run_failed() -> None:
            if not parent_run_row_created:
                return
            from hypergraph.checkpointers.types import WorkflowStatus

            error_count = sum(1 for result in results if result.status == RunStatus.FAILED)
            await checkpointer.update_run_status(
                workflow_id,
                WorkflowStatus.FAILED,
                duration_ms=(time.time() - start_time) * 1000,
                node_count=len(results),
                error_count=error_count,
            )

        teardown = AsyncRunTeardown(
            reservation,
            parent_span_id=_parent_span_id,
            shutdown_dispatcher=self._shutdown_dispatcher_async,
            settle_run_row=settle_created_parent_run_failed,
            checkpoint_error_sink=_checkpoint_error_sink,  # sync:skip: RunTeardown has no sink to forward
            checkpoint_errors=lambda: [message for messages in item_checkpoint_errors for message in messages],  # sync:skip: same
            release_concurrency_limiter=self._reset_concurrency_limiter,  # sync:skip: no limiter was installed
        )

        try:
            teardown.arm(workflow_id)

            # Parent-boundary identity gate (#309). The parent run row is upserted
            # below, so the stored graph/policy evidence has to be read and judged
            # HERE — before any run event, any item, any rewrite.
            if has_checkpointer:
                validate_map_parent_identity(
                    existing_run=await checkpointer.get_run_async(workflow_id),
                    workflow_id=workflow_id,
                    graph_hash=graph.structural_hash,
                    graph=graph,
                )

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
            if map_inspection_session is not None:
                map_inspection_session.bind_run(map_run_id)
            start_time = time.time()

            # Create parent batch run if checkpointing
            if has_checkpointer:
                await checkpointer.create_run(
                    workflow_id,
                    graph_name=graph.name,
                    parent_run_id=_parent_run_id,
                    config={
                        "graph_struct_hash": graph.structural_hash,
                        "graph_code_hash": graph.code_hash,
                        RETRY_POLICY_CONFIG_KEY: RetryPolicyManifest.from_graph(graph).to_config_value(),
                    },
                )
                parent_run_row_created = True

            # Resume: find completed child runs to skip by stable input signature.
            completed_runs = await _get_completed_child_runs(checkpointer, workflow_id)
            completed_by_signature, completed_legacy_by_index = index_completed_child_runs(completed_runs, workflow_id)

            # sync:skip-start: the shared limiter is a budget for concurrent items; sync has none to install
            existing_limiter = self._get_concurrency_limiter()
            teardown.adopt_limiter(
                self._set_concurrency_limiter(max_concurrency) if existing_limiter is None and max_concurrency is not None else None
            )
            # sync:skip-end
            map_stop_signal = get_stop_signal()
        except BaseException as error:
            try:
                await teardown.settle_completely(dispatcher, settle_run_row=True)
            except BaseException as final_error:
                inspection_settlement.abort(final_error, unstarted_item_indexes=tuple(range(len(input_variations))))
                raise
            inspection_settlement.abort(error, unstarted_item_indexes=tuple(range(len(input_variations))))
            raise

        async def _run_map_item(idx: int, variation_inputs: dict[str, Any]) -> RunResult:
            """Execute one map variation, or restore from checkpoint if completed."""
            claimed_indexes.add(idx)
            child_workflow_id = f"{workflow_id}/{idx}" if workflow_id else None
            child_inspection_session = (
                map_inspection_session.claim_item(
                    item_index=idx,
                    requested_inputs=variation_inputs,
                    workflow_id=child_workflow_id,
                )
                if map_inspection_session is not None
                else _inspection_session
            )
            item_signature = compute_map_item_signature(variation_inputs, map_over_list, map_mode) if has_checkpointer else None

            # Skip completed items — restore result from checkpoint.
            restore_run_id = (
                claim_completed_child_run_id(
                    idx=idx,
                    signature=item_signature,
                    by_signature=completed_by_signature,
                    legacy_by_index=completed_legacy_by_index,
                )
                if item_signature is not None
                else None
            )
            if restore_run_id is not None and has_checkpointer:
                state = await checkpointer.get_state(restore_run_id)
                restored_state = GraphState(values=dict(state))
                restored_values = filter_outputs(restored_state, graph, select, on_missing)
                result = build_restored_run_result(
                    values=restored_values,
                    graph_name=graph.name or "",
                    run_id=restore_run_id,
                )
                if map_inspection_session is not None:
                    map_inspection_session.settle_item(
                        item_index=idx,
                        result=result,
                    )
                return result

            try:
                result = await self.run(
                    graph,
                    variation_inputs,
                    select=select,
                    on_missing=on_missing,
                    entrypoint=entrypoint,
                    max_concurrency=max_concurrency,  # sync:skip: run() takes no concurrency budget in sync
                    inspect=owns_inspection,
                    error_handling="continue",
                    event_processors=event_processors,
                    show_progress=False,
                    workflow_id=child_workflow_id,
                    _parent_span_id=map_span_id,
                    _parent_run_id=workflow_id,
                    _validation_ctx=ctx,
                    _run_config=({MAP_SIGNATURE_CONFIG_KEY: item_signature} if item_signature is not None else None),
                    _item_index=idx,
                    # sync:skip-start: no background saves per item, so there is nothing to route to a sink
                    _checkpoint_error_sink=(item_checkpoint_errors[idx].append if _checkpoint_error_sink is not None else None),
                    # sync:skip-end
                    _inspection_session=child_inspection_session,
                    _inspection_path=_inspection_path,
                )
            except Exception as e:
                # Catch validation errors (e.g., MissingInputError) that raise
                # before run()'s execution try block
                result = build_pre_run_failed_result(e)
            if map_inspection_session is not None:
                map_inspection_session.settle_item(
                    item_index=idx,
                    result=result,
                )
            return result

        terminal_error: BaseException | None = None
        try:
            # sync:only-start: sync map is sequential and fail-fast — that is user-visible behavior, not an implementation detail
            # for idx, variation_inputs in enumerate(input_variations):
            #     if map_stop_signal is not None and map_stop_signal.is_set:
            #         break
            #     result = _run_map_item(idx, variation_inputs)
            #     results.append(result)
            #     if error_handling == "raise" and result.status == RunStatus.FAILED:
            #         error = result.error
            #         assert error is not None, "FAILED status requires an error"
            #         with _failure_evidence_context(error, result.node_failures):
            #             raise error from None
            # sync:only-end
            # sync:skip-start: concurrent fan-out (gather, then a bounded worker queue) has no sequential counterpart
            if max_concurrency is None:
                if map_stop_signal is None or not map_stop_signal.is_set:
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
                results = results_list
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
            # sync:skip-end

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

                await dispatcher.emit_async(
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

            await self._emit_run_end_async(
                dispatcher,
                map_run_id,
                map_span_id,
                graph,
                start_time,
                _parent_span_id,
                context=RunContext(workflow_id=workflow_id, item_index=_item_index),
                status=batch_summary.event_status,
                batch_summary=batch_summary,
            )

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

            dispatcher = await teardown.settle(dispatcher)

            if map_inspection_session is not None:
                map_result = replace(
                    map_result,
                    _inspection=inspection_settlement.publish(
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
                    await self._emit_run_end_async(
                        dispatcher,
                        map_run_id,
                        map_span_id,
                        graph,
                        start_time,
                        _parent_span_id,
                        context=RunContext(workflow_id=workflow_id, item_index=_item_index),
                        error=e,
                    )
                # Mark parent batch run as failed
                await settle_created_parent_run_failed()
                dispatcher = await teardown.settle(dispatcher)
            except BaseException as final_error:
                terminal_error = final_error
                raise
            if map_inspection_session is not None:
                unstarted_item_indexes = tuple(idx for idx in range(len(input_variations)) if idx not in claimed_indexes)
                batch_error = None if any(result.error is e for result in results) else e
                inspection_settlement.publish(
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
                await teardown.settle_completely(dispatcher, settle_run_row=terminal_error is not None)
            except BaseException as final_error:
                inspection_settlement.abort(
                    final_error,
                    unstarted_item_indexes=tuple(idx for idx in range(len(input_variations)) if idx not in claimed_indexes),
                )
                raise
            if terminal_error is not None:
                inspection_settlement.abort(
                    terminal_error,
                    unstarted_item_indexes=tuple(idx for idx in range(len(input_variations)) if idx not in claimed_indexes),
                )

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
        max_concurrency: int | None = None,  # sync:skip: sync map_iter is sequential; there is no budget to set
        error_handling: ErrorHandling = "raise",
        **input_values: Any,
    ) -> AsyncIterator[tuple[int, RunResult]]:
        # sync:only-start: sync streams one item at a time, so it promises order, not completion order
        # """Stream ``(index, RunResult)`` pairs as each mapped item completes.
        #
        # Like :meth:`map`, but yields incrementally instead of buffering a
        # ``MapResult`` — bounding memory to one item at a time. ``index`` is the
        # input item's position, so a consumer can correlate a result with its
        # source item regardless of arrival order. ``error_handling="raise"``
        # re-raises when a failed item is reached; ``"continue"`` yields the failed
        # ``RunResult`` and keeps going.
        # """
        # sync:only-end
        # sync:skip-start: same, the async docstring promises backpressure and completion order
        """Stream ``(index, RunResult)`` pairs as each mapped item completes.

        Concurrent and backpressured: at most ``max_concurrency`` items run at
        once, and a bounded internal buffer means a slow consumer pauses
        production instead of materializing the whole batch — so peak memory is
        bounded, not proportional to the input size. ``index`` is the input
        item's position; results arrive in completion order. ``error_handling``
        matches :meth:`map`: ``"raise"`` re-raises when a failed item is reached,
        ``"continue"`` yields the failed ``RunResult`` and keeps going.
        """
        # sync:skip-end
        run_option_names = runner_option_names(self.run)
        map_option_names = runner_option_names(self.map)
        validate_error_handling(error_handling)
        validate_on_missing(on_missing)
        validate_max_concurrency(max_concurrency)  # sync:skip: no max_concurrency parameter to validate
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

        # sync:only-start: sync map_iter pulls one input variation at a time — a sync run must never require an event loop
        # # Lazy: pull one input variation at a time so peak memory stays bounded
        # # by a single item, not the whole batch.
        # for idx, variation_inputs in enumerate(generate_map_inputs(normalized_values, map_over_list, map_mode, clone)):
        #     try:
        #         result = self.run(
        #             graph,
        #             variation_inputs,
        #             select=select,
        #             on_missing=on_missing,
        #             entrypoint=entrypoint,
        #             error_handling="continue",
        #             show_progress=False,
        #             _validation_ctx=ctx,
        #             _item_index=idx,
        #         )
        #     except Exception as e:  # per-item validation error (e.g. missing input) → failed row
        #         result = build_pre_run_failed_result(e)
        #     if error_handling == "raise" and result.status == RunStatus.FAILED:
        #         error = result.error
        #         assert error is not None, "FAILED status requires an error"
        #         with _failure_evidence_context(error, result.node_failures):
        #             raise error from None
        #     yield idx, result
        # sync:only-end
        # sync:skip-start: a worker pool, a backpressured queue and a cancelling finally have no sequential counterpart
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
        # sync:skip-end


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


# sync:skip-start: gate-option validation only runs at a pause, and no sync runner can pause yet
def _validate_pause_options_have_routes(
    graph: Graph,
    pause_info: PauseInfo,
    state: GraphState | None,
) -> None:
    """Reject dead gate options when every gate input is settled at pause time."""
    options = getattr(pause_info.value, "options", None)
    if options is None:
        return

    from hypergraph.nodes.gate import IfElseNode, RouteNode

    if state is None:
        raise RuntimeError(
            f"InterruptNode '{pause_info.node_name}' could not validate its question options because no partial graph state is available\n\n"
            f"How to fix: Surface this pause through AsyncRunner.run() so route inputs are available."
        )

    active_nodes = compute_execution_scope(graph).active_nodes
    for gate in graph._nodes.values():
        if not isinstance(gate, (RouteNode, IfElseNode)):
            continue
        if active_nodes is not None and gate.name not in active_nodes:
            continue
        if pause_info.response_key not in gate.inputs:
            continue

        for option in options:
            try:
                gate_inputs = collect_inputs_for_node(
                    gate,
                    graph,
                    state,
                    {pause_info.response_key: option},
                )
            except KeyError:
                # Another gate input has not settled at this pause boundary.
                # Ordinary routing-time target validation remains authoritative.
                break

            try:
                result = gate.func(**gate.map_inputs_to_params(gate_inputs))
                if isinstance(gate, IfElseNode):
                    decision = gate.when_true if result is True else gate.when_false if result is False else None
                else:
                    decision = gate.fallback if result is None else result
                decisions = decision if isinstance(decision, list) else [decision]
                has_matching_targets = bool(decisions) and all(candidate in gate.targets for candidate in decisions)
            except Exception as exc:
                raise RuntimeError(
                    f"InterruptNode '{pause_info.node_name}' option '{option}' could not be mapped to a route target on gate '{gate.name}'\n\n"
                    f"How to fix: Make the gate a pure function that maps every question option to one declared target."
                ) from exc

            if not has_matching_targets:
                raise RuntimeError(
                    f"InterruptNode '{pause_info.node_name}' returned option '{option}' with no matching route target on gate '{gate.name}'\n\n"
                    f"The gate returned {decision!r}; declared targets are {gate.targets!r}.\n\n"
                    f"How to fix: Map that option to a declared route target, or remove it from the question."
                )


# sync:skip-end
