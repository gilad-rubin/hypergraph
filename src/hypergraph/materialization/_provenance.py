"""HyperTable provenance policy and pure reconcile planning.

This module owns the recipe/value-chain decisions that decide which stored
columns can be reused and which graph node must run next.  It deliberately has
no store or runner dependency: callers capture physical state, feed runner
results back into the immutable reconcile state, and apply writes elsewhere.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from hypergraph import Graph
from hypergraph.materialization._fingerprint import (
    _component_config_hashes,
    _plain_value_payload,
    compute_child_fingerprint,
    compute_column_provenance,
    compute_node_definition_hash,
    compute_payload_hash,
    compute_recipe_fingerprint,
    compute_row_fingerprint,
    compute_table_recipe_fingerprint,
)
from hypergraph.materialization._recipe_journal import (
    KIND_BOUND_VALUE,
    KIND_COMPONENT_CONFIG,
    KIND_NODE_SOURCE,
)
from hypergraph.materialization._schema import TableSpec, is_internal_column, node_func

_Items = tuple[tuple[str, Any], ...]


def _freeze(values: Mapping[str, Any]) -> _Items:
    return tuple(values.items())


def _thaw(values: _Items) -> dict[str, Any]:
    return dict(values)


def normalize_value(value: Any) -> Any:
    """Convert numpy/arrow scalars into the public Python representation."""
    import numpy as np

    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    return value


def split_boundary_provenance(value: Any) -> tuple[str | None, int | None]:
    """Parse ``<provenance>#<item-count>`` stored at a fan-out boundary."""
    if not isinstance(value, str) or "#" not in value:
        return None, None
    provenance, _, count = value.rpartition("#")
    try:
        return provenance, int(count)
    except ValueError:
        return None, None


def find_boundary_node(graph: Any, child_spec: TableSpec) -> Any:
    """Find the root node that produces a child's mapped-items column."""
    if not child_spec.map_input:
        return None
    nodes = graph.nodes if isinstance(graph.nodes, dict) else {}
    for node in nodes.values():
        if child_spec.map_input in (node.data_outputs if hasattr(node, "data_outputs") else ()):
            return node
    return None


@dataclass(frozen=True, slots=True)
class RecipeEntry:
    """One durable recipe-journal entry planned without touching the journal."""

    hash: str
    kind: str
    payload: str


@dataclass(frozen=True, slots=True)
class RebuildChildren:
    """Reuse child source rows because their fan-out boundary is fresh."""

    spec: TableSpec


@dataclass(frozen=True, slots=True)
class DerivedChildren:
    """Use the item list returned by a newly executed fan-out boundary."""

    spec: TableSpec
    items: tuple[Any, ...]


ChildSelection = RebuildChildren | DerivedChildren


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """The complete, runner-neutral outcome of column reconciliation."""

    outputs: _Items
    provenances: tuple[tuple[str, str], ...]
    children: tuple[ChildSelection, ...]

    def output_values(self) -> dict[str, Any]:
        return _thaw(self.outputs)

    def provenance_values(self) -> dict[str, str]:
        return dict(self.provenances)


@dataclass(frozen=True, slots=True)
class ReconcileState:
    """Immutable progress through derived nodes and child boundaries."""

    spec: TableSpec
    existing: _Items
    values: _Items
    incoming_names: tuple[str, ...]
    outputs: _Items
    provenances: tuple[tuple[str, str], ...]
    nodes: tuple[Any, ...]
    node_index: int
    boundary_counts: tuple[tuple[str, int], ...]
    boundary_index: int
    children: tuple[ChildSelection, ...]


@dataclass(frozen=True, slots=True)
class RunNode:
    """A single unavoidable runner call requested by the pure planner."""

    node: Any
    inputs: _Items
    provenance: str
    kind: Literal["column", "boundary"]
    child_spec: TableSpec | None = None

    def input_values(self) -> dict[str, Any]:
        return _thaw(self.inputs)


@dataclass(frozen=True, slots=True)
class ReconcileUnavailable:
    """Stored values cannot support column-scoped reconciliation."""


@dataclass(frozen=True, slots=True)
class ReconcileComplete:
    """The planner needs no further runner calls."""

    result: ReconcileResult


ReconcileStep = RunNode | ReconcileUnavailable | ReconcileComplete


class Provenance:
    """Cohesive recipe and value-chain policy for one analyzed HyperTable."""

    def __init__(
        self,
        graph: Any,
        spec: TableSpec,
        components: Mapping[str, Any],
        column_graphs: dict[int, Any],
    ) -> None:
        self.graph = graph
        self.spec = spec
        self.components = components
        self._column_graphs = column_graphs

    def derived_columns(self, spec: TableSpec | None = None) -> list[Any]:
        target = spec or self.spec
        return [column for column in target.columns if column.role in ("derived", "answer")]

    @staticmethod
    def column_producers(column: Any) -> tuple[Any, ...]:
        producer = column.produced_by
        return producer if isinstance(producer, tuple) else (producer,)

    @staticmethod
    def node_params(node: Any) -> Mapping[str, inspect.Parameter]:
        func = node_func(node)
        if func is not None:
            return inspect.signature(func).parameters
        params: dict[str, inspect.Parameter] = {}
        for name in getattr(node, "inputs", ()):
            default = inspect.Parameter.empty
            if hasattr(node, "has_default_for") and node.has_default_for(name):
                default = node.get_default_for(name)
            params[name] = inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default)
        return params

    def node_columns(self, node: Any, spec: TableSpec | None = None) -> list[Any]:
        return [column for column in self.derived_columns(spec) if any(producer is node for producer in self.column_producers(column))]

    def producing_node(self, column: str) -> Any:
        for column_spec in self.derived_columns():
            if column_spec.name == column:
                return self.column_producers(column_spec)[0]
        raise KeyError(f"{column!r} is not a derived column")

    def nodes_in_dependency_order(self, spec: TableSpec | None = None) -> tuple[Any, ...]:
        derived = self.derived_columns(spec)
        nodes: list[Any] = []
        seen: set[int] = set()
        for column in derived:
            for producer in self.column_producers(column):
                if id(producer) not in seen:
                    seen.add(id(producer))
                    nodes.append(producer)
        derived_names = {column.name for column in derived}
        placed: set[str] = set()
        ordered: list[Any] = []
        remaining = list(nodes)
        while remaining:
            progressed = False
            for node in list(remaining):
                dependencies = {name for name in self.node_params(node) if name in derived_names}
                if dependencies <= placed:
                    ordered.append(node)
                    placed.update(column.name for column in self.node_columns(node, spec))
                    remaining.remove(node)
                    progressed = True
            if not progressed:
                ordered.extend(remaining)
                break
        return tuple(ordered)

    def node_provenance(self, node: Any, values: Mapping[str, Any]) -> str | None:
        params = self.node_params(node)
        components = {name: value for name, value in self.components.items() if name in params}
        inputs: dict[str, Any] = {}
        for name, parameter in params.items():
            if name in components:
                continue
            if name in values:
                inputs[name] = values[name]
            elif parameter.default is inspect.Parameter.empty:
                return None
        return compute_column_provenance(node, inputs, _component_config_hashes(components))

    def node_recipe(self, node: Any) -> str:
        params = self.node_params(node)
        components = {name: value for name, value in self.components.items() if name in params}
        return compute_recipe_fingerprint(node, _component_config_hashes(components))

    def root_fingerprint(self, graph_inputs: Mapping[str, Any]) -> str:
        return compute_row_fingerprint(self.graph, dict(self.components), dict(graph_inputs))

    def child_fingerprint(self, child_inputs: Mapping[str, Any], child_spec: TableSpec) -> str:
        return compute_child_fingerprint(child_spec.child_graph, dict(self.components), dict(child_inputs))

    def table_stamps_recipe(self) -> bool:
        return bool(self.derived_columns() or self.spec.children)

    def current_recipe_fingerprint(self) -> str:
        return compute_table_recipe_fingerprint(self.graph, dict(self.components))

    def current_child_recipe_fingerprint(self, child_spec: TableSpec) -> str:
        child_graph = child_spec.child_graph
        valid_inputs = set(child_graph.inputs.all) if child_graph is not None and hasattr(child_graph.inputs, "all") else set()
        return compute_table_recipe_fingerprint(child_graph, dict(self.components), valid_inputs)

    def row_missing_stamp(self, row: Mapping[str, Any], recipe_column: str) -> bool:
        stamp = row.get(recipe_column)
        return self.table_stamps_recipe() and (not isinstance(stamp, str) or not stamp)

    def recipe_entries(self, node: Any) -> tuple[RecipeEntry, ...]:
        func = node_func(node)
        # Identity is the node's construction-time hash; the func is kept only
        # for the readable source text (a functionless GraphNode reads as its
        # repr — never as "None").
        entries = [RecipeEntry(compute_node_definition_hash(node), KIND_NODE_SOURCE, self.node_source(func if func is not None else node))]
        params = self.node_params(node)
        for name, component in self.components.items():
            if name not in params:
                continue
            payload, kind = self.component_payload(component)
            if payload is not None:
                entries.append(RecipeEntry(compute_payload_hash(payload), kind, payload))
        return tuple(entries)

    @staticmethod
    def node_source(func: Any) -> str:
        try:
            return inspect.getsource(func)
        except (OSError, TypeError):
            return repr(func)

    @staticmethod
    def component_payload(component: Any) -> tuple[str | None, str]:
        config = getattr(component, "__component_config__", None) or (component._config() if hasattr(component, "_config") else None)
        if config is not None:
            return str(config), KIND_COMPONENT_CONFIG
        plain = _plain_value_payload(component)
        if plain is not None:
            return plain, KIND_BOUND_VALUE
        return None, KIND_BOUND_VALUE

    def column_graph(self, node: Any) -> Any:
        graph = self._column_graphs.get(id(node))
        if graph is None:
            label = getattr(node_func(node), "__name__", None) or getattr(node, "name", "column")
            graph = Graph([node], name=f"{self.spec.name}__{label}")
            binds = {name: value for name, value in self.components.items() if name in set(graph.inputs.all)}
            if binds:
                graph = graph.bind(**binds)
            self._column_graphs[id(node)] = graph
        return graph

    def node_inputs(self, node: Any, values: Mapping[str, Any]) -> dict[str, Any]:
        return {name: values[name] for name in self.node_params(node) if name not in self.components and name in values}

    @staticmethod
    def stored_values(row: Mapping[str, Any]) -> dict[str, Any]:
        return {name: normalize_value(value) for name, value in row.items() if not is_internal_column(name)}

    def node_is_fresh(self, node: Any, provenance: str, existing: Mapping[str, Any], spec: TableSpec | None = None) -> bool:
        return all(
            existing.get(f"_provenance_{column.name}") == provenance and not self.column_is_null(existing.get(column.name))
            for column in self.node_columns(node, spec)
        )

    def boundary_node(self, child_spec: TableSpec) -> Any:
        return find_boundary_node(self.graph, child_spec)

    def boundary_provenance_value(self, provenance: str, items: Any) -> str:
        count = len(items) if isinstance(items, list) else 0
        return f"{provenance}#{count}"

    def child_source_inputs(self, row: Mapping[str, Any], child_spec: TableSpec) -> dict[str, Any]:
        return {
            column.name: normalize_value(row[column.name])
            for column in child_spec.columns
            if column.role == "source" and column.content_key and column.name in row
        }

    def source_inputs(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Reconstruct root graph inputs from stored source columns."""
        return {column.name: normalize_value(row[column.name]) for column in self.spec.columns if column.role == "source" and column.name in row}

    @staticmethod
    def column_is_null(value: Any) -> bool:
        import math

        return value is None or (isinstance(value, float) and math.isnan(value))

    def row_converged(self, row: Mapping[str, Any]) -> bool:
        values = self.stored_values(row)
        for node in self.nodes_in_dependency_order():
            provenance = self.node_provenance(node, values)
            for column in self.node_columns(node):
                if provenance is None or self.column_is_null(row.get(column.name)) or row.get(f"_provenance_{column.name}") != provenance:
                    return False
        return True

    def start_reconcile(
        self,
        spec: TableSpec,
        existing: Mapping[str, Any],
        incoming_values: Mapping[str, Any],
        boundary_counts: Mapping[str, int] | None = None,
    ) -> ReconcileState:
        values = self.stored_values(existing)
        values.update(incoming_values)
        return ReconcileState(
            spec=spec,
            existing=_freeze(existing),
            values=_freeze(values),
            incoming_names=tuple(incoming_values),
            outputs=(),
            provenances=(),
            nodes=self.nodes_in_dependency_order(spec),
            node_index=0,
            boundary_counts=tuple((boundary_counts or {}).items()),
            boundary_index=0,
            children=(),
        )

    def next_reconcile_step(self, state: ReconcileState) -> tuple[ReconcileState, ReconcileStep]:
        current = state
        while current.node_index < len(current.nodes):
            node = current.nodes[current.node_index]
            values = _thaw(current.values)
            existing = _thaw(current.existing)
            provenance = self.node_provenance(node, values)
            if provenance is None:
                return current, ReconcileUnavailable()
            if getattr(node, "is_interrupt", False):
                answer_columns = self.node_columns(node, current.spec)
                if answer_columns and all(column.name in current.incoming_names for column in answer_columns):
                    current = self._advance_column(
                        current,
                        node,
                        provenance,
                        {column.name: values[column.name] for column in answer_columns},
                    )
                    continue
            if not self.node_is_fresh(node, provenance, existing, current.spec):
                return current, RunNode(
                    node=node,
                    inputs=_freeze(self.node_inputs(node, values)),
                    provenance=provenance,
                    kind="column",
                )
            node_outputs = {column.name: normalize_value(existing[column.name]) for column in self.node_columns(node, current.spec)}
            current = self._advance_column(current, node, provenance, node_outputs)

        while current.boundary_index < len(current.spec.children):
            child_spec = current.spec.children[current.boundary_index]
            boundary = self.boundary_node(child_spec)
            if boundary is None or any(boundary in self.column_producers(column) for column in self.derived_columns()):
                return current, ReconcileUnavailable()
            values = _thaw(current.values)
            provenance = self.node_provenance(boundary, values)
            if provenance is None:
                return current, ReconcileUnavailable()
            existing = _thaw(current.existing)
            stored = existing.get(f"_provenance_{child_spec.map_input}")
            stored_provenance, stored_count = split_boundary_provenance(stored)
            counts = dict(current.boundary_counts)
            if stored_provenance == provenance and stored_count == counts.get(child_spec.name, 0):
                current = ReconcileState(
                    spec=current.spec,
                    existing=current.existing,
                    values=current.values,
                    incoming_names=current.incoming_names,
                    outputs=current.outputs,
                    provenances=(*current.provenances, (child_spec.map_input, stored)),
                    nodes=current.nodes,
                    node_index=current.node_index,
                    boundary_counts=current.boundary_counts,
                    boundary_index=current.boundary_index + 1,
                    children=(*current.children, RebuildChildren(child_spec)),
                )
                continue
            return current, RunNode(
                node=boundary,
                inputs=_freeze(self.node_inputs(boundary, values)),
                provenance=provenance,
                kind="boundary",
                child_spec=child_spec,
            )

        return current, ReconcileComplete(
            ReconcileResult(
                outputs=current.outputs,
                provenances=current.provenances,
                children=current.children,
            )
        )

    def apply_reconcile_result(
        self,
        state: ReconcileState,
        request: RunNode,
        node_outputs: Mapping[str, Any],
    ) -> ReconcileState:
        if request.kind == "column":
            return self._advance_column(state, request.node, request.provenance, node_outputs)
        child_spec = request.child_spec
        if child_spec is None:
            raise RuntimeError("boundary reconcile request is missing its child table spec")
        raw_items = node_outputs.get(child_spec.map_input)
        items = raw_items if isinstance(raw_items, list) else []
        return ReconcileState(
            spec=state.spec,
            existing=state.existing,
            values=state.values,
            incoming_names=state.incoming_names,
            outputs=state.outputs,
            provenances=(
                *state.provenances,
                (child_spec.map_input, self.boundary_provenance_value(request.provenance, items)),
            ),
            nodes=state.nodes,
            node_index=state.node_index,
            boundary_counts=state.boundary_counts,
            boundary_index=state.boundary_index + 1,
            children=(*state.children, DerivedChildren(child_spec, tuple(items))),
        )

    def _advance_column(
        self,
        state: ReconcileState,
        node: Any,
        provenance: str,
        node_outputs: Mapping[str, Any],
    ) -> ReconcileState:
        values = _thaw(state.values)
        outputs = _thaw(state.outputs)
        provenances = dict(state.provenances)
        for column in self.node_columns(node, state.spec):
            if column.name in node_outputs:
                outputs[column.name] = node_outputs[column.name]
                values[column.name] = node_outputs[column.name]
            provenances[column.name] = provenance
        return ReconcileState(
            spec=state.spec,
            existing=state.existing,
            values=_freeze(values),
            incoming_names=state.incoming_names,
            outputs=_freeze(outputs),
            provenances=tuple(provenances.items()),
            nodes=state.nodes,
            node_index=state.node_index + 1,
            boundary_counts=state.boundary_counts,
            boundary_index=state.boundary_index,
            children=state.children,
        )
