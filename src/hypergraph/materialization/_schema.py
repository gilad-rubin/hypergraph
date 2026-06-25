"""Schema analysis for HyperTable.

Turns a graph + identity (+ any ``map_over`` child nodes) into a ``TableSpec``:
the physical columns each table materializes. This is a pure transform —
``analyze_table`` reads the graph's inputs/outputs and never touches a store.

It is also the single source of truth for the reserved / internal column names
that the read path strips and that build-time validation rejects.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass, field
from typing import Any

# --- Reserved / internal column names (one source of truth) ---

FINGERPRINT_COLUMNS = ("_row_fingerprint", "_write_gen")
STATUS_COLUMNS = ("_status", "_error")
PARENT_LINK_COLUMN = "_parent_id"
PROVENANCE_PREFIX = "_provenance_"

# Names a user may not give an identity/source/derived column. A reserved name is
# any framework-managed column plus the parent link.
RESERVED_NAMES = frozenset({*FINGERPRINT_COLUMNS, *STATUS_COLUMNS, PARENT_LINK_COLUMN})


def is_reserved_name(name: str) -> bool:
    return name in RESERVED_NAMES or name.startswith(PROVENANCE_PREFIX)


def is_internal_column(name: str) -> bool:
    """Internal columns are stripped from public rows (status is handled separately)."""
    return name in FINGERPRINT_COLUMNS or name in STATUS_COLUMNS or name.startswith(PROVENANCE_PREFIX)


# --- Column / table specs ---


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    role: str  # identity, source, derived, parent_link, internal
    produced_by: Any = None
    content_key: bool = False
    arrow_type: Any = None


@dataclass(frozen=True)
class TableSpec:
    name: str
    identity: str
    columns: list[ColumnSpec] = field(default_factory=list)
    children: list[TableSpec] = field(default_factory=list)
    parent_link: str | None = None
    child_graph: Any = None
    map_input: str | None = None


# --- Graph introspection helpers ---


def node_func(node: Any) -> Any:
    """A node's underlying callable, whichever attribute exposes it."""
    return getattr(node, "func", None) or getattr(node, "_func", None)


def input_names(value: Any) -> set[str]:
    """Normalize an InputSpec field (tuple of names or name->type dict) to a name set."""
    return set(value) if isinstance(value, tuple) else set(value.keys())


def return_type(node: Any) -> Any:
    func = node_func(node)
    if func is None:
        return str
    return typing.get_type_hints(func).get("return", str)


def _input_types(graph: Any) -> dict[str, Any]:
    input_types: dict[str, Any] = {}
    nodes_dict = graph.nodes if isinstance(graph.nodes, dict) else {}
    for _name, node_obj in nodes_dict.items():
        func = node_func(node_obj)
        if func is None:
            continue
        for name, hint in typing.get_type_hints(func).items():
            if name != "return":
                input_types.setdefault(name, hint)
    return input_types


def python_type_to_arrow(tp: Any) -> Any:
    import pyarrow as pa

    if tp is str:
        return pa.utf8()
    if tp is int:
        return pa.int64()
    if tp is float:
        return pa.float64()
    if tp is bool:
        return pa.bool_()
    if tp is bytes:
        return pa.large_binary()

    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is list:
        if args and args[0] is float:
            return pa.list_(pa.float32())
        if args and args[0] is str:
            return pa.list_(pa.utf8())
        if args and args[0] is int:
            return pa.list_(pa.int64())
        return pa.list_(pa.utf8())
    return pa.utf8()


def _column(name: str, *, role: str, produced_by: Any = None, content_key: bool = False, python_type: Any = str) -> ColumnSpec:
    return ColumnSpec(name, role=role, produced_by=produced_by, content_key=content_key, arrow_type=python_type_to_arrow(python_type))


def _validate_column_name(name: str, context: str) -> None:
    if is_reserved_name(name):
        raise ValueError(
            f"{context} column name {name!r} is reserved for internal use. Reserved names: {', '.join(sorted(RESERVED_NAMES))} and {PROVENANCE_PREFIX}*"
        )


def _internal_columns() -> list[ColumnSpec]:
    return [
        _column("_row_fingerprint", role="internal"),
        _column("_write_gen", role="internal", python_type=int),
        _column("_status", role="internal"),
        _column("_error", role="internal"),
    ]


def analyze_table(graph: Any, identity: str, components: dict[str, Any], map_over_nodes: list) -> TableSpec:
    """Build the root ``TableSpec``: identity + source columns + derived columns + child tables."""
    input_types = _input_types(graph)
    _validate_column_name(identity, "identity")
    root_columns = [_column(identity, role="identity")]

    for inp_name in sorted(input_names(graph.inputs.required)):
        if inp_name == identity:
            continue
        _validate_column_name(inp_name, "source")
        root_columns.append(_column(inp_name, role="source", content_key=True, python_type=input_types.get(inp_name, str)))

    child_specs = [spec for map_node in map_over_nodes if (spec := _analyze_map_over(map_node, components)) is not None]
    child_map_inputs = {cs.map_input for cs in child_specs if cs.map_input}

    nodes_dict = graph.nodes if isinstance(graph.nodes, dict) else {}
    for _name, n in nodes_dict.items():
        for out_name in n.data_outputs if hasattr(n, "data_outputs") else ():
            if out_name not in child_map_inputs:
                root_columns.append(_column(out_name, role="derived", produced_by=n, python_type=return_type(n)))

    derived_cols = [c for c in root_columns if c.role == "derived"]
    prov_cols = [_column(f"{PROVENANCE_PREFIX}{c.name}", role="internal") for c in derived_cols]
    final_columns = [
        *root_columns,
        _column("_row_fingerprint", role="internal"),
        *prov_cols,
        _column("_write_gen", role="internal", python_type=int),
        _column("_status", role="internal"),
        _column("_error", role="internal"),
    ]

    return TableSpec(name=identity.replace("_id", ""), identity=identity, columns=final_columns, children=child_specs)


def _analyze_map_over(map_node: Any, components: dict[str, Any]) -> TableSpec | None:
    config = map_node._map_config if hasattr(map_node, "_map_config") else {}
    identity = config.get("identity", "item_id")
    inner_graph = getattr(map_node, "graph", None) or getattr(map_node, "_graph", None)
    raw_map_over = getattr(map_node, "_map_over", None)
    map_input = raw_map_over[0] if isinstance(raw_map_over, list) and raw_map_over else config.get("map_over")

    child_columns = [_column(identity, role="identity"), _column(PARENT_LINK_COLUMN, role="parent_link")]

    if inner_graph:
        input_types = _input_types(inner_graph)
        component_names = set(components.keys())
        inner_all = input_names(inner_graph.inputs.required) | input_names(inner_graph.inputs.optional)
        for inp_name in sorted(inner_all):
            if inp_name != identity and inp_name not in component_names:
                child_columns.append(_column(inp_name, role="source", content_key=True, python_type=input_types.get(inp_name, str)))
        nodes_dict = inner_graph.nodes if isinstance(inner_graph.nodes, dict) else {}
        for _name, n in nodes_dict.items():
            for out_name in n.data_outputs if hasattr(n, "data_outputs") else []:
                child_columns.append(_column(out_name, role="derived", produced_by=n, python_type=return_type(n)))

    child_columns.extend(_internal_columns())

    return TableSpec(
        name=identity.replace("_id", ""),
        identity=identity,
        columns=child_columns,
        parent_link=PARENT_LINK_COLUMN,
        child_graph=inner_graph,
        map_input=map_input,
    )
