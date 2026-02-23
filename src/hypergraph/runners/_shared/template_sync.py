"""Shared sync runner lifecycle template."""

from __future__ import annotations

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
from hypergraph.runners._shared.types import ErrorHandling, GraphState, RunResult, RunStatus
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

    def run(
        self,
        graph: Graph,
        values: dict[str, Any] | None = None,
        *,
        select: str | list[str] = _UNSET_SELECT,
        on_missing: Literal["ignore", "warn", "error"] = "ignore",
        entrypoint: str | None = None,
        max_iterations: int | None = None,
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
        run_id, run_span_id = self._emit_run_start_sync(dispatcher, graph, _parent_span_id)
        start_time = time.time()

        try:
            state = self._execute_graph_impl(
                graph,
                normalized_values,
                max_iter,
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
            self._emit_run_end_sync(
                dispatcher,
                run_id,
                run_span_id,
                graph,
                start_time,
                _parent_span_id,
            )
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
            partial_values = filter_outputs(partial_state, graph, select) if partial_state is not None else {}
            return RunResult(
                values=partial_values,
                status=RunStatus.FAILED,
                run_id=run_id,
                error=error,
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
        select: str | list[str] = _UNSET_SELECT,
        on_missing: Literal["ignore", "warn", "error"] = "ignore",
        entrypoint: str | None = None,
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

        dispatcher = self._create_dispatcher(event_processors)
        map_run_id, map_span_id = self._emit_run_start_sync(
            dispatcher,
            graph,
            _parent_span_id,
            is_map=True,
            map_size=len(input_variations),
        )
        start_time = time.time()

        try:
            results = []
            for variation_inputs in input_variations:
                result = self.run(
                    graph,
                    variation_inputs,
                    select=select,
                    on_missing=on_missing,
                    entrypoint=entrypoint,
                    event_processors=event_processors,
                    _parent_span_id=map_span_id,
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
            return results
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
            raise
        finally:
            if _parent_span_id is None and dispatcher.active:
                self._shutdown_dispatcher_sync(dispatcher)
