"""Pin HyperTable(nodes=[]) — the degenerate 'plain table' mode.

A HyperTable with an empty node list is a durable typed table: identity +
store + schema handling, zero derivation. Downstream projects rely on it as
an append-only log (unique identities, bytes payloads). Nothing else in this
suite constructs it, so these tests pin the mode's actual semantics:

- insert is insert-if-absent BY IDENTITY: re-inserting an existing identity
  is a no-op even when field values differ (with no nodes, the row
  fingerprint covers no graph inputs, so an existing identity always reads
  as unchanged). Changing a stored row requires the explicit update() verb.
- bytes round-trip untouched; rows survive a fresh handle over the same
  store path; delete removes by identity.
"""

import pytest

from hypergraph.materialization import HyperTable
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.runners import SyncRunner


@pytest.fixture()
def table(tmp_path):
    return HyperTable(nodes=[], identity="upload_id", store=LanceDBStore(str(tmp_path))).with_runner(SyncRunner())


def test_insert_and_roundtrip_including_bytes(table):
    table.insert(upload_id="u1", name="a.pdf", content=b"bytes-1", sha256="aaa")
    assert table.count() == 1
    row = table.get("u1")
    assert row["name"] == "a.pdf"
    assert row["content"] == b"bytes-1"
    assert row["sha256"] == "aaa"


def test_reinsert_same_identity_is_a_noop_even_with_changed_fields(table):
    table.insert(upload_id="u1", name="a.pdf", content=b"bytes-1", sha256="aaa")
    table.insert(upload_id="u1", name="a-renamed.pdf", content=b"bytes-1", sha256="aaa")
    assert table.count() == 1
    assert table.get("u1")["name"] == "a.pdf"  # unchanged: insert never updates


def test_update_is_the_explicit_change_verb(table):
    table.insert(upload_id="u1", name="a.pdf", content=b"bytes-1", sha256="aaa")
    table.update("u1", name="a-renamed.pdf")
    assert table.get("u1")["name"] == "a-renamed.pdf"
    assert table.count() == 1


def test_multiple_identities_filter_and_delete(table):
    table.insert(upload_id="u1", name="a.pdf", content=b"1", sha256="aaa")
    table.insert(upload_id="u2", name="b.pdf", content=b"2", sha256="bbb")
    assert table.count() == 2
    assert {r["upload_id"] for r in table.filter()} == {"u1", "u2"}
    table.delete("u1")
    assert table.count() == 1
    assert table.get("u1") is None


def test_rows_survive_a_fresh_handle_over_the_same_store(tmp_path):
    store_path = str(tmp_path)
    first = HyperTable(nodes=[], identity="upload_id", store=LanceDBStore(store_path)).with_runner(SyncRunner())
    first.insert(upload_id="u2", name="b.pdf", content=b"bytes-2", sha256="bbb")
    fresh = HyperTable(nodes=[], identity="upload_id", store=LanceDBStore(store_path)).with_runner(SyncRunner())
    assert fresh.count() == 1
    assert fresh.get("u2")["content"] == b"bytes-2"
