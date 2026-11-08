"""A new child row continues the row it shares its lineage with: ``map_over(lineage=...)``.

A new parent row (a new version of a document) makes all-new child rows, so
every step of every page ran again, though most pages did not change. With
``lineage`` naming the item fields a page shares with the same page of the
earlier version, the new row reconciles against that row: each step whose
inputs did not change is reused instead of run.
"""

from __future__ import annotations

from typing import TypedDict

import pytest

from hypergraph import Graph, GraphConfigError, node
from hypergraph.materialization import SqliteTableStore
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.runners import AsyncRunner, SyncRunner

CALLS = {"enrich": 0, "locate": 0}


class Page(TypedDict):
    page_id: str
    document_id: int
    page_number: int
    page_text: str
    station: str


@node(output_name="pages")
def split_pages(document_id: int, version: str, text: str) -> list[Page]:
    return [
        Page(page_id=f"{document_id}_{version}_p{i}", document_id=document_id, page_number=i, page_text=part, station="onc")
        for i, part in enumerate(text.split("|"))
    ]


@node(output_name="summary")
def enrich(page_text: str) -> str:
    CALLS["enrich"] += 1
    return page_text.upper()


@node(output_name="locator")
def locate(document_id: int, page_number: int) -> str:
    CALLS["locate"] += 1
    return f"{document_id}#{page_number}"


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
    CALLS.update(enrich=0, locate=0)


def _table(store, lineage=("document_id", "page_number"), runner=None):
    pages = Graph([enrich, locate], name="page").as_node().map_over("pages", identity="page_id", schema=Page, lineage=lineage)
    return Graph([split_pages, pages]).as_table(identity="doc_version_id", store=store, runner=runner or SyncRunner())


async def _call(result):
    return await result if hasattr(result, "__await__") else result


@pytest.mark.parametrize("runner", [SyncRunner, AsyncRunner])
async def test_a_new_version_reuses_each_step_whose_inputs_did_not_change(store, runner):
    table = _table(store, runner=runner())
    await _call(table.insert(doc_version_id="1_v1", document_id=1, version="v1", text="alpha|beta|gamma"))
    CALLS.update(enrich=0, locate=0)

    # Version 2 changes page 1 only.
    await _call(table.insert(doc_version_id="1_v2", document_id=1, version="v2", text="alpha|BETA2|gamma"))

    assert CALLS == {"enrich": 1, "locate": 0}
    pages = {row["page_id"]: row for row in table.child("page").rows(parent="1_v2")}
    assert {key: (row["summary"], row["locator"]) for key, row in pages.items()} == {
        "1_v2_p0": ("ALPHA", "1#0"),
        "1_v2_p1": ("BETA2", "1#1"),
        "1_v2_p2": ("GAMMA", "1#2"),
    }
    # The earlier version keeps its own rows.
    assert {row["page_id"] for row in table.child("page").rows(parent="1_v1")} == {"1_v1_p0", "1_v1_p1", "1_v1_p2"}


def test_a_new_page_with_no_earlier_row_runs_every_step(store):
    table = _table(store)
    table.insert(doc_version_id="1_v1", document_id=1, version="v1", text="alpha")
    CALLS.update(enrich=0, locate=0)

    table.insert(doc_version_id="1_v2", document_id=1, version="v2", text="alpha|beta")

    assert CALLS == {"enrich": 1, "locate": 1}


def test_another_document_is_not_a_donor(store):
    table = _table(store)
    table.insert(doc_version_id="1_v1", document_id=1, version="v1", text="alpha")
    CALLS.update(enrich=0, locate=0)

    table.insert(doc_version_id="2_v1", document_id=2, version="v1", text="alpha")

    # `enrich` reads only the text, which matches nothing of document 2's: it runs.
    assert CALLS == {"enrich": 1, "locate": 1}


def test_a_lineage_field_the_store_keeps_no_column_for_reuses_nothing(store):
    # `station` is a schema field no child step reads, so the shipped stores
    # keep no column for it: there is nothing to match on, and nothing breaks.
    table = _table(store, lineage=("station",))
    table.insert(doc_version_id="1_v1", document_id=1, version="v1", text="alpha")
    CALLS.update(enrich=0, locate=0)

    table.insert(doc_version_id="1_v2", document_id=1, version="v2", text="alpha")

    assert CALLS == {"enrich": 1, "locate": 1}


def test_a_lineage_field_the_child_rows_do_not_hold_is_refused(store):
    table = _table(store, lineage=("document_id", "chapter"))
    with pytest.raises(GraphConfigError, match="chapter"):
        table.insert(doc_version_id="1_v1", document_id=1, version="v1", text="alpha")


def test_map_over_records_the_lineage():
    pages = Graph([enrich], name="page").as_node().map_over("pages", identity="page_id", lineage=["document_id", "page_number"])
    assert pages._map_config["lineage"] == ("document_id", "page_number")
