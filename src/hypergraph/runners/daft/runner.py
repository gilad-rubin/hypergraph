"""Translation runner: converts DAGs into Daft query plans.

DaftRunner only supports DAG graphs (no cycles, no gates, no interrupts).
Async nodes are handled natively by Daft's async UDF support.
"""

from __future__ import annotations

import time
import warnings
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Literal

from hypergraph.runners._shared.helpers import (
    _UNSET_SELECT,
    _validate_error_handling,
    _validate_on_missing,
    filter_outputs,
    generate_map_inputs,
)
from hypergraph.runners._shared.input_normalization import (
    MAP_RESERVED_OPTION_NAMES,
    RUN_RESERVED_OPTION_NAMES,
    normalize_inputs,
)
from hypergraph.runners._shared.types import (
    GraphState,
    MapResult,
    RunnerCapabilities,
    RunResult,
    RunStatus,
    _generate_run_id,
)
from hypergraph.runners._shared.validation import (
    precompute_input_validation,
    resolve_runtime_selected,
    validate_item_inputs,
    validate_runner_compatibility,
)
from hypergraph.runners.base import BaseRunner

if TYPE_CHECKING:
    from daft import DataFrame

    from hypergraph.cache import CacheBackend
    from hypergraph.events.processor import EventProcessor
    from hypergraph.graph import Graph


class DaftRunner(BaseRunner):
    """Translation runner: converts DAGs into Daft query plans.

    Each node becomes a Daft UDF chained via ``df.with_column()``.
    Supports sync nodes, async nodes (Daft handles natively),
    stateful UDFs (``@stateful``), batch UDFs (``batch=True``),
    and nested GraphNodes.

    Does NOT support cycles, gates, interrupts, or runner delegation
    (``with_runner()`` on nested GraphNodes).
    """

    def __init__(self, *, cache: CacheBackend | None = None):
        """Initialize the Daft runner.

        Args:
            cache: Optional cache backend for node-level caching.

        Raises:
            ImportError: If the optional ``daft`` dependency is not installed.
        """
        self._require_daft()
        self._cache = cache

    @staticmethod
    def _require_daft():
        """Import Daft lazily with a clear installation hint."""
        try:
            import daft
        except ImportError:
            raise ImportError("DaftRunner requires daft. Install it with: pip install 'hypergraph[daft]'") from None
        return daft

    @property
    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(
            supports_cycles=False,
            supports_gates=False,
            supports_interrupts=False,
            supports_async_nodes=True,
            supports_events=False,
            supports_distributed=True,
            returns_coroutine=False,
            supports_checkpointing=False,
        )

    # ------------------------------------------------------------------
    # run()
    # ------------------------------------------------------------------

    def run(
        self,
        graph: Graph,
        values: dict[str, Any] | None = None,
        *,
        select: str | list[str] = _UNSET_SELECT,
        on_missing: Literal["ignore", "warn", "error"] = "ignore",
        entrypoint: str | None = None,
        max_iterations: int | None = None,
        error_handling: Literal["raise", "continue"] = "raise",
        event_processors: list[EventProcessor] | None = None,
        show_progress: bool | None = None,
        **input_values: Any,
    ) -> RunResult:
        """Execute graph once via a 1-row Daft plan."""
        normalized = normalize_inputs(
            values,
            input_values,
            reserved_option_names=RUN_RESERVED_OPTION_NAMES,
        )
        self._warn_ignored(event_processors=event_processors, show_progress=show_progress)

        validate_runner_compatibility(graph, self.capabilities)
        _validate_no_runner_overrides(graph)
        _validate_error_handling(error_handling)
        effective_selected = resolve_runtime_selected(select, graph)
        _validate_on_missing(on_missing)
        ctx = precompute_input_validation(
            graph,
            entrypoint=entrypoint,
            selected=effective_selected,
        )
        validate_item_inputs(ctx, normalized)

        try:
            result_values = self._execute_columnar(
                graph,
                [normalized],
                clone=False,
            )[0]
            state = GraphState(values=result_values)
            output = filter_outputs(state, graph, select, on_missing)
            return RunResult(
                values=output,
                status=RunStatus.COMPLETED,
                run_id=_generate_run_id(),
            )
        except Exception as exc:
            if error_handling == "continue":
                return RunResult(
                    values={},
                    status=RunStatus.FAILED,
                    run_id=_generate_run_id(),
                    error=exc,
                )
            raise

    # ------------------------------------------------------------------
    # map()
    # ------------------------------------------------------------------

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
        event_processors: list[EventProcessor] | None = None,
        show_progress: bool | None = None,
        error_handling: Literal["raise", "continue"] = "raise",
        **input_values: Any,
    ) -> MapResult:
        """Execute graph for each item via Daft columnar execution."""
        normalized = normalize_inputs(
            values,
            input_values,
            reserved_option_names=MAP_RESERVED_OPTION_NAMES,
        )
        self._warn_ignored(event_processors=event_processors, show_progress=show_progress)

        validate_runner_compatibility(graph, self.capabilities)
        _validate_no_runner_overrides(graph)
        _validate_error_handling(error_handling)
        effective_selected = resolve_runtime_selected(select, graph)
        _validate_on_missing(on_missing)
        ctx = precompute_input_validation(
            graph,
            entrypoint=None,
            selected=effective_selected,
        )
        validate_item_inputs(ctx, normalized)

        map_over_list = [map_over] if isinstance(map_over, str) else list(map_over)
        input_variations = list(
            generate_map_inputs(normalized, map_over_list, map_mode, clone),
        )

        if not input_variations:
            return MapResult(
                results=(),
                run_id=None,
                total_duration_ms=0,
                map_over=tuple(map_over_list),
                map_mode=map_mode,
                graph_name=graph.name or "",
            )

        start = time.time()
        try:
            all_values = self._execute_columnar(graph, input_variations, clone=clone)
        except Exception:
            if error_handling == "raise":
                raise
            # For continue mode, fall back to per-item execution
            all_values = None

        results = self._collect_results(
            graph,
            input_variations,
            all_values,
            select,
            on_missing,
            error_handling,
            start,
            clone,
        )
        total_duration = (time.time() - start) * 1000

        if error_handling == "raise":
            for r in results:
                if r.status == RunStatus.FAILED:
                    raise r.error  # type: ignore[misc]

        return MapResult(
            results=tuple(results),
            run_id=_generate_run_id(),
            total_duration_ms=total_duration,
            map_over=tuple(map_over_list),
            map_mode=map_mode,
            graph_name=graph.name or "",
        )

    # ------------------------------------------------------------------
    # map_dataframe()
    # ------------------------------------------------------------------

    def map_dataframe(
        self,
        graph: Graph,
        dataframe: DataFrame,
        *,
        columns: str | Iterable[str] | None = None,
        values: dict[str, Any] | None = None,
        clone: bool | list[str] = False,
        **input_values: Any,
    ) -> DataFrame:
        """Execute graph per DataFrame row, returning a new DataFrame.

        The execution plan is applied directly to the input DataFrame —
        no materialization to Python. Broadcast values (``values`` / kwargs)
        are captured in UDF closures alongside ``graph.bind()`` values.

        Args:
            graph: A validated DAG graph.
            dataframe: Input Daft DataFrame. Each row is one graph execution.
            columns: Which DataFrame columns to use as graph inputs.
                     Defaults to all columns.
            values: Broadcast values shared across all rows (captured in
                    UDF closures, not added as DataFrame columns).
            clone: Deep-copy strategy (generally not needed — Daft provides
                   row isolation).
            **input_values: Additional broadcast values (merged with ``values``).

        Returns:
            Daft DataFrame with original input columns plus output columns
            from graph execution.
        """
        from hypergraph.runners.daft.engine import build_execution_plan, execute_plan

        normalized = normalize_inputs(
            values,
            input_values,
            reserved_option_names=MAP_RESERVED_OPTION_NAMES,
        )

        validate_runner_compatibility(graph, self.capabilities)
        _validate_no_runner_overrides(graph)

        column_names = _resolve_columns(dataframe, columns)
        _check_column_overlap(column_names, normalized)

        # Merge graph.bind() + broadcast values — both captured in UDF closures
        bound = dict(graph.inputs.bound) if graph.inputs.bound else {}
        all_bound = {**bound, **normalized}
        validation_values = {name: None for name in column_names}
        validation_values.update(all_bound)
        ctx = precompute_input_validation(graph, entrypoint=None, selected=None)
        # DataFrames often carry passthrough columns that are not graph inputs.
        # Keep stale-address and missing-input validation, but do not warn for
        # extra columns that Daft will preserve untouched.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            validate_item_inputs(ctx, validation_values)

        plan = build_execution_plan(graph, all_bound, self._cache, clone)
        return execute_plan(dataframe.select(*column_names), plan)

    # ------------------------------------------------------------------
    # Internal: columnar execution
    # ------------------------------------------------------------------

    def _execute_columnar(
        self,
        graph: Graph,
        input_variations: list[dict[str, Any]],
        *,
        clone: bool | list[str] = False,
    ) -> list[dict[str, Any]]:
        """Build and execute Daft plan, returning output dicts per row."""
        from hypergraph.runners.daft.engine import (
            build_execution_plan,
            build_input_dataframe,
            execute_plan,
        )

        bound = dict(graph.inputs.bound) if graph.inputs.bound else {}

        plan = build_execution_plan(graph, bound, self._cache, clone)

        # Determine input columns (all keys not provided by bound values)
        all_keys = sorted({k for v in input_variations for k in v})
        df = build_input_dataframe(input_variations, all_keys)

        result_df = execute_plan(df, plan)
        collected = result_df.collect().to_pydict()

        # Extract per-row output dicts
        n_rows = len(input_variations)
        output_names = [name for node in graph._nodes.values() for name in node.data_outputs]
        results = []
        for i in range(n_rows):
            row = {}
            for col in collected:
                if col in output_names or col in all_keys:
                    row[col] = collected[col][i]
            results.append(row)
        return results

    def _collect_results(
        self,
        graph: Graph,
        input_variations: list[dict[str, Any]],
        all_values: list[dict[str, Any]] | None,
        select: str | list[str],
        on_missing: str,
        error_handling: str,
        start_time: float,
        clone: bool | list[str],
    ) -> list[RunResult]:
        """Build RunResult list, falling back to per-item on error."""
        if all_values is not None:
            results = []
            for values in all_values:
                state = GraphState(values=values)
                output = filter_outputs(state, graph, select, on_missing)
                results.append(
                    RunResult(
                        values=output,
                        status=RunStatus.COMPLETED,
                        run_id=_generate_run_id(),
                    )
                )
            return results

        # Fallback: per-item execution for continue mode
        results = []
        for item_inputs in input_variations:
            try:
                row_values = self._execute_columnar(
                    graph,
                    [item_inputs],
                    clone=clone,
                )[0]
                state = GraphState(values=row_values)
                output = filter_outputs(state, graph, select, on_missing)
                results.append(
                    RunResult(
                        values=output,
                        status=RunStatus.COMPLETED,
                        run_id=_generate_run_id(),
                    )
                )
            except Exception as exc:
                results.append(
                    RunResult(
                        values={},
                        status=RunStatus.FAILED,
                        run_id=_generate_run_id(),
                        error=exc,
                    )
                )
        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _warn_ignored(
        *,
        event_processors: list[EventProcessor] | None = None,
        show_progress: bool | None = None,
    ) -> None:
        """Warn if event_processors or show_progress is set (DaftRunner ignores events)."""
        if event_processors:
            warnings.warn(
                "DaftRunner does not support event_processors. The provided processors will be ignored.",
                UserWarning,
                stacklevel=3,
            )
        if show_progress:
            warnings.warn(
                "DaftRunner does not support show_progress. The flag will be ignored.",
                UserWarning,
                stacklevel=3,
            )


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _validate_no_runner_overrides(graph: Graph) -> None:
    """Reject with_runner() overrides — DaftRunner controls all subgraphs."""
    from hypergraph.nodes.graph_node import GraphNode

    for node in graph._nodes.values():
        if isinstance(node, GraphNode) and node.runner_override is not None:
            from hypergraph.runners._shared.validation import IncompatibleRunnerError

            raise IncompatibleRunnerError(
                f"DaftRunner does not support runner overrides on nested GraphNodes "
                f"(GraphNode {node.name!r} has with_runner() set). "
                f"DaftRunner translates the entire graph to Daft UDFs.",
                capability="runner_delegation",
            )


def _resolve_columns(
    dataframe: DataFrame,
    columns: str | Iterable[str] | None,
) -> list[str]:
    """Resolve which DataFrame columns to use as graph inputs."""
    if columns is None:
        return list(dataframe.column_names)
    column_names = [columns] if isinstance(columns, str) else list(columns)

    for name in column_names:
        if name not in dataframe.column_names:
            from hypergraph.graph.validation import GraphConfigError

            raise GraphConfigError(
                f"Daft DataFrame is missing requested column {name!r}.\n\n"
                f"Available columns: {list(dataframe.column_names)}\n\n"
                f"How to fix: Check column names or use the 'columns' parameter "
                f"to specify which columns to map."
            )
    return column_names


def _check_column_overlap(
    column_names: list[str],
    values: dict[str, Any],
) -> None:
    """Reject overlapping column/broadcast names."""
    overlap = sorted(set(column_names) & set(values))
    if overlap:
        from hypergraph.graph.validation import GraphConfigError

        overlap_str = ", ".join(repr(name) for name in overlap)
        raise GraphConfigError(
            f"Input keys provided by both the Daft DataFrame and broadcast "
            f"values: {overlap_str}.\n\n"
            f"How to fix: Rename one side or drop the duplicate."
        )
