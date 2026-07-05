"""HyperTable: a Hypergraph graph where each node output is a stored column."""

from __future__ import annotations

import inspect
import math
import warnings
from typing import Any

from hypergraph import Graph
from hypergraph.materialization._fingerprint import (
    _component_config_hashes,
    compute_child_fingerprint,
    compute_column_provenance,
    compute_recipe_fingerprint,
    compute_row_fingerprint,
)
from hypergraph.materialization._schema import (
    STATUS_COLUMNS,
    TableSpec,
    analyze_table,
    input_names,
    is_internal_column,
    node_func,
    python_type_to_arrow,
    return_type,
)
from hypergraph.materialization._types import ErrorRow, SyncResult, TableStatus


def _normalize_to_dict(item: Any) -> dict[str, Any]:
    """Convert a mapped child item to a plain dict if it isn't one already."""
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="python")
    if hasattr(item, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(item)
    return dict(item)


def _normalize_value(v: Any) -> Any:
    """Convert numpy/arrow types back to Python-native for the public API."""
    import numpy as np

    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, (np.floating, np.integer)):
        return v.item()
    return v


def _dedup_rows(rows: list[dict[str, Any]], identity: str) -> list[dict[str, Any]]:
    """Keep only the highest _write_gen per identity (crash-leftover dedup)."""
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        id_val = str(row.get(identity, ""))
        existing = best.get(id_val)
        if existing is None or row.get("_write_gen", 0) > existing.get("_write_gen", 0):
            best[id_val] = row
    return list(best.values())


def _dedup_child_rows(rows: list[dict[str, Any]], identity: str) -> list[dict[str, Any]]:
    """Keep only the highest _write_gen per parent+child identity."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        id_val = str(row.get(identity, ""))
        parent_val = str(row.get("_parent_id", ""))
        key = (parent_val, id_val)
        existing = best.get(key)
        if existing is None or row.get("_write_gen", 0) > existing.get("_write_gen", 0):
            best[key] = row
    return list(best.values())


def _public_row(row: dict[str, Any], *, include_status: bool = False) -> dict[str, Any]:
    result = {}
    for k, v in row.items():
        if include_status and k in STATUS_COLUMNS:
            if k == "_status" and v is None:
                result[k] = "complete"
            else:
                result[k] = _normalize_value(v)
        elif not is_internal_column(k):
            result[k] = _normalize_value(v)
    return result


def _where_predicate(where: Any) -> list[tuple[str, str, Any]]:
    if where is None:
        return []
    if isinstance(where, dict):
        return [(key, "eq", value) for key, value in where.items()]
    return list(where)


def _split_boundary_provenance(value: Any) -> tuple[str | None, int | None]:
    """Parse a stored boundary provenance ("<hash>#<item count>") into its parts."""
    if not isinstance(value, str) or "#" not in value:
        return None, None
    prov, _, count = value.rpartition("#")
    try:
        return prov, int(count)
    except ValueError:
        return None, None


class HyperTable:
    """A Hypergraph graph where each node output is a stored column."""

    def __init__(
        self,
        nodes: list,
        *,
        identity: str,
        store: Any,
        on_error: str = "raise",
        _components: dict[str, Any] | None = None,
        _runner: Any | None = None,
        _graph: Graph | None = None,
    ):
        if on_error not in ("raise", "store"):
            raise ValueError(f"on_error must be 'raise' or 'store', got {on_error!r}")
        self._nodes = nodes
        self._identity = identity
        self._store = store
        self._on_error = on_error
        self._components = _components or {}
        self._runner = _runner
        self._graph = _graph
        self._spec: TableSpec | None = None
        self._analyzed = False
        self._column_graphs: dict[int, Any] = {}

    def bind(self, **components: Any) -> HyperTable:
        merged = {**self._components, **components}
        return HyperTable(
            self._nodes,
            identity=self._identity,
            store=self._store,
            on_error=self._on_error,
            _components=merged,
            _runner=self._runner,
        )

    def with_runner(self, runner: Any) -> HyperTable:
        return HyperTable(
            self._nodes,
            identity=self._identity,
            store=self._store,
            on_error=self._on_error,
            _components=self._components,
            _runner=runner,
        )

    def _ensure_analyzed(self):
        if self._analyzed:
            return
        self._build_graph()
        self._analyze_graph()
        self._resolve_store()
        self._analyzed = True

    def _build_graph(self):
        if self._graph is not None:
            return
        plain_nodes = []
        self._map_over_nodes = []
        for n in self._nodes:
            if hasattr(n, "_map_config") and n._map_config:
                self._map_over_nodes.append(n)
            else:
                plain_nodes.append(n)

        self._graph = Graph(plain_nodes, name=f"hypertable_{self._identity}")
        if self._components:
            valid_inputs = set(self._graph.inputs.all)
            root_binds = {k: v for k, v in self._components.items() if k in valid_inputs}
            if root_binds:
                self._graph = self._graph.bind(**root_binds)

    def _analyze_graph(self):
        self._spec = analyze_table(self._graph, self._identity, self._components, getattr(self, "_map_over_nodes", []))

    def _resolve_store(self):
        from hypergraph.materialization._table_store import TableStore

        if not isinstance(self._store, TableStore):
            raise TypeError(f"store must be a TableStore instance (e.g. LanceDBStore), got {type(self._store)}")

        self._store.open(self._spec, self._spec.children)

    def _require_runner(self):
        if self._runner is None:
            raise RuntimeError("No runner set. Call .with_runner(SyncRunner()) before write operations.")

    def _is_async_runner(self) -> bool:
        from hypergraph.runners import AsyncRunner

        return isinstance(self._runner, AsyncRunner)

    # --- Shared helpers ---

    def _graph_required_inputs(self) -> set[str]:
        return input_names(self._graph.inputs.required)

    def _extract_graph_inputs(self, item: dict[str, Any]) -> dict[str, Any]:
        required = self._graph_required_inputs()
        return {k: v for k, v in item.items() if k != self._identity and k in required}

    def _extract_outputs(self, result: Any) -> dict[str, Any]:
        if hasattr(result, "values") and isinstance(result.values, dict):
            return result.values
        if isinstance(result, dict):
            return result
        return {}

    def _build_row(
        self,
        item: dict[str, Any],
        graph_inputs: dict[str, Any],
        outputs: dict[str, Any],
        write_gen: int,
        provenances: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        identity_value = item[self._identity]
        row: dict[str, Any] = {self._identity: identity_value}
        row.update({k: v for k, v in item.items() if k != self._identity})

        derived_cols = [c for c in self._spec.columns if c.role == "derived"]
        for col in derived_cols:
            if col.name in outputs:
                row[col.name] = outputs[col.name]

        row["_row_fingerprint"] = self._compute_row_fingerprint(graph_inputs)
        row["_write_gen"] = write_gen

        if provenances is None:
            values = {**{k: v for k, v in item.items() if k != self._identity}, **outputs}
            provenances = {c.name: self._node_provenance(c.produced_by, values) for c in derived_cols}
            for child_spec in self._spec.children:
                bnode = self._boundary_node(child_spec)
                if bnode is None:
                    continue
                prov = self._node_provenance(bnode, values)
                if prov is not None:
                    provenances[child_spec.map_input] = self._boundary_provenance_value(prov, outputs.get(child_spec.map_input))
        for name, prov in provenances.items():
            row[f"_provenance_{name}"] = prov

        return row

    def _write_error_row(
        self,
        item: dict[str, Any],
        graph_inputs: dict[str, Any],
        write_gen: int,
        error: Exception,
        existing: dict[str, Any] | None,
    ) -> None:
        identity_value = item[self._identity]
        self._evolve_for_metadata(item)

        row: dict[str, Any] = {self._identity: identity_value}
        row.update({k: v for k, v in item.items() if k != self._identity})

        derived_cols = [c for c in self._spec.columns if c.role == "derived"]
        for col in derived_cols:
            row[col.name] = None

        row["_row_fingerprint"] = self._compute_row_fingerprint(graph_inputs)
        row["_write_gen"] = write_gen
        row["_status"] = "error"
        row["_error"] = f"{type(error).__name__}: {error}"

        self._store.write_rows(self._spec.name, [row])

        if existing is not None:
            self._store.delete_rows(
                self._spec.name,
                [
                    (self._identity, "eq", identity_value),
                    ("_write_gen", "lt", write_gen),
                ],
            )

    def _evolve_for_metadata(self, item: dict[str, Any], *, table_name: str | None = None, identity: str | None = None) -> None:
        """Add schema columns for metadata keys the store hasn't seen."""
        store = self._store
        target = table_name or self._spec.name
        id_col = identity or self._identity
        sample = store.read_rows(target, limit=1)
        known_cols = set(sample[0].keys()) if sample else {c.name for c in self._spec.columns}
        new_meta = {k: python_type_to_arrow(type(v) if v is not None else str) for k, v in item.items() if k not in known_cols and k != id_col}
        if new_meta:
            store.evolve_schema(target, new_meta)

    def _get_derived_column_type(self, column_name: str) -> type:
        for c in self._spec.columns:
            if c.name == column_name and c.role == "derived" and c.produced_by:
                return return_type(c.produced_by)
        return str

    # --- Per-column provenance (config-aware, value-chained) ---

    def _derived_columns(self, spec: TableSpec | None = None) -> list:
        spec = spec or self._spec
        return [c for c in spec.columns if c.role == "derived"]

    def _node_params(self, node: Any) -> Any:
        return inspect.signature(node_func(node)).parameters

    def _node_columns(self, node: Any, spec: TableSpec | None = None) -> list:
        return [c for c in self._derived_columns(spec) if c.produced_by is node]

    def _producing_node(self, column: str) -> Any:
        for c in self._derived_columns():
            if c.name == column:
                return c.produced_by
        raise KeyError(f"{column!r} is not a derived column")

    def _nodes_in_dependency_order(self, spec: TableSpec | None = None) -> list:
        """Producing nodes ordered so every node's column inputs are produced before it."""
        derived = self._derived_columns(spec)
        nodes: list = []
        seen: set[int] = set()
        for col in derived:
            if id(col.produced_by) not in seen:
                seen.add(id(col.produced_by))
                nodes.append(col.produced_by)
        derived_names = {c.name for c in derived}
        placed: set[str] = set()
        ordered: list = []
        remaining = list(nodes)
        while remaining:
            progressed = False
            for n in list(remaining):
                deps = {p for p in self._node_params(n) if p in derived_names}
                if deps <= placed:
                    ordered.append(n)
                    placed.update(c.name for c in self._node_columns(n, spec))
                    remaining.remove(n)
                    progressed = True
            if not progressed:
                ordered.extend(remaining)
                break
        return ordered

    def _node_provenance(self, node: Any, values: dict[str, Any]) -> str | None:
        """Provenance for a node's output columns; None if a required input is unavailable."""
        params = self._node_params(node)
        comp = {k: v for k, v in self._components.items() if k in params}
        inputs: dict[str, Any] = {}
        for name, param in params.items():
            if name in comp:
                continue
            if name in values:
                inputs[name] = values[name]
            elif param.default is inspect.Parameter.empty:
                return None
        return compute_column_provenance(node_func(node), inputs, _component_config_hashes(comp))

    def _node_recipe(self, node: Any) -> str:
        """Recipe identity for a node: hash(node code + consumed component configs), no input values."""
        params = self._node_params(node)
        comp = {k: v for k, v in self._components.items() if k in params}
        return compute_recipe_fingerprint(node_func(node), _component_config_hashes(comp))

    def _column_graph(self, node: Any) -> Any:
        """A cached single-node graph for column-scoped execution."""
        graph = self._column_graphs.get(id(node))
        if graph is None:
            graph = Graph([node], name=f"{self._spec.name}__{node_func(node).__name__}")
            binds = {k: v for k, v in self._components.items() if k in set(graph.inputs.all)}
            if binds:
                graph = graph.bind(**binds)
            self._column_graphs[id(node)] = graph
        return graph

    def _node_inputs(self, node: Any, values: dict[str, Any]) -> dict[str, Any]:
        return {name: values[name] for name in self._node_params(node) if name not in self._components and name in values}

    def _run_column_node(self, node: Any, values: dict[str, Any]) -> dict[str, Any]:
        return self._extract_outputs(self._runner.run(self._column_graph(node), **self._node_inputs(node, values)))

    async def _run_column_node_async(self, node: Any, values: dict[str, Any]) -> dict[str, Any]:
        return self._extract_outputs(await self._runner.run(self._column_graph(node), **self._node_inputs(node, values)))

    def _stored_values(self, row: dict[str, Any]) -> dict[str, Any]:
        return {k: _normalize_value(v) for k, v in row.items() if not is_internal_column(k)}

    def _node_is_fresh(self, node: Any, prov: str, existing: dict[str, Any], spec: TableSpec | None = None) -> bool:
        return all(
            existing.get(f"_provenance_{c.name}") == prov and not self._column_is_null(existing.get(c.name)) for c in self._node_columns(node, spec)
        )

    def _boundary_node(self, child_spec: TableSpec) -> Any:
        """The root-graph node whose output is the child spec's mapped-items list."""
        if not child_spec.map_input:
            return None
        nodes_dict = self._graph.nodes if isinstance(self._graph.nodes, dict) else {}
        for n in nodes_dict.values():
            if child_spec.map_input in (n.data_outputs if hasattr(n, "data_outputs") else ()):
                return n
        return None

    def _stored_child_count(self, child_spec: TableSpec, parent_id: Any) -> int:
        rows = self._store.read_rows(child_spec.name, [("_parent_id", "eq", parent_id)])
        return len(_dedup_child_rows(rows, child_spec.identity))

    def _boundary_provenance_value(self, prov: str, items: Any) -> str:
        """Stored boundary provenance: recipe hash + how many items it produced.

        The count is checked against the stored child rows on reconcile, so a
        lost child row (crash leftover) forces the boundary node to re-run.
        """
        count = len(items) if isinstance(items, list) else 0
        return f"{prov}#{count}"

    def _boundary_freshness(
        self, child_spec: TableSpec, item: dict[str, Any], existing: dict[str, Any], values: dict[str, Any]
    ) -> tuple[Any, str, Any, bool] | None:
        """Decide whether stored child rows can stand in for a boundary-node run.

        Returns ``(boundary_node, provenance, stored_value, fresh)``, or None when
        the boundary can't be reconciled column-scoped (full-run fallback).
        """
        bnode = self._boundary_node(child_spec)
        if bnode is None or any(c.produced_by is bnode for c in self._derived_columns()):
            return None
        prov = self._node_provenance(bnode, values)
        if prov is None:
            return None
        stored = existing.get(f"_provenance_{child_spec.map_input}")
        stored_prov, stored_count = _split_boundary_provenance(stored)
        fresh = stored_prov == prov and stored_count == self._stored_child_count(child_spec, item[self._identity])
        return bnode, prov, stored, fresh

    def _reconcile_columns(self, item: dict[str, Any], existing: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]] | None:
        """Column-scoped upsert: reuse fresh columns, re-derive stale ones from stored upstream values.

        Returns (outputs, provenances, child_plan), or None when a required input
        is unavailable — the caller falls back to a full graph run. A child_plan
        entry of None means: rebuild the mapped items from stored child rows
        instead of re-running the boundary node.
        """
        values = self._stored_values(existing)
        values.update({k: v for k, v in item.items() if k != self._identity})
        outputs: dict[str, Any] = {}
        provenances: dict[str, str] = {}
        for node in self._nodes_in_dependency_order():
            prov = self._node_provenance(node, values)
            if prov is None:
                return None
            if self._node_is_fresh(node, prov, existing):
                node_outputs = {c.name: _normalize_value(existing[c.name]) for c in self._node_columns(node)}
            else:
                node_outputs = self._run_column_node(node, values)
            for c in self._node_columns(node):
                if c.name in node_outputs:
                    outputs[c.name] = node_outputs[c.name]
                    values[c.name] = node_outputs[c.name]
                provenances[c.name] = prov
        child_plan: dict[str, Any] = {}
        for child_spec in self._spec.children:
            state = self._boundary_freshness(child_spec, item, existing, values)
            if state is None:
                return None
            bnode, prov, stored, fresh = state
            if fresh:
                provenances[child_spec.map_input] = stored
                child_plan[child_spec.name] = None
            else:
                items = self._run_column_node(bnode, values).get(child_spec.map_input)
                items = items if isinstance(items, list) else []
                provenances[child_spec.map_input] = self._boundary_provenance_value(prov, items)
                child_plan[child_spec.name] = items
        return outputs, provenances, child_plan

    async def _reconcile_columns_async(
        self, item: dict[str, Any], existing: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]] | None:
        """Async twin of ``_reconcile_columns`` — identical except the awaited node runs."""
        values = self._stored_values(existing)
        values.update({k: v for k, v in item.items() if k != self._identity})
        outputs: dict[str, Any] = {}
        provenances: dict[str, str] = {}
        for node in self._nodes_in_dependency_order():
            prov = self._node_provenance(node, values)
            if prov is None:
                return None
            if self._node_is_fresh(node, prov, existing):
                node_outputs = {c.name: _normalize_value(existing[c.name]) for c in self._node_columns(node)}
            else:
                node_outputs = await self._run_column_node_async(node, values)
            for c in self._node_columns(node):
                if c.name in node_outputs:
                    outputs[c.name] = node_outputs[c.name]
                    values[c.name] = node_outputs[c.name]
                provenances[c.name] = prov
        child_plan: dict[str, Any] = {}
        for child_spec in self._spec.children:
            state = self._boundary_freshness(child_spec, item, existing, values)
            if state is None:
                return None
            bnode, prov, stored, fresh = state
            if fresh:
                provenances[child_spec.map_input] = stored
                child_plan[child_spec.name] = None
            else:
                boundary_outputs = await self._run_column_node_async(bnode, values)
                items = boundary_outputs.get(child_spec.map_input)
                items = items if isinstance(items, list) else []
                provenances[child_spec.map_input] = self._boundary_provenance_value(prov, items)
                child_plan[child_spec.name] = items
        return outputs, provenances, child_plan

    def _row_converged(self, row: dict[str, Any]) -> bool:
        """All derived columns present with provenance matching the current recipe."""
        values = self._stored_values(row)
        for node in self._nodes_in_dependency_order():
            prov = self._node_provenance(node, values)
            for c in self._node_columns(node):
                if prov is None or self._column_is_null(row.get(c.name)) or row.get(f"_provenance_{c.name}") != prov:
                    return False
        return True

    # --- Public API ---

    @property
    def table_name(self) -> str:
        """The root table's physical name (e.g. for an index ``on=`` target)."""
        self._ensure_analyzed()
        return self._spec.name

    @property
    def child_table_names(self) -> tuple[str, ...]:
        """The child (mapped) tables' physical names, in declaration order.

        The public accessor for the name an application passes to
        ``create_index(on=...)`` for a 1:many derivation step — so callers never
        reach into ``_spec.children`` to find it.
        """
        self._ensure_analyzed()
        return tuple(child.name for child in self._spec.children)

    def visualize(self, *, include_children: bool = True, **kwargs) -> Any:
        self._ensure_analyzed()
        if not include_children or not self._spec.children:
            return self._graph.visualize(**kwargs)
        from hypergraph.graph import Graph as _Graph
        from hypergraph.viz.widget import render_flat_graph

        all_nodes = list(self._graph.nodes.values()) if isinstance(self._graph.nodes, dict) else []
        for map_node in getattr(self, "_map_over_nodes", []):
            all_nodes.append(map_node)
        combined = _Graph(all_nodes, name=self._spec.name)
        if self._components:
            valid_inputs = set(combined.inputs.all)
            binds = {k: v for k, v in self._components.items() if k in valid_inputs}
            if binds:
                combined = combined.bind(**binds)

        # The parent→mapped-child edge that auto-wiring can't infer: the mapped
        # GraphNode consumes the parent's list column (map_input) through the
        # derive lane, not through a name-matched input port, so no edge exists
        # in the combined graph. Inject it as a viz-only fan-out edge here — the
        # only place that holds both the producing node and the child spec.
        extra_edges = self._fanout_viz_edges()

        # `combined` is a throwaway Graph built fresh for this render (never
        # cached, never the runtime graph), so mutating its nx_graph in place
        # is safe. It must carry the same edges as the flat graph below:
        # LayoutEstimator sizes off `combined.nx_graph`, not the flat graph, so
        # without this the fan-out edge would render correctly but the iframe
        # would still be sized as if the mapped node were a disconnected root
        # (wrong width/height, e.g. clipped output).
        for src_id, tgt_id, value_names in extra_edges:
            if src_id in combined.nx_graph.nodes and tgt_id in combined.nx_graph.nodes and not combined.nx_graph.has_edge(src_id, tgt_id):
                combined.nx_graph.add_edge(src_id, tgt_id, edge_type="data", value_names=list(value_names), is_map=True)

        show_external_inputs = kwargs.pop("show_external_inputs", None)
        show_inputs = kwargs.pop("show_inputs", None)
        if show_external_inputs is not None:
            if show_inputs is not None and show_inputs != show_external_inputs:
                raise TypeError("Pass either show_inputs or show_external_inputs, not both.")
            warnings.warn(
                "show_external_inputs is deprecated; use show_inputs instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            show_inputs = show_external_inputs
        if show_inputs is None:
            show_inputs = True
        kwargs.setdefault("depth", 1)

        flat_graph = combined.to_flat_graph(extra_edges=extra_edges)
        return render_flat_graph(flat_graph, combined, show_inputs=show_inputs, **kwargs)

    def _fanout_viz_edges(self) -> list[tuple[str, str, tuple[str, ...]]]:
        """Viz-only ``(producer, mapped_node, (column,))`` edges for each child.

        Pairs each child spec with the mapped GraphNode that maps its column and
        the root node that produces that column, using hierarchical ids that
        match ``to_flat_graph``. Empty when a producer or map node can't be
        found, so viz degrades to the pre-fix disconnected view rather than
        raising.

        ``self._spec.children`` is built by ``analyze_table`` as
        ``[_analyze_map_over(map_node, ...) for map_node in self._map_over_nodes]``
        (one ``TableSpec`` per map node, same order, never filtered) — so the two
        lists are always the same length and positionally aligned. Pairing by
        position (rather than matching on ``column`` name) is required to keep
        two children that map the *same* column name correctly attached to
        their own map node instead of both resolving to whichever map node
        happens to appear first.
        """
        edges: list[tuple[str, str, tuple[str, ...]]] = []
        map_nodes = getattr(self, "_map_over_nodes", [])
        for child_spec, map_node in zip(self._spec.children, map_nodes, strict=True):
            column = child_spec.map_input
            if not column:
                continue
            producer = self._boundary_node(child_spec)
            if producer is None:
                continue
            edges.append((producer.name, map_node.name, (column,)))
        return edges

    def count(self, child_table: str | None = None) -> int:
        self._ensure_analyzed()
        if child_table:
            for child in self._spec.children:
                if child.name == child_table:
                    return len(_dedup_child_rows(self._store.read_rows(child.name), child.identity))
            return 0
        return len(_dedup_rows(self._store.read_rows(self._spec.name), self._identity))

    def status(self) -> TableStatus:
        """Report which stored rows would re-derive if sync ran now, without deriving anything.

        Read-only and runner-free: recomputes each row's fingerprint from its
        stored source values plus the *current* node code and component configs,
        and compares it with the fingerprint stored when the row was written.
        Child tables are checked with their scoped child fingerprints.
        """
        self._ensure_analyzed()
        rows = _dedup_rows(self._store.read_rows(self._spec.name), self._identity)
        stale_column_counts: dict[str, int] = {}
        root = self._classify_rows(
            rows,
            identity=self._identity,
            fingerprint_of=lambda row: self._compute_row_fingerprint(self._source_inputs_from_row(row)),
            on_stale=lambda row: self._count_stale_columns(row, stale_column_counts),
        )
        children = tuple(self._child_status(child_spec) for child_spec in self._spec.children)
        return TableStatus(
            table=self._spec.name,
            children=children,
            stale_columns=tuple(sorted(stale_column_counts.items())),
            **root,
        )

    def _count_stale_columns(self, row: dict[str, Any], counts: dict[str, int], spec: TableSpec | None = None) -> None:
        """Attribute a stale row to the specific columns whose provenance no longer matches."""
        values = self._stored_values(row)
        for node in self._nodes_in_dependency_order(spec):
            prov = self._node_provenance(node, values)
            for c in self._node_columns(node, spec):
                if prov is None or self._column_is_null(row.get(c.name)) or row.get(f"_provenance_{c.name}") != prov:
                    counts[c.name] = counts.get(c.name, 0) + 1

    def _child_status(self, child_spec: TableSpec) -> TableStatus:
        rows = _dedup_child_rows(self._store.read_rows(child_spec.name), child_spec.identity)
        stale_column_counts: dict[str, int] = {}
        counts = self._classify_rows(
            rows,
            identity=child_spec.identity,
            fingerprint_of=lambda row: self._compute_child_fingerprint(self._child_source_inputs_from_row(row, child_spec), child_spec),
            on_stale=lambda row: self._count_stale_columns(row, stale_column_counts, spec=child_spec),
        )
        return TableStatus(table=child_spec.name, stale_columns=tuple(sorted(stale_column_counts.items())), **counts)

    def _classify_rows(self, rows: list[dict[str, Any]], *, identity: str, fingerprint_of: Any, on_stale: Any = None) -> dict[str, Any]:
        """Split stored rows into fresh / stale / errored for a status report."""
        fresh = stale = errored = 0
        stale_ids: list[str] = []
        errored_ids: list[str] = []
        for row in rows:
            id_val = str(row.get(identity, ""))
            if row.get("_status") == "error":
                errored += 1
                errored_ids.append(id_val)
            elif row.get("_row_fingerprint") == fingerprint_of(row):
                fresh += 1
            else:
                stale += 1
                stale_ids.append(id_val)
                if on_stale is not None:
                    on_stale(row)
        return {
            "total": len(rows),
            "fresh": fresh,
            "stale": stale,
            "errored": errored,
            "stale_ids": tuple(sorted(stale_ids)),
            "errored_ids": tuple(sorted(errored_ids)),
        }

    def _child_source_inputs_from_row(self, row: dict[str, Any], child_spec: TableSpec) -> dict[str, Any]:
        """Reconstruct a child's content-key inputs from its stored row (mirrors _plan_child)."""
        return {c.name: _normalize_value(row[c.name]) for c in child_spec.columns if c.role == "source" and c.content_key and c.name in row}

    def get(self, identity_value: str, *, include_status: bool = False) -> dict[str, Any] | None:
        self._ensure_analyzed()
        row = self._store.read_one(self._spec.name, self._identity, identity_value)
        if row is None:
            return None
        return _public_row(row, include_status=include_status)

    def children(self, parent_id: str, *, include_status: bool = False) -> list[dict[str, Any]]:
        self._ensure_analyzed()
        if not self._spec.children:
            return []
        child_spec = self._spec.children[0]
        rows = self._store.read_rows(child_spec.name, [("_parent_id", "eq", parent_id)])
        rows = _dedup_child_rows(rows, child_spec.identity)
        return [_public_row(row, include_status=include_status) for row in rows]

    def filter(self, where: Any = None, *, limit: int | None = None, include_status: bool = False) -> list[dict[str, Any]]:
        """Return public rows matching a store predicate."""
        self._ensure_analyzed()
        rows = self._store.read_rows(self._spec.name, _where_predicate(where), limit=limit)
        rows = _dedup_rows(rows, self._identity)
        return [_public_row(row, include_status=include_status) for row in rows]

    def delete_children(self, where: Any = None) -> int:
        """Delete child rows matching a predicate. Returns count deleted."""
        self._ensure_analyzed()
        if not self._spec.children:
            return 0
        child_spec = self._spec.children[0]
        return self._store.delete_rows(child_spec.name, _where_predicate(where))

    def filter_children(self, where: Any = None, *, limit: int | None = None, include_status: bool = False) -> list[dict[str, Any]]:
        """Return child rows matching a store predicate."""
        self._ensure_analyzed()
        if not self._spec.children:
            return []
        child_spec = self._spec.children[0]
        rows = self._store.read_rows(child_spec.name, _where_predicate(where), limit=limit)
        rows = _dedup_child_rows(rows, child_spec.identity)
        return [_public_row(row, include_status=include_status) for row in rows]

    # --- Named indexes (persisted query specs) ---
    #
    # An index is a projection, not a table: a named, persisted query spec over
    # a vector column that already lives in the (root or child) table. The
    # LanceDB store ANN-searches that column directly — no separate artifact.
    # Materializing into external backends (Chroma, Azure Search) is an
    # application-layer concern, out of scope here.

    def _resolve_index_table(self, on: str | None) -> TableSpec:
        if on is None or on == self._spec.name:
            return self._spec
        for cs in self._spec.children:
            if cs.name == on:
                return cs
        known = [self._spec.name, *(cs.name for cs in self._spec.children)]
        raise ValueError(f"unknown table {on!r} for index; expected one of {known}")

    def _index_recipe_fingerprint(self, spec: TableSpec, vector: str) -> str | None:
        """The component-config + node-definition basis of the vector column's producing node."""
        for c in self._derived_columns(spec):
            if c.name == vector and c.produced_by is not None:
                return self._node_recipe(c.produced_by)
        return None

    def _index_queryable_columns(self, spec: TableSpec) -> set[str]:
        """Columns an index may reference: spec columns plus physical (metadata-evolved) ones."""
        columns = {c.name for c in spec.columns if c.role != "internal"}
        physical = self._store.open(self._spec, self._spec.children).get(spec.name, [])
        columns.update(name for name in physical if not is_internal_column(name))
        return columns

    def _load_indexes(self) -> dict[str, dict[str, Any]]:
        manifest = self._store.load_manifest(self._spec.name) or {}
        return dict(manifest.get("indexes", {}))

    def _save_indexes(self, indexes: dict[str, dict[str, Any]]) -> None:
        manifest = self._store.load_manifest(self._spec.name) or {}
        manifest["indexes"] = indexes
        self._store.save_manifest(self._spec.name, manifest)

    def create_index(
        self,
        name: str,
        *,
        on: str | None = None,
        rows: Any = None,
        text: str | None = None,
        vector: str | None = None,
    ) -> dict[str, Any]:
        """Record a named query spec: which table, which vector column, which row slice."""
        self._ensure_analyzed()
        if not self._store.supports_manifests():
            raise NotImplementedError(
                f"{type(self._store).__name__} does not implement save_manifest/load_manifest, "
                "so it cannot persist named indexes. Implement both manifest hooks to support "
                "create_index, or use a store that does (e.g. LanceDBStore)."
            )
        spec = self._resolve_index_table(on)
        if vector is None:
            raise ValueError("create_index requires vector=<column>: v1 indexes are vector-search specs")
        columns = self._index_queryable_columns(spec)
        for label, col in (("vector", vector), ("text", text)):
            if col is not None and col not in columns:
                raise ValueError(f"{label} column {col!r} does not exist on table {spec.name!r}; known columns: {sorted(columns)}")
        for col, _op, _val in _where_predicate(rows):
            if col not in columns:
                raise ValueError(f"rows filter column {col!r} does not exist on table {spec.name!r}; known columns: {sorted(columns)}")
        index_spec = {
            "name": name,
            "on": spec.name,
            "rows": rows,
            "text": text,
            "vector": vector,
            "recipe_fingerprint": self._index_recipe_fingerprint(spec, vector),
        }
        indexes = self._load_indexes()
        indexes[name] = index_spec
        self._save_indexes(indexes)
        return dict(index_spec)

    def list_indexes(self) -> list[dict[str, Any]]:
        """The persisted index specs, each with ``current``: does its recorded recipe match the recipe now?"""
        self._ensure_analyzed()
        specs = []
        for index_spec in self._load_indexes().values():
            spec = self._resolve_index_table(index_spec.get("on"))
            now = self._index_recipe_fingerprint(spec, index_spec["vector"])
            specs.append({**index_spec, "current": now == index_spec.get("recipe_fingerprint")})
        return specs

    def drop_index(self, name: str) -> None:
        self._ensure_analyzed()
        indexes = self._load_indexes()
        if name not in indexes:
            raise KeyError(f"no index named {name!r}")
        del indexes[name]
        self._save_indexes(indexes)

    def search(
        self,
        query_vector: list[float],
        *,
        index: str,
        limit: int = 10,
        where: Any = None,
        include_status: bool = False,
    ) -> list[dict[str, Any]]:
        """Vector search through a named index: public rows plus a ``_distance`` field.

        ``where`` is an optional query-time pre-filter (dict or predicate
        tuples) that stacks on top of the index's own recorded ``rows`` slice:
        both are applied, so a caller can narrow one search (e.g. by
        ``station``) without minting a separate index.
        """
        self._ensure_analyzed()
        indexes = self._load_indexes()
        if index not in indexes:
            raise KeyError(f"no index named {index!r}; known indexes: {sorted(indexes)}")
        index_spec = indexes[index]
        combined_where = [*_where_predicate(index_spec.get("rows")), *_where_predicate(where)]
        hits = self._store.search(
            index_spec["on"],
            query_vector=list(query_vector),
            vector_column=index_spec["vector"],
            where=combined_where or None,
            limit=limit,
        )
        results = []
        for row in hits:
            distance = row.pop("_distance", None)
            public = _public_row(row, include_status=include_status)
            public["_distance"] = _normalize_value(distance)
            results.append(public)
        return results

    def set_children(self, where: Any = None, **fields: Any) -> int:
        """Bulk metadata update for child rows matching a predicate."""
        self._ensure_analyzed()
        if not self._spec.children:
            return 0
        child_spec = self._spec.children[0]
        table_name = child_spec.name
        identity = child_spec.identity
        rows = _dedup_child_rows(
            self._store.read_rows(table_name, _where_predicate(where)),
            identity,
        )
        if not rows:
            return 0
        self._evolve_for_metadata(
            {identity: rows[0][identity], **fields},
            table_name=table_name,
            identity=identity,
        )
        write_gen = self._store.max_write_gen(table_name) + 1
        updated: list[dict[str, Any]] = []
        for row in rows:
            new_row = {k: _normalize_value(v) for k, v in row.items()}
            new_row.update(fields)
            new_row["_write_gen"] = write_gen
            updated.append(new_row)
        self._store.write_rows(table_name, updated)
        for row in rows:
            self._store.delete_rows(
                table_name,
                [
                    (identity, "eq", row[identity]),
                    ("_parent_id", "eq", row["_parent_id"]),
                    ("_write_gen", "lt", write_gen),
                ],
            )
        return len(updated)

    def set(self, where: Any, **fields: Any) -> Any:
        """Bulk metadata update for all rows matching a predicate."""
        self._ensure_analyzed()
        if self._is_async_runner():
            return self._set_async(where, **fields)
        return self._set_sync(where, **fields)

    def _set_sync(self, where: Any, **fields: Any) -> int:
        content_key_fields = {c.name for c in self._spec.columns if c.content_key}
        blocked = sorted(content_key_fields.intersection(fields))
        if blocked:
            raise ValueError(f"set() cannot update content-key fields: {', '.join(blocked)}")

        rows = _dedup_rows(self._store.read_rows(self._spec.name, _where_predicate(where)), self._identity)
        if not rows:
            return 0

        self._evolve_for_metadata({self._identity: rows[0][self._identity], **fields})
        write_gen = self._store.max_write_gen(self._spec.name) + 1
        updated_rows: list[dict[str, Any]] = []
        for row in rows:
            new_row = {k: _normalize_value(v) for k, v in row.items()}
            new_row.update(fields)
            new_row["_write_gen"] = write_gen
            updated_rows.append(new_row)

        self._store.write_rows(self._spec.name, updated_rows)
        for row in rows:
            self._store.delete_rows(
                self._spec.name,
                [
                    (self._identity, "eq", row[self._identity]),
                    ("_write_gen", "lt", write_gen),
                ],
            )
        return len(updated_rows)

    async def _set_async(self, where: Any, **fields: Any) -> int:
        return self._set_sync(where, **fields)

    def _insert_items(self, *args, **kwargs) -> list[dict[str, Any]]:
        if args and isinstance(args[0], list):
            return args[0]
        if kwargs:
            return [kwargs]
        raise ValueError("insert() requires kwargs or a list of dicts")

    def insert(self, *args, **kwargs) -> Any:
        self._require_runner()
        self._ensure_analyzed()
        items = self._insert_items(*args, **kwargs)
        if self._is_async_runner():
            return self._insert_async(items)
        return self._insert_sync(items)

    def _insert_sync(self, items: list[dict[str, Any]]) -> None:
        write_gen = self._store.max_write_gen(self._spec.name) + 1

        for item in items:
            self._insert_one(item, write_gen)

    async def _insert_async(self, items: list[dict[str, Any]]) -> None:
        write_gen = self._store.max_write_gen(self._spec.name) + 1

        for item in items:
            await self._insert_one_async(item, write_gen)

    def _plan_insert(self, item: dict[str, Any], graph_inputs: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        """Read the current row and decide whether the parent can be skipped.

        A parent is skippable when its fingerprint is unchanged and its last write
        completed. Returns ``(existing_row, parent_skipped)``.
        """
        existing = self._store.read_one(self._spec.name, self._identity, item[self._identity])
        parent_skipped = False
        if existing is not None and existing.get("_row_fingerprint") == self._compute_row_fingerprint(graph_inputs):
            status = existing.get("_status")
            parent_skipped = status is None or status == "complete"
        return existing, parent_skipped

    def _write_parent_row(self, item: dict[str, Any], graph_inputs: dict[str, Any], outputs: dict[str, Any], write_gen: int) -> None:
        """Persist the parent row for a completed derivation."""
        self._evolve_for_metadata(item)
        row = self._build_row(item, graph_inputs, outputs, write_gen)
        row["_status"] = "complete"
        row["_error"] = None
        self._store.write_rows(self._spec.name, [row])

    def _cleanup_old_parent_gens(self, identity_value: Any, write_gen: int) -> None:
        self._store.delete_rows(
            self._spec.name,
            [(self._identity, "eq", identity_value), ("_write_gen", "lt", write_gen)],
        )

    def _cleanup_old_child_gens(self, identity_value: Any, write_gen: int) -> None:
        for child_spec in self._spec.children:
            self._store.delete_rows(
                child_spec.name,
                [("_parent_id", "eq", identity_value), ("_write_gen", "lt", write_gen)],
            )

    def _can_reconcile(self, existing: dict[str, Any] | None) -> bool:
        """Column-scoped reconcile applies to any complete (non-error) prior row."""
        return existing is not None and existing.get("_status") != "error"

    def _apply_reconciled(
        self,
        item: dict[str, Any],
        graph_inputs: dict[str, Any],
        existing: dict[str, Any],
        reconciled: tuple[dict[str, Any], dict[str, str], dict[str, Any]],
        parent_skipped: bool,
        write_gen: int,
    ) -> str:
        """Persist a reconciled row: children first, then the parent row that vouches for them.

        The parent row is rewritten only when it changed (or when a provenance
        column must be persisted for a pre-provenance row); a skipped parent's
        stored row is left untouched so its old generation is never cleaned away.
        """
        outputs, provenances, child_plan = reconciled
        identity_value = item[self._identity]
        for child_spec in self._spec.children:
            items = child_plan.get(child_spec.name)
            if items is None:
                items = self._rebuild_child_items(child_spec, identity_value)
            self._insert_children_items(identity_value, items, child_spec, write_gen)
        prov_changed = any(existing.get(f"_provenance_{name}") != prov for name, prov in provenances.items())
        if not parent_skipped or prov_changed:
            self._evolve_for_metadata(item)
            row = self._build_row(item, graph_inputs, outputs, write_gen, provenances=provenances)
            row["_status"] = "complete"
            row["_error"] = None
            self._store.write_rows(self._spec.name, [row])
            self._cleanup_old_parent_gens(identity_value, write_gen)
        self._cleanup_old_child_gens(identity_value, write_gen)
        return "skipped" if parent_skipped else "updated"

    async def _apply_reconciled_async(
        self,
        item: dict[str, Any],
        graph_inputs: dict[str, Any],
        existing: dict[str, Any],
        reconciled: tuple[dict[str, Any], dict[str, str], dict[str, Any]],
        parent_skipped: bool,
        write_gen: int,
    ) -> str:
        """Async twin of ``_apply_reconciled`` — identical except the awaited child runs."""
        outputs, provenances, child_plan = reconciled
        identity_value = item[self._identity]
        for child_spec in self._spec.children:
            items = child_plan.get(child_spec.name)
            if items is None:
                items = self._rebuild_child_items(child_spec, identity_value)
            await self._insert_children_items_async(identity_value, items, child_spec, write_gen)
        prov_changed = any(existing.get(f"_provenance_{name}") != prov for name, prov in provenances.items())
        if not parent_skipped or prov_changed:
            self._evolve_for_metadata(item)
            row = self._build_row(item, graph_inputs, outputs, write_gen, provenances=provenances)
            row["_status"] = "complete"
            row["_error"] = None
            self._store.write_rows(self._spec.name, [row])
            self._cleanup_old_parent_gens(identity_value, write_gen)
        self._cleanup_old_child_gens(identity_value, write_gen)
        return "skipped" if parent_skipped else "updated"

    def _insert_one(self, item: dict[str, Any], write_gen: int) -> str:
        """Insert or upsert a single row. Returns 'inserted', 'updated', 'skipped', or 'errored'."""
        identity_value = item[self._identity]
        graph_inputs = self._extract_graph_inputs(item)
        existing, parent_skipped = self._plan_insert(item, graph_inputs)
        if parent_skipped and not self._spec.children:
            return "skipped"

        if self._can_reconcile(existing):
            try:
                reconciled = self._reconcile_columns(item, existing)
            except Exception as e:
                if self._on_error == "raise":
                    raise
                if parent_skipped:
                    # Parent is complete and unchanged; a transient failure while
                    # reconciling solely for the children must not downgrade the
                    # stored-complete parent to an error row.
                    return "skipped"
                self._write_error_row(item, graph_inputs, write_gen, e, existing)
                return "errored"
            if reconciled is not None:
                return self._apply_reconciled(item, graph_inputs, existing, reconciled, parent_skipped, write_gen)

        try:
            outputs = self._extract_outputs(self._runner.run(self._graph, **graph_inputs))
        except Exception as e:
            if self._on_error == "raise":
                raise
            if parent_skipped:
                # Parent is complete and unchanged; a transient failure while
                # re-deriving solely to reconcile children must not downgrade the
                # stored-complete parent to an error row.
                return "skipped"
            self._write_error_row(item, graph_inputs, write_gen, e, existing)
            return "errored"

        if parent_skipped:
            # Parent unchanged — reconcile children only (don't rewrite parent).
            for child_spec in self._spec.children:
                self._insert_children(identity_value, outputs, child_spec, write_gen)
            self._cleanup_old_child_gens(identity_value, write_gen)
            return "skipped"

        # Children before the parent row: the parent row carries the boundary
        # provenance that vouches for the stored child set, so it must land last.
        for child_spec in self._spec.children:
            self._insert_children(identity_value, outputs, child_spec, write_gen)
        self._write_parent_row(item, graph_inputs, outputs, write_gen)
        if existing is not None:
            self._cleanup_old_parent_gens(identity_value, write_gen)
            self._cleanup_old_child_gens(identity_value, write_gen)
        return "updated" if existing is not None else "inserted"

    async def _insert_one_async(self, item: dict[str, Any], write_gen: int) -> str:
        """Async twin of ``_insert_one`` — identical except the awaited graph run."""
        identity_value = item[self._identity]
        graph_inputs = self._extract_graph_inputs(item)
        existing, parent_skipped = self._plan_insert(item, graph_inputs)
        if parent_skipped and not self._spec.children:
            return "skipped"

        if self._can_reconcile(existing):
            try:
                reconciled = await self._reconcile_columns_async(item, existing)
            except Exception as e:
                if self._on_error == "raise":
                    raise
                if parent_skipped:
                    # Parent is complete and unchanged; a transient failure while
                    # reconciling solely for the children must not downgrade the
                    # stored-complete parent to an error row.
                    return "skipped"
                self._write_error_row(item, graph_inputs, write_gen, e, existing)
                return "errored"
            if reconciled is not None:
                return await self._apply_reconciled_async(item, graph_inputs, existing, reconciled, parent_skipped, write_gen)

        try:
            outputs = self._extract_outputs(await self._runner.run(self._graph, **graph_inputs))
        except Exception as e:
            if self._on_error == "raise":
                raise
            if parent_skipped:
                # Parent is complete and unchanged; a transient failure while
                # re-deriving solely to reconcile children must not downgrade the
                # stored-complete parent to an error row.
                return "skipped"
            self._write_error_row(item, graph_inputs, write_gen, e, existing)
            return "errored"

        if parent_skipped:
            for child_spec in self._spec.children:
                await self._insert_children_async(identity_value, outputs, child_spec, write_gen)
            self._cleanup_old_child_gens(identity_value, write_gen)
            return "skipped"

        # Children before the parent row: the parent row carries the boundary
        # provenance that vouches for the stored child set, so it must land last.
        for child_spec in self._spec.children:
            await self._insert_children_async(identity_value, outputs, child_spec, write_gen)
        self._write_parent_row(item, graph_inputs, outputs, write_gen)
        if existing is not None:
            self._cleanup_old_parent_gens(identity_value, write_gen)
            self._cleanup_old_child_gens(identity_value, write_gen)
        return "updated" if existing is not None else "inserted"

    def _bind_child_components(self, child_graph: Any) -> Any:
        if not self._components:
            return child_graph
        valid_inputs = set(child_graph.inputs.all)
        binds = {key: value for key, value in self._components.items() if key in valid_inputs}
        return child_graph.bind(**binds) if binds else child_graph

    def _child_items(self, outputs: dict, child_spec: TableSpec) -> list | None:
        """The mapped child items for a child table, or None if there's nothing to process."""
        if not child_spec.child_graph:
            return None
        child_items = outputs.get(child_spec.map_input)
        if not child_items or not isinstance(child_items, list):
            return None
        return child_items

    def _plan_child(
        self, child_spec: TableSpec, child_item: dict[str, Any], parent_id: str, write_gen: int
    ) -> tuple[str, dict[str, Any], str, dict[str, Any] | None] | None:
        """Compute ``(child_identity, child_inputs, fingerprint, existing_child)`` for a child item.

        If the child is unchanged and complete, bump its ``_write_gen`` so it
        survives cleanup and return None to signal a skip.
        """
        child_identity = child_item.get(child_spec.identity, "")
        child_inputs = {
            col.name: child_item[col.name] for col in child_spec.columns if col.role == "source" and col.content_key and col.name in child_item
        }
        new_fingerprint = self._compute_child_fingerprint(child_inputs, child_spec)

        existing_children = self._store.read_rows(
            child_spec.name,
            [("_parent_id", "eq", parent_id), (child_spec.identity, "eq", child_identity)],
        )
        existing_child = max(existing_children, key=lambda r: r.get("_write_gen", 0)) if existing_children else None
        if existing_child is not None and existing_child.get("_row_fingerprint") == new_fingerprint:
            status = existing_child.get("_status")
            if status is None or status == "complete":
                bumped = dict(existing_child)
                bumped["_write_gen"] = write_gen
                self._store.write_rows(child_spec.name, [bumped])
                return None
        return child_identity, child_inputs, new_fingerprint, existing_child

    def _child_row(
        self,
        child_spec: TableSpec,
        child_item: dict[str, Any],
        child_identity: str,
        parent_id: str,
        new_fingerprint: str,
        write_gen: int,
        *,
        status: str,
        error: str | None,
        outputs: dict[str, Any] | None = None,
        provenances: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Assemble a child row from its source item plus (on success) its derived outputs."""
        row = {
            child_spec.identity: child_identity,
            "_parent_id": parent_id,
            "_write_gen": write_gen,
            "_row_fingerprint": new_fingerprint,
            "_status": status,
            "_error": error,
        }
        for k, v in child_item.items():
            if k != child_spec.identity and k != "_parent_id":
                row[k] = v
        for k, v in (outputs or {}).items():
            row[k] = v
        for name, prov in (provenances or {}).items():
            row[f"_provenance_{name}"] = prov
        return row

    def _child_provenances(self, child_spec: TableSpec, values: dict[str, Any]) -> dict[str, str]:
        """Per-column provenance for a child row, from the inner graph's nodes."""
        provenances: dict[str, str] = {}
        for node in self._nodes_in_dependency_order(child_spec):
            prov = self._node_provenance(node, values)
            for c in self._node_columns(node, child_spec):
                provenances[c.name] = prov
        return provenances

    def _rebuild_child_items(self, child_spec: TableSpec, parent_id: str) -> list[dict[str, Any]]:
        """Reconstruct the mapped items from stored child rows (source + metadata columns).

        Error child rows are included — their source columns are preserved — so
        they naturally retry.
        """
        rows = _dedup_child_rows(
            self._store.read_rows(child_spec.name, [("_parent_id", "eq", parent_id)]),
            child_spec.identity,
        )
        derived = {c.name for c in child_spec.columns if c.role == "derived"}
        return [
            {k: _normalize_value(v) for k, v in row.items() if k not in derived and k != "_parent_id" and not is_internal_column(k)} for row in rows
        ]

    def _reconcile_child_row(
        self,
        child_spec: TableSpec,
        child_item: dict[str, Any],
        existing_child: dict[str, Any],
        parent_id: str,
        new_fingerprint: str,
        write_gen: int,
    ) -> dict[str, Any] | None:
        """Column-scoped child upsert: reuse fresh columns, re-derive stale ones.

        Returns the new child row, or None when a required input is unavailable —
        the caller falls back to a full child-graph run.
        """
        values = self._stored_values(existing_child)
        values.update(child_item)
        outputs: dict[str, Any] = {}
        provenances: dict[str, str] = {}
        for node in self._nodes_in_dependency_order(child_spec):
            prov = self._node_provenance(node, values)
            if prov is None:
                return None
            if self._node_is_fresh(node, prov, existing_child, child_spec):
                node_outputs = {c.name: _normalize_value(existing_child[c.name]) for c in self._node_columns(node, child_spec)}
            else:
                node_outputs = self._run_column_node(node, values)
            for c in self._node_columns(node, child_spec):
                if c.name in node_outputs:
                    outputs[c.name] = node_outputs[c.name]
                    values[c.name] = node_outputs[c.name]
                provenances[c.name] = prov
        child_identity = child_item.get(child_spec.identity, "")
        return self._child_row(
            child_spec,
            child_item,
            child_identity,
            parent_id,
            new_fingerprint,
            write_gen,
            status="complete",
            error=None,
            outputs=outputs,
            provenances=provenances,
        )

    async def _reconcile_child_row_async(
        self,
        child_spec: TableSpec,
        child_item: dict[str, Any],
        existing_child: dict[str, Any],
        parent_id: str,
        new_fingerprint: str,
        write_gen: int,
    ) -> dict[str, Any] | None:
        """Async twin of ``_reconcile_child_row`` — identical except the awaited node runs."""
        values = self._stored_values(existing_child)
        values.update(child_item)
        outputs: dict[str, Any] = {}
        provenances: dict[str, str] = {}
        for node in self._nodes_in_dependency_order(child_spec):
            prov = self._node_provenance(node, values)
            if prov is None:
                return None
            if self._node_is_fresh(node, prov, existing_child, child_spec):
                node_outputs = {c.name: _normalize_value(existing_child[c.name]) for c in self._node_columns(node, child_spec)}
            else:
                node_outputs = await self._run_column_node_async(node, values)
            for c in self._node_columns(node, child_spec):
                if c.name in node_outputs:
                    outputs[c.name] = node_outputs[c.name]
                    values[c.name] = node_outputs[c.name]
                provenances[c.name] = prov
        child_identity = child_item.get(child_spec.identity, "")
        return self._child_row(
            child_spec,
            child_item,
            child_identity,
            parent_id,
            new_fingerprint,
            write_gen,
            status="complete",
            error=None,
            outputs=outputs,
            provenances=provenances,
        )

    def _insert_children(self, parent_id: str, outputs: dict, child_spec: TableSpec, write_gen: int) -> None:
        child_items = self._child_items(outputs, child_spec)
        if child_items is None:
            return
        self._insert_children_items(parent_id, child_items, child_spec, write_gen)

    def _insert_children_items(self, parent_id: str, child_items: list, child_spec: TableSpec, write_gen: int) -> None:
        if child_spec.child_graph is None:
            return
        bound_graph = self._bind_child_components(child_spec.child_graph)
        for raw_item in child_items:
            child_item = _normalize_to_dict(raw_item)
            plan = self._plan_child(child_spec, child_item, parent_id, write_gen)
            if plan is None:
                continue
            child_identity, child_inputs, new_fingerprint, existing_child = plan
            row = None
            if existing_child is not None and existing_child.get("_status") in (None, "complete"):
                try:
                    row = self._reconcile_child_row(child_spec, child_item, existing_child, parent_id, new_fingerprint, write_gen)
                except Exception as e:
                    if self._on_error == "raise":
                        raise
                    row = self._child_row(
                        child_spec,
                        child_item,
                        child_identity,
                        parent_id,
                        new_fingerprint,
                        write_gen,
                        status="error",
                        error=f"{type(e).__name__}: {e}",
                    )
                    self._store.write_rows(child_spec.name, [row])
                    continue
            if row is None:
                try:
                    child_outputs = self._extract_outputs(self._runner.run(bound_graph, **child_inputs))
                except Exception as e:
                    if self._on_error == "raise":
                        raise
                    row = self._child_row(
                        child_spec,
                        child_item,
                        child_identity,
                        parent_id,
                        new_fingerprint,
                        write_gen,
                        status="error",
                        error=f"{type(e).__name__}: {e}",
                    )
                    self._store.write_rows(child_spec.name, [row])
                    continue
                child_values = {**child_item, **child_outputs}
                row = self._child_row(
                    child_spec,
                    child_item,
                    child_identity,
                    parent_id,
                    new_fingerprint,
                    write_gen,
                    status="complete",
                    error=None,
                    outputs=child_outputs,
                    provenances=self._child_provenances(child_spec, child_values),
                )
            self._store.write_rows(child_spec.name, [row])

    async def _insert_children_async(self, parent_id: str, outputs: dict, child_spec: TableSpec, write_gen: int) -> None:
        child_items = self._child_items(outputs, child_spec)
        if child_items is None:
            return
        await self._insert_children_items_async(parent_id, child_items, child_spec, write_gen)

    async def _insert_children_items_async(self, parent_id: str, child_items: list, child_spec: TableSpec, write_gen: int) -> None:
        if child_spec.child_graph is None:
            return
        bound_graph = self._bind_child_components(child_spec.child_graph)
        for raw_item in child_items:
            child_item = _normalize_to_dict(raw_item)
            plan = self._plan_child(child_spec, child_item, parent_id, write_gen)
            if plan is None:
                continue
            child_identity, child_inputs, new_fingerprint, existing_child = plan
            row = None
            if existing_child is not None and existing_child.get("_status") in (None, "complete"):
                try:
                    row = await self._reconcile_child_row_async(child_spec, child_item, existing_child, parent_id, new_fingerprint, write_gen)
                except Exception as e:
                    if self._on_error == "raise":
                        raise
                    row = self._child_row(
                        child_spec,
                        child_item,
                        child_identity,
                        parent_id,
                        new_fingerprint,
                        write_gen,
                        status="error",
                        error=f"{type(e).__name__}: {e}",
                    )
                    self._store.write_rows(child_spec.name, [row])
                    continue
            if row is None:
                try:
                    child_outputs = self._extract_outputs(await self._runner.run(bound_graph, **child_inputs))
                except Exception as e:
                    if self._on_error == "raise":
                        raise
                    row = self._child_row(
                        child_spec,
                        child_item,
                        child_identity,
                        parent_id,
                        new_fingerprint,
                        write_gen,
                        status="error",
                        error=f"{type(e).__name__}: {e}",
                    )
                    self._store.write_rows(child_spec.name, [row])
                    continue
                child_values = {**child_item, **child_outputs}
                row = self._child_row(
                    child_spec,
                    child_item,
                    child_identity,
                    parent_id,
                    new_fingerprint,
                    write_gen,
                    status="complete",
                    error=None,
                    outputs=child_outputs,
                    provenances=self._child_provenances(child_spec, child_values),
                )
            self._store.write_rows(child_spec.name, [row])

    def update(self, identity_value: str, **changes: Any) -> Any:
        """Update a row. Re-derives downstream if source columns changed."""
        self._require_runner()
        self._ensure_analyzed()
        if self._is_async_runner():
            return self._update_async(identity_value, **changes)
        return self._update_sync(identity_value, **changes)

    def _prepare_update(self, identity_value: str, changes: dict[str, Any]) -> tuple[dict[str, Any], bool, int]:
        existing = self._store.read_one(self._spec.name, self._identity, identity_value)
        if existing is None:
            raise KeyError(identity_value)

        item: dict[str, Any] = {self._identity: identity_value}
        for c in self._spec.columns:
            if c.role == "source" and c.name in existing:
                item[c.name] = _normalize_value(existing[c.name])
        spec_col_names = {c.name for c in self._spec.columns}
        for k, v in existing.items():
            if k not in spec_col_names and not is_internal_column(k):
                item[k] = _normalize_value(v)
        item.update(changes)

        source_names = {c.name for c in self._spec.columns if c.role == "source"}
        needs_rederive = any(k in source_names for k in changes)

        write_gen = self._store.max_write_gen(self._spec.name) + 1
        return item, needs_rederive, write_gen

    def _row_with_changes(self, identity_value: str, changes: dict[str, Any], write_gen: int) -> dict[str, Any]:
        """Next generation of a row with metadata changes applied (no re-derivation)."""
        existing = self._store.read_one(self._spec.name, self._identity, identity_value)
        # A metadata-only update may introduce a brand-new column (a curated key
        # the schema has never seen); evolve for it first, or the write would
        # silently drop the unknown key.
        self._evolve_for_metadata({self._identity: identity_value, **changes})
        row = {k: _normalize_value(v) for k, v in existing.items()}
        row.update(changes)
        row["_write_gen"] = write_gen
        return row

    def _update_sync(self, identity_value: str, **changes: Any) -> None:
        item, needs_rederive, write_gen = self._prepare_update(identity_value, changes)
        if not needs_rederive:
            row = self._row_with_changes(identity_value, changes, write_gen)
            self._store.write_rows(self._spec.name, [row])
            self._cleanup_old_parent_gens(identity_value, write_gen)
            return

        graph_inputs = self._extract_graph_inputs(item)
        existing = self._store.read_one(self._spec.name, self._identity, identity_value)
        if self._can_reconcile(existing):
            reconciled = self._reconcile_columns(item, existing)
            if reconciled is not None:
                self._apply_reconciled(item, graph_inputs, existing, reconciled, parent_skipped=False, write_gen=write_gen)
                return

        outputs = self._extract_outputs(self._runner.run(self._graph, **graph_inputs))
        self._evolve_for_metadata(item)
        row = self._build_row(item, graph_inputs, outputs, write_gen)
        # Write new children BEFORE the parent row and old-gen cleanup — crash-safe
        # ordering: the parent row carries the boundary provenance that vouches
        # for the stored child set.
        for child_spec in self._spec.children:
            self._insert_children(identity_value, outputs, child_spec, write_gen)
        self._store.write_rows(self._spec.name, [row])
        self._cleanup_old_child_gens(identity_value, write_gen)
        self._cleanup_old_parent_gens(identity_value, write_gen)

    async def _update_async(self, identity_value: str, **changes: Any) -> None:
        item, needs_rederive, write_gen = self._prepare_update(identity_value, changes)
        if not needs_rederive:
            row = self._row_with_changes(identity_value, changes, write_gen)
            self._store.write_rows(self._spec.name, [row])
            self._cleanup_old_parent_gens(identity_value, write_gen)
            return

        graph_inputs = self._extract_graph_inputs(item)
        existing = self._store.read_one(self._spec.name, self._identity, identity_value)
        if self._can_reconcile(existing):
            reconciled = await self._reconcile_columns_async(item, existing)
            if reconciled is not None:
                await self._apply_reconciled_async(item, graph_inputs, existing, reconciled, parent_skipped=False, write_gen=write_gen)
                return

        outputs = self._extract_outputs(await self._runner.run(self._graph, **graph_inputs))
        self._evolve_for_metadata(item)
        row = self._build_row(item, graph_inputs, outputs, write_gen)
        # Write new children BEFORE the parent row and old-gen cleanup — crash-safe
        # ordering: the parent row carries the boundary provenance that vouches
        # for the stored child set.
        for child_spec in self._spec.children:
            await self._insert_children_async(identity_value, outputs, child_spec, write_gen)
        self._store.write_rows(self._spec.name, [row])
        self._cleanup_old_child_gens(identity_value, write_gen)
        self._cleanup_old_parent_gens(identity_value, write_gen)

    def delete(self, identity_value: str) -> Any:
        """Delete a row and cascade-delete its children."""
        self._ensure_analyzed()
        if self._is_async_runner():
            return self._delete_async(identity_value)
        return self._delete_sync(identity_value)

    def _delete_sync(self, identity_value: str) -> None:
        existing = self._store.read_one(self._spec.name, self._identity, identity_value)
        if existing is None:
            return

        for child_spec in self._spec.children:
            self._store.delete_rows(child_spec.name, [("_parent_id", "eq", identity_value)])
        self._store.delete_rows(self._spec.name, [(self._identity, "eq", identity_value)])

    async def _delete_async(self, identity_value: str) -> None:
        self._delete_sync(identity_value)

    def sync(self, items: list[dict[str, Any]]) -> Any:
        """Reconcile: insert new, update changed, delete missing, skip unchanged."""
        self._require_runner()
        self._ensure_analyzed()
        if self._is_async_runner():
            return self._sync_async(items)
        return self._sync_sync(items)

    def _sync_existing_index(self) -> dict[str, dict[str, Any]]:
        """Map identity -> newest stored row, for reconciliation."""
        rows = _dedup_rows(self._store.read_rows(self._spec.name), self._identity)
        return {str(row[self._identity]): row for row in rows if row.get(self._identity) is not None}

    def _row_unchanged(self, item: dict[str, Any], existing: dict[str, Any]) -> bool:
        return existing.get("_row_fingerprint") == self._compute_row_fingerprint(self._extract_graph_inputs(item))

    def _error_row_for(self, id_val: str) -> ErrorRow | None:
        """Build an ErrorRow from the persisted error row for a failed identity."""
        row = self._store.read_one(self._spec.name, self._identity, id_val)
        if not row:
            return None
        err = row.get("_error", "")
        return ErrorRow(
            identity={self._identity: id_val},
            error_type=err.split(":")[0] if err else "Unknown",
            error_msg=err,
        )

    def _sync_sync(self, items: list[dict[str, Any]]) -> Any:
        existing_by_id = self._sync_existing_index()
        incoming_ids: set[str] = set()
        inserted = updated = skipped = errored = 0
        errors: list[ErrorRow] = []
        write_gen = self._store.max_write_gen(self._spec.name) + 1

        for item in items:
            id_val = str(item[self._identity])
            incoming_ids.add(id_val)
            existing = existing_by_id.get(id_val)
            if existing is None:
                if self._insert_one(item, write_gen) == "errored":
                    errored += 1
                    err = self._error_row_for(id_val)
                    if err is not None:
                        errors.append(err)
                else:
                    inserted += 1
            elif self._row_unchanged(item, existing):
                skipped += 1
            else:
                self.update(id_val, **{k: v for k, v in item.items() if k != self._identity})
                updated += 1

        deleted = 0
        for id_val in existing_by_id:
            if id_val not in incoming_ids:
                self.delete(id_val)
                deleted += 1

        return SyncResult(inserted=inserted, updated=updated, deleted=deleted, skipped=skipped, errored=errored, errors=tuple(errors))

    async def _sync_async(self, items: list[dict[str, Any]]) -> Any:
        existing_by_id = self._sync_existing_index()
        incoming_ids: set[str] = set()
        inserted = updated = skipped = errored = 0
        errors: list[ErrorRow] = []
        write_gen = self._store.max_write_gen(self._spec.name) + 1

        for item in items:
            id_val = str(item[self._identity])
            incoming_ids.add(id_val)
            existing = existing_by_id.get(id_val)
            if existing is None:
                if await self._insert_one_async(item, write_gen) == "errored":
                    errored += 1
                    err = self._error_row_for(id_val)
                    if err is not None:
                        errors.append(err)
                else:
                    inserted += 1
            elif self._row_unchanged(item, existing):
                skipped += 1
            else:
                await self._update_async(id_val, **{k: v for k, v in item.items() if k != self._identity})
                updated += 1

        deleted = 0
        for id_val in existing_by_id:
            if id_val not in incoming_ids:
                await self._delete_async(id_val)
                deleted += 1

        return SyncResult(inserted=inserted, updated=updated, deleted=deleted, skipped=skipped, errored=errored, errors=tuple(errors))

    # --- Column re-derivation (recompute / backfill) ---

    def _source_inputs_from_row(self, existing: dict[str, Any]) -> dict[str, Any]:
        """Reconstruct graph inputs from a stored row's source columns."""
        return {c.name: _normalize_value(existing[c.name]) for c in self._spec.columns if c.role == "source" and c.name in existing}

    def _persist_node_outputs(self, existing: dict[str, Any], node: Any, node_outputs: dict[str, Any], write_gen: int) -> None:
        """Write a new generation of one row with the node's columns re-derived, then drop the old one.

        The row fingerprint is refreshed only when every derived column now
        matches the current recipe — a partially-current row must keep reporting
        stale so the pending cascade stays visible.
        """
        new_row = {k: _normalize_value(v) for k, v in existing.items()}
        for c in self._node_columns(node):
            if c.name in node_outputs:
                new_row[c.name] = node_outputs[c.name]
        prov = self._node_provenance(node, self._stored_values(new_row))
        for c in self._node_columns(node):
            new_row[f"_provenance_{c.name}"] = prov
        new_row["_write_gen"] = write_gen
        if self._row_converged(new_row):
            new_row["_row_fingerprint"] = self._compute_row_fingerprint(self._source_inputs_from_row(new_row))

        self._store.write_rows(self._spec.name, [new_row])
        self._store.delete_rows(
            self._spec.name,
            [
                (self._identity, "eq", existing[self._identity]),
                ("_write_gen", "lt", write_gen),
            ],
        )

    def _evolve_for_backfill_column(self, column: str) -> None:
        """Add `column` (and its provenance) to the schema if it doesn't exist yet."""
        sample = self._store.read_rows(self._spec.name, limit=1)
        if sample and column not in sample[0]:
            col_type = self._get_derived_column_type(column)
            self._store.evolve_schema(
                self._spec.name,
                {column: python_type_to_arrow(col_type), f"_provenance_{column}": python_type_to_arrow(str)},
            )

    @staticmethod
    def _column_is_null(value: Any) -> bool:
        return value is None or (isinstance(value, float) and math.isnan(value))

    def recompute(self, column: str) -> Any:
        """Re-derive one column for all rows using current bound components."""
        self._require_runner()
        self._ensure_analyzed()
        if self._is_async_runner():
            return self._recompute_async(column)
        return self._recompute_sync(column)

    def _recompute_sync(self, column: str) -> None:
        node = self._producing_node(column)
        write_gen = self._store.max_write_gen(self._spec.name) + 1
        for existing in _dedup_rows(self._store.read_rows(self._spec.name), self._identity):
            node_outputs = self._run_column_node(node, self._stored_values(existing))
            self._persist_node_outputs(existing, node, node_outputs, write_gen)

    async def _recompute_async(self, column: str) -> None:
        node = self._producing_node(column)
        write_gen = self._store.max_write_gen(self._spec.name) + 1
        for existing in _dedup_rows(self._store.read_rows(self._spec.name), self._identity):
            node_outputs = await self._run_column_node_async(node, self._stored_values(existing))
            self._persist_node_outputs(existing, node, node_outputs, write_gen)

    def backfill(self, column: str) -> Any:
        """Derive a new column for existing rows that have NULL."""
        self._require_runner()
        self._ensure_analyzed()
        if self._is_async_runner():
            return self._backfill_async(column)
        return self._backfill_sync(column)

    def _backfill_sync(self, column: str) -> None:
        self._evolve_for_backfill_column(column)
        node = self._producing_node(column)
        write_gen = self._store.max_write_gen(self._spec.name) + 1
        for existing in _dedup_rows(self._store.read_rows(self._spec.name), self._identity):
            if not self._column_is_null(existing.get(column)):
                continue
            node_outputs = self._run_column_node(node, self._stored_values(existing))
            self._persist_node_outputs(existing, node, node_outputs, write_gen)

    async def _backfill_async(self, column: str) -> None:
        self._evolve_for_backfill_column(column)
        node = self._producing_node(column)
        write_gen = self._store.max_write_gen(self._spec.name) + 1
        for existing in _dedup_rows(self._store.read_rows(self._spec.name), self._identity):
            if not self._column_is_null(existing.get(column)):
                continue
            node_outputs = await self._run_column_node_async(node, self._stored_values(existing))
            self._persist_node_outputs(existing, node, node_outputs, write_gen)

    # --- Fingerprint and provenance ---

    def _compute_row_fingerprint(self, graph_inputs: dict) -> str:
        return compute_row_fingerprint(self._graph, self._components, graph_inputs)

    def _compute_child_fingerprint(self, child_inputs: dict, child_spec: TableSpec) -> str:
        return compute_child_fingerprint(child_spec.child_graph, self._components, child_inputs)
