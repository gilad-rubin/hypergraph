"""Foreman-owned falsifiers for ticket #206: failure-safe vector-schema specialization.

``LanceDBStore`` specializes a plain ``list`` vector column to
``fixed_size_list<float32, dim>`` on the first write that carries a real vector.
These tests pin the failure-safety contract of that specialization: no injected
or real failure at any stage may destroy or hide previously committed rows, and
every failure surfaces loudly while a FRESH store handle over the same directory
still reads the committed table completely (row counts + blob byte equality).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.materialization._schema import ColumnSpec, TableSpec

_BLOB_A = bytes([0x00, 0x01, 0x02, 0x80, 0xFF, 0xFE]) + b"\x00PDF\x00" + bytes(range(256))
_BLOB_B = _BLOB_A[::-1]


def _vec_spec() -> TableSpec:
    return TableSpec(
        name="t",
        identity="cid",
        columns=[
            ColumnSpec("cid", role="identity", arrow_type=pa.utf8()),
            ColumnSpec("content", role="source", content_key=True, arrow_type=pa.large_binary()),
            ColumnSpec("vec", role="derived", arrow_type=pa.list_(pa.float64())),
            ColumnSpec("_row_fingerprint", role="internal", arrow_type=pa.utf8()),
            ColumnSpec("_write_gen", role="internal", arrow_type=pa.int64()),
            ColumnSpec("_status", role="internal", arrow_type=pa.utf8()),
            ColumnSpec("_error", role="internal", arrow_type=pa.utf8()),
        ],
    )


def _row(cid: str, *, vec: list[float] | None, content: bytes, write_gen: int = 1) -> dict[str, Any]:
    return {
        "cid": cid,
        "content": content,
        "vec": vec,
        "_row_fingerprint": f"fp-{cid}",
        "_write_gen": write_gen,
        "_status": "complete",
        "_error": None,
    }


def _vec2_spec() -> TableSpec:
    """Two independent vector columns — exercises multi-column alterations."""
    return TableSpec(
        name="t",
        identity="cid",
        columns=[
            ColumnSpec("cid", role="identity", arrow_type=pa.utf8()),
            ColumnSpec("content", role="source", content_key=True, arrow_type=pa.large_binary()),
            ColumnSpec("vec_a", role="derived", arrow_type=pa.list_(pa.float64())),
            ColumnSpec("vec_b", role="derived", arrow_type=pa.list_(pa.float64())),
            ColumnSpec("_row_fingerprint", role="internal", arrow_type=pa.utf8()),
            ColumnSpec("_write_gen", role="internal", arrow_type=pa.int64()),
            ColumnSpec("_status", role="internal", arrow_type=pa.utf8()),
            ColumnSpec("_error", role="internal", arrow_type=pa.utf8()),
        ],
    )


def _row2(cid: str, *, vec_a: list[float] | None, vec_b: list[float] | None, content: bytes, write_gen: int = 1) -> dict[str, Any]:
    return {
        "cid": cid,
        "content": content,
        "vec_a": vec_a,
        "vec_b": vec_b,
        "_row_fingerprint": f"fp-{cid}",
        "_write_gen": write_gen,
        "_status": "complete",
        "_error": None,
    }


def _seed_committed_rows(path: str, rows: list[dict[str, Any]], spec: TableSpec | None = None) -> LanceDBStore:
    """Commit rows through their own handle, leaving vector columns un-specialized.

    The first row must carry ``None`` for every vector column so dimension
    detection (which samples the first row of the first write) finds nothing to
    specialize — the exact state of a real store whose rows were written before
    any embedding existed. Returns the seeding handle so a test can commit a
    further batch (a second physical fragment) through it.
    """
    assert not any(isinstance(v, list) for v in rows[0].values())
    store = LanceDBStore(path)
    store.open(spec or _vec_spec(), [])
    store.write_rows("t", rows)
    for field in store._tables["t"].schema:
        assert not pa.types.is_fixed_size_list(field.type)
    return store


def _fresh_committed_state(path: str) -> tuple[LanceDBStore, list[dict[str, Any]], pa.DataType]:
    """What a brand-new handle over the directory observes: rows + vec column type."""
    fresh = LanceDBStore(path)
    fresh.open(_vec_spec(), [])
    rows = sorted(fresh.read_rows("t"), key=lambda r: r["cid"])
    vec_type = fresh._tables["t"].schema.field("vec").type
    return fresh, rows, vec_type


def _assert_rows_preserved(path: str, expected: list[dict[str, Any]], spec: TableSpec | None = None) -> None:
    """A fresh handle must read exactly the committed rows, blob bytes included."""
    fresh = LanceDBStore(path)
    fresh.open(spec or _vec_spec(), [])
    rows = sorted(fresh.read_rows("t"), key=lambda r: r["cid"])
    keys = sorted(expected[0].keys())
    got = [tuple(r.get(k) for k in keys) for r in rows]
    want = [tuple(r.get(k) for k in keys) for r in sorted(expected, key=lambda r: r["cid"])]
    assert got == want, (
        f"committed rows were destroyed, hidden, or altered: got cids {[r['cid'] for r in rows]!r}, expected {[r['cid'] for r in expected]!r}"
    )


_SEED = [
    _row("a", vec=None, content=_BLOB_A),
    _row("b", vec=None, content=_BLOB_B, write_gen=2),
]


# --- H1 failpoint (a): failure before any specialization work ---


def test_failure_before_specialization_work_preserves_committed_rows(tmp_path, monkeypatch) -> None:
    path = str(tmp_path / "failpoint-before")
    _seed_committed_rows(path, _SEED)

    store = LanceDBStore(path)
    store.open(_vec_spec(), [])
    table_type = type(store._tables["t"])
    fired = {"count": 0}

    def refuse_alter(table, *alterations, **kwargs):
        fired["count"] += 1
        raise RuntimeError("simulated failure before specialization work")

    monkeypatch.setattr(table_type, "alter_columns", refuse_alter)

    with pytest.raises(RuntimeError, match="preserved"):
        store.write_rows("t", [_row("c", vec=[0.1, 0.2, 0.3], content=b"new")])

    assert fired["count"] == 1, "the failpoint must actually fire for this proof to mean anything"
    _assert_rows_preserved(path, _SEED)
    _fresh, _rows, vec_type = _fresh_committed_state(path)
    assert vec_type == pa.list_(pa.float64()), "a failed specialization must leave the schema untouched"


# --- H1 failpoint (b) + H4: REAL mid-rewrite crash, replacement staged but never published ---


def test_real_mid_rewrite_failure_stages_files_but_never_publishes(tmp_path) -> None:
    """H4 mid-swap proof on Lance's REAL rewrite path, no mocks: a two-column
    alteration where the second altered column's cast fails on a committed
    wrong-length row AFTER earlier column fragments were already rewritten.
    Real staged data files appear in the dataset directory, yet nothing is
    published: the version and both column schemas are unchanged and every
    committed row is byte-identical through a fresh handle. Publication is
    Lance's single atomic manifest commit — staged files without a manifest
    reference are unreachable, so preservation is at the TABLE level (the
    physical directory legitimately retains the aborted rewrite's files)."""
    path = str(tmp_path / "real-midswap")
    spec = _vec2_spec()
    batch_one = [
        _row2("a", vec_a=None, vec_b=None, content=_BLOB_A),
        _row2("b", vec_a=[1.0, 2.0, 3.0], vec_b=[1.0, 2.0, 3.0], content=_BLOB_B, write_gen=2),
    ]
    seeder = _seed_committed_rows(path, batch_one, spec)
    # A second physical fragment holding the row that breaks the SECOND altered
    # column (sorted alteration order: vec_a rewrites first, vec_b then fails).
    batch_two = [_row2("bad", vec_a=[4.0, 5.0, 6.0], vec_b=[0.1, 0.2], content=b"short", write_gen=3)]
    seeder.write_rows("t", batch_two)
    seed = [*batch_one, *batch_two]

    data_dir = Path(path) / "t.lance" / "data"
    files_before = {p.name for p in data_dir.iterdir()}
    probe = LanceDBStore(path)
    probe.open(spec, [])
    version_before = probe._tables["t"].version

    store = LanceDBStore(path)
    store.open(spec, [])
    with pytest.raises(RuntimeError, match=r"(?s)'t'.*vec_a.*vec_b.*preserved"):
        store.write_rows("t", [_row2("c", vec_a=[7.0, 8.0, 9.0], vec_b=[7.0, 8.0, 9.0], content=b"new", write_gen=4)])

    staged = {p.name for p in data_dir.iterdir()} - files_before
    assert staged, "Lance must have physically staged rewritten column files before the failure — the failpoint fired mid-rewrite"

    fresh = LanceDBStore(path)
    fresh.open(spec, [])
    tbl = fresh._tables["t"]
    assert tbl.version == version_before, "a failed alteration must not publish a new version"
    assert tbl.schema.field("vec_a").type == pa.list_(pa.float64()), "no column of a failed multi-column alteration may be published"
    assert tbl.schema.field("vec_b").type == pa.list_(pa.float64())
    _assert_rows_preserved(path, seed, spec)  # exact set: the trigger row is absent


# --- H1 failpoint (c): a REAL conversion failure during replacement, no mocks ---


def test_real_cast_failure_raises_loudly_preserves_rows_and_retries_clean(tmp_path) -> None:
    """A committed row whose list length differs from the detected dimension makes
    the physical cast fail. That failure must raise loudly (naming the table and
    the preserved state), leave every committed row readable, and a retry after
    removing the offending row must converge with no duplicates."""
    path = str(tmp_path / "failpoint-real-cast")
    seed = [*_SEED, _row("bad", vec=[0.1, 0.2], content=b"short", write_gen=3)]
    _seed_committed_rows(path, seed)

    store = LanceDBStore(path)
    store.open(_vec_spec(), [])
    with pytest.raises(RuntimeError, match=r"(?s)'t'.*vec.*preserved"):
        store.write_rows("t", [_row("c", vec=[0.1, 0.2, 0.3], content=b"new")])

    _assert_rows_preserved(path, seed)
    _fresh, _rows, vec_type = _fresh_committed_state(path)
    assert vec_type == pa.list_(pa.float64())

    # Crashed-then-retried (H3): drop the offending row, retry through a fresh
    # handle — specialization converges, no duplicate rows, no leftover tables.
    cleaner = LanceDBStore(path)
    cleaner.open(_vec_spec(), [])
    assert cleaner.delete_rows("t", [("cid", "eq", "bad")]) == 1

    retry = LanceDBStore(path)
    retry.open(_vec_spec(), [])
    retry.write_rows("t", [_row("c", vec=[0.1, 0.2, 0.3], content=b"new", write_gen=4)])

    _fresh, rows, vec_type = _fresh_committed_state(path)
    assert [r["cid"] for r in rows] == ["a", "b", "c"], "retry must converge with no duplicates"
    assert vec_type == pa.list_(pa.float32(), 3)
    assert _fresh._db.list_tables().tables == ["t"], "no leftover temp table may be visible as live"


# --- Sentinel: the live table is never dropped or recreated during specialization ---


def test_specialization_never_drops_or_recreates_the_live_table(tmp_path, monkeypatch) -> None:
    path = str(tmp_path / "no-drop")
    _seed_committed_rows(path, _SEED)

    store = LanceDBStore(path)
    store.open(_vec_spec(), [])
    connection_type = type(store._db)

    def forbid_drop(connection, *args, **kwargs):
        raise AssertionError("destructive drop_table attempted during specialization")

    def forbid_create(connection, *args, **kwargs):
        raise AssertionError("create_table attempted during specialization (drop/recreate window)")

    monkeypatch.setattr(connection_type, "drop_table", forbid_drop)
    monkeypatch.setattr(connection_type, "create_table", forbid_create)

    store.write_rows("t", [_row("c", vec=[0.1, 0.2, 0.3], content=b"new")])

    monkeypatch.undo()
    _fresh, rows, vec_type = _fresh_committed_state(path)
    assert [r["cid"] for r in rows] == ["a", "b", "c"]
    assert vec_type == pa.list_(pa.float32(), 3)


# --- H2: successful specialization preserves everything and stays searchable ---


def test_successful_specialization_preserves_rows_and_searches_after_reopen(tmp_path) -> None:
    path = str(tmp_path / "success")
    _seed_committed_rows(path, _SEED)

    writer = LanceDBStore(path)
    writer.open(_vec_spec(), [])
    trigger = _row("c", vec=[0.1, 0.2, 0.3], content=b"new", write_gen=3)
    writer.write_rows("t", [trigger])

    _fresh, rows, vec_type = _fresh_committed_state(path)
    assert vec_type == pa.list_(pa.float32(), 3)
    by_cid = {r["cid"]: r for r in rows}
    assert by_cid["a"]["content"] == _BLOB_A and by_cid["a"]["vec"] is None
    assert by_cid["b"]["content"] == _BLOB_B and by_cid["b"]["vec"] is None
    assert by_cid["c"]["content"] == b"new"
    assert by_cid["c"]["vec"] == pytest.approx([0.1, 0.2, 0.3])

    searcher = LanceDBStore(path)
    searcher.open(_vec_spec(), [])
    hits = searcher.search("t", query_vector=[0.1, 0.2, 0.3], vector_column="vec", limit=1)
    assert [h["cid"] for h in hits] == ["c"]
    assert "_distance" in hits[0]


# --- H3: repeating specialization across handles is a no-op, no temp tables ---


def test_specialization_is_idempotent_across_handles(tmp_path) -> None:
    path = str(tmp_path / "idempotent")
    _seed_committed_rows(path, _SEED)

    first = LanceDBStore(path)
    first.open(_vec_spec(), [])
    first.write_rows("t", [_row("c", vec=[0.1, 0.2, 0.3], content=b"new", write_gen=3)])

    second = LanceDBStore(path)
    second.open(_vec_spec(), [])
    version_before = second._tables["t"].version
    second.write_rows("t", [_row("d", vec=[0.4, 0.5, 0.6], content=b"newer", write_gen=4)])

    assert second._tables["t"].version == version_before + 1, "an already-specialized table must see exactly the row append, no hidden rewrite"
    _fresh, rows, vec_type = _fresh_committed_state(path)
    assert [r["cid"] for r in rows] == ["a", "b", "c", "d"], "no duplicate rows after repeated specialization"
    assert vec_type == pa.list_(pa.float32(), 3)
    assert _fresh._db.list_tables().tables == ["t"], "no leftover temp table may be visible as live"


# --- H5: fresh world — pristine directory created by the store itself ---


def test_fresh_world_first_write_specializes_and_searches(tmp_path) -> None:
    path = str(tmp_path / "pristine" / "kb")
    assert not Path(path).exists()

    store = LanceDBStore(path)
    store.open(_vec_spec(), [])
    store.write_rows("t", [_row("a", vec=[1.0, 0.0, 0.0], content=_BLOB_A)])

    assert Path(path).exists(), "the store itself must have created the directory"
    _fresh, rows, vec_type = _fresh_committed_state(path)
    assert [r["cid"] for r in rows] == ["a"]
    assert rows[0]["content"] == _BLOB_A
    assert vec_type == pa.list_(pa.float32(), 3)
    hits = _fresh.search("t", query_vector=[1.0, 0.0, 0.0], vector_column="vec", limit=1)
    assert [h["cid"] for h in hits] == ["a"]


# --- Crash AFTER publish: the trigger-row append fails post-specialization ------
# Historical note: at this ``Table.add`` seam the pre-#206 implementation
# re-added ALL data to a freshly recreated table and an injected crash hid every
# row (RED proof for gate H1(c): "readable rows: []"). Its sibling seam,
# create_table-after-drop, cannot fire at all under the failure-safe
# implementation and is pinned shut by the drop/recreate sentinel above. Today
# ``Table.add`` fires only for the ordinary trigger-row append, AFTER the
# alteration commit — so this test pins two guarantees at once: a crashed append
# hides nothing, and the specialization commit is already durable.


def test_crash_during_trigger_row_append_keeps_specialization_and_rows(tmp_path, monkeypatch) -> None:
    path = str(tmp_path / "append-crash")
    _seed_committed_rows(path, _SEED)

    store = LanceDBStore(path)
    store.open(_vec_spec(), [])
    table_type = type(store._tables["t"])
    fired = {"count": 0}

    def crash_add(table, *args, **kwargs):
        fired["count"] += 1
        raise RuntimeError("simulated crash while appending the trigger row")

    monkeypatch.setattr(table_type, "add", crash_add)
    with pytest.raises(RuntimeError, match="appending the trigger row"):
        store.write_rows("t", [_row("c", vec=[0.1, 0.2, 0.3], content=b"new")])
    monkeypatch.undo()

    assert fired["count"] == 1, "the failpoint must actually fire for this proof to mean anything"
    _fresh, rows, vec_type = _fresh_committed_state(path)
    assert [r["cid"] for r in rows] == ["a", "b"], "a crashed append must neither hide committed rows nor publish a partial trigger row"
    assert rows[0]["content"] == _BLOB_A and rows[1]["content"] == _BLOB_B
    assert vec_type == pa.list_(pa.float32(), 3), "the specialization commit is durable even when the following append crashes"


# --- Cached-handle coherence: no reopen needed after specialization -------------


def test_cached_handle_serves_new_schema_and_next_write_without_reopen(tmp_path) -> None:
    path = str(tmp_path / "cached-handle")
    _seed_committed_rows(path, _SEED)

    store = LanceDBStore(path)
    store.open(_vec_spec(), [])
    handle_before = store._tables["t"]

    store.write_rows("t", [_row("c", vec=[0.1, 0.2, 0.3], content=b"new", write_gen=3)])

    assert store._tables["t"] is handle_before, "specialization must advance the cached handle in place, never swap it"
    assert handle_before.schema.field("vec").type == pa.list_(pa.float32(), 3), "the cached handle must serve the specialized schema immediately"

    store.write_rows("t", [_row("d", vec=[0.4, 0.5, 0.6], content=b"newer", write_gen=4)])

    _fresh, rows, vec_type = _fresh_committed_state(path)
    assert [r["cid"] for r in rows] == ["a", "b", "c", "d"], "a write through the cached handle after specialization must land without a reopen"
    assert vec_type == pa.list_(pa.float32(), 3)


# --- Multi-column success: both columns publish in ONE atomic version -----------


def test_multi_column_specialization_publishes_one_atomic_version(tmp_path) -> None:
    path = str(tmp_path / "multi-col")
    spec = _vec2_spec()
    seed = [
        _row2("a", vec_a=None, vec_b=None, content=_BLOB_A),
        _row2("b", vec_a=[1.0, 2.0, 3.0], vec_b=[1.0, 2.0, 3.0, 4.0], content=_BLOB_B, write_gen=2),
    ]
    _seed_committed_rows(path, seed, spec)

    probe = LanceDBStore(path)
    probe.open(spec, [])
    version_before = probe._tables["t"].version

    writer = LanceDBStore(path)
    writer.open(spec, [])
    trigger = _row2("c", vec_a=[7.0, 8.0, 9.0], vec_b=[6.0, 7.0, 8.0, 9.0], content=b"new", write_gen=3)
    writer.write_rows("t", [trigger])

    fresh = LanceDBStore(path)
    fresh.open(spec, [])
    tbl = fresh._tables["t"]
    assert tbl.schema.field("vec_a").type == pa.list_(pa.float32(), 3)
    assert tbl.schema.field("vec_b").type == pa.list_(pa.float32(), 4), "each column keeps its own detected dimension"
    assert tbl.version == version_before + 2, "both column alterations must share ONE version commit (the second bump is the row append)"
    _assert_rows_preserved(path, [*seed, trigger], spec)
