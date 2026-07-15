"""T28 public-contract tests: a graph with a table behind it."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import ClassVar, TypedDict

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, ifelse, interrupt, node
from hypergraph.materialization import (
    LanceDBStore,
    RowStatus,
    WaitingRow,
    WriteOutcome,
)


@dataclass(frozen=True)
class ReviewQuestion:
    answer_type: ClassVar[object] = str
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple[object, ...] = ()


@pytest.mark.asyncio
async def test_cold_boot_waits_then_answer_update_converges_without_rerunning_upstream(tmp_path) -> None:
    calls = {"prepare": 0, "ask": 0, "apply": 0}

    @node(output_name="prepared")
    def prepare(text: str) -> str:
        calls["prepare"] += 1
        return text.strip().lower()

    @interrupt(answer_name="decision")
    def review(prepared: str) -> ReviewQuestion:
        calls["ask"] += 1
        return ReviewQuestion(
            prompt=f"Publish {prepared}?",
            options=("publish", "archive"),
            evidence=({"preview": prepared},),
        )

    @ifelse(when_true="publish", when_false="archive")
    def choose_route(decision: str) -> bool:
        return decision == "publish"

    @node(output_name="filed")
    def publish(prepared: str) -> str:
        calls["apply"] += 1
        return f"published:{prepared}"

    @node(output_name="filed")
    def archive(prepared: str) -> str:
        calls["apply"] += 1
        return f"archived:{prepared}"

    store = LanceDBStore(str(tmp_path / "cold_boot"))
    table = Graph([prepare, review, choose_route, publish, archive], name="intake").as_table(
        identity="upload_id",
        store=store,
        runner=AsyncRunner(),
    )

    first = await table.insert(upload_id="u-041", text="  Draft  ")

    assert first.id == "u-041"
    assert first.outcome is WriteOutcome.INSERTED
    assert first.status is RowStatus.WAITING
    assert first.paused and not first.completed and not first.failed
    assert first.pause is not None
    assert first.pause.value.prompt == "Publish draft?"
    assert first.pause.value.options == ("publish", "archive")
    assert first.pause.value.evidence == ({"preview": "draft"},)
    assert first.pause.response_key == "decision"

    waiting = table.waiting()
    assert len(waiting) == 1
    assert isinstance(waiting[0], WaitingRow)
    assert waiting[0].id == "u-041"
    assert waiting[0].provenance
    assert waiting[0].pause.value.prompt == "Publish draft?"
    assert waiting[0].pause.value.options == ("publish", "archive")
    assert waiting[0].pause.value.evidence == ({"preview": "draft"},)
    assert waiting[0].pause.value.answer_type == "builtins.str"
    assert waiting[0].pause.response_key == "decision"
    first_provenance = waiting[0].provenance

    second = await table.update("u-041", decision="publish")

    assert second.outcome is WriteOutcome.UPDATED
    assert second.completed and not second.paused and not second.failed
    assert table.waiting() == ()
    assert table.get("u-041") == {
        "upload_id": "u-041",
        "text": "  Draft  ",
        "prepared": "draft",
        "decision": "publish",
        "filed": "published:draft",
    }
    assert table.status().is_fresh
    assert calls == {"prepare": 1, "ask": 1, "apply": 1}

    reasked = await table.update("u-041", text="A new draft")

    assert reasked.paused
    assert reasked.pause is not None
    assert reasked.pause.value.prompt == "Publish a new draft?"
    assert table.waiting()[0].provenance != first_provenance
    assert calls == {"prepare": 2, "ask": 2, "apply": 1}


@pytest.mark.asyncio
async def test_paused_insert_never_writes_a_complete_generation(tmp_path) -> None:
    @interrupt(answer_name="decision")
    def review(text: str) -> ReviewQuestion:
        return ReviewQuestion(prompt=f"Approve {text}?")

    store = LanceDBStore(str(tmp_path / "swallow_falsifier"))
    table = Graph([review]).as_table(
        identity="doc_id",
        store=store,
        runner=AsyncRunner(),
    )

    receipt = await table.insert(doc_id="d1", text="draft")
    physical_rows = store.read_rows(table.table_name)

    assert receipt.status is RowStatus.WAITING
    assert [row["_status"] for row in physical_rows] == ["waiting"]
    assert not any(row["_status"] == "complete" for row in physical_rows)
    assert json.loads(physical_rows[0]["_question"])["response_key"] == "decision"


@pytest.mark.asyncio
async def test_answer_supplied_at_insert_is_driven_through_graph_without_asking(tmp_path) -> None:
    asks = 0

    @interrupt(answer_name="decision")
    def review(text: str) -> ReviewQuestion:
        nonlocal asks
        asks += 1
        return ReviewQuestion(prompt=f"Approve {text}?")

    @node(output_name="filed")
    def apply(text: str, decision: str) -> str:
        return f"{decision}:{text}"

    table = Graph([review, apply]).as_table(
        identity="doc_id",
        store=LanceDBStore(str(tmp_path / "headless")),
        runner=AsyncRunner(),
    )

    receipt = await table.insert(doc_id="d1", text="draft", decision="publish")

    assert receipt.completed
    assert asks == 0
    assert table.get("d1")["filed"] == "publish:draft"


@pytest.mark.asyncio
async def test_non_serializable_evidence_fails_loudly_at_pause_persistence(tmp_path) -> None:
    marker = object()

    @interrupt(answer_name="decision")
    def review(text: str) -> ReviewQuestion:
        return ReviewQuestion(prompt="Approve?", evidence=("ok", marker))

    table = Graph([review]).as_table(
        identity="doc_id",
        store=LanceDBStore(str(tmp_path / "bad_evidence")),
        runner=AsyncRunner(),
    )

    with pytest.raises(TypeError, match=r"evidence item 1.*not JSON-serializable"):
        await table.insert(doc_id="d1", text="draft")


@pytest.mark.asyncio
async def test_rederive_answer_returns_truthful_waiting_receipt(tmp_path) -> None:
    @node(output_name="prepared")
    def prepare(text: str) -> str:
        return text.upper()

    @interrupt(answer_name="decision")
    def review(prepared: str) -> ReviewQuestion:
        return ReviewQuestion(prompt=f"Approve {prepared}?")

    @node(output_name="filed")
    def apply(prepared: str, decision: str) -> str:
        return f"{decision}:{prepared}"

    store = LanceDBStore(str(tmp_path / "rederive_answer"))
    table = Graph([prepare, review, apply]).as_table(
        identity="doc_id",
        store=store,
        runner=AsyncRunner(),
    )
    completed = await table.insert(doc_id="d1", text="draft", decision="publish")
    assert completed.completed

    receipt = await table.rederive("decision")

    assert receipt.paused and not receipt.completed and not receipt.failed
    assert len(receipt.waiting) == 1
    assert receipt.waiting[0].pause is not None
    assert receipt.waiting[0].pause.response_key == "decision"
    assert table.waiting()[0].pause.value.prompt == "Approve DRAFT?"
    assert [row["_status"] for row in store.read_rows(table.table_name)] == ["waiting"]

    skipped = await table.rederive("prepared", missing_only=True)
    assert skipped.paused and skipped.waiting[0].outcome is WriteOutcome.SKIPPED


def test_routed_same_name_outputs_insert_into_one_lancedb_column(tmp_path) -> None:
    @ifelse(when_true="positive", when_false="negative")
    def choose(positive_number: bool) -> bool:
        return positive_number

    @node(output_name="label")
    def positive(value: int) -> str:
        return f"positive:{value}"

    @node(output_name="label")
    def negative(value: int) -> str:
        return f"negative:{value}"

    table = Graph([choose, positive, negative]).as_table(
        identity="item_id",
        store=LanceDBStore(str(tmp_path / "union_output")),
        runner=SyncRunner(),
    )

    receipt = table.insert(item_id="i1", positive_number=True, value=3)

    assert receipt.completed
    assert table.get("i1")["label"] == "positive:3"

    updated = table.update("i1", positive_number=False)

    assert updated.completed
    assert table.get("i1")["label"] == "negative:3"
    assert table.status().is_fresh

    rederived = table.rederive("label")
    assert rederived.completed
    assert table.get("i1")["label"] == "negative:3"

    routed_back = table.update("i1", positive_number=True)
    assert routed_back.completed
    assert table.get("i1")["label"] == "positive:3"
    assert table.status().is_fresh
    assert table._store.column_names(table.table_name).count("label") == 1


class ChildItem(TypedDict):
    item_id: str
    text: str


def test_child_handle_exposes_parent_identity_and_filters_on_parent_columns(tmp_path) -> None:
    @node(output_name="items")
    def split(text: str) -> list[ChildItem]:
        return [ChildItem(item_id="a", text=text), ChildItem(item_id="b", text=f"{text}!")]

    @node(output_name="upper")
    def uppercase(text: str) -> str:
        return text.upper()

    child_graph = Graph([uppercase], name="process_item")
    graph = Graph(
        [split, child_graph.as_node().map_over("items", identity="item_id")],
        name="documents",
    )
    table = graph.as_table(
        identity="doc_id",
        store=LanceDBStore(str(tmp_path / "child_join")),
        runner=SyncRunner(),
    )
    table.insert(doc_id="d1", text="one", station="NICU")
    table.insert(doc_id="d2", text="two", station="ER")

    items = table.child("item")
    nicu_rows = items.rows(where={"station": "NICU"})

    assert {row["item_id"] for row in nicu_rows} == {"a", "b"}
    assert {row["doc_id"] for row in nicu_rows} == {"d1"}
    assert all("_parent_id" not in row for row in nicu_rows)
    assert items.get("d1", "a")["doc_id"] == "d1"
