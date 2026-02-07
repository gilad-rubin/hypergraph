"""Shared async runner lifecycle template."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

from hypergraph.runners._shared.helpers import filter_outputs, generate_map_inputs
from hypergraph.runners._shared.input_normalization import normalize_inputs
from hypergraph.runners._shared.types import (
    ErrorHandling,
    GraphState,
    PauseExecution,
    RunResult,
    RunStatus,
)
from hypergraph.runners._shared.validation import (
    validate_inputs,
    validate_map_compatible,
    validate_node_types,
    validate_runner_compatibility,
)
from hypergraph.runners.async_.superstep import (
    get_concurrency_limiter,
    reset_concurrency_limiter,
    set_concurrency_limiter,
)
from hypergraph.runners.base import BaseRunner

if TYPE_CHECKING:
    from hypergraph.events.dispatcher import EventDispatcher
    from hypergraph.events.processor import EventProcessor
    from hypergraph.graph import Graph
    from hypergraph.nodes.base import HyperNode


class AsyncRunnerTemplate(BaseRunner, ABC):
    """Template implementation for async run/map lifecycle."""

    @property
    @abstractmethod
    def supported_node_types(self) -> set[type["HyperNode"]]:
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
        graph: "Graph",
        values: dict[str, Any],
        max_iterations: int,
        max_concurrency: int | None,
        *,
        dispatcher: "EventDispatcher",
        run_id: str,
        run_span_id: str,
        event_processors: list["EventProcessor"] | None = None,
    ) -> GraphState:
        """Execute graph and return final state."""
        ...

    @abstractmethod
    def _create_dispatcher(
        self,
        processors: list["EventProcessor"] | None,
    ) -> "EventDispatcher":
        """Create event dispatcher."""
        ...

    @abstractmethod
    async def _emit_run_start_async(
        self,
        dispatcher: "EventDispatcher",
        graph: "Graph",
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
        dispatcher: "EventDispatcher",
        run_id: str,
        span_id: str,
        graph: "Graph",
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
        dispatcher: "EventDispatcher",
    ) -> None:
        """Shut down dispatcher."""
        ...

    async def run(
        self,
        graph: "Graph",
        values: dict[str, Any] | None = None,
        *,
        select: list[str] | None = None,
        max_iterations: int | None = None,
        max_concurrency: int | None = None,
        event_processors: list["EventProcessor"] | None = None,
        _parent_span_id: str | None = None,
        **input_values: Any,
    ) -> RunResult:
        """Execute a graph once."""
        normalized_values = normalize_inputs(values, input_values)

        validate_runner_compatibility(graph, self.capabilities)
        validate_node_types(graph, self.supported_node_types)
        validate_inputs(graph, normalized_values)

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
            output_values = filter_outputs(state, graph, select)
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
            await self._emit_run_end_async(
                dispatcher, run_id, run_span_id, graph, start_time, _parent_span_id,
                error=e,
            )
            partial_state = getattr(e, "_partial_state", None)
            partial_values = (
                filter_outputs(partial_state, graph, select)
                if partial_state is not None
                else {}
            )
            return RunResult(
                values=partial_values,
                status=RunStatus.FAILED,
                run_id=run_id,
                error=e,
            )
        finally:
            if _parent_span_id is None and dispatcher.active:
                await self._shutdown_dispatcher_async(dispatcher)

    async def map(
        self,
        graph: "Graph",
        values: dict[str, Any] | None = None,
        *,
        map_over: str | list[str],
        map_mode: Literal["zip", "product"] = "zip",
        select: list[str] | None = None,
        max_concurrency: int | None = None,
        error_handling: ErrorHandling = "raise",
        event_processors: list["EventProcessor"] | None = None,
        _parent_span_id: str | None = None,
        **input_values: Any,
    ) -> list[RunResult]:
        """Execute a graph multiple times with different inputs."""
        normalized_values = normalize_inputs(values, input_values)

        validate_runner_compatibility(graph, self.capabilities)
        validate_node_types(graph, self.supported_node_types)
        validate_map_compatible(graph)

        map_over_list = [map_over] if isinstance(map_over, str) else list(map_over)
        input_variations = list(generate_map_inputs(normalized_values, map_over_list, map_mode))
        if not input_variations:
            return []

        dispatcher = self._create_dispatcher(event_processors)
        map_run_id, map_span_id = await self._emit_run_start_async(
            dispatcher,
            graph,
            _parent_span_id,
            is_map=True,
            map_size=len(input_variations),
        )
        start_time = time.time()

        existing_limiter = get_concurrency_limiter()
        if existing_limiter is None and max_concurrency is not None:
            semaphore = asyncio.Semaphore(max_concurrency)
            token = set_concurrency_limiter(semaphore)
        else:
            token = None

        try:
            if max_concurrency is None:
                tasks = [
                    self.run(
                        graph,
                        v,
                        select=select,
                        max_concurrency=max_concurrency,
                        event_processors=event_processors,
                        _parent_span_id=map_span_id,
                    )
                    for v in input_variations
                ]
                results = list(await asyncio.gather(*tasks))
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
                    while not stop_event.is_set():
                        try:
                            idx, v = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        result = await self.run(
                            graph,
                            v,
                            select=select,
                            max_concurrency=max_concurrency,
                            event_processors=event_processors,
                            _parent_span_id=map_span_id,
                        )
                        results_list.append(result)
                        order.append(idx)
                        if (
                            error_handling == "raise"
                            and result.status == RunStatus.FAILED
                        ):
                            stop_event.set()

                num_workers = min(max_concurrency, len(input_variations))
                workers = [asyncio.create_task(_worker()) for _ in range(num_workers)]
                await asyncio.gather(*workers)
                results = [r for _, r in sorted(zip(order, results_list))]
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
                reset_concurrency_limiter(token)
            if _parent_span_id is None and dispatcher.active:
                await self._shutdown_dispatcher_async(dispatcher)
