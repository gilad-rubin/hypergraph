"""Contract tests for HyperTable's insert-and-derive node adapter (#353)."""

from __future__ import annotations

from typing import TypedDict

import pytest

from hypergraph import Graph, interrupt, node
from hypergraph.checkpointers import JsonSerializer, SqliteCheckpointer
from hypergraph.events import EventProcessor
from hypergraph.events.types import NodeStartEvent, RunStartEvent
from hypergraph.exceptions import MissingInputError
from hypergraph.materialization import MaterializationReceipt, RowStatus, WriteOutcome
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.runners import AsyncRunner, SyncRunner


@node(output_name="normalized")
def normalize(text: str, active: bool) -> str:
    prefix = "active" if active else "inactive"
    return f"{prefix}:{text.strip().lower()}"


class Page(TypedDict):
    page_id: str
    text: str


class StructuralQuestion:
    prompt = "Publish draft?"
    options = ("yes", "no")
    evidence = ({"document": "d1"},)
    answer_type = str


@node(output_name="pages")
def split_pages(text: str) -> list[Page]:
    return [Page(page_id=f"p{index}", text=value) for index, value in enumerate(text.split("|"))]


@node(output_name="indexed_text")
def index_page(text: str) -> str:
    return text.upper()


@node(output_name="published_id")
def consume_receipt(receipt: MaterializationReceipt) -> str:
    assert isinstance(receipt, MaterializationReceipt)
    assert receipt.completed
    return receipt.id


class ListProcessor(EventProcessor):
    def __init__(self) -> None:
        self.events: list[object] = []
        self.shutdown_count = 0

    def on_event(self, event: object) -> None:
        self.events.append(event)

    def shutdown(self) -> None:
        self.shutdown_count += 1


def test_as_node_inserts_derives_binds_and_emits_typed_receipt(tmp_path) -> None:
    table = Graph([normalize], name="protocol_recipe").as_table(
        identity="version_id",
        store=LanceDBStore(str(tmp_path / "table")),
        runner=SyncRunner(),
    )
    materialize = table.as_node(name="materialize_protocol", output_name="receipt")

    assert materialize.name == "materialize_protocol"
    assert materialize.inputs == ("version_id", "active", "text")
    assert materialize.outputs == ("receipt",)
    assert materialize.get_output_type("receipt") is MaterializationReceipt

    workflow = Graph([materialize, consume_receipt]).bind(active=False)
    result = SyncRunner().run(workflow, version_id="v1", text=" Hello ")

    receipt = result["receipt"]
    assert receipt == MaterializationReceipt(
        id="v1",
        outcome=WriteOutcome.INSERTED,
        status=RowStatus.COMPLETE,
    )
    assert result["published_id"] == "v1"
    assert table.get("v1") == {
        "version_id": "v1",
        "active": False,
        "text": " Hello ",
        "normalized": "inactive:hello",
    }


def test_as_node_missing_unbound_source_fails_before_materialization(tmp_path) -> None:
    table = Graph([normalize]).as_table(
        identity="version_id",
        store=LanceDBStore(str(tmp_path / "table")),
        runner=SyncRunner(),
    )
    workflow = Graph([table.as_node()]).bind(active=False)

    with pytest.raises(MissingInputError, match="text"):
        SyncRunner().run(workflow, version_id="v1")

    assert table.count() == 0


@pytest.mark.asyncio
async def test_as_node_receipt_survives_json_checkpoint_restore(tmp_path) -> None:
    table = Graph([normalize]).as_table(
        identity="version_id",
        store=LanceDBStore(str(tmp_path / "table")),
        runner=AsyncRunner(),
    )
    materialize = table.as_node(output_name="receipt")
    checkpointer = SqliteCheckpointer(
        str(tmp_path / "runs.db"),
        serializer=JsonSerializer(),
    )
    runner = AsyncRunner(checkpointer=checkpointer)
    try:
        result = await runner.run(
            Graph([materialize]).bind(active=False),
            version_id="v1",
            text="Hello",
            workflow_id="materialize",
        )
        assert isinstance(result["receipt"], MaterializationReceipt)

        checkpoint = checkpointer.checkpoint("materialize")
        assert checkpoint is not None
        assert checkpoint.values["receipt"] == {
            "id": "v1",
            "outcome": "inserted",
            "status": "complete",
            "pause": None,
            "error": None,
        }

        restored = await runner.run(
            Graph([consume_receipt]),
            checkpoint=checkpoint,
            workflow_id="consume-restored-receipt",
        )
        assert restored["published_id"] == "v1"
    finally:
        await checkpointer.close()


@pytest.mark.asyncio
async def test_async_runner_executes_a_sync_backed_table(tmp_path) -> None:
    table = Graph([normalize]).as_table(
        identity="version_id",
        store=LanceDBStore(str(tmp_path / "table")),
        runner=SyncRunner(),
    )

    result = await AsyncRunner().run(
        Graph([table.as_node()]).bind(active=False),
        version_id="v1",
        text="Hello",
    )

    assert result["receipt"].completed
    assert table.get("v1")["normalized"] == "inactive:hello"


@pytest.mark.asyncio
async def test_waiting_receipt_is_strict_json_safe(tmp_path) -> None:
    @interrupt(answer_name="decision")
    def review(text: str) -> StructuralQuestion:
        return StructuralQuestion()

    table = Graph([review]).as_table(
        identity="version_id",
        store=LanceDBStore(str(tmp_path / "table")),
        runner=AsyncRunner(),
    )
    receipt = (
        await AsyncRunner().run(
            Graph([table.as_node()]),
            version_id="v1",
            text="draft",
        )
    )["receipt"]

    restored = JsonSerializer().deserialize(JsonSerializer().serialize(receipt))
    assert restored["status"] == "waiting"
    assert restored["pause"] == {
        "node_name": "review",
        "value": {
            "prompt": "Publish draft?",
            "options": ["yes", "no"],
            "evidence": [{"document": "d1"}],
            "answer_type": "builtins.str",
        },
        "response_key": "decision",
    }


def test_as_node_hashes_include_the_table_recipe(tmp_path) -> None:
    @node(output_name="normalized")
    def lower(text: str) -> str:
        return text.lower()

    @node(output_name="normalized")
    def upper(text: str) -> str:
        return text.upper()

    @node(output_name="decorated")
    def decorate(normalized: str) -> str:
        return f"[{normalized}]"

    lower_transform = lower.with_name("transform")
    upper_transform = upper.with_name("transform")
    lower_node = Graph([lower_transform]).as_table(
        identity="version_id",
        store=LanceDBStore(str(tmp_path / "lower")),
        runner=SyncRunner(),
    ).as_node()
    upper_node = Graph([upper_transform]).as_table(
        identity="version_id",
        store=LanceDBStore(str(tmp_path / "upper")),
        runner=SyncRunner(),
    ).as_node()
    deeper_node = Graph([lower_transform, decorate]).as_table(
        identity="version_id",
        store=LanceDBStore(str(tmp_path / "deeper")),
        runner=SyncRunner(),
    ).as_node()

    assert lower_node.definition_hash != upper_node.definition_hash
    assert lower_node.structural_signature != deeper_node.structural_signature


@pytest.mark.asyncio
async def test_as_node_propagates_inner_recipe_events_to_outer_processors(tmp_path) -> None:
    page_recipe = Graph([index_page], name="page_recipe")
    table = Graph(
        [split_pages, page_recipe.as_node().map_over("pages", identity="page_id")],
        name="protocol_recipe",
    ).as_table(
        identity="version_id", store=LanceDBStore(str(tmp_path / "table")), runner=AsyncRunner()
    )
    processor = ListProcessor()

    await AsyncRunner().run(
        Graph([table.as_node(name="materialize_protocol")]),
        version_id="v1",
        text="one|two",
        workflow_id="ingest-v1",
        event_processors=[processor],
    )

    assert [row["indexed_text"] for row in table.child("page").rows(parent="v1")] == ["ONE", "TWO"]
    starts = [event for event in processor.events if isinstance(event, NodeStartEvent)]
    assert any(event.node_name == "materialize_protocol" for event in starts)
    assert any(event.node_name == "split_pages" and event.graph_name == "protocol_recipe" for event in starts)
    assert any(event.node_name == "index_page" and event.graph_name == "page_recipe" for event in starts)
    materialization_start = next(event for event in starts if event.node_name == "materialize_protocol")
    inner_start = next(
        event
        for event in processor.events
        if isinstance(event, RunStartEvent) and event.graph_name == "protocol_recipe"
    )
    assert inner_start.parent_span_id == materialization_start.span_id
    assert inner_start.parent_workflow_id == "ingest-v1"
    assert processor.shutdown_count == 1
