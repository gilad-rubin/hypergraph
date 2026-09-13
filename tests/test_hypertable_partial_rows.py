"""One failing node degrades its own column, not the whole row (#323)."""

from __future__ import annotations

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import ChangeReason, RowStatus
from hypergraph.runners import AsyncRunner, SyncRunner
from tests.test_hypertable_on_error import MemoryStore

# ---------------------------------------------------------------------------
# A three-stage recipe: the expensive stages come first, the fragile one last.
# ---------------------------------------------------------------------------

calls: dict[str, int] = {}
failing: set[str] = set()


def _record(name: str) -> None:
    calls[name] = calls.get(name, 0) + 1
    if name in failing:
        raise RuntimeError(f"{name} service is down")


@node(output_name="extracted")
def extract(text: str) -> str:
    _record("extract")
    return text.upper()


@node(output_name="summary")
def summarize(extracted: str) -> str:
    _record("summarize")
    return extracted[:5]


@node(output_name="embedding")
def embed(summary: str) -> str:
    _record("embed")
    return f"vec({summary})"


@pytest.fixture(autouse=True)
def _reset() -> None:
    calls.clear()
    failing.clear()


def _table(store: MemoryStore, runner=None, **kwargs):
    return Graph([extract, summarize, embed]).as_table(
        identity="doc_id",
        store=store,
        on_error="store",
        runner=runner or SyncRunner(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The record shape
# ---------------------------------------------------------------------------


def test_a_late_failure_keeps_the_earlier_columns_and_names_the_failed_one() -> None:
    table = _table(MemoryStore())
    failing.add("embed")

    receipt = table.insert(doc_id="d1", text="hello world")

    row = table.get("d1")
    assert row["extracted"] == "HELLO WORLD"
    assert row["summary"] == "HELLO"
    assert row["embedding"] is None
    assert receipt.status is RowStatus.PARTIAL
    assert not receipt.failed
    assert table.errors() == ()

    (partial,) = table.partial()
    assert partial.id == "d1"
    (change,) = partial.changes
    assert change.column == "embedding"
    assert change.reason is ChangeReason.NODE_ERROR
    assert change.node == "embed"
    assert change.error == "RuntimeError: embed service is down"


def test_a_column_the_failure_blocked_reads_as_an_upstream_error() -> None:
    table = _table(MemoryStore())
    failing.add("summarize")

    table.insert(doc_id="d1", text="hello world")

    (partial,) = table.partial()
    assert partial.row["extracted"] == "HELLO WORLD"
    by_column = {change.column: change for change in partial.changes}
    assert by_column["summary"].reason is ChangeReason.NODE_ERROR
    assert by_column["embedding"].reason is ChangeReason.UPSTREAM_ERROR
    assert by_column["embedding"].node == "embed"
    assert by_column["embedding"].error is None
    assert calls.get("embed") is None


def test_a_partial_row_reads_as_needing_heal() -> None:
    table = _table(MemoryStore())
    failing.add("embed")
    table.insert(doc_id="d1", text="hello world")

    status = table.status()

    assert status.total == 1
    assert status.fresh == 0
    assert status.stale == 1
    assert status.errored == 0
    assert status.stale_ids == ("d1",)
    assert dict(status.stale_columns)["embedding"] == 1
    assert not status.is_fresh


# ---------------------------------------------------------------------------
# The retry
# ---------------------------------------------------------------------------


def test_healing_a_partial_row_re_derives_only_the_nulled_column() -> None:
    table = _table(MemoryStore())
    failing.add("embed")
    table.insert(doc_id="d1", text="hello world")
    assert calls == {"extract": 1, "summarize": 1, "embed": 1}

    failing.clear()
    receipt = table.sync([{"doc_id": "d1", "text": "hello world"}])

    assert calls == {"extract": 1, "summarize": 1, "embed": 2}, "the expensive stages must not be paid twice"
    assert table.get("d1")["embedding"] == "vec(HELLO)"
    assert table.partial() == ()
    assert table.status().is_fresh
    assert receipt.completed


def test_a_second_failure_keeps_what_the_first_one_saved() -> None:
    table = _table(MemoryStore())
    failing.add("embed")
    table.insert(doc_id="d1", text="hello world")

    table.sync([{"doc_id": "d1", "text": "hello world"}])

    assert calls == {"extract": 1, "summarize": 1, "embed": 2}
    row = table.get("d1")
    assert row["extracted"] == "HELLO WORLD"
    assert row["summary"] == "HELLO"
    assert row["embedding"] is None
    assert table.errors() == ()
    (partial,) = table.partial()
    assert [change.column for change in partial.changes] == ["embedding"]


def test_a_source_change_still_re_derives_the_whole_row() -> None:
    table = _table(MemoryStore())
    failing.add("embed")
    table.insert(doc_id="d1", text="hello world")
    failing.clear()

    table.sync([{"doc_id": "d1", "text": "goodbye world"}])

    assert calls == {"extract": 2, "summarize": 2, "embed": 2}
    assert table.get("d1")["embedding"] == "vec(GOODB)"
    assert table.partial() == ()


# ---------------------------------------------------------------------------
# What a partial row refuses to claim
# ---------------------------------------------------------------------------


def test_a_first_node_failure_is_still_a_total_loss_error_row() -> None:
    """Nothing survived, so there is no column granularity to claim."""
    table = _table(MemoryStore())
    failing.add("extract")

    receipt = table.insert(doc_id="d1", text="hello world")

    assert receipt.failed
    assert receipt.status is RowStatus.ERROR
    assert table.partial() == ()
    assert table.errors()[0].id == "d1"
    assert "RuntimeError: extract service is down" in table.errors()[0].error
    assert table.get("d1")["extracted"] is None


def test_an_unattributable_failure_is_still_a_total_loss_error_row() -> None:
    """A plan-level failure names no node, so the row keeps its old shape."""
    store = MemoryStore()
    table = _table(store)

    receipt = table.insert(doc_id="d1")

    assert receipt.failed
    assert "MissingInputError" in receipt.error
    assert table.partial() == ()
    assert table.errors()[0].id == "d1"


def test_on_error_raise_still_raises_and_stores_nothing() -> None:
    store = MemoryStore()
    table = Graph([extract, summarize, embed]).as_table(identity="doc_id", store=store, runner=SyncRunner())
    failing.add("embed")

    with pytest.raises(RuntimeError, match="embed service is down"):
        table.insert(doc_id="d1", text="hello world")

    assert store.rows.get("doc", []) == []


# ---------------------------------------------------------------------------
# A real store
# ---------------------------------------------------------------------------


def _lance_table(path: str):
    from hypergraph.materialization import LanceDBStore

    return Graph([extract, summarize, embed]).as_table(
        identity="doc_id",
        store=LanceDBStore(path),
        on_error="store",
        runner=SyncRunner(),
    )


def test_a_partial_row_round_trips_through_lancedb_and_heals_there(tmp_path) -> None:
    path = str(tmp_path / "partial_store")
    failing.add("embed")
    _lance_table(path).insert(doc_id="d1", text="hello world")

    reader = _lance_table(path)
    (partial,) = reader.partial()
    assert partial.changes[0].column == "embedding"
    assert partial.changes[0].error == "RuntimeError: embed service is down"
    assert partial.row["summary"] == "HELLO"
    assert reader.status().stale_ids == ("d1",)

    failing.clear()
    _lance_table(path).sync([{"doc_id": "d1", "text": "hello world"}])

    healed = _lance_table(path)
    assert calls == {"extract": 1, "summarize": 1, "embed": 2}
    assert healed.get("d1")["embedding"] == "vec(HELLO)"
    assert healed.partial() == ()
    assert healed.status().is_fresh


def test_a_store_written_before_changes_existed_takes_a_partial_row(tmp_path) -> None:
    """The _changes column is additive: an existing table grows it on first use."""
    from dataclasses import replace

    from hypergraph.materialization import LanceDBStore
    from hypergraph.materialization._schema import CHANGES_COLUMN, analyze_table

    path = str(tmp_path / "legacy_store")
    spec = analyze_table(Graph([extract, summarize, embed]), "doc_id", {}, [])
    LanceDBStore(path).open(replace(spec, columns=[column for column in spec.columns if column.name != CHANGES_COLUMN]), [])
    assert CHANGES_COLUMN not in LanceDBStore(path).column_names("doc")

    failing.add("embed")
    receipt = _lance_table(path).insert(doc_id="d1", text="hello world")

    assert receipt.status is RowStatus.PARTIAL
    assert CHANGES_COLUMN in LanceDBStore(path).column_names("doc")
    (partial,) = _lance_table(path).partial()
    assert partial.changes[0].column == "embedding"


# ---------------------------------------------------------------------------
# Receipts, nested graphs, async parity
# ---------------------------------------------------------------------------


def test_a_sync_receipt_separates_partial_rows_from_errors() -> None:
    table = _table(MemoryStore())
    failing.update({"embed"})

    receipt = table.sync([{"doc_id": "d1", "text": "hello world"}, {"doc_id": "d2", "text": "second doc"}])

    assert {row.id for row in receipt.partial} == {"d1", "d2"}
    assert receipt.errors == ()
    assert not receipt.completed
    assert not receipt.failed


def test_a_failure_inside_a_mounted_graph_is_blamed_on_its_column() -> None:
    store = MemoryStore()
    stage = Graph([embed], name="embedding_stage")
    table = Graph([extract, summarize, stage.as_node(name="embed_stage")]).as_table(
        identity="doc_id",
        store=store,
        on_error="store",
        runner=SyncRunner(),
    )
    failing.add("embed")

    table.insert(doc_id="d1", text="hello world")

    (partial,) = table.partial()
    (change,) = partial.changes
    assert change.column == "embedding"
    assert change.reason is ChangeReason.NODE_ERROR
    assert change.node == "embed_stage/embed"
    assert partial.row["summary"] == "HELLO"


@pytest.mark.asyncio
async def test_async_keeps_the_same_partial_row_and_heals_it_the_same_way() -> None:
    table = _table(MemoryStore(), runner=AsyncRunner())
    failing.add("embed")

    await table.insert(doc_id="d1", text="hello world")

    (partial,) = table.partial()
    assert partial.changes[0].column == "embedding"
    assert partial.row["summary"] == "HELLO"
    assert table.status().stale == 1

    failing.clear()
    await table.sync([{"doc_id": "d1", "text": "hello world"}])

    assert calls == {"extract": 1, "summarize": 1, "embed": 2}
    assert table.get("d1")["embedding"] == "vec(HELLO)"
    assert table.partial() == ()
