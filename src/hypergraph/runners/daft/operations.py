"""Node → Daft UDF translation operations."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    import daft

    from hypergraph.cache import CacheBackend
    from hypergraph.graph import Graph
    from hypergraph.nodes.base import HyperNode


@runtime_checkable
class DaftStateful(Protocol):
    """Protocol for objects loaded once per Daft worker.

    Mark a class with ``@stateful`` so DaftRunner wraps it with ``@daft.cls``
    instead of ``@daft.func``.  This is useful for heavy resources
    (ML models, DB connections) that should be initialized once per
    worker process rather than once per row.

    Example::

        @stateful
        class MyModel:
            def __init__(self):
                self.model = load_heavy_model()

        graph = Graph([embed]).bind(model=MyModel())
        DaftRunner().map(graph, {"text": texts}, map_over="text")
    """

    __daft_stateful__: ClassVar[bool]


def stateful(cls: type) -> type:
    """Mark a class for per-worker initialization in DaftRunner.

    Stateful objects are re-created once per Daft worker process via
    ``@daft.cls``, so heavy resources (models, DB connections) aren't
    serialized per row.

    Example::

        @stateful
        class MyModel:
            def __init__(self):
                self.model = load_heavy_model()

        graph = Graph([embed]).bind(model=MyModel())
    """
    cls.__daft_stateful__ = True
    return cls


def is_batch(node: HyperNode) -> bool:
    """Check if a node is marked for batch (vectorized) execution."""
    return getattr(node, "batch", False) is True


def _is_stateful(v: Any) -> bool:
    """Check if a value is DaftStateful (attribute exists AND is truthy)."""
    return getattr(type(v), "__daft_stateful__", False) is True


def has_stateful_values(bound_values: dict[str, Any]) -> bool:
    """Check if any bound values implement DaftStateful protocol."""
    return any(_is_stateful(v) for v in bound_values.values())


# ---------------------------------------------------------------------------
# Base operation
# ---------------------------------------------------------------------------


class DaftOperation(ABC):
    """A single step in a Daft execution plan.

    Translates a hypergraph node into a Daft ``df.with_column()`` call.
    """

    def __init__(
        self,
        node: HyperNode,
        input_columns: list[str],
        output_columns: list[str],
        bound_values: dict[str, Any],
        clone: bool | list[str] = False,
    ):
        self.node = node
        self.input_columns = input_columns
        self.output_columns = output_columns
        self.bound_values = bound_values
        self.clone = clone

    @abstractmethod
    def apply(self, df: daft.DataFrame) -> daft.DataFrame:
        """Apply this operation to a DataFrame, returning one with new column(s)."""
        ...


# ---------------------------------------------------------------------------
# Concrete operations
# ---------------------------------------------------------------------------


class FunctionNodeOperation(DaftOperation):
    """Wraps a sync or async FunctionNode as a ``@daft.func`` UDF.

    Daft auto-detects async functions and handles the event loop.
    """

    def apply(self, df: daft.DataFrame) -> daft.DataFrame:
        import daft as daft_mod

        node = self.node
        bound = self.bound_values
        input_cols = self.input_columns
        # For multi-output nodes we pack all outputs into a single Python column
        output_col = self.output_columns[0] if len(self.output_columns) == 1 else f"_pack_{node.name}"
        multi_output = len(self.output_columns) > 1

        func = node.func
        col_names = input_cols  # capture for closure

        def wrapper(*args: Any) -> Any:
            kwargs = {**bound, **dict(zip(col_names, args, strict=True))}
            return func(**kwargs)

        if asyncio.iscoroutinefunction(func):

            async def async_wrapper(*args: Any) -> Any:
                kwargs = {**bound, **dict(zip(col_names, args, strict=True))}
                return await func(**kwargs)

            udf = daft_mod.func(return_dtype=daft_mod.DataType.python())(async_wrapper)
        else:
            udf = daft_mod.func(return_dtype=daft_mod.DataType.python())(wrapper)

        col_refs = [df[c] for c in input_cols]
        df = df.with_column(output_col, udf(*col_refs))

        if multi_output:
            df = _unpack_multi_output(df, output_col, self.output_columns)

        return df


class StatefulNodeOperation(DaftOperation):
    """Wraps a node with DaftStateful bound values as a ``@daft.cls`` UDF.

    Stateful objects are initialized once per worker process.
    """

    def apply(self, df: daft.DataFrame) -> daft.DataFrame:
        import daft as daft_mod

        node = self.node
        bound = self.bound_values
        input_cols = self.input_columns
        output_col = self.output_columns[0] if len(self.output_columns) == 1 else f"_pack_{node.name}"
        multi_output = len(self.output_columns) > 1

        func = node.func
        col_names = input_cols  # capture for closure

        # Separate stateful from plain bound values
        stateful_vals = {k: v for k, v in bound.items() if _is_stateful(v)}
        plain_vals = {k: v for k, v in bound.items() if not _is_stateful(v)}

        @daft_mod.cls
        class StatefulWrapper:
            def __init__(self):
                # Each stateful object is re-created once per worker
                self._stateful = {k: type(v)() for k, v in stateful_vals.items()}
                self._plain = plain_vals

            @daft_mod.method(return_dtype=daft_mod.DataType.python())
            def __call__(self, *args: Any) -> Any:
                kwargs = {
                    **self._stateful,
                    **self._plain,
                    **dict(zip(col_names, args, strict=True)),
                }
                return func(**kwargs)

        col_refs = [df[c] for c in input_cols]
        df = df.with_column(output_col, StatefulWrapper()(*col_refs))

        if multi_output:
            df = _unpack_multi_output(df, output_col, self.output_columns)

        return df


class BatchNodeOperation(DaftOperation):
    """Wraps a batch-marked node as a ``@daft.func.batch`` UDF.

    Batch UDFs receive ``daft.Series`` instead of scalar values.
    """

    def apply(self, df: daft.DataFrame) -> daft.DataFrame:
        import daft as daft_mod

        node = self.node
        bound = self.bound_values
        input_cols = self.input_columns
        output_col = self.output_columns[0]

        func = node.func
        col_names = input_cols  # capture for closure

        def batch_wrapper(*series_args: Any) -> Any:
            kwargs = {**bound, **dict(zip(col_names, series_args, strict=True))}
            return func(**kwargs)

        udf = daft_mod.func.batch(return_dtype=daft_mod.DataType.python())(batch_wrapper)

        col_refs = [df[c] for c in input_cols]
        return df.with_column(output_col, udf(*col_refs))


class GraphNodeOperation(DaftOperation):
    """Wraps a nested GraphNode in a single ``@daft.func`` UDF.

    For non-mapped graphs: runs the inner graph via SyncRunner.
    For mapped graphs: runs the inner graph's map() via SyncRunner.
    """

    def __init__(
        self,
        node: HyperNode,
        input_columns: list[str],
        output_columns: list[str],
        bound_values: dict[str, Any],
        clone: bool | list[str] = False,
        cache: CacheBackend | None = None,
    ):
        super().__init__(node, input_columns, output_columns, bound_values, clone)
        self.cache = cache

    def apply(self, df: daft.DataFrame) -> daft.DataFrame:
        import daft as daft_mod

        node = self.node
        input_cols = self.input_columns
        output_col = self.output_columns[0] if len(self.output_columns) == 1 else f"_pack_{node.name}"
        multi_output = len(self.output_columns) > 1
        cache = self.cache
        bound = self.bound_values

        inner_graph = node.graph
        # map_config is (params, mode, error_handling) or None
        map_config = node.map_config
        col_names = input_cols  # capture for closure

        @daft_mod.func(return_dtype=daft_mod.DataType.python())
        def execute_graph(*args: Any) -> Any:
            from hypergraph.runners._shared.helpers import (
                collect_as_lists,
                map_inputs_to_func_params,
            )
            from hypergraph.runners.sync.runner import SyncRunner

            raw_inputs = {**bound, **dict(zip(col_names, args, strict=True))}
            # Translate renamed input keys back to original inner graph names
            inner_inputs = map_inputs_to_func_params(node, raw_inputs)
            runner = SyncRunner(cache=cache)

            if map_config:
                _map_over_params, mode, error_handling = map_config
                # Use original (pre-rename) param names for inner graph
                original_params = node._original_map_params()
                map_result = runner.map(
                    inner_graph,
                    inner_inputs,
                    map_over=original_params,
                    map_mode=mode,
                    clone=node._original_clone(),
                    error_handling=error_handling,
                )
                # Collect into {output_name: [values...]} dict
                collected = collect_as_lists(
                    map_result.results,
                    node,
                    error_handling,
                )
                if multi_output:
                    return collected
                # Single output mapped: return the list directly
                return next(iter(collected.values()))
            else:
                result = runner.run(inner_graph, inner_inputs)
                if multi_output:
                    return result.values
                return next(iter(result.values.values()))

        col_refs = [df[c] for c in input_cols]
        df = df.with_column(output_col, execute_graph(*col_refs))

        if multi_output:
            df = _unpack_multi_output(df, output_col, self.output_columns)

        return df


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_operation(
    node: HyperNode,
    graph: Graph,
    bound_values: dict[str, Any],
    cache: CacheBackend | None = None,
    clone: bool | list[str] = False,
) -> DaftOperation:
    """Route a node to the appropriate DaftOperation class."""
    from hypergraph.nodes.function import FunctionNode
    from hypergraph.nodes.graph_node import GraphNode

    # Determine which bound values apply to this node
    node_bound = {k: v for k, v in bound_values.items() if k in node.inputs}

    # Determine input columns (those not provided by bound values)
    input_cols = [p for p in node.inputs if p not in node_bound]
    output_cols = list(node.data_outputs)

    if isinstance(node, GraphNode):
        if node.graph.has_async_nodes:
            from hypergraph.runners._shared.validation import IncompatibleRunnerError

            raise IncompatibleRunnerError(
                f"DaftRunner does not support nested graphs with async nodes (GraphNode {node.name!r} contains async nodes). "
                f"Use SyncRunner or AsyncRunner for graphs with async nested subgraphs.",
                capability="node_types",
            )
        return GraphNodeOperation(
            node=node,
            input_columns=input_cols,
            output_columns=output_cols,
            bound_values=node_bound,
            clone=clone,
            cache=cache,
        )

    if isinstance(node, FunctionNode):
        if has_stateful_values(node_bound):
            _validate_stateful_constructable(node_bound)
            if asyncio.iscoroutinefunction(node.func):
                from hypergraph.runners._shared.validation import IncompatibleRunnerError

                raise IncompatibleRunnerError(
                    f"Stateful UDFs with async node functions are not supported (node {node.name!r}).",
                    capability="node_types",
                )
            return StatefulNodeOperation(
                node=node,
                input_columns=input_cols,
                output_columns=output_cols,
                bound_values=node_bound,
                clone=clone,
            )
        if is_batch(node):
            if len(output_cols) > 1:
                from hypergraph.runners._shared.validation import IncompatibleRunnerError

                raise IncompatibleRunnerError(
                    f"Batch UDFs with multiple outputs are not supported (node {node.name!r} has {output_cols}).",
                    capability="node_types",
                )
            return BatchNodeOperation(
                node=node,
                input_columns=input_cols,
                output_columns=output_cols,
                bound_values=node_bound,
                clone=clone,
            )
        return FunctionNodeOperation(
            node=node,
            input_columns=input_cols,
            output_columns=output_cols,
            bound_values=node_bound,
            clone=clone,
        )

    from hypergraph.runners._shared.validation import IncompatibleRunnerError

    raise IncompatibleRunnerError(
        f"DaftRunner does not support {type(node).__name__}. Only FunctionNode and GraphNode are supported.",
        capability="node_types",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_stateful_constructable(bound_values: dict[str, Any]) -> None:
    """Validate that DaftStateful objects support zero-arg construction."""
    for k, v in bound_values.items():
        if not isinstance(v, DaftStateful):
            continue
        try:
            type(v)()
        except TypeError as e:
            raise TypeError(
                f"DaftStateful object {k!r} ({type(v).__name__}) must support "
                f"zero-arg construction for per-worker re-initialization. Got: {e}\n\n"
                f"How to fix: Ensure {type(v).__name__}.__init__() works with no arguments."
            ) from e


def _unpack_multi_output(
    df: daft.DataFrame,
    pack_col: str,
    output_names: list[str],
) -> daft.DataFrame:
    """Unpack a packed Python column into individual output columns."""
    import daft as daft_mod

    for i, name in enumerate(output_names):

        @daft_mod.func(return_dtype=daft_mod.DataType.python())
        def extract(packed: Any, idx: int = i) -> Any:
            if isinstance(packed, dict):
                return packed.get(output_names[idx])
            if isinstance(packed, (tuple, list)):
                return packed[idx]
            return packed

        df = df.with_column(name, extract(df[pack_col]))

    return df.exclude(pack_col)
