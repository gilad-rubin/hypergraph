"""Shared async runner lifecycle template."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

from hypergraph.exceptions import ExecutionError
from hypergraph.runners._shared.helpers import (
    _UNSET_SELECT,
    _validate_on_missing,
    filter_outputs,
    generate_map_inputs,
)
from hypergraph.runners._shared.input_normalization import (
    ASYNC_MAP_RESERVED_OPTION_NAMES,
    ASYNC_RUN_RESERVED_OPTION_NAMES,
    normalize_inputs,
)
from hypergraph.runners._shared.types import (
    ErrorHandling,
    GraphState,
    PauseExecution,
    RunResult,
    RunStatus,
    _generate_run_id,
)
from hypergraph.runners._shared.validation import (
    validate_inputs,
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


MAX_UNBOUNDED_MAP_TASKS = 10_000


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
        is_map: bool = False,
        map_size: int | None = None,
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
        error: BaseException | None = None,
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
        event_processors: list[EventProcessor] | None = None,
        _parent_span_id: str | None = None,
        **input_values: Any,
    ) -> RunResult:
        """Execute a graph once."""
        normalized_values = normalize_inputs(
            values,
            input_values,
            reserved_option_names=ASYNC_RUN_RESERVED_OPTION_NAMES,
        )

        validate_runner_compatibility(graph, self.capabilities)
        validate_node_types(graph, self.supported_node_types)
        validate_inputs(graph, normalized_values, entrypoint=entrypoint)
        _validate_on_missing(on_missing)

        max_iter = max_iterations or self.default_max_iterations
        dispatcher = self._create_dispatcher(event_processors)
        run_id, run_span_id = await self._emit_run_start_async(
            dispatcher, graph, _parent_span_id,
        )
        start_time = time.time()

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
            )
            output_values = filter_outputs(state, graph, select, on_missing)
            result = RunResult(
                values=output_values,
                status=RunStatus.COMPLETED,
                run_id=run_id,
            )
            await self._emit_run_end_async(
                dispatcher, run_id, run_span_id, graph, start_time, _parent_span_id,
            )
            return result
        except PauseExecution as pause:
            partial_state = getattr(pause, "_partial_state", None)
            partial_values = (
                filter_outputs(partial_state, graph, select)
                if partial_state is not None
                else {}
            )
            return RunResult(
                values=partial_values,
                status=RunStatus.PAUSED,
                run_id=run_id,
                pause=pause.pause_info,
            )
        except Exception as e:
            error = e
            partial_state = getattr(e, "_partial_state", None)
            if isinstance(e, ExecutionError):
                error = e.__cause__ or e
                partial_state = e.partial_state

            await self._emit_run_end_async(
                dispatcher, run_id, run_span_id, graph, start_time, _parent_span_id,
                error=error,
            )
            partial_values = (
                filter_outputs(partial_state, graph, select)
                if partial_state is not None
                else {}
            )
            return RunResult(
                values=partial_values,
                status=RunStatus.FAILED,
                run_id=run_id,
                error=error,
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
        select: str | list[str] = _UNSET_SELECT,
        on_missing: Literal["ignore", "warn", "error"] = "ignore",
        entrypoint: str | None = None,
        max_concurrency: int | None = None,
        error_handling: ErrorHandling = "raise",
        event_processors: list[EventProcessor] | None = None,
        _parent_span_id: str | None = None,
        **input_values: Any,
    ) -> list[RunResult]:
        """Execute a graph multiple times with different inputs."""
        normalized_values = normalize_inputs(
            values,
            input_values,
            reserved_option_names=ASYNC_MAP_RESERVED_OPTION_NAMES,
        )

        validate_runner_compatibility(graph, self.capabilities)
        validate_node_types(graph, self.supported_node_types)
        validate_map_compatible(graph)

        map_over_list = [map_over] if isinstance(map_over, str) else list(map_over)
        input_variations = list(generate_map_inputs(normalized_values, map_over_list, map_mode))
        if not input_variations:
            return []
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
            is_map=True,
            map_size=len(input_variations),
        )
        start_time = time.time()

        existing_limiter = self._get_concurrency_limiter()
        token = self._set_concurrency_limiter(max_concurrency) if existing_limiter is None and max_concurrency is not None else None

        async def _run_map_item(variation_inputs: dict[str, Any]) -> RunResult:
            """Execute one map variation and normalize failures to RunResult."""
            try:
                return await self.run(
                    graph,
                    variation_inputs,
                    select=select,
                    on_missing=on_missing,
                    entrypoint=entrypoint,
                    max_concurrency=max_concurrency,
                    event_processors=event_processors,
                    _parent_span_id=map_span_id,
                )
            except Exception as e:
                return RunResult(
                    values={},
                    status=RunStatus.FAILED,
                    run_id=_generate_run_id(),
                    error=e,
                )

        try:
            if max_concurrency is None:
                tasks = [
                    _run_map_item(v)
                    for v in input_variations
                ]
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
                        result = await _run_map_item(v)
                        results_list.append(result)
                        order.append(idx)
                        if (
                            error_handling == "raise"
                            and result.status == RunStatus.FAILED
                        ):
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

            await self._emit_run_end_async(
                dispatcher, map_run_id, map_span_id, graph, start_time, _parent_span_id,
            )
            return results
        except Exception as e:
            await self._emit_run_end_async(
                dispatcher, map_run_id, map_span_id, graph, start_time, _parent_span_id,
                error=e,
            )
            raise
        finally:
            if token is not None:
                self._reset_concurrency_limiter(token)
            if _parent_span_id is None and dispatcher.active:
                await self._shutdown_dispatcher_async(dispatcher)
