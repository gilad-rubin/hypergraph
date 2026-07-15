"""Async HyperTable mutation behavior."""

from __future__ import annotations

import pytest

from hypergraph import Graph, node
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.runners import AsyncRunner


@node(output_name="clean_text")
async def clean(text: str) -> str:
    return text.strip().lower()


@node(output_name="word_count")
def count_words(clean_text: str) -> int:
    return len(clean_text.split())


@pytest.fixture
def table(tmp_path):
    return Graph([clean, count_words]).as_table(identity="doc_id", store=LanceDBStore(str(tmp_path / "async_store")), runner=AsyncRunner())


@pytest.mark.asyncio
async def test_async_insert_update_set_delete(table) -> None:
    """AsyncRunner-bound tables expose awaitable mutations with derived outputs."""

    await table.insert(doc_id="d1", text="Hello World", active=False)
    assert table.get("d1")["clean_text"] == "hello world"

    await table.update("d1", text="one two three")
    assert table.get("d1")["word_count"] == 3

    assert await table.set([("doc_id", "eq", "d1")], active=True, station="NICU") == 1
    assert table.get("d1")["station"] == "NICU"

    await table.delete("d1")
    assert table.get("d1") is None


@pytest.mark.asyncio
async def test_async_sync_reconciles_rows(table) -> None:
    """sync() is awaitable and returns the same reconciliation result as sync tables."""

    result = await table.sync(
        [
            {"doc_id": "d1", "text": "unchanged"},
            {"doc_id": "d2", "text": "will change"},
        ]
    )
    assert result.inserted == 2

    result = await table.sync(
        [
            {"doc_id": "d1", "text": "unchanged"},
            {"doc_id": "d2", "text": "changed text"},
            {"doc_id": "d3", "text": "brand new"},
        ]
    )

    assert result.skipped == 1
    assert result.updated == 1
    assert result.inserted == 1
    assert table.get("d2")["word_count"] == 2


@pytest.mark.asyncio
async def test_async_backfill_populates_null_columns(tmp_path) -> None:
    """backfill() runs the graph under AsyncRunner — a newly added column is derived,
    not silently left null. Regression: recompute/backfill had no async dispatch and
    fed an un-awaited coroutine to _extract_outputs, deriving nothing."""
    store = LanceDBStore(str(tmp_path / "async_backfill_store"))

    table_v1 = Graph([clean]).as_table(identity="doc_id", store=store, runner=AsyncRunner())
    await table_v1.insert(doc_id="d1", text="hello world")
    await table_v1.insert(doc_id="d2", text="one two three")

    table_v2 = Graph([clean, count_words]).as_table(identity="doc_id", store=store, runner=AsyncRunner())
    await table_v2.rederive("word_count", missing_only=True)

    assert table_v2.get("d1")["word_count"] == 2
    assert table_v2.get("d2")["word_count"] == 3


@pytest.mark.asyncio
async def test_async_recompute_rederives_column(tmp_path) -> None:
    """recompute() is awaitable under AsyncRunner and re-derives through the async
    graph instead of leaving a stale value (or raising on an un-awaitable None)."""
    store = LanceDBStore(str(tmp_path / "async_recompute_store"))
    table = Graph([clean, count_words]).as_table(identity="doc_id", store=store, runner=AsyncRunner())

    await table.insert(doc_id="d1", text="hello world")
    assert table.get("d1")["word_count"] == 2

    await table.rederive("word_count")
    assert table.get("d1")["word_count"] == 2
