"""What a parent write leaves of the child rows it does not need to touch.

An unchanged child row (its inputs matched) stays where it is, at its own
write generation: writing it again only moves it to a new generation, and a
store that projects rows elsewhere (a search index) sees every such move as a
change. A child row that does re-derive keeps what was said about it after its
birth (a value ``ChildTable.set`` wrote), as ``update()`` keeps a root row's
metadata.
"""

from __future__ import annotations

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import SqliteTableStore
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.runners import AsyncRunner, SyncRunner

CALLS = {"enrich": 0}


@node(output_name="pages")
def split_pages(text: str) -> list[dict]:
    return [{"page_id": f"p{i}", "page_text": part} for i, part in enumerate(text.split("|"))]


@node(output_name="summary")
def enrich(page_text: str) -> str:
    CALLS["enrich"] += 1
    return page_text.upper()


@node(output_name="label")
def label(tag: str) -> str:
    return tag.upper()


@pytest.fixture(params=["lance", "sqlite"])
def store(request, tmp_path):
    if request.param == "sqlite":
        s = SqliteTableStore()
        yield s
        s.close()
    else:
        yield LanceDBStore(str(tmp_path / "store"))


@pytest.fixture(autouse=True)
def _reset():
    CALLS["enrich"] = 0


def _table(store, runner=None):
    pages = Graph([enrich], name="page").as_node().map_over("pages", identity="page_id")
    return Graph([split_pages, pages, label]).as_table(identity="doc_id", store=store, runner=runner or SyncRunner())


def _physical(store, table="page"):
    return {row["page_id"]: row for row in store.read_rows(table)}


async def _call(result):
    return await result if hasattr(result, "__await__") else result


@pytest.mark.parametrize("runner", [SyncRunner, AsyncRunner])
async def test_an_unchanged_child_row_stays_at_its_own_generation(store, runner):
    table = _table(store, runner())
    await _call(table.insert(doc_id="d1", text="alpha|beta", tag="a"))
    before = _physical(store)

    # Page p1 changes, p0 does not.
    await _call(table.update("d1", text="alpha|gamma"))
    after = _physical(store)

    assert after["p0"]["_write_gen"] == before["p0"]["_write_gen"]
    assert after["p1"]["_write_gen"] > before["p1"]["_write_gen"]
    assert after["p1"]["summary"] == "GAMMA"
    assert len(store.read_rows("page")) == 2
    assert CALLS["enrich"] == 3


def test_a_row_whose_item_is_gone_is_still_retired(store):
    table = _table(store)
    table.insert(doc_id="d1", text="alpha|beta|gamma", tag="a")

    table.update("d1", text="alpha|beta")

    assert sorted(_physical(store)) == ["p0", "p1"]
    assert {row["page_id"] for row in table.child("page").rows()} == {"p0", "p1"}


def test_a_parent_edit_that_reaches_no_child_moves_no_child_row(store):
    table = _table(store)
    table.insert(doc_id="d1", text="alpha|beta", tag="a")
    before = _physical(store)

    table.update("d1", tag="b")

    after = _physical(store)
    assert {key: row["_write_gen"] for key, row in after.items()} == {key: row["_write_gen"] for key, row in before.items()}
    assert table.get("d1")["label"] == "B"


def test_a_rederived_child_row_keeps_what_was_set_on_it(store):
    table = _table(store)
    table.insert(doc_id="d1", text="alpha|beta", tag="a")
    table.child("page").set({"page_id": "p1"}, reviewed="yes")

    # p1's content changes, so its row re-derives from the new item, which
    # does not carry `reviewed`.
    table.update("d1", text="alpha|gamma")

    page = table.child("page").get("d1", "p1")
    assert page["summary"] == "GAMMA"
    assert page["reviewed"] == "yes"
