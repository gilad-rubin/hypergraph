"""Issue #355 — durable, page-denominated Host admission."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from hypergraph import Graph, RunHome, RunRef, SyncRunner, node, serve
from hypergraph.host.errors import WorkflowIdConflictError

aiosqlite = pytest.importorskip("aiosqlite")


@node(output_name="out")
def compute(doc_id: int, page_count: int, key: str = "") -> int:
    return doc_id + page_count


@pytest_asyncio.fixture
async def home(tmp_path):
    opened = RunHome.open(f"file:{tmp_path / 'runs.db'}")
    yield opened
    await opened.close()


def _host(home):
    graph = Graph([compute], name="pages").with_runner(SyncRunner())
    return serve(graph, home=home, deployment_version="v1"), graph


async def _submit(host, graph, costs, workflow_id="batch"):
    return await host.submit_batch(
        graph,
        [{"doc_id": index, "page_count": cost} for index, cost in enumerate(costs, 1)],
        identity="doc_id",
        admission_cost="page_count",
        workflow_id=workflow_id,
    )


async def _claim(home, host):
    return await home._claim_eligible(served=host._served_identities)


def _claimed_costs(home):
    return [
        row[0]
        for row in home._sync_db()
        .execute("SELECT admission_cost FROM host_submissions WHERE state = 'claimed' ORDER BY created_at, rowid")
        .fetchall()
    ]


class TestPageAdmissionContract:
    async def test_cost_field_config_is_pinned_for_dedup_and_batch_rerun(self, home):
        host, graph = _host(home)
        items = [{"doc_id": 1, "page_count": 20}]
        source = await _submit(host, graph, [20], workflow_id="rerun-cost")
        child = (await _claim(home, host))[0]
        await host._execute_submission(child)
        rerun = await host.client.rerun(source.batch_ref)

        batch = await home._get_batch(rerun.batch_ref.batch_id)
        assert batch["admission_cost"] == "page_count"
        assert home._get_submission_sync(f"{rerun.workflow_id}:1")["admission_cost"] == 20
        manifest = (
            home._sync_db()
            .execute(
                "SELECT payload FROM batch_updates WHERE batch_id = ? AND kind = 'manifest'",
                (rerun.batch_ref.batch_id,),
            )
            .fetchone()[0]
        )
        assert json.loads(manifest)["admission_cost"] == "page_count"

        await host.submit_batch(graph, items, identity="doc_id", workflow_id="dedup-cost")
        with pytest.raises(WorkflowIdConflictError, match="admission_cost differs"):
            await host.submit_batch(
                graph,
                items,
                identity="doc_id",
                admission_cost="page_count",
                workflow_id="dedup-cost",
            )

    async def test_cost_field_is_validated_once_and_persisted(self, home):
        host, graph = _host(home)
        home.max_admission_units = 64
        await _submit(host, graph, [3, 20])

        assert home.max_admission_units == 64
        assert [row[0] for row in home._sync_db().execute("SELECT admission_cost FROM host_submissions ORDER BY rowid")] == [3, 20]

        await host.submit_batch(
            graph,
            [{"doc_id": 3, "page_count": 99}],
            identity="doc_id",
            workflow_id="default-cost",
        )
        assert home._get_submission_sync("default-cost:3")["admission_cost"] == 1

        for bad in (0, -1, True, 1.5, "2"):
            with pytest.raises(ValueError, match="admission_cost"):
                await host.submit_batch(
                    graph,
                    [{"doc_id": 99, "page_count": bad}],
                    identity="doc_id",
                    admission_cost="page_count",
                    workflow_id=f"bad-{bad!r}",
                )

    async def test_normal_items_fit_the_page_budget(self, home):
        host, graph = _host(home)
        home.max_admission_units = 6
        await _submit(host, graph, [3, 3, 3])

        claimed = await _claim(home, host)

        assert [row["item_key"] for row in claimed] == ["1", "2"]
        assert _claimed_costs(home) == [3, 3]

    async def test_floor_admits_two_documents_even_when_the_second_crosses_budget(self, home):
        host, graph = _host(home)
        home.max_admission_units = 6
        await _submit(host, graph, [4, 4, 2])

        claimed = await _claim(home, host)

        assert [row["item_key"] for row in claimed] == ["1", "2"]
        assert _claimed_costs(home) == [4, 4]

    async def test_oversized_document_runs_alone_and_then_the_queue_continues(self, home):
        host, graph = _host(home)
        home.max_admission_units = 6
        await _submit(host, graph, [10, 2, 2])

        first = await _claim(home, host)
        assert [row["item_key"] for row in first] == ["1"]
        assert await _claim(home, host) == []

        await home._release_submission(first[0]["workflow_id"], first[0]["claim_seq"])
        assert [row["item_key"] for row in await _claim(home, host)] == ["2", "3"]

    async def test_pause_release_and_answer_reacquire_use_durable_claimed_rows(self, home):
        host, graph = _host(home)
        home.max_admission_units = 6
        home.max_active_runs = 1
        await _submit(host, graph, [6, 6, 1])
        first = (await _claim(home, host))[0]
        db = home._sync_db()
        db.execute("UPDATE host_submissions SET state = 'paused' WHERE workflow_id = ?", (first["workflow_id"],))
        db.commit()

        second = (await _claim(home, host))[0]
        assert second["item_key"] == "2"
        db.execute("UPDATE host_submissions SET state = 'pending' WHERE workflow_id = ?", (first["workflow_id"],))
        db.commit()
        assert await _claim(home, host) == []

        await home._release_submission(second["workflow_id"], second["claim_seq"])
        reacquired = await _claim(home, host)
        assert reacquired[0]["item_key"] == "1", "answered work returns through its original fair queue position"

    async def test_a_reopened_home_reconstructs_reservations_from_claimed_rows(self, tmp_path, home):
        host, graph = _host(home)
        home.max_admission_units = 6
        await _submit(host, graph, [4, 4, 2])
        assert len(await _claim(home, host)) == 2
        await home.close()

        reopened = RunHome.open(f"file:{tmp_path / 'runs.db'}")
        try:
            reopened_host, _ = _host(reopened)
            assert _claimed_costs(reopened) == [4, 4]
            assert await _claim(reopened, reopened_host) == []
            await reopened._reclaim_expired()
            reclaimed = await _claim(reopened, reopened_host)
            assert [row["admission_cost"] for row in reclaimed] == [4, 4]
            assert _claimed_costs(reopened) == [4, 4]
        finally:
            await reopened.close()

    async def test_run_rerun_preserves_a_weighted_batch_child_cost(self, home):
        host, graph = _host(home)
        await _submit(host, graph, [20], workflow_id="source")
        source = (await _claim(home, host))[0]
        await host._execute_submission(source)

        rerun = await host.client.rerun(RunRef(home=home.uri, run_id="source:1"))

        assert home._get_submission_sync(rerun.workflow_id)["admission_cost"] == 20

    async def test_sync_run_rerun_preserves_a_weighted_batch_child_cost(self, home):
        host, graph = _host(home)
        await _submit(host, graph, [20], workflow_id="source-sync")
        source = (await _claim(home, host))[0]
        await host._execute_submission(source)

        rerun = host.client.rerun_sync(RunRef(home=home.uri, run_id="source-sync:1"))

        assert home._get_submission_sync(rerun.workflow_id)["admission_cost"] == 20
