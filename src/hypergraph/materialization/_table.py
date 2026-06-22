"""DerivedTable — declarative incremental materialization."""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import threading
from typing import Any, TypeVar

import pyarrow as pa

from hypergraph import FunctionNode, Graph, RunStatus
from hypergraph.materialization._keys import (
    compute_content_key,
    compute_definition_hash,
    compute_graph_definition_hash,
    compute_schema_fingerprint,
    extract_markers_lenient,
)
from hypergraph.materialization._sink import LanceSink
from hypergraph.materialization._store import LanceStore, get_store
from hypergraph.materialization._types import (
    ChainedTableError,
    DerivationError,
    ErrorRow,
    SyncResult,
)
from hypergraph.runners import SyncRunner

T = TypeVar("T")


def _escape(val: Any) -> str:
    return str(val).replace("'", "''")


class _SnapshotView:
    """Read-only snapshot of a DerivedTable at a specific version."""

    def __init__(self, table: DerivedTable, version: int):
        self._table = table
        self._version = version

    def _get_snapshot_dataset(self):
        import lance as lmod

        tbl = self._table._get_lance_table()
        if tbl is None:
            return None
        uri = tbl.to_lance().uri
        return lmod.dataset(uri, version=self._version)

    def count(self, include_errors: bool = False) -> int:
        ds = self._get_snapshot_dataset()
        if ds is None:
            return 0
        try:
            filt = None if include_errors else "_error = false"
            return ds.count_rows(filter=filt)
        except Exception:
            return 0

    def get(self, **kwargs: Any) -> Any | None:
        ds = self._get_snapshot_dataset()
        if ds is None:
            return None
        try:
            predicates = [f"{k} = '{_escape(v)}'" for k, v in kwargs.items()]
            predicates.append("_error = false")
            batch = ds.to_table(filter=" AND ".join(predicates))
            if len(batch) == 0:
                return None
            return self._table._arrow_row_to_output(batch, 0)
        except Exception:
            return None


class DerivedTable:
    """Declarative incremental materialization backed by LanceDB."""

    def __init__(
        self,
        source: type | DerivedTable,
        output: type,
        derive: Any,
        components: dict[str, Any] | None = None,
        store: str | LanceStore | None = None,
        runner: Any = None,
    ):
        components = components or {}

        for name, comp in components.items():
            if not hasattr(comp, "_config"):
                raise TypeError(f"Component '{name}' ({type(comp).__name__}) must implement _config()")

        if isinstance(source, DerivedTable):
            self._is_root = False
            self._source_cls = source._output_cls
            self._parent = source
            ancestor = source
            while ancestor is not None:
                if ancestor._output_cls is output:
                    raise ValueError(f"Circular dependency: {output.__name__} already appears in the chain via {ancestor._output_cls.__name__}")
                ancestor = ancestor._parent
        else:
            self._is_root = True
            self._source_cls = source
            self._parent = None

        self._output_cls = output
        self._derive = derive
        self._components = components
        self._markers = extract_markers_lenient(self._source_cls)
        self._schema_fingerprint = compute_schema_fingerprint(output)
        self._definition_hash = compute_graph_definition_hash(derive) if isinstance(derive, Graph) else compute_definition_hash(derive)
        self._dependents: list[DerivedTable] = []
        self._lock = threading.RLock()

        if isinstance(store, str):
            self._store_path = store
            self._store = get_store(store)
        elif isinstance(store, LanceStore):
            self._store_path = store.path
            self._store = store
        else:
            raise ValueError("store must be a path string or LanceStore instance")

        self._store.ensure_table(output, self._source_cls, self._is_root)

        # Engine: set on the root, inherited by chained tables, so a chain shares
        # one engine and execution color (ADR 0001).
        if self._is_root:
            self._runner = runner if runner is not None else SyncRunner()
        else:
            self._runner = self._parent._runner

        caps = self._runner.capabilities
        if not caps.supports_streaming or caps.returns_coroutine:
            raise TypeError(
                f"{type(self._runner).__name__} is not a synchronous streaming runner; "
                f"DerivedTable needs a sync streaming runner such as SyncRunner "
                f"(async runners will be supported by AsyncDerivedTable)."
            )

        # Compile the derive + build its sink BEFORE linking to the parent, so a
        # bad derive raises without leaving a half-initialized child registered
        # for cascade.
        self._build_runtime()

        if self._parent is not None:
            self._parent._dependents.append(self)
            self._store.register_dependent(self._parent._output_cls, output)

    def _compile_derive(self) -> tuple[Any, str, str, list[str]]:
        """Turn the derive into a bound graph plus its source input port and row
        output port. A plain function becomes a one-node graph; a Graph derive is
        used directly. Components are bound; the single remaining input is the
        source port to map over.
        """
        output_port = "derived_output"
        if isinstance(self._derive, Graph):
            graph = self._derive.bind(**self._components) if self._components else self._derive
            source_param = self._detect_graph_source_param(graph)
            output_port = self._detect_graph_output_port(graph)
            return graph, source_param, output_port, list(graph.outputs)

        sig = inspect.signature(self._derive)
        required = [name for name, p in sig.parameters.items() if name not in self._components and p.default is inspect.Parameter.empty]
        if len(required) != 1:
            raise ValueError(
                f"derive function must take exactly one required source parameter besides its "
                f"components; got required params {required} (components: {sorted(self._components)})"
            )
        node = FunctionNode(self._derive, name="derive", output_name=output_port)
        graph = Graph([node], name=f"{self._output_cls.__name__.lower()}_derive")
        graph = graph.bind(**self._components) if self._components else graph
        return graph, required[0], output_port, [output_port]

    def _detect_graph_source_param(self, graph: Any) -> str:
        # After binding components, they live in InputSpec.bound; the remaining
        # required input is the source to map over.
        candidates = [name for name in graph.inputs.required if name not in self._components]
        if len(candidates) != 1:
            raise ValueError(f"derive graph must have exactly one unbound source input; got {candidates}")
        return candidates[0]

    def _detect_graph_output_port(self, graph: Any) -> str:
        # Respect an explicit graph.select(...); otherwise use the sole output.
        names = list(graph.selected) if graph.selected else list(graph.outputs)
        if len(names) == 1:
            return names[0]
        if not names:
            raise ValueError("derive graph produces no outputs")
        raise ValueError(
            f"derive graph produces multiple outputs {sorted(names)}; narrow it to the row output with graph.select(...) before using it as a derive."
        )

    def _build_runtime(self) -> None:
        """(Re)compile the derive into a graph + source/output ports and build its
        sink. Called at construction and whenever components change, so a swapped
        component is actually run, not just reflected in the content key."""
        self._graph, self._source_param, self._output_port, output_names = self._compile_derive()
        self._sink = LanceSink(
            self._store,
            self._output_cls,
            self._source_cls,
            self._markers,
            self._is_root,
            writes=self._output_port,
        )
        self._sink.validate_against(output_names)

    @property
    def is_root(self) -> bool:
        return self._is_root

    @property
    def version(self) -> int:
        tbl = self._get_lance_table()
        if tbl is None:
            return 0
        try:
            return tbl.version
        except Exception:
            return 0

    def _get_lance_table(self):
        return self._store.get_table(self._output_cls)

    def _component_configs(self) -> dict[str, Any]:
        return {name: comp._config() for name, comp in self._components.items()}

    def _compute_key(self, item: Any) -> str:
        return compute_content_key(
            item,
            self._component_configs(),
            self._definition_hash,
            self._schema_fingerprint,
        )

    def _get_identity_values(self, item: Any) -> dict[str, Any]:
        return {f: getattr(item, f) for f in self._markers.identity_fields}

    def _get_identity_str(self, item: Any) -> str:
        vals = self._get_identity_values(item)
        return ":".join(str(vals[k]) for k in sorted(vals))

    def _find_existing_row(self, identity_values: dict[str, Any]) -> dict | None:
        """Find an existing row by identity fields. Returns dict or None."""
        tbl = self._get_lance_table()
        if tbl is None:
            return None
        predicates = [f"{k} = '{_escape(v)}'" for k, v in identity_values.items()]
        try:
            rows = tbl.search().where(" AND ".join(predicates)).limit(1).to_list()
            if not rows:
                return None
            return rows[0]
        except Exception:
            return None

    def _arrow_row_to_output(self, table: pa.Table, idx: int) -> Any:
        """Convert an Arrow table row to an output dataclass."""
        row = table.to_pydict()
        field_names = [f.name for f in dataclasses.fields(self._output_cls)]
        kwargs = {}
        for name in field_names:
            val = row[name][idx]
            kwargs[name] = val
        return self._output_cls(**kwargs)

    def _dict_row_to_output(self, row: dict) -> Any:
        """Convert a dict row to an output dataclass."""
        field_names = [f.name for f in dataclasses.fields(self._output_cls)]
        kwargs = {}
        for name in field_names:
            kwargs[name] = row.get(name)
        return self._output_cls(**kwargs)

    def _delete_by_identity(self, identity_values: dict[str, Any]):
        tbl = self._get_lance_table()
        if tbl is None:
            return
        predicates = [f"{k} = '{_escape(v)}'" for k, v in identity_values.items()]
        with contextlib.suppress(Exception):
            tbl.delete(" AND ".join(predicates))

    def _delete_by_source_id(self, source_id: str):
        tbl = self._get_lance_table()
        if tbl is None:
            return
        with contextlib.suppress(Exception):
            tbl.delete(f"_source_id = '{_escape(source_id)}'")

    def _get_child_identity_strings(self, source_id: str) -> list[str]:
        """Get identity strings of rows that have _source_id == source_id."""
        tbl = self._get_lance_table()
        if tbl is None:
            return []
        try:
            rows = tbl.search().where(f"_source_id = '{_escape(source_id)}'").to_list()
            ids = []
            for row in rows:
                id_vals = {f: row[f] for f in self._markers.identity_fields if f in row}
                ids.append(":".join(str(id_vals[k]) for k in sorted(id_vals)))
            return ids
        except Exception:
            return []

    def _cascade_delete(self, source_ids: list[str]):
        for dep in self._dependents:
            for sid in source_ids:
                child_ids = dep._get_child_identity_strings(sid)
                dep._delete_by_source_id(sid)
                if child_ids:
                    dep._cascade_delete(child_ids)

    def _cascade_insert(self, source_results: list[Any], on_error: str = "raise"):
        for dep in self._dependents:
            dep._derive_and_store(source_results, on_error=on_error)

    def _derive_and_store(
        self,
        source_items: list[Any],
        on_error: str = "raise",
    ) -> tuple[list[dict], list[dict]]:
        """Derive outputs from source items and store them.

        Write ordering: write-new-then-delete-old per item.
        A crash between write and delete leaves recoverable duplicates,
        not data loss.
        """
        # De-dup within the batch by identity (last occurrence wins), matching the
        # old sequential upsert: a repeated identity in one call collapses to one
        # row, not two.
        by_identity: dict[tuple, Any] = {}
        for item in source_items:
            id_key = tuple(sorted(self._get_identity_values(item).items()))
            by_identity[id_key] = item

        # Incrementality stays here: only compute items whose content changed or
        # previously errored.
        pending: list[tuple] = []
        for item in by_identity.values():
            identity = self._get_identity_values(item)
            content_key = self._compute_key(item)
            source_id = self._get_identity_str(item)
            existing = self._find_existing_row(identity)
            if existing and existing.get("_content_key") == content_key and not existing.get("_error", False):
                continue
            pending.append((item, identity, content_key, source_id, existing))

        succeeded: list[dict] = []
        failed: list[dict] = []
        cascade_results: list[Any] = []

        if pending:
            self._sink.start()
            items_only = [p[0] for p in pending]
            # The runner streams the derive over the pending items; the sink
            # persists each result write-new-then-delete-old as it arrives.
            for index, run_result in self._runner.map_iter(
                self._graph,
                {self._source_param: items_only},
                map_over=self._source_param,
                error_handling="continue",
            ):
                item, identity, content_key, source_id, existing = pending[index]

                if run_result.status == RunStatus.FAILED:
                    if on_error == "ignore":
                        self._sink.write_error(source_item=item, content_key=content_key, error=run_result.error)
                        if existing:
                            self._cascade_delete([source_id])
                            self._sink.delete_superseded(source_id, content_key)
                        failed.append(identity)
                    else:
                        failed.append(identity)
                    continue

                outputs = self._sink.write(run_result, source_item=item, content_key=content_key)
                if existing:
                    self._cascade_delete([source_id])
                    self._sink.delete_superseded(source_id, content_key)
                cascade_results.extend(outputs)
                succeeded.append(identity)
            self._sink.finalize()

        if cascade_results:
            self._cascade_insert(cascade_results, on_error=on_error)

        return succeeded, failed

    # -----------------------------------------------------------------------
    # Public mutation API
    # -----------------------------------------------------------------------

    def insert(self, items: list, on_error: str = "raise"):
        if not self._is_root:
            raise ChainedTableError("insert")
        with self._lock:
            succeeded, failed = self._derive_and_store(items, on_error=on_error)
            if failed and on_error == "raise":
                raise DerivationError(succeeded=succeeded, failed=failed)

    def update(self, **kwargs: Any):
        if not self._is_root:
            raise ChainedTableError("update")

        identity_kwargs = {}
        override_kwargs = {}
        for k, v in kwargs.items():
            if k in self._markers.identity_fields:
                identity_kwargs[k] = v
            else:
                override_kwargs[k] = v

        if not identity_kwargs:
            raise ValueError("Must provide at least one identity field")

        with self._lock:
            existing = self._find_existing_row(identity_kwargs)
            if existing is None:
                raise ValueError(f"No row found for identity {identity_kwargs}")

            src_fields = {}
            for f in dataclasses.fields(self._source_cls):
                src_key = f"_src_{f.name}"
                if src_key in existing:
                    src_fields[f.name] = existing[src_key]
            src_fields.update(override_kwargs)
            src_fields.update(identity_kwargs)

            new_item = self._source_cls(**src_fields)
            self._derive_and_store([new_item])

    def delete(self, **kwargs: Any):
        if not self._is_root:
            raise ChainedTableError("delete")
        with self._lock:
            if len(kwargs) == 1:
                field, values = next(iter(kwargs.items()))
                if not isinstance(values, list):
                    values = [values]
                for val in values:
                    identity = {field: val}
                    source_id = ":".join(str(identity[k]) for k in sorted(identity))
                    self._cascade_delete([source_id])
                    self._delete_by_identity(identity)
            else:
                source_id = ":".join(str(kwargs[k]) for k in sorted(kwargs))
                self._cascade_delete([source_id])
                self._delete_by_identity(kwargs)

    def sync(self, items: list, on_error: str = "ignore") -> SyncResult:
        if not self._is_root:
            raise ChainedTableError("sync")

        with self._lock:
            item_map = {}
            for item in items:
                identity = self._get_identity_values(item)
                id_key = tuple(sorted(identity.items()))
                item_map[id_key] = item

            stored_ids = set()
            tbl = self._get_lance_table()
            if tbl is not None:
                try:
                    rows = tbl.search().where("_error = false OR _error = true").to_list()
                    for row in rows:
                        id_vals = {f: row[f] for f in self._markers.identity_fields}
                        id_key = tuple(sorted(id_vals.items()))
                        stored_ids.add(id_key)
                except Exception:
                    pass

            incoming_ids = set(item_map.keys())
            to_delete_ids = stored_ids - incoming_ids

            to_process = []
            skipped = 0
            update_keys: set[tuple] = set()
            insert_keys: set[tuple] = set()

            for id_key in incoming_ids:
                item = item_map[id_key]
                identity = dict(id_key)
                content_key = self._compute_key(item)

                existing = self._find_existing_row(identity)
                if existing:
                    if existing.get("_content_key") == content_key and not existing.get("_error", False):
                        skipped += 1
                    else:
                        to_process.append(item)
                        update_keys.add(id_key)
                else:
                    to_process.append(item)
                    insert_keys.add(id_key)

            for id_key in to_delete_ids:
                identity = dict(id_key)
                source_id = ":".join(str(identity[k]) for k in sorted(identity))
                self._cascade_delete([source_id])
                self._delete_by_identity(identity)

            errored = 0
            if to_process:
                _, fail_list = self._derive_and_store(to_process, on_error=on_error)
                errored = len(fail_list)
                for f in fail_list:
                    fk = tuple(sorted(f.items()))
                    if fk in update_keys:
                        update_keys.discard(fk)
                    elif fk in insert_keys:
                        insert_keys.discard(fk)

            return SyncResult(
                inserted=len(insert_keys),
                updated=len(update_keys),
                deleted=len(to_delete_ids),
                skipped=skipped,
                errored=errored,
            )

    def recompute(
        self,
        components: dict[str, Any] | None = None,
        errors_only: bool = False,
    ):
        if components:
            for name, comp in components.items():
                self._components[name] = comp
            # Rebind the graph so the swapped component is actually run, not just
            # reflected in the content key.
            self._build_runtime()

        with self._lock:
            tbl = self._get_lance_table()
            if tbl is None:
                return

            try:
                if errors_only:
                    rows = tbl.search().where("_error = true").to_list()
                else:
                    rows = tbl.search().where("_error = false OR _error = true").to_list()
            except Exception:
                return

            items_to_process = []
            source_ids_to_clean = []

            for row in rows:
                if self._is_root:
                    src_fields = {}
                    for f in dataclasses.fields(self._source_cls):
                        src_key = f"_src_{f.name}"
                        if src_key in row:
                            src_fields[f.name] = row[src_key]
                    if src_fields:
                        source_item = self._source_cls(**src_fields)
                        identity = self._get_identity_values(source_item)
                        self._delete_by_identity(identity)
                        items_to_process.append(source_item)
                elif self._parent is not None:
                    source_id = row.get("_source_id", "")
                    if not source_id:
                        continue
                    id_fields = self._markers.identity_fields
                    if len(id_fields) == 1:
                        parent_item = self._parent.get(**{id_fields[0]: source_id})
                    else:
                        parts = source_id.split(":")
                        if len(parts) == len(id_fields):
                            parent_item = self._parent.get(**dict(zip(sorted(id_fields), parts, strict=False)))
                        else:
                            continue
                    if parent_item is not None:
                        self._delete_by_source_id(source_id)
                        items_to_process.append(parent_item)
                    else:
                        source_ids_to_clean.append(source_id)

            for sid in source_ids_to_clean:
                self._delete_by_source_id(sid)

            if items_to_process:
                self._derive_and_store(items_to_process, on_error="ignore")

    # -----------------------------------------------------------------------
    # Query API
    # -----------------------------------------------------------------------

    def get(self, **kwargs: Any) -> Any | None:
        tbl = self._get_lance_table()
        if tbl is None:
            return None
        predicates = [f"{k} = '{_escape(v)}'" for k, v in kwargs.items()]
        predicates.append("_error = false")
        try:
            rows = tbl.search().where(" AND ".join(predicates)).limit(1).to_list()
            if not rows:
                return None
            return self._dict_row_to_output(rows[0])
        except Exception:
            return None

    def filter(self, **kwargs: Any) -> list:
        tbl = self._get_lance_table()
        if tbl is None:
            return []
        predicates = ["_error = false"]
        for k, v in kwargs.items():
            if "__" in k:
                field, op = k.rsplit("__", 1)
                op_map = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}
                if op == "in":
                    vals = ", ".join(f"'{_escape(x)}'" for x in v)
                    predicates.append(f"{field} IN ({vals})")
                elif op in op_map:
                    predicates.append(f"{field} {op_map[op]} {_escape(v)}")
            else:
                predicates.append(f"{k} = '{_escape(v)}'")
        try:
            rows = tbl.search().where(" AND ".join(predicates)).to_list()
            return [self._dict_row_to_output(r) for r in rows]
        except Exception:
            return []

    def search(self, query_vector: list[float], limit: int = 10) -> list:
        tbl = self._get_lance_table()
        if tbl is None:
            return []
        try:
            rows = tbl.search(query_vector).where("_error = false").limit(limit).to_list()
            return [self._dict_row_to_output(r) for r in rows]
        except Exception:
            return []

    def errors(self) -> list[ErrorRow]:
        tbl = self._get_lance_table()
        if tbl is None:
            return []
        try:
            rows = tbl.search().where("_error = true").to_list()
            errors = []
            for row in rows:
                identity = {}
                if self._is_root:
                    for f in self._markers.identity_fields:
                        src_key = f"_src_{f}"
                        if src_key in row:
                            identity[f] = row[src_key]
                        elif f in row:
                            identity[f] = row[f]
                else:
                    identity["_source_id"] = row.get("_source_id", "")
                errors.append(
                    ErrorRow(
                        identity=identity,
                        error_type=row.get("_error_type", ""),
                        error_msg=row.get("_error_msg", ""),
                    )
                )
            return errors
        except Exception:
            return []

    def count(self, include_errors: bool = False) -> int:
        tbl = self._get_lance_table()
        if tbl is None:
            return 0
        try:
            if include_errors:
                return tbl.count_rows()
            return tbl.count_rows(filter="_error = false")
        except Exception:
            return 0

    # -----------------------------------------------------------------------
    # Versioning
    # -----------------------------------------------------------------------

    def at(self, version: int) -> _SnapshotView:
        return _SnapshotView(self, version)

    def revert(self):
        tbl = self._get_lance_table()
        if tbl is None:
            return
        try:
            current = tbl.version
            if current > 1:
                tbl.restore(current - 1)
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def drop(self):
        if self._parent:
            self._parent._dependents.remove(self)
            self._store.deregister_dependent(self._parent._output_cls, self._output_cls)
        self._store.drop_table(self._output_cls)
