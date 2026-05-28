"""Node → Daft UDF translation operations."""

from __future__ import annotations

import asyncio
import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hypergraph.runners.daft._options import DEFAULT_OPTIONS, Options, get_node_options
from hypergraph.runners.daft._stateful import DaftStateful, get_stateful_options, has_stateful_values, is_stateful
from hypergraph.stateful import StatefulHandle

if TYPE_CHECKING:
    import daft

    from hypergraph.cache import CacheBackend
    from hypergraph.graph import Graph
    from hypergraph.nodes.base import HyperNode


def is_batch(node: HyperNode) -> bool:
    """Check if a node is marked for batch (vectorized) execution."""
    return get_options(node).batch is True


def get_options(node: HyperNode) -> Options:
    """Return Daft lowering options for a node."""
    return get_node_options(node)


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

            udf = daft_mod.func(**_func_options(node, daft_mod))(async_wrapper)
        else:
            udf = daft_mod.func(**_func_options(node, daft_mod))(wrapper)

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
        stateful_vals = {k: v for k, v in bound.items() if is_stateful(v)}
        plain_vals = {k: v for k, v in bound.items() if not is_stateful(v)}

        class_options = _stateful_options(stateful_vals)

        @daft_mod.cls(**class_options.for_cls())
        class StatefulWrapper:
            def __init__(self):
                self._stateful: dict[str, Any] | None = None
                self._plain = plain_vals

            def _stateful_values(self) -> dict[str, Any]:
                if self._stateful is None:
                    self._stateful = _materialize_stateful_values(stateful_vals)
                return self._stateful

            def _call_kwargs(self, *args: Any) -> dict[str, Any]:
                return {
                    **self._stateful_values(),
                    **self._plain,
                    **dict(zip(col_names, args, strict=True)),
                }

            if asyncio.iscoroutinefunction(func):

                @_stateful_method_decorator(daft_mod, node)
                async def __call__(self, *args: Any) -> Any:
                    return await func(**self._call_kwargs(*args))

            else:

                @_stateful_method_decorator(daft_mod, node)
                def __call__(self, *args: Any) -> Any:
                    return func(**self._call_kwargs(*args))

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

        udf = daft_mod.func.batch(**_batch_func_options(node))(batch_wrapper)

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

        from hypergraph.runners._shared.helpers import address_for_node_input

        node = self.node
        # GraphNode inputs are already projected to DataFrame column names:
        # flat, namespaced, or exposed depending on the boundary.
        param_to_col = {p: address_for_node_input(node, p) for p in self.input_columns}
        col_names_for_df = list(param_to_col.values())
        col_names_for_inner = list(param_to_col.keys())

        output_col = self.output_columns[0] if len(self.output_columns) == 1 else f"_pack_{node.name}"
        multi_output = len(self.output_columns) > 1
        cache = self.cache
        bound = self.bound_values

        inner_graph = node.graph
        # map_config is (params, mode, error_handling) or None
        map_config = node.map_config

        # Capture for the closure: build inner_inputs from positional args
        # using the GraphNode's projected input names (not necessarily the same
        # strings as the DataFrame column names).
        inner_param_names = col_names_for_inner

        @daft_mod.func(return_dtype=daft_mod.DataType.python())
        def execute_graph(*args: Any) -> Any:
            from hypergraph.runners._shared.helpers import (
                collect_as_lists,
                map_inputs_to_func_params,
            )
            from hypergraph.runners.sync.runner import SyncRunner

            raw_inputs = {**bound, **dict(zip(inner_param_names, args, strict=True))}
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
                return collected[output_col]
            else:
                result = runner.run(inner_graph, inner_inputs)
                mapped_values = node.map_outputs_from_original(result.values)
                if multi_output:
                    return mapped_values
                return mapped_values[output_col]

        col_refs = [df[c] for c in col_names_for_df]
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
    from hypergraph.runners._shared.helpers import address_for_node_input

    # Determine which bound values apply to this node. GraphNode bound values
    # are keyed by their resolved parent-facing address; store them under the
    # input name this operation receives.
    node_bound: dict[str, Any] = {}
    for param in node.inputs:
        addr = address_for_node_input(node, param)
        if addr in bound_values:
            node_bound[param] = bound_values[addr]

    # Determine input columns (those not provided by bound values). GraphNode
    # boundaries have already projected the parent-facing column addresses.
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
        _validate_common_options(node)
        if is_batch(node):
            _validate_batch_options(node, output_cols)
        if has_stateful_values(node_bound):
            _validate_stateful_constructable(node_bound)
            _validate_stateful_node_options(node)
            return StatefulNodeOperation(
                node=node,
                input_columns=input_cols,
                output_columns=output_cols,
                bound_values=node_bound,
                clone=clone,
            )
        _validate_stateless_options(node)
        if is_batch(node):
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
        if isinstance(v, StatefulHandle):
            continue
        if not isinstance(v, DaftStateful):
            continue
        try:
            inspect.signature(type(v)).bind()
        except TypeError as e:
            raise TypeError(
                f"DaftStateful object {k!r} ({type(v).__name__}) must support "
                f"zero-arg construction for per-worker re-initialization. Got: {e}\n\n"
                f"How to fix: Ensure {type(v).__name__}.__init__() works with no arguments."
            ) from e


def _materialize_stateful(value: Any) -> Any:
    if isinstance(value, StatefulHandle):
        return value.materialize()
    return type(value)()


def _materialize_stateful_values(stateful_vals: dict[str, Any]) -> dict[str, Any]:
    materialized_handles: dict[StatefulHandle, Any] = {}
    materialized: dict[str, Any] = {}
    for name, value in stateful_vals.items():
        if isinstance(value, StatefulHandle):
            if value not in materialized_handles:
                materialized_handles[value] = value.materialize()
            materialized[name] = materialized_handles[value]
        else:
            materialized[name] = _materialize_stateful(value)
    return materialized


def _validate_batch_options(node: HyperNode, output_cols: list[str]) -> None:
    from hypergraph.runners._shared.validation import IncompatibleRunnerError

    if asyncio.iscoroutinefunction(node.func):
        raise IncompatibleRunnerError(
            f"Async batch UDF node {node.name!r} is not supported until Daft batch lowering supports awaiting coroutine results.",
            capability="node_types",
        )
    if len(output_cols) > 1:
        raise IncompatibleRunnerError(
            f"Batch UDFs with multiple outputs are not supported (node {node.name!r} has {output_cols}).",
            capability="node_types",
        )
    if get_options(node).return_dtype is None:
        raise IncompatibleRunnerError(
            f"Batch UDF node {node.name!r} requires return_dtype. Pass return_dtype=... to hypergraph.integrations.daft.node(..., batch=True).",
            capability="node_types",
        )


def _validate_stateful_node_options(node: HyperNode) -> None:
    from hypergraph.runners._shared.validation import IncompatibleRunnerError

    options = get_options(node)
    unsupported = [name for name in ("cpus", "gpus", "use_process", "max_concurrency", "ray_options") if getattr(options, name) is not None]
    if unsupported:
        names = ", ".join(unsupported)
        raise IncompatibleRunnerError(
            f"Daft resource option(s) {names} on stateful node {node.name!r} must be set on @stateful(...), not daft_node(...).",
            capability="node_types",
        )


def _validate_common_options(node: HyperNode) -> None:
    from hypergraph.runners._shared.validation import IncompatibleRunnerError

    options = get_options(node)
    if options.batch_size is not None and not is_batch(node):
        raise IncompatibleRunnerError(
            f"Daft option batch_size is only supported for batch UDF nodes (node {node.name!r} is not batch=True).",
            capability="node_types",
        )


def _validate_stateless_options(node: HyperNode) -> None:
    from hypergraph.runners._shared.validation import IncompatibleRunnerError

    options = get_options(node)
    if options.max_concurrency is not None and not asyncio.iscoroutinefunction(node.func):
        raise IncompatibleRunnerError(
            f"Daft option max_concurrency is only supported for async stateless UDF nodes "
            f"(node {node.name!r} is synchronous). Use @stateful(max_concurrency=...) "
            f"for actor-pool concurrency.",
            capability="node_types",
        )


def _func_options(node: HyperNode, daft_mod: Any) -> dict[str, Any]:
    options = get_options(node)
    kwargs = options.for_func()
    kwargs.setdefault("return_dtype", daft_mod.DataType.python())
    return kwargs


def _batch_func_options(node: HyperNode) -> dict[str, Any]:
    return get_options(node).for_batch_func()


def _method_options(node: HyperNode, daft_mod: Any) -> dict[str, Any]:
    options = get_options(node)
    kwargs = options.for_method()
    kwargs.setdefault("return_dtype", daft_mod.DataType.python())
    return kwargs


def _stateful_options(stateful_vals: dict[str, Any]) -> Options:
    options_by_name = {name: get_stateful_options(value) for name, value in stateful_vals.items()}
    unique: list[Options] = []
    for options in options_by_name.values():
        if options not in unique:
            unique.append(options)
    if len(unique) > 1:
        details = ", ".join(f"{name}={options!r}" for name, options in sorted(options_by_name.items()))
        raise ValueError(f"Stateful Daft resources in one node must use identical class options. Got: {details}")
    return unique[0] if unique else DEFAULT_OPTIONS


def _stateful_method_decorator(daft_mod: Any, node: HyperNode) -> Callable[[Callable], Callable]:
    if is_batch(node):
        return daft_mod.method.batch(**_batch_method_options(node))
    return daft_mod.method(**_method_options(node, daft_mod))


def _batch_method_options(node: HyperNode) -> dict[str, Any]:
    return get_options(node).for_batch_method()


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
