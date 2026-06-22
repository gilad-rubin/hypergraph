"""LanceDB store backend for DerivedTable."""

from __future__ import annotations

import dataclasses
from typing import Any

import lancedb
import pyarrow as pa


def _python_type_to_arrow(tp: type) -> pa.DataType:
    """Map Python type annotations to PyArrow types."""
    if tp is str:
        return pa.utf8()
    if tp is int:
        return pa.int64()
    if tp is float:
        return pa.float64()
    if tp is bool:
        return pa.bool_()
    if hasattr(tp, "__origin__"):
        origin = tp.__origin__
        if origin is list:
            args = tp.__args__
            if args and args[0] is float:
                return pa.list_(pa.float64())
            if args and args[0] is str:
                return pa.list_(pa.utf8())
            if args and args[0] is int:
                return pa.list_(pa.int64())
            return pa.list_(pa.utf8())
    return pa.utf8()


def _get_field_type(cls: type, field_name: str) -> type:
    """Get the raw type for a dataclass field, stripping Annotated."""
    import typing

    hints = typing.get_type_hints(cls)
    return hints.get(field_name, str)


class LanceStore:
    """Manages LanceDB tables for DerivedTable instances."""

    def __init__(self, path: str):
        self.path = path
        self.db = lancedb.connect(path)
        self._tables: dict[str, Any] = {}
        self._metadata: dict[str, dict] = {}

    def _table_name(self, output_cls: type) -> str:
        return output_cls.__name__

    def _build_schema(
        self,
        output_cls: type,
        source_cls: type | None,
        is_root: bool,
    ) -> pa.Schema:
        """Build PyArrow schema for the output table."""
        fields = []

        for f in dataclasses.fields(output_cls):
            tp = _get_field_type(output_cls, f.name)
            fields.append(pa.field(f.name, _python_type_to_arrow(tp)))

        fields.append(pa.field("_source_id", pa.utf8()))
        fields.append(pa.field("_content_key", pa.utf8()))
        fields.append(pa.field("_error", pa.bool_()))
        fields.append(pa.field("_error_type", pa.utf8()))
        fields.append(pa.field("_error_msg", pa.utf8()))
        fields.append(pa.field("_version", pa.int64()))

        if is_root and source_cls is not None:
            for f in dataclasses.fields(source_cls):
                tp = _get_field_type(source_cls, f.name)
                fields.append(pa.field(f"_src_{f.name}", _python_type_to_arrow(tp)))

        return pa.schema(fields)

    def ensure_table(
        self,
        output_cls: type,
        source_cls: type | None,
        is_root: bool,
    ) -> Any:
        """Get or create a LanceDB table for the output type."""
        name = self._table_name(output_cls)
        if name in self._tables:
            return self._tables[name]

        try:
            tbl = self.db.open_table(name)
            self._tables[name] = tbl
            return tbl
        except Exception:
            schema = self._build_schema(output_cls, source_cls, is_root)
            tbl = self.db.create_table(name, schema=schema)
            self._tables[name] = tbl
            return tbl

    def get_table(self, output_cls: type) -> Any | None:
        name = self._table_name(output_cls)
        try:
            tbl = self.db.open_table(name)
            self._tables[name] = tbl
            return tbl
        except Exception:
            return None

    def drop_table(self, output_cls: type):
        name = self._table_name(output_cls)
        try:
            self.db.drop_table(name)
        except Exception:
            pass
        self._tables.pop(name, None)

    def register_dependent(self, parent_output: type, child_output: type):
        """Store that child_output depends on parent_output."""
        parent_name = self._table_name(parent_output)
        if parent_name not in self._metadata:
            self._metadata[parent_name] = {"dependents": []}
        child_name = self._table_name(child_output)
        if child_name not in self._metadata[parent_name].get("dependents", []):
            self._metadata[parent_name].setdefault("dependents", []).append(child_name)

    def deregister_dependent(self, parent_output: type, child_output: type):
        parent_name = self._table_name(parent_output)
        child_name = self._table_name(child_output)
        if parent_name in self._metadata:
            deps = self._metadata[parent_name].get("dependents", [])
            if child_name in deps:
                deps.remove(child_name)

    def get_dependents(self, output_cls: type) -> list[str]:
        name = self._table_name(output_cls)
        return self._metadata.get(name, {}).get("dependents", [])


_store_cache: dict[str, LanceStore] = {}


def get_store(path: str) -> LanceStore:
    """Get or create a LanceStore for the given path."""
    if path not in _store_cache:
        _store_cache[path] = LanceStore(path)
    return _store_cache[path]


def clear_store_cache():
    _store_cache.clear()
