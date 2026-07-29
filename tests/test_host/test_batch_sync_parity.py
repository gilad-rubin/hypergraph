"""Issue #342 — the synchronous mirror of every durable-Batch verb.

Sync/async parity is a repository invariant, and settlement is the one
place where drift would leave an answered child parked forever on the
surface a synchronous operator process uses.

The graph itself stays async-served wherever a question is asked:
interrupts require an ``AsyncRunner`` (``SyncRunner`` refuses them
outright), so the synchronous half of a pause story is the *client and the
store*, never the runner — a blocking ops tool answering a question an
async worker asked.
"""

from __future__ import annotations

import asyncio

import pytest

from hypergraph import (
    RunHome,
    RunHomeClient,
    serve,
)
from tests.test_host._batch_interrupt import (
    batch_where,
    paused_items,
    submit_ids,
    worker,
)
from tests.test_host._ingestion_fixture import (
    answer_value,
    ingestion_graph,
    read_ledger,
)

aiosqlite = pytest.importorskip("aiosqlite")

pytestmark = pytest.mark.host_batch_interrupt


# === 1. Submission and views ===


class TestSyncSubmission:
    async def test_the_sync_mirror_submits_the_same_frozen_manifest(self, tmp_path, ledger):
        run_home = RunHome.open(f"file:{tmp_path / 'sync.db'}")
        graph = ingestion_graph("ingest", sync=True)
        host = serve(graph, home=run_home, deployment_version="v1")
        try:
            receipt = host.submit_batch_sync(
                graph,
                {"work_item_id": ["work-a", "work-dup-b"]},
                map_over="work_item_id",
                identity="work_item_id",
                workflow_id="drop-sync",
            )
            view = RunHomeClient(run_home).get_sync(receipt.batch_ref)
            assert list(view.items) == ["work-a", "work-dup-b"]
            assert view.counts["queued"] == 2 and view.counts["paused"] == 0
            assert host.submit_sync(graph, {"work_item_id": "work-solo"}).duplicate is False
        finally:
            await run_home.close()


# === 2. Answer settlement ===


class TestSyncAnswerSettlement:
    async def test_the_sync_mirror_answers_and_re_admits_the_same_way(self, tmp_path, ledger):
        """``answer_sync`` commits the SAME two halves as ``answer``."""
        run_home = RunHome.open(f"file:{tmp_path / 'sync-answer.db'}")
        graph = ingestion_graph("ingest")
        host = serve(graph, home=run_home, deployment_version="v1")
        client = host.client
        try:
            receipt = host.submit_batch_sync(
                graph,
                {"work_item_id": ["work-dup-1"]},
                map_over="work_item_id",
                identity="work_item_id",
                workflow_id="drop-sync-answer",
            )
            async with worker(host):
                view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
                item = view.items["work-dup-1"]

                slot = client.get_run_slot_sync(item.run_ref)
                settled = client.answer_sync(item.run_ref, pause_id=slot.pause_id, value=answer_value("replace_existing", 3143))
                assert settled.settled_at is not None

                # The same worker picks the child up again and finishes it.
                final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

            # One committed transaction moved BOTH halves, exactly as async.
            kinds = [kind for _seq, kind, _payload, _at in await run_home._read_batch_updates(receipt.batch_ref.batch_id)]
            assert kinds.count("child_runnable") == 1
            assert final.outcomes["work-dup-1"] == "completed"
            assert read_ledger(ledger) == ["replaced:work-dup-1:3143"]
        finally:
            await run_home.close()

    async def test_the_sync_store_mirror_parks_and_re_admits_identically(self, home, ledger):
        """Sync/async parity for both transitions, at the store."""
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-sync-parity")
        client = host.client

        async with worker(host) as task:
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            # The pause transition was written by the ASYNC mirror; read it
            # back through the SYNC one and settle through the sync answer.
            assert home._get_submission_sync(item.workflow_id)["state"] == "paused"
            host.shutdown()
            await asyncio.wait_for(task, timeout=25)

            slot = client.get_run_slot_sync(item.run_ref)
            client.answer_sync(item.run_ref, pause_id=slot.pause_id, value=answer_value("create_new"))

        assert home._get_submission_sync(item.workflow_id)["state"] == "pending"
        kinds = [k for _s, k, _p, _a in await home._read_batch_updates(receipt.batch_ref.batch_id)]
        assert kinds.count("child_runnable") == 1
