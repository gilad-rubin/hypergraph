"""HyperTable: a Hypergraph graph where each node output is a stored column."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, Literal

from hypergraph import Graph
from hypergraph.materialization._fingerprint import compute_definition_hash
from hypergraph.materialization._hypertable_viz import render_hypertable
from hypergraph.materialization._indexes import IndexPolicy
from hypergraph.materialization._provenance import Provenance
from hypergraph.materialization._provenance import normalize_value as _normalize_value
from hypergraph.materialization._schema import (
    PARENT_LINK_COLUMN,
    QUESTION_COLUMN,
    RECIPE_COLUMN,
    STATUS_COLUMNS,
    TableSpec,
    analyze_table,
    is_internal_column,
    node_func,
    python_type_to_arrow,
)
from hypergraph.materialization._types import (
    ErroredRow,
    RecipeDrift,
    RowReceipt,
    RowStatus,
    TableReceipt,
    TableStatus,
    WaitingRow,
    deserialize_question,
)
from hypergraph.materialization._write_actions import RunGraph, WriteOperation
from hypergraph.materialization._writes import WritePlanner
from hypergraph.materialization._writes import (
    dedup_child_rows as _dedup_child_rows,
)
from hypergraph.materialization._writes import (
    dedup_rows as _dedup_rows,
)

if TYPE_CHECKING:
    from hypergraph.materialization._table_store import TableStore
    from hypergraph.runners import BaseRunner


def _public_row(row: dict[str, Any], spec: TableSpec | None = None) -> dict[str, Any]:
    gate_outputs = {
        column.name
        for column in (spec.columns if spec is not None else ())
        if any(
            getattr(producer, "is_gate", False)
            for producer in (column.produced_by if isinstance(column.produced_by, tuple) else (column.produced_by,))
        )
    }
    result = {}
    for k, v in row.items():
        if not is_internal_column(k) and k not in gate_outputs:
            result[k] = _normalize_value(v)
    return result


def _where_predicate(where: Any) -> list[tuple[str, str, Any]]:
    if where is None:
        return []
    if isinstance(where, dict):
        return [(key, "eq", value) for key, value in where.items()]
    return list(where)


class ChildTable:
    """Read and annotate one named child grain."""

    def __init__(self, parent: HyperTable, spec: TableSpec) -> None:
        self._parent = parent
        self._spec = spec

    def _public_row(self, row: dict[str, Any]) -> dict[str, Any]:
        public = _public_row(row, self._spec)
        public[self._parent._identity] = _normalize_value(row[PARENT_LINK_COLUMN])
        return public

    def _matching_rows(
        self,
        where: Any = None,
        *,
        parent: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        predicates = _where_predicate(where)
        child_columns = set(self._parent._store.column_names(self._spec.name))
        parent_columns = set(self._parent._store.column_names(self._parent._spec.name))
        child_predicates: list[tuple[str, str, Any]] = []
        parent_predicates: list[tuple[str, str, Any]] = []
        for predicate in predicates:
            column = predicate[0]
            if column in child_columns:
                child_predicates.append(predicate)
            elif column in parent_columns:
                parent_predicates.append(predicate)
            else:
                child_predicates.append(predicate)
        if parent is not None:
            child_predicates.append((PARENT_LINK_COLUMN, "eq", parent))
        if parent_predicates:
            parents = _dedup_rows(
                self._parent._store.read_rows(self._parent._spec.name, parent_predicates),
                self._parent._identity,
            )
            parent_ids = [row[self._parent._identity] for row in parents]
            if not parent_ids:
                return []
            child_predicates.append((PARENT_LINK_COLUMN, "in", parent_ids))
        rows = self._parent._store.read_rows(self._spec.name, child_predicates or None)
        rows = _dedup_child_rows(rows, self._spec.identity)
        return rows[:limit] if limit is not None else rows

    def get(self, parent_id: str, child_id: str) -> dict[str, Any] | None:
        rows = self._matching_rows(
            [(PARENT_LINK_COLUMN, "eq", parent_id), (self._spec.identity, "eq", child_id)],
            limit=1,
        )
        return self._public_row(rows[0]) if rows else None

    def rows(
        self,
        where: Any = None,
        *,
        parent: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return [self._public_row(row) for row in self._matching_rows(where, parent=parent, limit=limit)]

    def waiting(self) -> tuple[WaitingRow, ...]:
        result: list[WaitingRow] = []
        for row in self._matching_rows([(STATUS_COLUMNS[0], "eq", RowStatus.WAITING.value)]):
            pause, provenance = deserialize_question(row[QUESTION_COLUMN])
            result.append(WaitingRow(str(row[self._spec.identity]), pause, self._public_row(row), provenance))
        return tuple(result)

    def errors(self) -> tuple[ErroredRow, ...]:
        return tuple(
            ErroredRow(str(row[self._spec.identity]), str(row.get("_error") or ""), self._public_row(row))
            for row in self._matching_rows([(STATUS_COLUMNS[0], "eq", RowStatus.ERROR.value)])
        )

    def set(self, where: Any, **fields: Any) -> int:
        blocked = sorted(column.name for column in self._spec.columns if column.content_key and column.name in fields)
        if blocked:
            raise ValueError(
                "ChildTable.set() cannot update content-key fields.\n\n"
                f"Fields: {', '.join(blocked)}\n\n"
                "How to fix: update annotation metadata only; converge content changes through the parent graph."
            )
        rows = self._matching_rows(where)
        if not rows:
            return 0
        self._parent._store.evolve_schema(
            self._spec.name,
            {name: python_type_to_arrow(type(value) if value is not None else str) for name, value in fields.items()},
        )
        write_gen = self._parent._store.max_write_gen(self._spec.name) + 1
        for existing in rows:
            row = {key: _normalize_value(value) for key, value in existing.items()}
            row.update(fields)
            row["_write_gen"] = write_gen
            self._parent._store.write_rows(self._spec.name, [row])
            self._parent._store.delete_rows(
                self._spec.name,
                [
                    (self._spec.identity, "eq", existing[self._spec.identity]),
                    (PARENT_LINK_COLUMN, "eq", existing[PARENT_LINK_COLUMN]),
                    ("_write_gen", "lt", write_gen),
                ],
            )
        return len(rows)

    def delete(self, where: Any) -> int:
        rows = self._matching_rows(where)
        deleted = 0
        for row in rows:
            deleted += self._parent._store.delete_rows(
                self._spec.name,
                [
                    (self._spec.identity, "eq", row[self._spec.identity]),
                    (PARENT_LINK_COLUMN, "eq", row[PARENT_LINK_COLUMN]),
                ],
            )
        return deleted

    def count(self) -> int:
        return len(self._matching_rows())


class HyperTable:
    """A Hypergraph graph where each node output is a stored column."""

    def __init__(
        self,
        graph: Graph,
        *,
        identity: str,
        store: TableStore,
        runner: BaseRunner,
        on_error: Literal["raise", "store"] = "raise",
        name: str | None = None,
    ) -> None:
        if on_error not in ("raise", "store"):
            raise ValueError(
                "HyperTable on_error must be 'raise' or 'store'.\n\n"
                f"Received: {on_error!r}\n\n"
                "How to fix: pass on_error='raise' for immediate failures or "
                "on_error='store' for typed errored rows."
            )
        if not isinstance(graph, Graph):
            raise TypeError(
                "HyperTable requires a Graph, not a node list.\n\n"
                f"Received: {type(graph).__name__}\n\n"
                "How to fix: construct Graph([...]) and call graph.as_table(...)."
            )
        self._source_graph = graph
        self._identity = identity
        self._store = store
        self._on_error = on_error
        self._name = name
        self._runner = runner
        self._components = dict(graph._bound)
        graph_nodes = list(graph.nodes.values()) if isinstance(graph.nodes, dict) else []
        if not graph_nodes:
            raise ValueError(
                "Graph.as_table() requires at least one derivation node.\n\n"
                f"Graph: {graph.name or 'unnamed'}\n\n"
                "How to fix: add a derivation node, or use hypergraph.materialization.Table "
                "for a durable table without derivation."
            )
        self._map_over_nodes = [node for node in graph_nodes if getattr(node, "_map_config", None)]
        if self._map_over_nodes:
            plain_nodes = [node for node in graph_nodes if node not in self._map_over_nodes]
            self._graph = Graph(plain_nodes, name=graph.name)
            root_bindings = {key: value for key, value in self._components.items() if key in set(self._graph.inputs.all)}
            if root_bindings:
                self._graph = self._graph.bind(root_bindings)
        else:
            self._graph = graph
        self._spec: TableSpec | None = None
        self._analyzed = False
        self._column_graphs: dict[int, Any] = {}
        self._provenance_obj: Provenance | None = None
        self._indexes_obj: IndexPolicy | None = None
        self._write_planner_obj: WritePlanner | None = None

    def _ensure_analyzed(self):
        if self._analyzed:
            return
        self._analyze_graph()
        self._resolve_store()
        self._analyzed = True

    def _analyze_graph(self):
        self._spec = analyze_table(
            self._graph,
            self._identity,
            self._components,
            self._map_over_nodes,
            name=self._name,
        )
        self._provenance_obj = Provenance(
            self._graph,
            self._spec,
            self._components,
            self._column_graphs,
        )
        self._indexes_obj = IndexPolicy(self._store, self._spec, self._provenance_obj)
        self._write_planner_obj = WritePlanner(
            self._graph,
            self._store,
            self._spec,
            self._identity,
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
    def _write_planner(self) -> WritePlanner:
        if self._write_planner_obj is None:
            raise RuntimeError("HyperTable write planner requested before graph analysis")
        return self._write_planner_obj

    def _resolve_store(self):
        from hypergraph.materialization._table_store import TableStore

        if not isinstance(self._store, TableStore):
            raise TypeError(
                "HyperTable store must implement TableStore.\n\n"
                f"Received: {type(self._store).__name__}\n\n"
                "How to fix: pass LanceDBStore(...) or a validated TableStore subclass."
            )

        self._store.open(self._spec, self._spec.children)

    def _is_async_runner(self) -> bool:
        from hypergraph.runners import AsyncRunner

        return isinstance(self._runner, AsyncRunner)

    # --- Shared helpers ---

    def _drive_sync(self, operation: WriteOperation) -> Any:
        """Execute one shared write plan with a synchronous runner."""
        try:
            action = next(operation)
        except StopIteration as complete:
            return complete.value
        while True:
            if isinstance(action, RunGraph):
                try:
                    response = self._runner.run(action.graph, **action.input_values())
                except Exception as error:
                    try:
                        action = operation.throw(error)
                    except StopIteration as complete:
                        return complete.value
                    continue
            else:
                raise TypeError(f"unsupported write effect: {type(action).__name__}")
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
                    response = await self._runner.run(action.graph, **action.input_values())
                except Exception as error:
                    try:
                        action = operation.throw(error)
                    except StopIteration as complete:
                        return complete.value
                    continue
            else:
                raise TypeError(f"unsupported write effect: {type(action).__name__}")
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
    def graph(self) -> Graph:
        """The graph artifact this table persists."""
        return self._source_graph

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

    def count(self) -> int:
        self._ensure_analyzed()
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

    def get(self, identity_value: str) -> dict[str, Any] | None:
        self._ensure_analyzed()
        row = self._store.read_one(self._spec.name, self._identity, identity_value)
        if row is None:
            return None
        return _public_row(row, self._spec)

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
                "source": self._write_planner.journal.resolve(def_hash),
            }
        return explained

    def resolve_provenance(self, stamp: str) -> str | None:
        """The recipe text a provenance/definition hash was journaled under, or None.

        The public single-verb resolver behind ``explain``: hand it any stamp
        (a column's ``_provenance_*`` value, a bare node definition hash, a
        config/bound-value payload hash) and get the readable payload back.
        """
        self._ensure_analyzed()
        return self._write_planner.journal.resolve(stamp)

    def journal_rows(self) -> list[dict[str, Any]]:
        """Every journaled ``(hash, kind, payload, first_seen_at)`` row — the raw recipe journal."""
        self._ensure_analyzed()
        return self._write_planner.journal.rows()

    def rows(self, where: Any = None, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return public rows matching a store predicate."""
        self._ensure_analyzed()
        rows = self._store.read_rows(self._spec.name, _where_predicate(where), limit=limit)
        rows = _dedup_rows(rows, self._identity)
        return [_public_row(row, self._spec) for row in rows]

    def waiting(self) -> tuple[WaitingRow, ...]:
        """Return the typed inbox of rows blocked on a human answer."""
        self._ensure_analyzed()
        rows = _dedup_rows(
            self._store.read_rows(self._spec.name, [("_status", "eq", RowStatus.WAITING.value)]),
            self._identity,
        )
        waiting: list[WaitingRow] = []
        for row in rows:
            pause, provenance = deserialize_question(row[QUESTION_COLUMN])
            waiting.append(WaitingRow(str(row[self._identity]), pause, _public_row(row, self._spec), provenance))
        return tuple(waiting)

    def errors(self) -> tuple[ErroredRow, ...]:
        """Return rows whose stored derivation failed."""
        self._ensure_analyzed()
        rows = _dedup_rows(
            self._store.read_rows(self._spec.name, [("_status", "eq", RowStatus.ERROR.value)]),
            self._identity,
        )
        return tuple(ErroredRow(str(row[self._identity]), str(row.get("_error") or ""), _public_row(row, self._spec)) for row in rows)

    def child(self, name: str) -> ChildTable:
        """Return the handle for one child grain by physical name or identity."""
        self._ensure_analyzed()
        for child_spec in self._spec.children:
            if name in (child_spec.name, child_spec.identity):
                return ChildTable(self, child_spec)
        available = ", ".join(child.name for child in self._spec.children) or "none"
        raise KeyError(f"unknown child table {name!r}; available: {available}")

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
            public = _public_row(row, self._spec)
            if PARENT_LINK_COLUMN in row:
                public[self._identity] = _normalize_value(row[PARENT_LINK_COLUMN])
            public["_distance"] = _normalize_value(distance)
            results.append(public)
        return results

    def set(self, where: Any, **fields: Any) -> int | Awaitable[int]:
        """Bulk metadata update for all rows matching a predicate."""
        self._ensure_analyzed()
        if self._is_async_runner():
            return self._set_async(where, fields)
        return self._write_planner.set_rows(tuple(_where_predicate(where)), fields)

    async def _set_async(self, where: Any, fields: dict[str, Any]) -> int:
        return self._write_planner.set_rows(tuple(_where_predicate(where)), fields)

    @staticmethod
    def _insert_items(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if args and isinstance(args[0], list):
            return args[0]
        if kwargs:
            return [kwargs]
        raise ValueError(
            "HyperTable.insert() requires one keyword row or a list of row dictionaries.\n\n"
            "Received no row values.\n\n"
            "How to fix: call insert(item_id='i-1', ...) or insert([{'item_id': 'i-1', ...}])."
        )

    def insert(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> RowReceipt | TableReceipt | Awaitable[RowReceipt | TableReceipt]:
        self._ensure_analyzed()
        items = self._insert_items(*args, **kwargs)
        single = not (args and isinstance(args[0], list))
        operation = self._write_planner.insert(items)
        if self._is_async_runner():
            return self._insert_async(operation, single=single)
        receipt = self._drive_sync(operation)
        return receipt.receipts[0] if single else receipt

    async def _insert_async(self, operation: WriteOperation, *, single: bool) -> RowReceipt | TableReceipt:
        receipt = await self._drive_async(operation)
        return receipt.receipts[0] if single else receipt

    def update(self, identity_value: str, **changes: Any) -> RowReceipt | Awaitable[RowReceipt]:
        """Update a row. Re-derives downstream if source columns changed."""
        self._ensure_analyzed()
        operation = self._write_planner.update(identity_value, changes)
        if self._is_async_runner():
            return self._drive_async(operation)
        return self._drive_sync(operation)

    def delete(self, identity_value: str) -> None | Awaitable[None]:
        """Delete a row and cascade-delete its children."""
        self._ensure_analyzed()
        if self._is_async_runner():
            return self._delete_async(identity_value)
        return self._write_planner.delete(identity_value)

    async def _delete_async(self, identity_value: str) -> None:
        self._write_planner.delete(identity_value)

    def sync(self, items: list[dict[str, Any]]) -> TableReceipt | Awaitable[TableReceipt]:
        """Reconcile: insert new, update changed, delete missing, skip unchanged."""
        self._ensure_analyzed()
        operation = self._write_planner.sync(items)
        if self._is_async_runner():
            return self._drive_async(operation)
        return self._drive_sync(operation)

    def rederive(
        self,
        column: str,
        *,
        missing_only: bool = False,
    ) -> TableReceipt | Awaitable[TableReceipt]:
        """Re-derive one column, optionally only where its value is missing."""
        self._ensure_analyzed()
        operation = self._write_planner.derive_column(column, backfill=missing_only)
        if self._is_async_runner():
            return self._drive_async(operation)
        return self._drive_sync(operation)
