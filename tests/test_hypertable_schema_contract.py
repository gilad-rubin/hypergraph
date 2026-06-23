"""Schema contract passed from HyperTable to TableStore implementations."""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from hypergraph import node
from hypergraph.materialization import HyperTable, TableStore


class RecordingStore(TableStore):
    def __init__(self) -> None:
        self.opened_spec = None

    def open(self, spec, children):
        self.opened_spec = spec
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


@node(output_name="clean_text")
def clean(text: str) -> str:
    return text.strip().lower()


@node(output_name="word_count")
def count_words(clean_text: str) -> int:
    return len(clean_text.split())


@node(output_name="embedding")
def embed(clean_text: str) -> list[float]:
    return [1.0, 0.0]


def test_column_specs_include_arrow_types_for_store_open() -> None:
    """Stores receive backend-agnostic Arrow types on every column spec."""

    store = RecordingStore()
    table = HyperTable([clean, count_words, embed], identity="doc_id", store=store)

    assert table.count() == 0

    columns: dict[str, Any] = {column.name: column for column in store.opened_spec.columns}
    assert columns["doc_id"].arrow_type == pa.utf8()
    assert columns["text"].arrow_type == pa.utf8()
    assert columns["clean_text"].arrow_type == pa.utf8()
    assert columns["word_count"].arrow_type == pa.int64()
    assert columns["embedding"].arrow_type == pa.list_(pa.float32())
    assert columns["_write_gen"].arrow_type == pa.int64()
