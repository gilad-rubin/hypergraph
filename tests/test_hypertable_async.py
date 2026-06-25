"""Async HyperTable mutation behavior."""

from __future__ import annotations

import pytest

from hypergraph import node
from hypergraph.materialization import HyperTable
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
    return HyperTable(
        [clean, count_words],
        identity="doc_id",
        store=LanceDBStore(str(tmp_path / "async_store")),
    ).with_runner(AsyncRunner())


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
