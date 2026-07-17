"""Foreman-owned falsifiers for ticket #206: failure-safe vector-schema specialization.

``LanceDBStore`` specializes a plain ``list`` vector column to
``fixed_size_list<float32, dim>`` on the first write that carries a real vector.
These tests pin the failure-safety contract of that specialization: no injected
or real failure at any stage may destroy or hide previously committed rows, and
every failure surfaces loudly while a FRESH store handle over the same directory
still reads the committed table completely (row counts + blob byte equality).
"""

from __future__ import annotations

import contextlib
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


def _seed_committed_rows(path: str, rows: list[dict[str, Any]]) -> None:
    """Commit rows through their own handle, leaving the vector column un-specialized.

    The first row must carry ``vec=None`` so dimension detection (which samples
    the first row of the first write) finds nothing to specialize — the exact
    state of a real store whose rows were written before any embedding existed.
    """
    assert rows[0]["vec"] is None
    store = LanceDBStore(path)
    store.open(_vec_spec(), [])
    store.write_rows("t", rows)
    assert store._tables["t"].schema.field("vec").type == pa.list_(pa.float64())


def _fresh_committed_state(path: str) -> tuple[LanceDBStore, list[dict[str, Any]], pa.DataType]:
    """What a brand-new handle over the directory observes: rows + vec column type."""
    fresh = LanceDBStore(path)
    fresh.open(_vec_spec(), [])
    rows = sorted(fresh.read_rows("t"), key=lambda r: r["cid"])
    vec_type = fresh._tables["t"].schema.field("vec").type
    return fresh, rows, vec_type


def _assert_rows_preserved(path: str, expected: list[dict[str, Any]]) -> None:
    """A fresh handle must read exactly the committed rows, blob bytes included."""
    _fresh, rows, _vec_type = _fresh_committed_state(path)
    got = [(r["cid"], r["content"], r["vec"], r["_write_gen"]) for r in rows]
    want = [(r["cid"], r["content"], r["vec"], r["_write_gen"]) for r in sorted(expected, key=lambda r: r["cid"])]
    assert got == want, (
        f"committed rows were destroyed or hidden: got {[(c, v) for c, _, v, _ in got]!r}, expected {[(c, v) for c, _, v, _ in want]!r}"
    )


def _assert_seed_rows_readable(path: str, expected: list[dict[str, Any]]) -> None:
    """Every previously committed row must still be readable byte-identically.

    Subset semantics: rows written AFTER the seed (a trigger row whose write
    succeeded) are allowed — what may never happen is a seed row going missing
    or changing bytes.
    """
    _fresh, rows, _vec_type = _fresh_committed_state(path)
    by_cid = {r["cid"]: r for r in rows}
    for exp in expected:
        got = by_cid.get(exp["cid"])
        assert got is not None, f"previously committed row {exp['cid']!r} was destroyed or hidden; readable rows: {sorted(by_cid)!r}"
        assert got["content"] == exp["content"], f"blob bytes of committed row {exp['cid']!r} changed"
        assert got["_write_gen"] == exp["_write_gen"], f"_write_gen of committed row {exp['cid']!r} changed"


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


# --- H1 failpoint (b) + H4: crash mid-rewrite, replacement staged but never published ---


def test_crash_mid_rewrite_before_publish_preserves_committed_rows(tmp_path, monkeypatch) -> None:
    """Replacement column files physically staged in the dataset dir, crash before
    the manifest commit: the staged artifact is on disk yet a fresh handle reads
    only committed data — publication is Lance's atomic manifest commit, so an
    incomplete replacement is never readable under the live table name."""
    path = str(tmp_path / "failpoint-midswap")
    _seed_committed_rows(path, _SEED)

    store = LanceDBStore(path)
    store.open(_vec_spec(), [])
    table_type = type(store._tables["t"])
    staged = Path(path) / "t.lance" / "data" / "zz-staged-replacement-not-committed.lance"

    def crash_mid_rewrite(table, *alterations, **kwargs):
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"\x00partial-replacement-column-data\xff")
        raise RuntimeError("simulated crash mid-rewrite, before manifest commit")

    monkeypatch.setattr(table_type, "alter_columns", crash_mid_rewrite)

    with pytest.raises(RuntimeError, match="preserved"):
        store.write_rows("t", [_row("c", vec=[0.1, 0.2, 0.3], content=b"new")])

    assert staged.exists(), "the staged replacement artifact must physically exist for this proof"
    _assert_rows_preserved(path, _SEED)
    _fresh, _rows, vec_type = _fresh_committed_state(path)
    assert vec_type == pa.list_(pa.float64()), "an unpublished replacement must not change the live schema"


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


# --- Historical drop/recreate seams (the #206 RED proofs) ----------------------
# These two inject at the physical seams where the pre-#206 implementation lost
# data: ``create_table`` (fired after the destructive drop — rows destroyed) and
# ``Table.add`` (fired while re-adding data to the replacement — rows hidden).
# Against that implementation both FAILED with "readable rows: []". They stay in
# the suite as trip-wires: any regression toward a drop/recreate window loses
# committed rows again and trips the subset-preservation assertion.


def test_crash_at_historical_create_table_seam_leaves_rows_readable(tmp_path, monkeypatch) -> None:
    path = str(tmp_path / "red-after-drop")
    _seed_committed_rows(path, _SEED)

    store = LanceDBStore(path)
    store.open(_vec_spec(), [])
    connection_type = type(store._db)

    def crash_create(connection, *args, **kwargs):
        raise RuntimeError("simulated crash during replacement construction")

    monkeypatch.setattr(connection_type, "create_table", crash_create)
    with contextlib.suppress(RuntimeError):
        store.write_rows("t", [_row("c", vec=[0.1, 0.2, 0.3], content=b"new")])
    monkeypatch.undo()

    _assert_seed_rows_readable(path, _SEED)


def test_crash_at_historical_readd_seam_leaves_rows_readable(tmp_path, monkeypatch) -> None:
    path = str(tmp_path / "red-during-readd")
    _seed_committed_rows(path, _SEED)

    store = LanceDBStore(path)
    store.open(_vec_spec(), [])
    table_type = type(store._tables["t"])

    def crash_add(table, *args, **kwargs):
        raise RuntimeError("simulated crash while re-adding data to the replacement")

    monkeypatch.setattr(table_type, "add", crash_add)
    with contextlib.suppress(RuntimeError):
        store.write_rows("t", [_row("c", vec=[0.1, 0.2, 0.3], content=b"new")])
    monkeypatch.undo()

    _assert_seed_rows_readable(path, _SEED)
