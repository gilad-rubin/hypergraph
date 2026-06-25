"""Run the TableStore conformance harness against the reference LanceDBStore.

A store author's own test suite should mirror this one test against their store.
"""

from __future__ import annotations

from hypergraph.materialization import check_store_conformance
from hypergraph.materialization._lancedb_store import LanceDBStore


def test_lancedb_store_conforms(tmp_path) -> None:
    check_store_conformance(LanceDBStore(str(tmp_path / "conformance_store")))
