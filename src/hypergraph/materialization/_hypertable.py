"""HyperTable: a Hypergraph graph where each node output is a stored column."""

from __future__ import annotations

from typing import Any

from hypergraph import Graph
from hypergraph.materialization._fingerprint import compute_definition_hash
from hypergraph.materialization._hypertable_viz import render_hypertable
from hypergraph.materialization._indexes import IndexPolicy
from hypergraph.materialization._provenance import Provenance
from hypergraph.materialization._provenance import normalize_value as _normalize_value
from hypergraph.materialization._schema import (
    RECIPE_COLUMN,
    STATUS_COLUMNS,
    TableSpec,
    analyze_table,
    is_internal_column,
    node_func,
)
from hypergraph.materialization._types import RecipeDrift, TableStatus
from hypergraph.materialization._writes import (
    RunGraph,
    WriteOperation,
    Writes,
)
from hypergraph.materialization._writes import (
    dedup_child_rows as _dedup_child_rows,
)
from hypergraph.materialization._writes import (
    dedup_rows as _dedup_rows,
)


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
        _plain: bool = False,
    ):
        if on_error not in ("raise", "store"):
            raise ValueError(f"on_error must be 'raise' or 'store', got {on_error!r}")
        if not nodes and not _plain:
            # The degenerate no-derivation mode is promoted to its own class:
            # a derivation substrate declaring "I don't derive" hides what the
            # table is. Table wraps this machinery through the private _plain
            # flag, so the on-disk shape stays byte-identical.
            raise ValueError(
                "HyperTable requires at least one derivation node — a table with "
                "no nodes is not a derivation substrate. For a durable typed "
                "table (identity + store + schema, zero derivation) use "
                "hypergraph.materialization.Table instead."
            )
        self._plain = _plain
        self._nodes = nodes
        self._identity = identity
        self._store = store
        self._on_error = on_error
        self._components = _components or {}
        self._runner = _runner
        self._graph = _graph
        self._map_over_nodes: list[Any] = []
        self._spec: TableSpec | None = None
        self._analyzed = False
        self._column_graphs: dict[int, Any] = {}
        self._provenance_obj: Provenance | None = None
        self._indexes_obj: IndexPolicy | None = None
        self._writes_obj: Writes | None = None

    def bind(self, **components: Any) -> HyperTable:
        merged = {**self._components, **components}
        return HyperTable(
            self._nodes,
            identity=self._identity,
            store=self._store,
            on_error=self._on_error,
            _components=merged,
            _runner=self._runner,
            _plain=self._plain,
        )

    def with_runner(self, runner: Any) -> HyperTable:
        return HyperTable(
            self._nodes,
            identity=self._identity,
            store=self._store,
            on_error=self._on_error,
            _components=self._components,
            _runner=runner,
            _plain=self._plain,
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
        self._map_over_nodes.clear()
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
        self._spec = analyze_table(self._graph, self._identity, self._components, self._map_over_nodes)
        self._provenance_obj = Provenance(
            self._graph,
            self._spec,
            self._components,
            tuple(self._nodes),
            self._column_graphs,
        )
        self._indexes_obj = IndexPolicy(self._store, self._spec, self._provenance_obj)
        self._writes_obj = Writes(
            self._graph,
            self._spec,
            self._identity,
            self._store,
            self._components,
            self._on_error,
            self._provenance_obj,
        )

    @property
    def _provenance_policy(self) -> Provenance:
        if self._provenance_obj is None:
            raise RuntimeError("HyperTable provenance requested before graph analysis")
        return self._provenance_obj

    @property
    def _indexes(self) -> IndexPolicy:
        if self._indexes_obj is None:
            raise RuntimeError("HyperTable indexes requested before graph analysis")
        return self._indexes_obj

    @property
    def _writes(self) -> Writes:
        if self._writes_obj is None:
            raise RuntimeError("HyperTable writes requested before graph analysis")
        return self._writes_obj

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

    def _extract_outputs(self, result: Any) -> dict[str, Any]:
        if hasattr(result, "values") and isinstance(result.values, dict):
            return result.values
        if isinstance(result, dict):
            return result
        return {}

    def _drive_sync(self, operation: WriteOperation) -> Any:
        """Execute one shared write plan with a synchronous runner."""
        try:
            action = next(operation)
        except StopIteration as complete:
            return complete.value
        while True:
            if isinstance(action, RunGraph):
                try:
                    response = self._extract_outputs(self._runner.run(action.graph, **action.input_values()))
                except Exception as error:
                    try:
                        action = operation.throw(error)
                    except StopIteration as complete:
                        return complete.value
                    continue
            else:
                response = self._writes.executor.apply(action)
            try:
                action = operation.send(response)
            except StopIteration as complete:
                return complete.value

    async def _drive_async(self, operation: WriteOperation) -> Any:
        """Execute the same write plan; only the runner action is awaited."""
        try:
            action = next(operation)
        except StopIteration as complete:
            return complete.value
        while True:
            if isinstance(action, RunGraph):
                try:
                    response = self._extract_outputs(await self._runner.run(action.graph, **action.input_values()))
                except Exception as error:
                    try:
                        action = operation.throw(error)
                    except StopIteration as complete:
                        return complete.value
                    continue
            else:
                response = self._writes.executor.apply(action)
            try:
                action = operation.send(response)
            except StopIteration as complete:
                return complete.value

    def recipe_drift(self) -> RecipeDrift:
        """Which stored rows were derived under something other than today's recipe.

        A pure stored-column comparison: each row's ``_recipe_fingerprint``
        stamp against the current recipe-only fingerprint (node code +
        component configs + bound plain values — never input values). Reads
        project ONLY identity/reserved columns, so content bytes never leave
        the disk regardless of table size. Rows with no stamp (written before
        stamping existed) count as UNKNOWN — honestly stale, never current.
        """
        self._ensure_analyzed()
        root = self._table_recipe_drift(
            self._spec.name,
            self._identity,
            self._provenance_policy.current_recipe_fingerprint(),
            child=False,
        )
        children = tuple(
            self._table_recipe_drift(
                child_spec.name,
                child_spec.identity,
                self._provenance_policy.current_child_recipe_fingerprint(child_spec),
                child=True,
            )
            for child_spec in self._spec.children
        )
        return RecipeDrift(
            table=root.table,
            total=root.total,
            current=root.current,
            drifted=root.drifted,
            unknown=root.unknown,
            children=children,
        )

    def _table_recipe_drift(self, table_name: str, identity: str, current_fingerprint: str, *, child: bool) -> RecipeDrift:
        columns = [identity, "_write_gen"]
        if child:
            columns.append("_parent_id")
        # A store that raises on unknown projected columns (LanceDB) reports
        # its physical schema; only ask for the stamp when it exists there. A
        # store that cannot introspect (``column_names() == []``) either
        # returns full rows or silently omits missing projected keys — both
        # read the stamp when present and as None when not.
        physical = self._store.column_names(table_name)
        if not physical or RECIPE_COLUMN in physical:
            columns.append(RECIPE_COLUMN)
        rows = self._read_projected(table_name, columns)
        rows = _dedup_child_rows(rows, identity) if child else _dedup_rows(rows, identity)
        current = drifted = unknown = 0
        for row in rows:
            stamp = row.get(RECIPE_COLUMN)
            if not isinstance(stamp, str) or not stamp:
                unknown += 1
            elif stamp == current_fingerprint:
                current += 1
            else:
                drifted += 1
        return RecipeDrift(table=table_name, total=len(rows), current=current, drifted=drifted, unknown=unknown)

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
        return render_hypertable(
            self._graph,
            self._spec,
            self._map_over_nodes,
            self._components,
            include_children=include_children,
            options=kwargs,
        )

    def _read_projected(self, table_name: str, columns: list[str], where: Any = None) -> list[dict[str, Any]]:
        """Read only ``columns`` when the store can project; otherwise full rows.

        Gated on ``supports_column_projection`` so an older external store whose
        ``read_rows`` predates the ``columns=`` kwarg is never handed it — it
        simply returns full rows and the caller works unchanged. Used only by
        metadata-only internal reads (count/dedup) where dropping the blob
        columns is a clear, safe win: the columns requested are always identity
        and reserved keys, never derived content.
        """
        predicate = _where_predicate(where)
        if self._store.supports_column_projection():
            return self._store.read_rows(table_name, predicate, columns=columns)
        return self._store.read_rows(table_name, predicate)

    def count(self, child_table: str | None = None) -> int:
        self._ensure_analyzed()
        if child_table:
            for child in self._spec.children:
                if child.name == child_table:
                    rows = self._read_projected(child.name, [child.identity, "_parent_id", "_write_gen"])
                    return len(_dedup_child_rows(rows, child.identity))
            return 0
        rows = self._read_projected(self._spec.name, [self._identity, "_write_gen"])
        return len(_dedup_rows(rows, self._identity))

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
            fingerprint_of=lambda row: self._provenance_policy.root_fingerprint(self._provenance_policy.source_inputs(row)),
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
        values = self._provenance_policy.stored_values(row)
        for node in self._provenance_policy.nodes_in_dependency_order(spec):
            prov = self._provenance_policy.node_provenance(node, values)
            for c in self._provenance_policy.node_columns(node, spec):
                if prov is None or self._provenance_policy.column_is_null(row.get(c.name)) or row.get(f"_provenance_{c.name}") != prov:
                    counts[c.name] = counts.get(c.name, 0) + 1

    def _child_status(self, child_spec: TableSpec) -> TableStatus:
        rows = _dedup_child_rows(self._store.read_rows(child_spec.name), child_spec.identity)
        stale_column_counts: dict[str, int] = {}
        counts = self._classify_rows(
            rows,
            identity=child_spec.identity,
            fingerprint_of=lambda row: self._provenance_policy.child_fingerprint(
                self._provenance_policy.child_source_inputs(row, child_spec), child_spec
            ),
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

    def get(self, identity_value: str, *, include_status: bool = False) -> dict[str, Any] | None:
        self._ensure_analyzed()
        row = self._store.read_one(self._spec.name, self._identity, identity_value)
        if row is None:
            return None
        return _public_row(row, include_status=include_status)

    def explain(self, identity_value: str) -> dict[str, dict[str, str | None]]:
        """Resolve a row's per-column recipe to the readable source of THIS table's nodes.

        For each derived column returns ``{"provenance": <node definition hash>,
        "source": <node source verbatim>}``, pulled from the store's recipe
        journal (commits or not). The ``provenance`` key is the node's DEFINITION
        hash (stable across rows, the journal key), NOT the row's value-chained
        ``_provenance_*`` stamp — the journal deliberately holds recipe meaning,
        one payload per recipe, never one per row.

        The source reported is that of the node objects bound to THIS table
        instance — i.e. the recipe the row would derive under now. On a fresh or
        just-synced row that IS the recipe the row was derived under. To resolve
        an OLD stamp captured before a recipe change (the durable "what was it
        derived under, no git" guarantee), keep the stamp and call
        ``resolve_provenance`` — a direct journal lookup that never depends on the
        current table's nodes. A column whose recipe was never journaled resolves
        to ``source=None`` rather than raising, so explain over a
        partially-journaled legacy row degrades instead of blowing up.
        """
        self._ensure_analyzed()
        row = self._store.read_one(self._spec.name, self._identity, identity_value)
        if row is None:
            raise KeyError(identity_value)
        explained: dict[str, dict[str, str | None]] = {}
        for c in self._provenance_policy.derived_columns():
            if c.produced_by is None:
                continue
            def_hash = compute_definition_hash(node_func(c.produced_by))
            explained[c.name] = {
                "provenance": def_hash,
                "source": self._writes.journal.resolve(def_hash),
            }
        return explained

    def resolve_provenance(self, stamp: str) -> str | None:
        """The recipe text a provenance/definition hash was journaled under, or None.

        The public single-verb resolver behind ``explain``: hand it any stamp
        (a column's ``_provenance_*`` value, a bare node definition hash, a
        config/bound-value payload hash) and get the readable payload back.
        """
        self._ensure_analyzed()
        return self._writes.journal.resolve(stamp)

    def journal_rows(self) -> list[dict[str, Any]]:
        """Every journaled ``(hash, kind, payload, first_seen_at)`` row — the raw recipe journal."""
        self._ensure_analyzed()
        return self._writes.journal.rows()

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
        return self._indexes.create(name, on=on, rows=rows, text=text, vector=vector)

    def list_indexes(self) -> list[dict[str, Any]]:
        """The persisted index specs, each with ``current``: does its recorded recipe match the recipe now?"""
        self._ensure_analyzed()
        return self._indexes.list()

    def drop_index(self, name: str) -> None:
        self._ensure_analyzed()
        self._indexes.drop(name)

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
        hits = self._indexes.search(query_vector, index=index, limit=limit, where=where)
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
        operation = self._writes.plans.set_children(tuple(_where_predicate(where)), fields)
        return self._drive_sync(operation)

    def set(self, where: Any, **fields: Any) -> Any:
        """Bulk metadata update for all rows matching a predicate."""
        self._ensure_analyzed()
        operation = self._writes.plans.set_rows(tuple(_where_predicate(where)), fields)
        if self._is_async_runner():
            return self._drive_async(operation)
        return self._drive_sync(operation)

    @staticmethod
    def _insert_items(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if args and isinstance(args[0], list):
            return args[0]
        if kwargs:
            return [kwargs]
        raise ValueError("insert() requires kwargs or a list of dicts")

    def insert(self, *args, **kwargs) -> Any:
        self._require_runner()
        self._ensure_analyzed()
        operation = self._writes.plans.insert(self._insert_items(*args, **kwargs))
        if self._is_async_runner():
            return self._drive_async(operation)
        return self._drive_sync(operation)

    def update(self, identity_value: str, **changes: Any) -> Any:
        """Update a row. Re-derives downstream if source columns changed."""
        self._require_runner()
        self._ensure_analyzed()
        operation = self._writes.plans.update(identity_value, changes)
        if self._is_async_runner():
            return self._drive_async(operation)
        return self._drive_sync(operation)

    def delete(self, identity_value: str) -> Any:
        """Delete a row and cascade-delete its children."""
        self._ensure_analyzed()
        operation = self._writes.plans.delete(identity_value)
        if self._is_async_runner():
            return self._drive_async(operation)
        return self._drive_sync(operation)

    def sync(self, items: list[dict[str, Any]]) -> Any:
        """Reconcile: insert new, update changed, delete missing, skip unchanged."""
        self._require_runner()
        self._ensure_analyzed()
        operation = self._writes.plans.sync(items)
        if self._is_async_runner():
            return self._drive_async(operation)
        return self._drive_sync(operation)

    def recompute(self, column: str) -> Any:
        """Re-derive one column for all rows using current bound components."""
        self._require_runner()
        self._ensure_analyzed()
        operation = self._writes.plans.derive_column(column, backfill=False)
        if self._is_async_runner():
            return self._drive_async(operation)
        return self._drive_sync(operation)

    def backfill(self, column: str) -> Any:
        """Derive a new column for existing rows that have NULL."""
        self._require_runner()
        self._ensure_analyzed()
        operation = self._writes.plans.derive_column(column, backfill=True)
        if self._is_async_runner():
            return self._drive_async(operation)
        return self._drive_sync(operation)
