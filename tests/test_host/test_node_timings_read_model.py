"""Issue #386 — durable per-node cost, readable after the process that made it.

The facts were always there: ``steps.duration_ms``/``cached``/``error`` and
the Run's own ``duration_ms``/``node_count`` are committed while the work
runs. What was missing was a public way to read them. The only route was
``SqliteRunInspector``, which needs a ``SqliteCheckpointer`` — and therefore
a schema-ensuring WRITE on open — so an operator whose notebook kernel died
mid-sweep had no read-side answer for the work it had driven.

The fold also walks ``runs.parent_run_id``. A Host Run is only the outermost
run of the work it drove: nested graphs and every item of a ``map`` commit
their own runs rows, and a join matching only
``runs.id = host_submissions.workflow_id`` threw exactly that fan-out
evidence away.
"""

from __future__ import annotations

import json

import pytest

from hypergraph import AsyncRunner, Graph, RunHomeReadModel, node, serve
from tests.test_host._batch_interrupt import batch_where, submit_ids, worker
from tests.test_host._ingestion_fixture import ingestion_graph

pytest.importorskip("aiosqlite")


def fan_out_graph(name: str = "fanout") -> Graph:
    """One document that derives three pages through an inner map."""

    @node(output_name="derived")
    def derive_page(page: int) -> int:
        return page * 2

    @node(output_name="pages")
    def split_pages(work_item_id: str) -> list[int]:
        return [1, 2, 3]

    @node(output_name="outcome")
    def publish(derived: list) -> int:
        return sum(derived)

    fan = Graph([derive_page], name="page").as_node(name="derive_pages").map_over("page").rename_inputs(page="pages")
    return Graph([split_pages, fan, publish], name=name).with_runner(AsyncRunner())


async def settled_sweep(host, graph, ids, workflow_id):
    receipt = await submit_ids(host, graph, ids, workflow_id)
    async with worker(host):
        await batch_where(host.client, receipt.batch_ref, lambda view: view.settled)
    return receipt


async def test_per_node_cost_folds_every_execution_the_journal_recorded(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await settled_sweep(host, graph, ["work-clean", "work-two"], "sweep-cost")
    read = RunHomeReadModel(host.client)

    timings = await read.node_timings(definition=graph.name)

    assert timings.definition == graph.name
    assert {run.workflow_id for run in timings.runs} == {"sweep-cost:work-clean", "sweep-cost:work-two"}
    assert all(run.item_key in {"work-clean", "work-two"} for run in timings.runs)
    assert all(run.batch_id == receipt.batch_ref.batch_id for run in timings.runs)
    assert all(run.status == "completed" and run.duration_ms is not None and run.node_count > 0 for run in timings.runs)

    # Both Runs took the clean path, so every node on it ran exactly twice.
    by_name = {node_timing.node_name: node_timing for node_timing in timings.nodes}
    assert by_name["stage_candidate"].executions == 2
    assert by_name["keep_candidate"].executions == 2
    assert "wait_for_duplicate_review" not in by_name  # never reached: nothing to report
    assert all(timing.errors == 0 and timing.cached == 0 for timing in timings.nodes)
    assert all(timing.average_ms is not None and timing.total_seconds >= 0.0 for timing in timings.nodes)

    # Heaviest first — the question an operator brings to this table.
    assert [timing.total_seconds for timing in timings.nodes] == sorted((t.total_seconds for t in timings.nodes), reverse=True)
    json.dumps(timings.to_dict())


async def test_the_raw_step_records_let_a_caller_fold_per_document(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    await settled_sweep(host, graph, ["work-clean", "work-two"], "sweep-steps")
    read = RunHomeReadModel(host.client)

    timings = await read.node_timings()

    per_document: dict[str, float] = {}
    for step in timings.steps:
        per_document[step.item_key] = per_document.get(step.item_key, 0.0) + step.duration_ms
    assert set(per_document) == {"work-clean", "work-two"}

    # The steps ARE the aggregate's evidence: folding them the caller's way
    # must reach the library's number, or one of the two is lying.
    folded = sum(step.duration_ms for step in timings.steps) / 1000.0
    assert folded == pytest.approx(sum(timing.total_seconds for timing in timings.nodes))
    assert all(step.status == "completed" and step.error is None and not step.cached for step in timings.steps)
    assert all(step.completed_at is not None and step.superstep >= 0 for step in timings.steps)


async def test_a_failed_node_reports_its_error_count_not_a_silent_zero(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    await settled_sweep(host, graph, ["work-boom"], "sweep-failure")
    read = RunHomeReadModel(host.client)

    timings = await read.node_timings()

    staging = next(timing for timing in timings.nodes if timing.node_name == "stage_candidate")
    assert (staging.executions, staging.errors) == (1, 1)
    assert timings.runs[0].status == "failed"
    assert timings.runs[0].error_count == 1
    assert [step.error for step in timings.steps if step.node_name == "stage_candidate"] != [None]


async def test_a_fan_out_s_inner_nodes_are_folded_under_the_host_run_that_drove_them(home, ledger):
    """The evidence a `runs.id = host_submissions.workflow_id` join loses."""
    graph = fan_out_graph()
    host = serve(graph, home=home, deployment_version="v1")
    await settled_sweep(host, graph, ["doc-1"], "sweep-fanout")
    read = RunHomeReadModel(host.client)

    timings = await read.node_timings()

    by_name = {timing.node_name: timing for timing in timings.nodes}
    assert by_name["split_pages"].executions == 1
    # Three inner runs, one per page — invisible to the parent's own runs row.
    assert by_name["derive_page"].executions == 3

    inner = [step for step in timings.steps if step.node_name == "derive_page"]
    assert len({step.workflow_id for step in inner}) == 3
    assert {step.root_workflow_id for step in inner} == {"sweep-fanout:doc-1"}
    assert {step.item_key for step in inner} == {"doc-1"}
    assert {step.run_ref.home for step in timings.steps} == {home.uri}

    # The Run's own totals stay the Run's: wall time is not the sum of the
    # parts a fan-out ran concurrently, and both numbers are true.
    assert timings.runs[0].node_count == 3
    assert timings.runs[0].workflow_id == "sweep-fanout:doc-1"


async def test_the_selection_narrows_by_definition_batch_and_limit(home, ledger):
    graph = ingestion_graph()
    other = ingestion_graph(name="other-ingest")
    host = serve(graph, home=home, deployment_version="v1")
    other_host = serve(other, home=home, deployment_version="v1")
    first = await settled_sweep(host, graph, ["work-clean"], "sweep-one")
    await settled_sweep(host, graph, ["work-two"], "sweep-two")
    await settled_sweep(other_host, other, ["work-clean"], "sweep-other")
    read = RunHomeReadModel(host.client)

    assert {run.workflow_id for run in (await read.node_timings(definition="other-ingest")).runs} == {"sweep-other:work-clean"}
    assert {run.workflow_id for run in (await read.node_timings(batch=first.batch_ref)).runs} == {"sweep-one:work-clean"}
    assert {run.workflow_id for run in (await read.node_timings(batch=first.batch_ref.batch_id)).runs} == {"sweep-one:work-clean"}
    assert len((await read.node_timings(definition=graph.name, limit=1)).runs) == 1
    assert (await read.node_timings(definition="never-served")).nodes == ()

    with pytest.raises(ValueError, match="limit must be a positive int"):
        await read.node_timings(limit=0)
    with pytest.raises(TypeError, match="definition must be a Definition name string"):
        await read.node_timings(definition=object())
    with pytest.raises(TypeError, match="batch must be a BatchRef"):
        await read.node_timings(batch=object())


async def test_an_accepted_run_that_never_executed_reports_itself_honestly(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    await submit_ids(host, graph, ["work-never"], "sweep-unstarted")
    read = RunHomeReadModel(host.client)

    timings = await read.node_timings()

    assert [run.workflow_id for run in timings.runs] == ["sweep-unstarted:work-never"]
    assert timings.runs[0].status is None
    assert timings.runs[0].duration_ms is None
    assert (timings.runs[0].node_count, timings.runs[0].error_count) == (0, 0)
    # Nothing ran, so nothing is reported to have run — never a zero-cost node.
    assert timings.nodes == () and timings.steps == ()


def test_sync_timings_mirror_the_async_ones(home, ledger):
    graph = ingestion_graph(sync=True)
    host = serve(graph, home=home, deployment_version="v1")
    host.submit_batch_sync(
        graph,
        {"work_item_id": ["work-clean"]},
        map_over="work_item_id",
        identity="work_item_id",
        workflow_id="sweep-sync-timings",
    )
    read = RunHomeReadModel(host.client)

    timings = read.node_timings_sync(definition=graph.name)

    assert [run.workflow_id for run in timings.runs] == ["sweep-sync-timings:work-clean"]
    assert read.node_timings_sync(definition="never-served").runs == ()


def test_the_timing_read_writes_nothing_to_the_run_home(home, ledger):
    """The whole point: read durable cost WITHOUT becoming a writer of it."""
    graph = ingestion_graph(sync=True)
    host = serve(graph, home=home, deployment_version="v1")
    host.submit_batch_sync(
        graph,
        {"work_item_id": ["work-clean"]},
        map_over="work_item_id",
        identity="work_item_id",
        workflow_id="sweep-readonly-timings",
    )
    read = RunHomeReadModel(host.client)
    before = home._sync_db().total_changes

    read.node_timings_sync()
    read.node_timings_sync(definition=graph.name, limit=5)

    assert home._sync_db().total_changes == before
