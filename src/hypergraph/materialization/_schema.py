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
QUESTION_COLUMN = "_question"
# Per-column change entries for a PARTIAL row: which derived columns a
# failure nulled and why, plus one per failed fan-out boundary naming its
# map_over input (not a derived column). Additive — old stores gain the
# column via idempotent schema evolution the first time a partial row is written.
CHANGES_COLUMN = "_changes"
PARENT_LINK_COLUMN = "_parent_id"
PROVENANCE_PREFIX = "_provenance_"
# The per-row RECIPE-ONLY stamp (node code + component configs + bound plain
# values, NO input values) written at derive time. Additive: old stores gain
# the column via idempotent schema evolution on first stamped write; rows
# without it read as drifted-UNKNOWN, never as current (see RecipeDrift).
RECIPE_COLUMN = "_recipe_fingerprint"

# Names a user may not give an identity/source/derived column. A reserved name is
# any framework-managed column plus the parent link.
RESERVED_NAMES = frozenset({*FINGERPRINT_COLUMNS, *STATUS_COLUMNS, PARENT_LINK_COLUMN, RECIPE_COLUMN, QUESTION_COLUMN, CHANGES_COLUMN})


def is_reserved_name(name: str) -> bool:
    return name in RESERVED_NAMES or name.startswith(PROVENANCE_PREFIX)


def is_internal_column(name: str) -> bool:
    """Internal columns are stripped from public rows (status is handled separately)."""
    return (
        name in FINGERPRINT_COLUMNS
        or name in STATUS_COLUMNS
        or name in (PARENT_LINK_COLUMN, RECIPE_COLUMN, QUESTION_COLUMN, CHANGES_COLUMN)
        or name.startswith(PROVENANCE_PREFIX)
    )


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


def graph_bound_names(graph: Any) -> frozenset[str]:
    """Every name bound anywhere inside a graph, by its parent-facing address.

    ``InputSpec`` already surfaces a nested graph's bindings under the boundary
    address the enclosing graph addresses them by, so this is one read, not a
    second reachability walk.

    A bound name is recipe, never data: it is why such a name is neither a
    child source column nor a value a node may be handed at run time.
    """
    try:
        return frozenset(graph.inputs.bound)
    except (AttributeError, TypeError):  # pragma: no cover - non-Graph stand-ins in unit tests
        return frozenset()


def dead_bound_names(spec: TableSpec) -> frozenset[str]:
    """Names a store written before #447 may still hold a dead child column for.

    A name the child graph binds is recipe, so ``_analyze_map_over`` builds no
    column for it and nothing can write a value into one. Two same-named things
    are NOT that residue and must keep working:

    * a column the spec itself declares (a child node whose ``output_name``
      happens to match a name bound deeper down) — it holds a derived value;
    * a user annotation column ``ChildTable.set()`` evolved before the graph
      grew the binding — it holds whatever the user put there.

    Subtracting the declared columns separates the first. The second is
    indistinguishable by name, so callers additionally require the stored value
    to be NULL: a dead column is always NULL, a real annotation is not.
    """
    return graph_bound_names(spec.child_graph) - {column.name for column in spec.columns}


def return_type(node: Any) -> Any:
    func = node_func(node)
    if func is None:
        return str
    return typing.get_type_hints(func).get("return", str)


def item_schema_fields(schema: Any) -> tuple[str, ...]:
    """Field names of a mapped-item schema, or ``()`` when it has none.

    Used by the fan-out viz to tell an inner input that is fed by an item
    field (``page_text`` off a ``list[PageItem]``) apart from a genuine
    external input. Accepts either the item type directly (``PageItem``) or
    the producer's ``list[PageItem]`` annotation. Only mapping-like schemas
    (TypedDicts, dataclasses, Pydantic models) expose fields; a ``list[str]``
    item has none, so the caller falls back to the entrypoint.
    """
    if schema is None:
        return ()
    origin = typing.get_origin(schema)
    if origin in (list, tuple):
        args = typing.get_args(schema)
        if not args:
            return ()
        schema = args[0]
    # Pydantic models expose their fields under ``model_fields``; TypedDicts and
    # dataclasses expose them via annotations (get_type_hints resolves both).
    model_fields = getattr(schema, "model_fields", None)
    if isinstance(model_fields, dict) and model_fields:
        return tuple(model_fields.keys())
    try:
        hints = typing.get_type_hints(schema)
    except Exception:
        hints = getattr(schema, "__annotations__", {}) or {}
    return tuple(hints.keys())


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
    if tp is list:
        # The bare class, as Table._evolve passes for a list VALUE whose
        # element type nothing declared. Falling through to utf8 built a
        # string column that rejected the very value that created it.
        return pa.list_(pa.utf8())

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
        _column(QUESTION_COLUMN, role="internal"),
    ]


def analyze_table(
    graph: Any,
    identity: str,
    components: dict[str, Any],
    map_over_nodes: list,
    *,
    name: str | None = None,
) -> TableSpec:
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
    output_columns: dict[str, ColumnSpec] = {}
    for _name, n in nodes_dict.items():
        for out_name in n.data_outputs if hasattr(n, "data_outputs") else ():
            if out_name not in child_map_inputs:
                _validate_column_name(out_name, "derived")
                role = "answer" if getattr(n, "is_interrupt", False) else "derived"
                python_type = n.get_output_type(out_name) if role == "answer" else return_type(n)
                column = _column(out_name, role=role, produced_by=n, python_type=python_type or str)
                existing = output_columns.get(out_name)
                if existing is None:
                    output_columns[out_name] = column
                    root_columns.append(column)
                else:
                    producers = existing.produced_by if isinstance(existing.produced_by, tuple) else (existing.produced_by,)
                    merged = ColumnSpec(
                        name=existing.name,
                        role=existing.role,
                        produced_by=(*producers, n),
                        content_key=existing.content_key,
                        arrow_type=existing.arrow_type,
                    )
                    output_columns[out_name] = merged
                    root_columns[root_columns.index(existing)] = merged

    derived_cols = [c for c in root_columns if c.role in ("derived", "answer")]
    prov_cols = [_column(f"{PROVENANCE_PREFIX}{c.name}", role="internal") for c in derived_cols]
    # One boundary provenance column per map_over input: the recipe hash (+ distinct child-identity count)
    # of the node producing the mapped items. Child specs over one input share it; the items list itself
    # is never stored.
    boundary_inputs = dict.fromkeys(cs.map_input for cs in child_specs if cs.map_input)
    boundary_prov_cols = [_column(f"{PROVENANCE_PREFIX}{map_input}", role="internal") for map_input in boundary_inputs]
    # The recipe stamp exists only where a recipe exists: a plain table (no
    # derived columns, no children) never grows the column.
    recipe_cols = [_column(RECIPE_COLUMN, role="internal")] if derived_cols or child_specs else []
    final_columns = [
        *root_columns,
        _column("_row_fingerprint", role="internal"),
        *prov_cols,
        *boundary_prov_cols,
        *recipe_cols,
        _column("_write_gen", role="internal", python_type=int),
        _column("_status", role="internal"),
        _column("_error", role="internal"),
        _column(QUESTION_COLUMN, role="internal"),
        _column(CHANGES_COLUMN, role="internal"),
    ]

    return TableSpec(name=name or identity.replace("_id", ""), identity=identity, columns=final_columns, children=child_specs)


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
        # A name the child graph binds (at any depth) is recipe, so nothing can
        # ever write a value into a column for it. ``optional`` stays in the set:
        # a child input with a DEFAULT is fed from the item dict and is a real
        # column. Only the bound names drop out.
        bound = graph_bound_names(inner_graph)
        for inp_name in sorted(inner_all):
            if inp_name != identity and inp_name not in component_names and inp_name not in bound:
                child_columns.append(_column(inp_name, role="source", content_key=True, python_type=input_types.get(inp_name, str)))
        nodes_dict = inner_graph.nodes if isinstance(inner_graph.nodes, dict) else {}
        for _name, n in nodes_dict.items():
            for out_name in n.data_outputs if hasattr(n, "data_outputs") else []:
                _validate_column_name(out_name, "derived")
                child_columns.append(_column(out_name, role="derived", produced_by=n, python_type=return_type(n)))
        derived_child_cols = [c for c in child_columns if c.role == "derived"]
        child_columns.extend(_column(f"{PROVENANCE_PREFIX}{c.name}", role="internal") for c in derived_child_cols)
        # Child rows stamp their own child-graph recipe (scoped, like the
        # child fingerprint), so the column exists exactly when a recipe does.
        child_columns.append(_column(RECIPE_COLUMN, role="internal"))

    child_columns.extend(_internal_columns())

    return TableSpec(
        name=identity.replace("_id", ""),
        identity=identity,
        columns=child_columns,
        parent_link=PARENT_LINK_COLUMN,
        child_graph=inner_graph,
        map_input=map_input,
    )
