"""DaftRunner — translates hypergraph Graph to Daft DataFrame pipeline.

Each FunctionNode becomes a Daft UDF applied via df.with_column()
in topological order. The DataFrame flows through the chain and is
collected at the end.

run()  → 1-row DataFrame (values wrapped in lists)
map()  → N-row DataFrame (map_over params are lists, broadcast are scalars)

Limitations (by design):
- DAG only — no cycles (Daft has no iteration primitive)
- FunctionNode only — no gates, no interrupts, no async nodes
- No nested GraphNode support yet (future: flatten or delegate)
- entrypoint= and clone= parameters accepted for API compatibility but not applied;
  Daft always executes the full DAG and isolates rows natively
"""

from __future__ import annotations

import itertools
import time
from typing import TYPE_CHECKING, Any, Literal

import networkx as nx

from hypergraph.exceptions import IncompatibleRunnerError
from hypergraph.runners._shared.helpers import _UNSET_SELECT
from hypergraph.runners._shared.types import (
    ErrorHandling,
    MapResult,
    RunnerCapabilities,
    RunResult,
    RunStatus,
    _generate_run_id,
)
from hypergraph.runners._shared.validation import validate_runner_compatibility
from hypergraph.runners.base import BaseRunner

if TYPE_CHECKING:
    from hypergraph.events.processor import EventProcessor
    from hypergraph.graph import Graph
    from hypergraph.nodes.base import HyperNode


def _check_daft_available() -> None:
    try:
        import daft  # noqa: F401
    except ImportError:
        raise ImportError("daft is required for DaftRunner. Install it with:\n  pip install getdaft\n  # or: uv add getdaft") from None


def _validate_daft_compatible(graph: Graph) -> None:
    """Check Daft-specific constraints after validate_runner_compatibility has run.

    Checks:
    - Only FunctionNode allowed (no GraphNode, GateNode, etc.)
    - Each FunctionNode must have exactly one output (Daft maps 1 func → 1 column)
    """
    from hypergraph.nodes.function import FunctionNode

    for node in graph.iter_nodes():
        if not isinstance(node, FunctionNode):
            raise IncompatibleRunnerError(
                f"DaftRunner only supports FunctionNode. Found: {type(node).__name__} ('{node.name}'). Nested GraphNode support is not yet implemented.",
                node_name=node.name,
                capability="node_type",
            )
        if len(node.outputs) != 1:
            raise IncompatibleRunnerError(
                f"DaftRunner requires each node to have exactly one output. Node '{node.name}' has {len(node.outputs)} outputs: {node.outputs}.",
                node_name=node.name,
                capability="node_type",
            )


def _build_udf_chain(graph: Graph) -> list[HyperNode]:
    """Return nodes in topological order for the UDF chain."""
    topo_names = list(nx.topological_sort(graph._nx_graph))
    return [graph._nodes[name] for name in topo_names]


def _execute_pipeline(
    graph: Graph,
    df: Any,
    selected: tuple[str, ...] | None,
) -> dict[str, list[Any]]:
    """Apply each node as a daft.func column operation in topological order.

    Returns the collected result as a dict of column lists (via to_pydict).
    """
    import daft

    from hypergraph.nodes.function import FunctionNode

    chain = _build_udf_chain(graph)

    for node in chain:
        if not isinstance(node, FunctionNode):
            continue

        func = node.func
        input_cols = [daft.col(param) for param in node.inputs]
        output_name = node.outputs[0]

        # daft.func applies the function per-row on scalar values
        udf = daft.func(return_dtype=daft.DataType.python())(func)
        df = df.with_column(output_name, udf(*input_cols))

    return df.collect().to_pydict()


def _extract_single_row(pydict: dict[str, list[Any]], output_names: tuple[str, ...]) -> dict[str, Any]:
    """Extract values from a 1-row result dict."""
    return {name: pydict[name][0] for name in output_names}


def _extract_all_rows(pydict: dict[str, list[Any]], output_names: tuple[str, ...]) -> list[dict[str, Any]]:
    """Extract values from an N-row result dict."""
    n_rows = len(next(iter(pydict.values())))
    return [{name: pydict[name][i] for name in output_names} for i in range(n_rows)]


class DaftRunner(BaseRunner):
    """Translates a hypergraph Graph to a Daft DataFrame pipeline.

    Each FunctionNode becomes a Daft UDF applied as a column operation.
    The entire graph executes as a single lazy DataFrame pipeline,
    with Daft handling parallelism and optimization.

    Supported:
    - DAG graphs with FunctionNode only
    - run() for single execution
    - map() for batch execution over parameter lists

    Not supported:
    - Cyclic graphs (no iteration in Daft)
    - GateNode, InterruptNode (control flow not expressible as UDFs)
    - Async nodes (Daft manages its own parallelism)

    Example:
        >>> from hypergraph import Graph, node
        >>> from hypergraph.integrations.daft import DaftRunner
        >>>
        >>> @node(output_name="doubled")
        ... def double(x: int) -> int:
        ...     return x * 2
        >>>
        >>> graph = Graph([double])
        >>> runner = DaftRunner()
        >>> result = runner.run(graph, x=5)
        >>> result.values["doubled"]
        10
    """

    def __init__(self) -> None:
        _check_daft_available()

    @property
    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(
            supports_cycles=False,
            supports_async_nodes=False,
            supports_interrupts=False,
            supports_streaming=False,
            returns_coroutine=False,
            supports_checkpointing=False,
        )

    def run(
        self,
        graph: Graph,
        values: dict[str, Any] | None = None,
        *,
        select: str | list[str] = _UNSET_SELECT,
        on_missing: Literal["ignore", "warn", "error"] = "ignore",
        entrypoint: str | None = None,
        max_iterations: int | None = None,
        error_handling: ErrorHandling = "raise",
        event_processors: list[EventProcessor] | None = None,
        workflow_id: str | None = None,
        _parent_span_id: str | None = None,
        _parent_run_id: str | None = None,
        **input_values: Any,
    ) -> RunResult:
        """Execute graph as a 1-row Daft DataFrame pipeline."""
        import daft

        validate_runner_compatibility(graph, self.capabilities)
        _validate_daft_compatible(graph)

        # Merge: bound values (lowest priority) < provided values < kwargs
        merged = {**graph._bound, **(values or {}), **input_values}

        # Determine outputs
        selected = graph.selected if graph.selected is not None else graph.outputs

        # Build 1-row DataFrame: wrap each value in a list
        df_dict = {k: [v] for k, v in merged.items()}
        df = daft.from_pydict(df_dict)

        run_id = _generate_run_id()
        try:
            collected = _execute_pipeline(graph, df, selected)
            output_values = _extract_single_row(collected, selected)
            return RunResult(
                values=output_values,
                status=RunStatus.COMPLETED,
                run_id=run_id,
                workflow_id=workflow_id,
            )
        except Exception as e:
            if error_handling == "raise":
                raise
            return RunResult(
                values={},
                status=RunStatus.FAILED,
                run_id=run_id,
                workflow_id=workflow_id,
                error=e,
            )

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
        entrypoint: str | None = None,
        max_concurrency: int | None = None,
        error_handling: ErrorHandling = "raise",
        event_processors: list[EventProcessor] | None = None,
        workflow_id: str | None = None,
        _parent_span_id: str | None = None,
        _parent_run_id: str | None = None,
        **input_values: Any,
    ) -> MapResult:
        """Execute graph over multiple inputs as an N-row Daft DataFrame.

        Each row is an independent execution. Daft handles parallelism.

        Note: clone= is accepted for API compatibility but not applied.
        Daft isolates rows natively — broadcast values are never mutated across rows.
        """
        import daft

        validate_runner_compatibility(graph, self.capabilities)
        _validate_daft_compatible(graph)

        # Merge: bound values (lowest priority) < provided values < kwargs
        merged = {**graph._bound, **(values or {}), **input_values}

        map_over_list = [map_over] if isinstance(map_over, str) else list(map_over)
        selected = graph.selected if graph.selected is not None else graph.outputs

        # Build N-row DataFrame from map inputs
        df_dict = _build_map_dataframe(merged, map_over_list, map_mode)
        n_rows = len(next(iter(df_dict.values())))
        df = daft.from_pydict(df_dict)

        run_id = None if n_rows == 0 else _generate_run_id()
        start_time = time.monotonic()
        try:
            collected = _execute_pipeline(graph, df, selected)
            rows = _extract_all_rows(collected, selected)
            results = tuple(
                RunResult(
                    values=row,
                    status=RunStatus.COMPLETED,
                    run_id=_generate_run_id(),
                )
                for row in rows
            )
        except Exception as e:
            if error_handling == "raise":
                raise
            results = tuple(
                RunResult(
                    values={},
                    status=RunStatus.FAILED,
                    run_id=_generate_run_id(),
                    error=e,
                )
                for _ in range(n_rows)
            )

        total_duration_ms = (time.monotonic() - start_time) * 1000
        return MapResult(
            results=results,
            run_id=run_id,
            total_duration_ms=total_duration_ms,
            map_over=tuple(map_over_list),
            map_mode=map_mode,
            graph_name=graph.name or "unnamed",
        )


def _build_map_dataframe(
    values: dict[str, Any],
    map_over: list[str],
    map_mode: str,
) -> dict[str, list[Any]]:
    """Build column dict for an N-row DataFrame from map inputs.

    map_over params are lists; broadcast params are scalars repeated per row.
    """
    mapped = {k: values[k] for k in map_over}
    broadcast = {k: v for k, v in values.items() if k not in map_over}

    if map_mode == "zip":
        lengths = [len(v) for v in mapped.values()]
        if len(set(lengths)) > 1:
            raise ValueError(f"zip mode requires equal-length lists for map_over params. Got lengths: {dict(zip(map_over, lengths, strict=False))}")
        n_rows = lengths[0] if lengths else 0
        df_dict = dict(mapped)
    elif map_mode == "product":
        # Cartesian product of all mapped params
        keys = list(mapped.keys())
        combos = list(itertools.product(*(mapped[k] for k in keys)))
        n_rows = len(combos)
        df_dict = {k: [combo[i] for combo in combos] for i, k in enumerate(keys)}
    else:
        raise ValueError(f"Unknown map_mode: {map_mode!r}")

    # Broadcast scalars: repeat for each row
    for k, v in broadcast.items():
        df_dict[k] = [v] * n_rows

    return df_dict
