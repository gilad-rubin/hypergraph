"""Pin ``Table`` — the promoted durable typed table (superposition PRD 0027 F1).

A ``Table`` is identity + store + schema handling, zero derivation: the layer
``HyperTable`` builds on. Downstream projects use it as an append-only log
(unique identities, bytes payloads). ``Graph([]).as_table()`` now fails loudly
naming ``Table``, so the
class name always tells the truth about whether a table derives.

Behavioral pins (identical to the old plain-HyperTable mode, byte-compatible
on disk — same physical columns, same write semantics):

- append is append-if-absent BY IDENTITY: re-appending an existing identity
  is a no-op even when field values differ. Changing a stored row requires
  the explicit update() verb.
- bytes round-trip untouched; rows survive a fresh handle over the same
  store path; delete removes by identity.
- no runner ceremony: a Table needs no ``with_runner`` — it derives nothing.
"""

import pytest

from hypergraph import Graph, GraphConfigError
from hypergraph.materialization import LanceDBStore, Table


@pytest.fixture()
def table(tmp_path):
    return Table(identity="upload_id", store=LanceDBStore(str(tmp_path)))


def test_empty_graph_as_table_raises_naming_table(tmp_path):
    with pytest.raises(GraphConfigError, match="Table"):
        Graph([]).as_table(identity="upload_id", store=LanceDBStore(str(tmp_path)))


def test_insert_and_roundtrip_including_bytes(table):
    table.append(upload_id="u1", name="a.pdf", content=b"bytes-1", sha256="aaa")
    assert table.count() == 1
    row = table.get("u1")
    assert row["name"] == "a.pdf"
    assert row["content"] == b"bytes-1"
    assert row["sha256"] == "aaa"


def test_reinsert_same_identity_is_a_noop_even_with_changed_fields(table):
    table.append(upload_id="u1", name="a.pdf", content=b"bytes-1", sha256="aaa")
    table.append(upload_id="u1", name="a-renamed.pdf", content=b"bytes-1", sha256="aaa")
    assert table.count() == 1
    assert table.get("u1")["name"] == "a.pdf"  # unchanged: insert never updates


def test_update_is_the_explicit_change_verb(table):
    table.append(upload_id="u1", name="a.pdf", content=b"bytes-1", sha256="aaa")
    table.update("u1", name="a-renamed.pdf")
    assert table.get("u1")["name"] == "a-renamed.pdf"
    assert table.count() == 1


def test_multiple_identities_filter_and_delete(table):
    table.append(upload_id="u1", name="a.pdf", content=b"1", sha256="aaa")
    table.append(upload_id="u2", name="b.pdf", content=b"2", sha256="bbb")
    assert table.count() == 2
    assert {r["upload_id"] for r in table.rows()} == {"u1", "u2"}
    table.delete("u1")
    assert table.count() == 1
    assert table.get("u1") is None


def test_rows_survive_a_fresh_handle_over_the_same_store(tmp_path):
    store_path = str(tmp_path)
    first = Table(identity="upload_id", store=LanceDBStore(store_path))
    first.append(upload_id="u2", name="b.pdf", content=b"bytes-2", sha256="bbb")
    fresh = Table(identity="upload_id", store=LanceDBStore(store_path))
    assert fresh.count() == 1
    assert fresh.get("u2")["content"] == b"bytes-2"


def test_table_opens_a_store_written_by_the_old_plain_hypertable_shape(tmp_path):
    """On-disk compatibility: Table writes the exact physical columns the old
    the former plain-table mode wrote, so pre-promotion stores open and read
    identically (the live walkthrough KB's ``upload``/``meta`` tables)."""
    store_path = str(tmp_path)
    table = Table(identity="upload_id", store=LanceDBStore(store_path))
    table.append(upload_id="u1", name="a.pdf", content=b"bytes-1", sha256="aaa")
    # The physical row carries the same internal columns the old mode wrote.
    raw = LanceDBStore(store_path).read_rows("upload")
    assert len(raw) == 1
    assert {"upload_id", "name", "content", "sha256", "_row_fingerprint", "_write_gen", "_status", "_error"} <= set(raw[0].keys())
