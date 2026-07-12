"""Named-index policy for HyperTable materializations."""

from __future__ import annotations

from typing import Any

from hypergraph.materialization._provenance import Provenance
from hypergraph.materialization._schema import TableSpec, is_internal_column


def _where_predicate(where: Any) -> list[tuple[str, str, Any]]:
    if where is None:
        return []
    if isinstance(where, dict):
        return [(key, "eq", value) for key, value in where.items()]
    return list(where)


class IndexPolicy:
    """Own persisted named-index validation, freshness, and query policy."""

    def __init__(self, store: Any, spec: TableSpec, provenance: Provenance):
        self._store = store
        self._spec = spec
        self._provenance = provenance

    def _resolve_table(self, on: str | None) -> TableSpec:
        if on is None or on == self._spec.name:
            return self._spec
        for child_spec in self._spec.children:
            if child_spec.name == on:
                return child_spec
        known = [self._spec.name, *(child_spec.name for child_spec in self._spec.children)]
        raise ValueError(f"unknown table {on!r} for index; expected one of {known}")

    def _recipe_fingerprint(self, spec: TableSpec, vector: str) -> str | None:
        for column in self._provenance.derived_columns(spec):
            if column.name == vector and column.produced_by is not None:
                return self._provenance.node_recipe(column.produced_by)
        return None

    def _queryable_columns(self, spec: TableSpec) -> set[str]:
        columns = {column.name for column in spec.columns if column.role != "internal"}
        physical = self._store.open(self._spec, self._spec.children).get(spec.name, [])
        columns.update(name for name in physical if not is_internal_column(name))
        return columns

    def _load(self) -> dict[str, dict[str, Any]]:
        manifest = self._store.load_manifest(self._spec.name) or {}
        return dict(manifest.get("indexes", {}))

    def _save(self, indexes: dict[str, dict[str, Any]]) -> None:
        manifest = self._store.load_manifest(self._spec.name) or {}
        manifest["indexes"] = indexes
        self._store.save_manifest(self._spec.name, manifest)

    def create(
        self,
        name: str,
        *,
        on: str | None,
        rows: Any,
        text: str | None,
        vector: str | None,
    ) -> dict[str, Any]:
        if not self._store.supports_manifests():
            raise NotImplementedError(
                f"{type(self._store).__name__} does not implement save_manifest/load_manifest, "
                "so it cannot persist named indexes. Implement both manifest hooks to support "
                "create_index, or use a store that does (e.g. LanceDBStore)."
            )
        spec = self._resolve_table(on)
        if vector is None:
            raise ValueError("create_index requires vector=<column>: v1 indexes are vector-search specs")
        columns = self._queryable_columns(spec)
        for label, column in (("vector", vector), ("text", text)):
            if column is not None and column not in columns:
                raise ValueError(f"{label} column {column!r} does not exist on table {spec.name!r}; known columns: {sorted(columns)}")
        for column, _operator, _value in _where_predicate(rows):
            if column not in columns:
                raise ValueError(f"rows filter column {column!r} does not exist on table {spec.name!r}; known columns: {sorted(columns)}")
        index_spec = {
            "name": name,
            "on": spec.name,
            "rows": rows,
            "text": text,
            "vector": vector,
            "recipe_fingerprint": self._recipe_fingerprint(spec, vector),
        }
        indexes = self._load()
        indexes[name] = index_spec
        self._save(indexes)
        return dict(index_spec)

    def list(self) -> list[dict[str, Any]]:
        specs = []
        for index_spec in self._load().values():
            spec = self._resolve_table(index_spec.get("on"))
            current = self._recipe_fingerprint(spec, index_spec["vector"])
            specs.append({**index_spec, "current": current == index_spec.get("recipe_fingerprint")})
        return specs

    def drop(self, name: str) -> None:
        indexes = self._load()
        if name not in indexes:
            raise KeyError(f"no index named {name!r}")
        del indexes[name]
        self._save(indexes)

    def search(
        self,
        query_vector: list[float],
        *,
        index: str,
        limit: int,
        where: Any,
    ) -> list[dict[str, Any]]:
        indexes = self._load()
        if index not in indexes:
            raise KeyError(f"no index named {index!r}; known indexes: {sorted(indexes)}")
        index_spec = indexes[index]
        combined_where = [*_where_predicate(index_spec.get("rows")), *_where_predicate(where)]
        return self._store.search(
            index_spec["on"],
            query_vector=list(query_vector),
            vector_column=index_spec["vector"],
            where=combined_where or None,
            limit=limit,
        )
