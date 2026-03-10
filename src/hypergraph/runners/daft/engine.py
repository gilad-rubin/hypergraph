"""Columnar execution engine: builds and executes Daft query plans from hypergraph DAGs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import daft

    from hypergraph.cache import CacheBackend
    from hypergraph.graph import Graph
    from hypergraph.runners.daft.operations import DaftOperation


def build_execution_plan(
    graph: Graph,
    bound_values: dict[str, Any],
    cache: CacheBackend | None = None,
    clone: bool | list[str] = False,
) -> list[DaftOperation]:
    """Topologically sort DAG, create one DaftOperation per node.

    Args:
        graph: A validated DAG (no cycles, no gates, no interrupts).
        bound_values: Values bound to the graph via ``graph.bind()``.
        cache: Optional node-level cache backend.
        clone: Deep-copy strategy for bound values.

    Returns:
        Ordered list of operations ready for ``execute_plan()``.
    """
    import networkx as nx

    from hypergraph.runners.daft.operations import create_operation

    topo_order = list(nx.topological_sort(graph._nx_graph))
    operations = []
    for node_name in topo_order:
        node = graph._nodes[node_name]
        op = create_operation(node, graph, bound_values, cache, clone)
        operations.append(op)
    return operations


def execute_plan(
    df: daft.DataFrame,
    plan: list[DaftOperation],
) -> daft.DataFrame:
    """Apply each operation to the DataFrame in topological order.

    Each operation adds one or more columns via ``df.with_column()``.

    Args:
        df: Input DataFrame with initial columns.
        plan: Ordered operations from ``build_execution_plan()``.

    Returns:
        DataFrame with all output columns added.
    """
    for op in plan:
        df = op.apply(df)
    return df


def build_input_dataframe(
    input_variations: list[dict[str, Any]],
    all_keys: list[str],
) -> daft.DataFrame:
    """Build an N-row DataFrame from input variations.

    Args:
        input_variations: List of input dicts (one per row).
        all_keys: Column names to include.

    Returns:
        Daft DataFrame with one column per key and one row per variation.
    """
    import daft as daft_mod

    columns: dict[str, list[Any]] = {key: [] for key in all_keys}
    for variation in input_variations:
        for key in all_keys:
            columns[key].append(variation.get(key))
    return daft_mod.from_pydict(columns)
