"""HyperTable provenance policy and pure reconcile planning.

This module owns the recipe/value-chain decisions that decide which stored
columns can be reused and which graph node must run next.  It deliberately has
no store or runner dependency: callers capture physical state, feed runner
results back into the immutable reconcile state, and apply writes elsewhere.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from hypergraph import Graph
from hypergraph.materialization._fingerprint import (
    _component_config_hashes,
    _plain_value_payload,
    combine_recipe_fingerprints,
    compute_child_fingerprint,
    compute_column_provenance,
    compute_node_definition_hash,
    compute_payload_hash,
    compute_recipe_fingerprint,
    compute_row_fingerprint,
    compute_table_recipe_fingerprint,
    graph_bound_payloads,
    nested_bound_payloads,
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


def iter_child_specs(spec: TableSpec) -> Iterator[TableSpec]:
    """Every child TableSpec below ``spec``, at any depth."""
    for child in spec.children:
        yield child
        yield from iter_child_specs(child)


def _graph_bound_names(graph: Any) -> frozenset[str]:
    """Every name bound anywhere inside a graph, by its parent-facing address.

    ``InputSpec`` already surfaces a nested graph's bindings under the boundary
    address the enclosing graph addresses them by, so this is one read, not a
    second reachability walk.
    """
    try:
        return frozenset(graph.inputs.bound)
    except (AttributeError, TypeError):  # pragma: no cover - non-Graph stand-ins in unit tests
        return frozenset()


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
        self._mounted_payloads_cache: dict[str, list[str]] = {}
        self._bound_by_node: dict[int, tuple[Mapping[str, Any], frozenset[str]]] | None = None
        self._answer_graphs: dict[tuple[str, ...], Any] = {}
        self._routed_graphs: dict[tuple[str, str], Any] = {}

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

    def _bindings(self, node: Any) -> tuple[Mapping[str, Any], frozenset[str]]:
        """``(values visible at or above this node, every name it must not be handed)``.

        Only the ROOT graph's bindings ever reach ``self.components``. A graph
        mounted as a child — or nested inside one — carries its own
        ``bind(...)`` values, and to a node under it those are
        indistinguishable from a root binding: recipe, never a stored source
        input to be fed back from the row.

        The two halves answer different questions and must not be conflated.
        VALUES are what can be hashed and re-bound, so they come from the graphs
        at or ABOVE the node (a root binding wins on a shared name, mirroring
        ``Provenance.bind_child_components``). NAMES are what the node must
        never receive as a run value, and that set is strictly larger: a
        ``GraphNode`` still advertises in ``inputs`` a name its own subgraph
        binds, and the child-table schema turns such a name into a
        permanently-NULL column. Feeding that NULL back would override the real
        binding at run time and derive the row from ``None``. Each graph's
        ``inputs.bound`` already reports every name bound anywhere beneath it,
        under the parent-facing address the node and the column both use.
        """
        if self._bound_by_node is None:
            by_node: dict[int, tuple[Mapping[str, Any], frozenset[str]]] = {}

            def walk(graph: Any, inherited: Mapping[str, Any], inherited_names: frozenset[str]) -> None:
                if graph is None or not hasattr(graph, "iter_nodes"):
                    return
                visible = {**dict(getattr(graph, "_bound", None) or {}), **inherited}
                names = inherited_names | frozenset(visible) | frozenset(_graph_bound_names(graph))
                for inner in graph.iter_nodes():
                    by_node[id(inner)] = (visible, names)
                    walk(getattr(inner, "graph", None), visible, names)

            root_names = frozenset(self.components)
            walk(self.graph, self.components, root_names)
            for child in iter_child_specs(self.spec):
                walk(child.child_graph, self.components, root_names)
            self._bound_by_node = by_node
        return self._bound_by_node.get(id(node), (self.components, frozenset(self.components)))

    def visible_bound(self, node: Any) -> Mapping[str, Any]:
        """Bound VALUES visible to a node from the graphs at or above it."""
        return self._bindings(node)[0]

    def bound_names(self, node: Any) -> frozenset[str]:
        """Every name bound at, above, or anywhere BELOW a node — none is an input."""
        return self._bindings(node)[1]

    def node_provenance(self, node: Any, values: Mapping[str, Any]) -> str | None:
        params = self.node_params(node)
        visible, bound = self._bindings(node)
        components = {name: value for name, value in visible.items() if name in params}
        inputs: dict[str, Any] = {}
        for name, parameter in params.items():
            # A name bound below the node has no value here to hash, but it is
            # recipe all the same — ``compute_node_recipe_hash`` already folds
            # it in — so it is neither an input nor a missing required one.
            if name in bound:
                continue
            if name in values:
                inputs[name] = values[name]
            elif parameter.default is inspect.Parameter.empty:
                return None
        return compute_column_provenance(node, inputs, _component_config_hashes(components), frozenset(components))

    def node_recipe(self, node: Any) -> str:
        params = self.node_params(node)
        components = {name: value for name, value in self.visible_bound(node).items() if name in params}
        return compute_recipe_fingerprint(node, _component_config_hashes(components), frozenset(components))

    def column_recipe(self, column: Any) -> str:
        """Recipe identity of a derived COLUMN: its producer's recipe — or, for a
        routed union column with several producers, the order-free combination
        of every producer's recipe, so a change in ANY branch flips it."""
        producers = self.column_producers(column)
        if len(producers) == 1:
            return self.node_recipe(producers[0])
        return combine_recipe_fingerprints([self.node_recipe(producer) for producer in producers])

    def mounted_payloads(self, spec: TableSpec | None = None) -> list[str]:
        """Bound-value payloads of every graph mounted as a CHILD table below ``spec``.

        A ``map_over`` GraphNode is lifted out of the root graph when the table
        is analyzed, so the root graph alone cannot see a value bound on the
        child. Without these the root row fingerprint never moves for a child
        recipe change and ``sync()`` reports SKIPPED while the child rows read
        as drifted — identity saying one thing and execution doing another.

        Names this table binds are shadowed, the same rule
        ``recipe_component_hashes`` applies: a root binding flattens down onto
        the child graph at run time, so the child value it overrides is not
        recipe and must not report a drift the run would never honor.
        """
        target = spec or self.spec
        cached = self._mounted_payloads_cache.get(target.name)
        if cached is None:
            payloads: list[str] = []

            def descend(parent: TableSpec, shadowed: frozenset[str]) -> None:
                # Descend rather than flatten: a grain mounted under another
                # grain is shadowed by that grain's bindings too, so the shadow
                # set has to grow on the way down.
                for child in parent.children:
                    payloads.extend(f"{child.name}/{part}" for part in graph_bound_payloads(child.child_graph, shadowed))
                    descend(child, shadowed | frozenset(getattr(child.child_graph, "_bound", None) or {}))

            descend(target, frozenset(self.components))
            cached = payloads
            self._mounted_payloads_cache[target.name] = cached
        return cached

    def root_fingerprint(self, graph_inputs: Mapping[str, Any]) -> str:
        return compute_row_fingerprint(self.graph, dict(self.components), dict(graph_inputs), self.mounted_payloads())

    def child_fingerprint(self, child_inputs: Mapping[str, Any], child_spec: TableSpec) -> str:
        return compute_child_fingerprint(
            child_spec.child_graph,
            dict(self.components),
            dict(child_inputs),
            self.mounted_payloads(child_spec),
        )

    def table_stamps_recipe(self) -> bool:
        return bool(self.derived_columns() or self.spec.children)

    def current_recipe_fingerprint(self) -> str:
        return compute_table_recipe_fingerprint(self.graph, dict(self.components), mounted_payloads=self.mounted_payloads())

    def current_child_recipe_fingerprint(self, child_spec: TableSpec) -> str:
        child_graph = child_spec.child_graph
        valid_inputs = set(child_graph.inputs.all) if child_graph is not None and hasattr(child_graph.inputs, "all") else set()
        return compute_table_recipe_fingerprint(child_graph, dict(self.components), valid_inputs, self.mounted_payloads(child_spec))

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
        for name, component in self.visible_bound(node).items():
            if name not in params:
                continue
            payload, kind = self.component_payload(component)
            if payload is not None:
                entries.append(RecipeEntry(compute_payload_hash(payload), kind, payload))
        # A GraphNode's recipe also covers what its inner graphs bind: those
        # values never appear in its own signature, so without this the journal
        # cannot resolve a subgraph stamp back to the prompt text behind it.
        for payload in sorted(nested_bound_payloads(node, frozenset(self.visible_bound(node)))):
            entries.append(RecipeEntry(compute_payload_hash(payload), KIND_BOUND_VALUE, payload))
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
            binds = {name: value for name, value in self.visible_bound(node).items() if name in set(graph.inputs.all)}
            if binds:
                graph = graph.bind(**binds)
            self._column_graphs[id(node)] = graph
        return graph

    def node_inputs(self, node: Any, values: Mapping[str, Any]) -> dict[str, Any]:
        bound = self.bound_names(node)
        return {name: values[name] for name in self.node_params(node) if name not in bound and name in values}

    # -- graph slicing -------------------------------------------------------
    #
    # Three of a HyperTable's write paths need a *part* of the table's graph,
    # not the whole of it: resuming an answer runs the interrupt and everything
    # below it, a routed reconcile runs a gate and everything it can reach, and
    # a column reconcile runs one node. All three are cuts of the same graph
    # decided from the same recipe knowledge, so they are cut here (cached, and
    # re-bound to the table's components) rather than in the write plan.

    def _bind_components(self, graph: Any) -> Any:
        bindings = {name: value for name, value in self.components.items() if name in set(graph.inputs.all)}
        return graph.bind(**bindings) if bindings else graph

    def bind_child_components(self, child_graph: Any) -> Any:
        """Bind the table's components into a child graph that accepts them."""
        if not self.components:
            return child_graph
        return self._bind_components(child_graph)

    def node_names_downstream(self, roots: set[str], graph: Any | None = None) -> set[str]:
        """``roots`` plus every node reachable from them."""
        target_graph = graph or self.graph
        selected = set(roots)
        pending = list(roots)
        while pending:
            node_name = pending.pop()
            for successor in target_graph.nx_graph.successors(node_name):
                if successor not in selected:
                    selected.add(successor)
                    pending.append(successor)
        return selected

    def answer_graph(self, answer_names: set[str]) -> Any:
        """The slice that re-runs the interrupts owning ``answer_names``, downward."""
        key = tuple(sorted(answer_names))
        cached = self._answer_graphs.get(key)
        if cached is not None:
            return cached

        roots: set[str] = set()
        for column in self.spec.columns:
            if column.role != "answer" or column.name not in answer_names:
                continue
            for producer in self.column_producers(column):
                roots.add(producer.name)
        selected = self.node_names_downstream(roots)
        if not selected:
            raise RuntimeError(
                "HyperTable could not locate the interrupt that owns an answer column.\n\n"
                f"Answer columns: {', '.join(key)}\n\n"
                "How to fix: keep each answer_name on an interrupt node in the graph passed to as_table()."
            )

        graph = self._bind_components(
            Graph(
                [node for name, node in self.graph.nodes.items() if name in selected],
                name=f"{self.spec.name}__answer",
            )
        )
        self._answer_graphs[key] = graph
        return graph

    def routing_gate(self, node: Any, graph: Any) -> Any | None:
        """The nearest gate at or above ``node``, if its execution is routed."""
        if getattr(node, "is_gate", False):
            return node
        seen = {node.name}
        frontier = [node.name]
        while frontier:
            predecessors: list[str] = []
            for name in frontier:
                predecessors.extend(graph.nx_graph.predecessors(name))
            predecessors = [name for name in predecessors if name not in seen]
            for name in predecessors:
                candidate = graph.nodes[name]
                if getattr(candidate, "is_gate", False):
                    return candidate
            seen.update(predecessors)
            frontier = predecessors
        return None

    def routed_graph(self, gate: Any, source: Any, table_name: str) -> Any:
        """The slice a gate decides: the gate and everything it can reach."""
        cache_key = (table_name, gate.name)
        cached = self._routed_graphs.get(cache_key)
        if cached is not None:
            return cached
        selected = self.node_names_downstream({gate.name}, source)
        graph = self._bind_components(
            Graph(
                [node for name, node in source.nodes.items() if name in selected],
                name=f"{table_name}__{gate.name}",
            )
        )
        self._routed_graphs[cache_key] = graph
        return graph

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
