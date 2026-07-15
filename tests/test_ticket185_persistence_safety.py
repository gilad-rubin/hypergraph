"""Foreman-owned falsifiers for ticket #185 persistence hardening."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from hypergraph import Graph, node
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.materialization._schema import ColumnSpec, TableSpec


def _store_spec() -> TableSpec:
    return TableSpec(
        name="t",
        identity="cid",
        columns=[
            ColumnSpec("cid", role="identity", arrow_type=pa.utf8()),
            ColumnSpec("n", role="source", arrow_type=pa.int64()),
            ColumnSpec("content", role="source", content_key=True, arrow_type=pa.large_binary()),
            ColumnSpec("_write_gen", role="internal", arrow_type=pa.int64()),
            ColumnSpec("_status", role="internal", arrow_type=pa.utf8()),
            ColumnSpec("_row_fingerprint", role="internal", arrow_type=pa.utf8()),
            ColumnSpec("_error", role="internal", arrow_type=pa.utf8()),
        ],
    )


def _row(cid: str, n: Any, *, write_gen: int = 1, content: bytes = b"blob") -> dict[str, Any]:
    return {
        "cid": cid,
        "n": n,
        "content": content,
        "_write_gen": write_gen,
        "_status": "complete",
        "_row_fingerprint": "fp",
        "_error": None,
    }


class _TableProxy:
    def __init__(self, table: Any) -> None:
        self._table = table
        self.add_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._table, name)

    def add(self, data: Any) -> Any:
        self.add_calls += 1
        return self._table.add(data)


class _DatasetProjectionProxy:
    def __init__(self, dataset: Any, projections: list[tuple[str, ...]]) -> None:
        self._dataset = dataset
        self._projections = projections

    def to_table(self, *, columns: list[str]) -> pa.Table:
        self._projections.append(tuple(columns))
        return self._dataset.to_table(columns=columns)


class _ProjectionOnlyTable:
    def __init__(self, table: Any) -> None:
        self._table = table
        self.projections: list[tuple[str, ...]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._table, name)

    def to_arrow(self) -> pa.Table:
        raise AssertionError("metadata-only operations must not materialize full rows")

    def to_lance(self) -> _DatasetProjectionProxy:
        return _DatasetProjectionProxy(self._table.to_lance(), self.projections)


def test_write_rows_uses_one_lancedb_add_for_the_whole_batch(tmp_path) -> None:
    store = LanceDBStore(str(tmp_path / "one-add"))
    store.open(_store_spec(), [])
    proxy = _TableProxy(store._tables["t"])
    store._tables["t"] = proxy

    store.write_rows("t", [_row("a", 1), _row("b", 2)])

    assert proxy.add_calls == 1
    assert store.count("t") == 2


def test_invalid_lancedb_batch_leaves_no_committed_prefix(tmp_path) -> None:
    path = str(tmp_path / "atomic-batch")
    store = LanceDBStore(path)
    spec = _store_spec()
    store.open(spec, [])
    rows = [_row("good", 1), _row("bad", "not-an-int"), _row("later", 3)]
    for row in rows:
        del row["content"]

    with pytest.raises((pa.ArrowInvalid, pa.ArrowTypeError)):
        store.write_rows("t", rows)

    fresh = LanceDBStore(path)
    fresh.open(spec, [])
    assert fresh.read_rows("t") == []
    assert ["content" in row for row in rows] == [True, True, False]


def test_failed_lancedb_add_leaves_no_committed_rows(tmp_path, monkeypatch) -> None:
    path = str(tmp_path / "failed-add")
    store = LanceDBStore(path)
    spec = _store_spec()
    store.open(spec, [])

    table_type = type(store._tables["t"])

    def fail_add(table, data, *args, **kwargs):
        raise RuntimeError("simulated add failure")

    monkeypatch.setattr(table_type, "add", fail_add)
    with pytest.raises(RuntimeError, match="simulated add failure"):
        store.write_rows("t", [_row("a", 1), _row("b", 2)])

    fresh = LanceDBStore(path)
    fresh.open(spec, [])
    assert fresh.read_rows("t") == []


def test_write_rows_preserves_missing_field_null_fill_on_input_rows(tmp_path) -> None:
    store = LanceDBStore(str(tmp_path / "null-fill"))
    store.open(_store_spec(), [])
    row = _row("a", 1)
    del row["content"]

    store.write_rows("t", [row])

    assert row["content"] is None


def test_metadata_operations_physically_project_only_consumed_columns(tmp_path) -> None:
    store = LanceDBStore(str(tmp_path / "projection"))
    store.open(_store_spec(), [])
    store.write_rows("t", [_row("a", 1, content=b"large" * 1000), _row("b", 2, write_gen=4)])
    proxy = _ProjectionOnlyTable(store._tables["t"])
    store._tables["t"] = proxy

    assert store.max_write_gen("t") == 4
    assert store.delete_rows("t", [("cid", "eq", "a")]) == 1
    assert proxy.projections == [("_write_gen",), ("cid",)]
    assert store.read_rows("t", columns=["cid"]) == [{"cid": "b"}]


def test_evolve_schema_never_enters_a_drop_table_data_loss_window(tmp_path, monkeypatch) -> None:
    path = str(tmp_path / "drop-free-evolve")
    store = LanceDBStore(path)
    spec = _store_spec()
    store.open(spec, [])
    original = _row("a", 1, content=b"irreplaceable")
    store.write_rows("t", [original])
    before_version = store._tables["t"].version

    connection_type = type(store._db)
    original_drop = connection_type.drop_table

    def destructive_drop_then_fail(connection, table_name, *args, **kwargs):
        original_drop(connection, table_name, *args, **kwargs)
        raise RuntimeError("simulated crash after destructive drop")

    monkeypatch.setattr(connection_type, "drop_table", destructive_drop_then_fail)

    columns = store.evolve_schema("t", {"tag": pa.utf8()})

    assert "tag" in columns
    assert store._tables["t"].version == before_version + 1
    fresh = LanceDBStore(path)
    fresh.open(spec, [])
    assert fresh.read_rows("t", columns=["cid", "content", "tag"]) == [{"cid": "a", "content": b"irreplaceable", "tag": None}]
    fresh_version = fresh._tables["t"].version
    assert "tag" in fresh.evolve_schema("t", {"tag": pa.utf8()})
    assert fresh._tables["t"].version == fresh_version


@node(output_name="derived")
def _derive(source: str) -> str:
    return source.upper()


def test_hypertable_owns_map_over_state_before_lazy_analysis(tmp_path) -> None:
    graph = Graph([_derive])
    table = graph.as_table(identity="cid", store=LanceDBStore(str(tmp_path / "constructor-state")))

    assert table._map_over_nodes == []
    assert table.graph is graph
