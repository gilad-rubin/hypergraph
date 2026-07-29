"""PRD 0030 contracts for typed Batch items and per-port exclusivity (#354)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from pydantic import BaseModel, Field, ValidationError

from hypergraph import AsyncRunner, Graph, RunQuery, node, serve
from hypergraph.checkpointers.types import StepRecord, StepStatus
from hypergraph.host.errors import ItemKeyError
from tests.test_host._batch_interrupt import batch_where, worker

pytestmark = pytest.mark.host_batch_interrupt


class IngestionItem(BaseModel):
    doc_id: str
    pdf_uri: str
    page_count: int = Field(gt=0)


def ingestion_graph(seen: list[tuple[str, str, int]]) -> Graph:
    @node(output_name="indexed")
    def ingest(doc_id: str, pdf_uri: str, page_count: int) -> str:
        seen.append((doc_id, pdf_uri, page_count))
        return doc_id

    return Graph([ingest], name="typed_ingestion").with_runner(AsyncRunner())


async def test_typed_items_unpack_validate_and_resubmit_by_identity(home) -> None:
    seen: list[tuple[str, str, int]] = []
    graph = ingestion_graph(seen)
    host = serve(graph, home=home, deployment_version="v1")
    items = [
        {"doc_id": "d1", "pdf_uri": "papers/d1.pdf", "page_count": 2},
        {"doc_id": "d2", "pdf_uri": "papers/d2.pdf", "page_count": 3},
    ]

    receipt = await host.submit_batch(
        graph,
        items,
        identity="doc_id",
        schema=IngestionItem,
        workflow_id="sweep-1",
    )
    duplicate = await host.submit_batch(
        graph,
        items,
        identity="doc_id",
        schema=IngestionItem,
        workflow_id="sweep-1",
    )

    assert receipt.duplicate is False
    assert duplicate.duplicate is True
    assert duplicate.batch_ref == receipt.batch_ref
    async with worker(host):
        final = await batch_where(host.client, receipt.batch_ref, lambda view: view.settled)
    assert list(final.items) == ["d1", "d2"]
    assert seen == [("d1", "papers/d1.pdf", 2), ("d2", "papers/d2.pdf", 3)]


def test_typed_items_sync_mirror_persists_the_same_exclusivity_contract(home) -> None:
    graph = ingestion_graph([])
    host = serve(graph, home=home, deployment_version="v1")

    receipt = host.submit_batch_sync(
        graph,
        [{"doc_id": "d1", "pdf_uri": "papers/d1.pdf", "page_count": 2}],
        identity="doc_id",
        schema=IngestionItem,
        exclusive_by="doc_id",
        workflow_id="sync-sweep",
    )

    child = home._get_submission_sync(f"{receipt.workflow_id}:d1")
    assert child is not None
    assert child["exclusive_by"] == "doc_id"
    assert child["exclusive_key"] == "d1"


@pytest.mark.parametrize(
    ("items", "match"),
    [
        ([{"pdf_uri": "papers/missing-id.pdf", "page_count": 1}], "doc_id"),
        ([{"doc_id": "d1", "pdf_uri": "papers/d1.pdf", "page_count": 0}], "page_count"),
        ([{"doc_id": "d1", "pdf_uri": "papers/d1.pdf", "page_count": 1, "surprise": True}], "surprise"),
    ],
)
async def test_typed_item_refusals_happen_before_persistence(home, items, match) -> None:
    graph = ingestion_graph([])
    host = serve(graph, home=home, deployment_version="v1")

    with pytest.raises((ValueError, TypeError, ValidationError), match=match):
        await host.submit_batch(
            graph,
            items,
            identity="doc_id",
            schema=IngestionItem,
            workflow_id="refused-sweep",
        )

    assert await host.client.list(RunQuery()) == []


async def test_exclusive_by_serializes_same_key_across_batches(home) -> None:
    started: asyncio.Queue[str] = asyncio.Queue()
    releases = {"sweep-a": asyncio.Event(), "sweep-b": asyncio.Event()}

    @node(output_name="indexed")
    async def materialize(doc_id: str, sweep: str) -> str:
        await started.put(sweep)
        await releases[sweep].wait()
        return doc_id

    graph = Graph([materialize], name="exclusive_ingestion").with_runner(AsyncRunner())
    host = serve(graph, home=home, deployment_version="v1")
    first = await host.submit_batch(
        graph,
        [{"doc_id": "canonical-7", "sweep": "sweep-a"}],
        identity="doc_id",
        exclusive_by="doc_id",
        workflow_id="sweep-a",
    )
    second = await host.submit_batch(
        graph,
        [{"doc_id": "canonical-7", "sweep": "sweep-b"}],
        identity="doc_id",
        exclusive_by="doc_id",
        workflow_id="sweep-b",
    )

    async with worker(host):
        assert await asyncio.wait_for(started.get(), timeout=10) == "sweep-a"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(started.get(), timeout=0.2)
        releases["sweep-a"].set()
        assert await asyncio.wait_for(started.get(), timeout=10) == "sweep-b"
        releases["sweep-b"].set()
        await batch_where(host.client, first.batch_ref, lambda view: view.settled)
        await batch_where(host.client, second.batch_ref, lambda view: view.settled)


async def test_exclusive_by_allows_different_keys_to_run_together(home) -> None:
    started: asyncio.Queue[str] = asyncio.Queue()
    release = asyncio.Event()

    @node(output_name="indexed")
    async def materialize(doc_id: str) -> str:
        await started.put(doc_id)
        await release.wait()
        return doc_id

    graph = Graph([materialize], name="parallel_ingestion").with_runner(AsyncRunner())
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await host.submit_batch(
        graph,
        [{"doc_id": "d1"}, {"doc_id": "d2"}],
        identity="doc_id",
        exclusive_by="doc_id",
        workflow_id="parallel-sweep",
    )

    async with worker(host):
        assert {await asyncio.wait_for(started.get(), 10), await asyncio.wait_for(started.get(), 10)} == {"d1", "d2"}
        release.set()
        await batch_where(host.client, receipt.batch_ref, lambda view: view.settled)


async def test_committed_port_change_transfers_exclusivity_before_next_step(home) -> None:
    @node(output_name="indexed")
    def materialize(doc_id: str) -> str:
        return doc_id

    graph = Graph([materialize], name="transfer_ingestion").with_runner(AsyncRunner())
    host = serve(graph, home=home, deployment_version="v1")
    canonical = await host.submit_batch(
        graph,
        [{"doc_id": "canonical"}],
        identity="doc_id",
        exclusive_by="doc_id",
        workflow_id="canonical-sweep",
    )
    alias = await host.submit_batch(
        graph,
        [{"doc_id": "alias"}],
        identity="doc_id",
        exclusive_by="doc_id",
        workflow_id="alias-sweep",
    )
    claimed = await home._claim_eligible(served=host._served_identities)
    claims = {row["workflow_id"]: row for row in claimed}
    canonical_id = f"{canonical.workflow_id}:canonical"
    alias_id = f"{alias.workflow_id}:alias"
    await home.create_run(canonical_id, graph_name=graph.name, inputs={"doc_id": "canonical"})
    await home.create_run(alias_id, graph_name=graph.name, inputs={"doc_id": "alias"})

    transfer = asyncio.create_task(
        home.save_step(
            StepRecord(
                run_id=alias_id,
                superstep=0,
                node_name="resolve_duplicate",
                index=0,
                status=StepStatus.COMPLETED,
                input_versions={},
                values={"doc_id": "canonical"},
                completed_at=datetime.now(timezone.utc),
            )
        )
    )
    await asyncio.sleep(0.1)
    assert transfer.done() is False
    blocked_row = await home._get_submission(alias_id)
    assert blocked_row is not None and blocked_row["exclusive_key"] == "canonical"

    await home._release_submission(canonical_id, claims[canonical_id]["claim_seq"])
    await asyncio.wait_for(transfer, timeout=10)
    alias_row = await home._get_submission(alias_id)
    assert alias_row is not None and alias_row["exclusive_key"] == "canonical"
    await home._release_submission(alias_id, claims[alias_id]["claim_seq"])
    rerun = await host.client.rerun(alias.batch_ref)
    rerun_row = await home._get_submission(f"{rerun.workflow_id}:alias")
    assert rerun_row is not None and rerun_row["exclusive_key"] == "canonical"


async def test_restart_reclaims_its_durable_exclusivity_owner(home) -> None:
    graph = ingestion_graph([])
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await host.submit_batch(
        graph,
        [{"doc_id": "d1", "pdf_uri": "papers/d1.pdf", "page_count": 2}],
        identity="doc_id",
        exclusive_by="doc_id",
        workflow_id="restart-sweep",
    )
    first_claim = await home._claim_eligible(served=host._served_identities)
    assert [row["workflow_id"] for row in first_claim] == [f"{receipt.workflow_id}:d1"]

    await home._restart_scan()
    reclaimed = await home._claim_eligible(served=host._served_identities)

    assert [row["workflow_id"] for row in reclaimed] == [f"{receipt.workflow_id}:d1"]
    await home._release_submission(reclaimed[0]["workflow_id"], reclaimed[0]["claim_seq"])


async def test_exclusivity_scope_crosses_served_definitions_for_the_same_port(home) -> None:
    first_graph = ingestion_graph([])

    @node(output_name="stored")
    def archive(doc_id: str) -> str:
        return doc_id

    second_graph = Graph([archive], name="archive_ingestion").with_runner(AsyncRunner())
    host = serve(first_graph, second_graph, home=home, deployment_version="v1")
    await host.submit_batch(
        first_graph,
        [{"doc_id": "d1", "pdf_uri": "papers/d1.pdf", "page_count": 1}],
        identity="doc_id",
        exclusive_by="doc_id",
        workflow_id="ingest-sweep",
    )
    await host.submit_batch(
        second_graph,
        [{"doc_id": "d1"}],
        identity="doc_id",
        exclusive_by="doc_id",
        workflow_id="archive-sweep",
    )

    claimed = await home._claim_eligible(served=host._served_identities)
    assert len(claimed) == 1
    assert claimed[0]["exclusive_key"] == "d1"
    await home._release_submission(claimed[0]["workflow_id"], claimed[0]["claim_seq"])


async def test_blocked_keys_do_not_hide_later_unrelated_work(home) -> None:
    graph = ingestion_graph([])
    host = serve(graph, home=home, deployment_version="v1")
    owner = await host.submit_batch(
        graph,
        [{"doc_id": "busy", "pdf_uri": "papers/owner.pdf", "page_count": 1}],
        identity="doc_id",
        exclusive_by="doc_id",
        workflow_id="owner",
    )
    owner_claim = (await home._claim_eligible(served=host._served_identities))[0]
    for index in range(16):
        await host.submit_batch(
            graph,
            [{"doc_id": "busy", "pdf_uri": f"papers/{index}.pdf", "page_count": 1}],
            identity="doc_id",
            exclusive_by="doc_id",
            workflow_id=f"blocked-{index:02d}",
        )
    free = await host.submit_batch(
        graph,
        [{"doc_id": "free", "pdf_uri": "papers/free.pdf", "page_count": 1}],
        identity="doc_id",
        exclusive_by="doc_id",
        workflow_id="free",
    )

    claimed = await home._claim_eligible(served=host._served_identities)
    assert [row["workflow_id"] for row in claimed] == [f"{free.workflow_id}:free"]
    await home._release_submission(claimed[0]["workflow_id"], claimed[0]["claim_seq"])
    await home._release_submission(owner_claim["workflow_id"], owner_claim["claim_seq"])
    assert owner.workflow_id == "owner"


async def test_invalid_runtime_remap_rolls_back_the_step_and_keeps_the_owned_key(home) -> None:
    graph = ingestion_graph([])
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await host.submit_batch(
        graph,
        [{"doc_id": "alias", "pdf_uri": "papers/alias.pdf", "page_count": 1}],
        identity="doc_id",
        exclusive_by="doc_id",
        workflow_id="invalid-remap",
    )
    claim = (await home._claim_eligible(served=host._served_identities))[0]
    run_id = f"{receipt.workflow_id}:alias"
    await home.create_run(run_id, graph_name=graph.name, inputs={"doc_id": "alias"})

    with pytest.raises(ItemKeyError):
        await home.save_step(
            StepRecord(
                run_id=run_id,
                superstep=0,
                node_name="resolve_duplicate",
                index=0,
                status=StepStatus.COMPLETED,
                input_versions={},
                values={"doc_id": None},
            )
        )

    checkpoint = await home.get_checkpoint(run_id)
    assert checkpoint is not None and checkpoint.steps == []
    row = await home._get_submission(run_id)
    assert row is not None and row["exclusive_key"] == "alias"
    await home._release_submission(run_id, claim["claim_seq"])


async def test_stop_before_execution_releases_the_key_for_the_next_batch(home) -> None:
    started: asyncio.Queue[str] = asyncio.Queue()

    @node(output_name="indexed")
    async def materialize(doc_id: str, sweep: str) -> str:
        await started.put(sweep)
        return doc_id

    graph = Graph([materialize], name="prestop_ingestion").with_runner(AsyncRunner())
    host = serve(graph, home=home, deployment_version="v1")
    stopped = await host.submit_batch(
        graph,
        [{"doc_id": "d1", "sweep": "stopped"}],
        identity="doc_id",
        exclusive_by="doc_id",
        workflow_id="stopped-sweep",
    )
    stopped_view = await host.client.get(stopped.batch_ref)
    await host.client.stop(stopped_view.items["d1"].run_ref)
    live = await host.submit_batch(
        graph,
        [{"doc_id": "d1", "sweep": "live"}],
        identity="doc_id",
        exclusive_by="doc_id",
        workflow_id="live-sweep",
    )

    async with worker(host):
        assert await asyncio.wait_for(started.get(), timeout=10) == "live"
        await batch_where(host.client, live.batch_ref, lambda view: view.settled)
