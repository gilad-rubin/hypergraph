"""``HyperTable.rederive_row(id)``: a forced re-derive of one row and its child rows.

Every other write reuses what is stored where the inputs match, and
``rederive(column)`` reaches one column across the whole table. Asking one
row's steps again (a model answered badly, a provider's output changed with no
input change) needs a row-sized verb that skips nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import pytest

from hypergraph import Graph, interrupt, node
from hypergraph.materialization import RowStatus, SqliteTableStore, WriteOutcome
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.runners import AsyncRunner, SyncRunner

CALLS = {"split": 0, "enrich": 0, "label": 0}


@node(output_name="pages")
def split_pages(text: str) -> list[dict]:
    CALLS["split"] += 1
    return [{"page_id": f"p{i}", "page_text": part} for i, part in enumerate(text.split("|"))]


@node(output_name="summary")
def enrich(page_text: str) -> str:
    CALLS["enrich"] += 1
    return f"{page_text.upper()}#{CALLS['enrich']}"


@node(output_name="label")
def label(tag: str) -> str:
    CALLS["label"] += 1
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
    CALLS.update(split=0, enrich=0, label=0)


def _table(store, runner=None):
    pages = Graph([enrich], name="page").as_node().map_over("pages", identity="page_id")
    return Graph([split_pages, pages, label]).as_table(identity="doc_id", store=store, runner=runner or SyncRunner())


async def _call(result):
    return await result if hasattr(result, "__await__") else result


@pytest.mark.parametrize("runner", [SyncRunner, AsyncRunner])
async def test_every_step_of_the_row_and_its_children_runs_again(store, runner):
    table = _table(store, runner())
    await _call(table.insert(doc_id="d1", text="alpha|beta", tag="a"))
    await _call(table.insert(doc_id="d2", text="gamma", tag="b"))
    CALLS.update(split=0, enrich=0, label=0)

    receipt = await _call(table.rederive_row("d1"))

    assert (receipt.id, receipt.outcome, receipt.status) == ("d1", WriteOutcome.UPDATED, RowStatus.COMPLETE)
    assert CALLS == {"split": 1, "enrich": 2, "label": 1}
    # The new answers replaced the stored ones; the other row was not touched.
    assert {row["page_id"]: row["summary"] for row in table.child("page").rows(parent="d1")} == {"p0": "ALPHA#1", "p1": "BETA#2"}
    assert [row["summary"] for row in table.child("page").rows(parent="d2")] == ["GAMMA#3"]
    assert len(store.read_rows("page")) == 3


def test_what_was_set_on_a_child_row_is_kept(store):
    table = _table(store)
    table.insert(doc_id="d1", text="alpha|beta", tag="a")
    table.child("page").set({"page_id": "p1"}, reviewed="yes")

    table.rederive_row("d1")

    assert table.child("page").get("d1", "p1")["reviewed"] == "yes"


def test_an_unknown_row_is_a_key_error(store):
    table = _table(store)
    table.insert(doc_id="d1", text="alpha", tag="a")
    with pytest.raises(KeyError):
        table.rederive_row("missing")


@dataclass(frozen=True)
class Approval:
    answer_type: ClassVar[object] = str
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple[object, ...] = ()


@interrupt(answer_name="decision")
def approve(draft: str) -> Approval:
    return Approval(prompt=f"Publish {draft}?")


@node(output_name="draft")
def write_draft(text: str) -> str:
    CALLS["split"] += 1
    return text.title()


@node(output_name="final")
def publish(draft: str, decision: str) -> str:
    CALLS["label"] += 1
    return f"{draft}:{decision}"


async def test_a_stored_answer_is_an_input_of_the_rederive(store):
    table = Graph([write_draft, approve, publish]).as_table(identity="doc_id", store=store, runner=AsyncRunner())
    assert (await table.insert(doc_id="d1", text="hello")).status is RowStatus.WAITING
    assert (await table.update("d1", decision="ok")).status is RowStatus.COMPLETE
    CALLS.update(split=0, enrich=0, label=0)

    receipt = await table.rederive_row("d1")

    assert receipt.status is RowStatus.COMPLETE
    assert CALLS["split"] == 1 and CALLS["label"] == 1
    assert table.get("d1")["final"] == "Hello:ok"
