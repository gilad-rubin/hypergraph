"""HyperTable store extension contract tests."""

from __future__ import annotations

import pytest

from hypergraph.materialization import TableStore, validate_store


def test_table_store_requires_explicit_abstract_methods() -> None:
    """External stores subclass TableStore and fail fast when methods are missing."""

    class IncompleteStore(TableStore):
        pass

    with pytest.raises(TypeError):
        IncompleteStore()


def test_validate_store_rejects_structural_lookalikes() -> None:
    """validate_store requires the concrete TableStore seam, not duck typing."""

    class DuckStore:
        def open(self, spec, children):
            return {}

        def count(self, table_name):
            return 0

        def read_rows(self, table_name, where=None, *, limit=None):
            return []

        def read_one(self, table_name, identity_column, identity_value):
            return None

        def write_rows(self, table_name, rows):
            return None

        def delete_rows(self, table_name, where):
            return 0

        def max_write_gen(self, table_name):
            return 0

        def evolve_schema(self, table_name, new_columns):
            return []

    with pytest.raises(TypeError, match="TableStore"):
        validate_store(DuckStore())


def test_validate_store_opens_minimal_table() -> None:
    """validate_store verifies the store can provision a minimal table."""

    class RecordingStore(TableStore):
        def __init__(self) -> None:
            self.opened: list[tuple[str, list[str]]] = []

        def open(self, spec, children):
            self.opened.append((spec.name, [child.name for child in children]))
            return {spec.name: [column.name for column in spec.columns]}

        def count(self, table_name):
            return 0

        def read_rows(self, table_name, where=None, *, limit=None):
            return []

        def read_one(self, table_name, identity_column, identity_value):
            return None

        def write_rows(self, table_name, rows):
            return None

        def delete_rows(self, table_name, where):
            return 0

        def max_write_gen(self, table_name):
            return 0

        def evolve_schema(self, table_name, new_columns):
            return []

    store = RecordingStore()
    assert validate_store(store) is store
    assert store.opened == [("__validate_store", [])]
