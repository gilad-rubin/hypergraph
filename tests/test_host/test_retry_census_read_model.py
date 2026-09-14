"""Issue #392 (H9) — the attempt ledger, readable as one column.

A node that retried BELOW the graph leaves its evidence in
``attempt_series``/``attempt_records`` and nowhere in the Run's status, so a
Run that looks merely slow reads exactly like one that has been failing and
retrying all along. The only public route to that evidence was the
checkpointer's per-series attempt API — one question per series — which is
the wrong shape for an operator's table: a sweep of a thousand Runs would
ask a thousand questions to render one column.

So ``retry_census`` is one statement per id window, and this file pins both
halves of that promise: the number it reports, and the fact that narrowing
it costs no extra questions.
"""

from __future__ import annotations

import pytest

from hypergraph import AsyncRunner, Graph, RetryPolicy, RunHomeClient, RunHomeReadModel, SyncRunner, node, serve
from tests.test_host._batch_interrupt import submit_ids, worker
from tests.test_host._ingestion_fixture import ingestion_graph

pytest.importorskip("aiosqlite")

#: Fast, deterministic retries: a constant sub-millisecond delay with no
#: jitter, so a suite asserting exact attempt numbers never races a backoff.
FLAKY_RETRY = RetryPolicy(max_attempts=4, retry_on=(ValueError,), initial_delay=0.001, backoff_multiplier=1.0, jitter="none")


def flaky_graph(name: str = "flaky", *, tries_needed: dict[str, int] | None = None, sync: bool = False) -> Graph:
    """One attempt-managed node that each item settles on a fixed try."""
    needed = tries_needed or {}
    seen: dict[str, int] = {}

    @node(output_name="outcome", retry=FLAKY_RETRY)
    def stage_candidate(work_item_id: str) -> str:
        seen[work_item_id] = seen.get(work_item_id, 0) + 1
        if seen[work_item_id] < needed.get(work_item_id, 1):
            raise ValueError(f"staging refused {work_item_id} on try {seen[work_item_id]}")
        return f"staged:{work_item_id}"

    return Graph([stage_candidate], name=name).with_runner(SyncRunner() if sync else AsyncRunner())


def fan_out_flaky_graph(name: str = "fanout-flaky") -> Graph:
    """A retry that happens inside a mapped INNER run, not the Host Run."""
    seen: dict[int, int] = {}

    @node(output_name="derived", retry=FLAKY_RETRY)
    def derive_page(page: int) -> int:
        seen[page] = seen.get(page, 0) + 1
        if page == 2 and seen[page] < 3:
            raise ValueError(f"page {page} refused on try {seen[page]}")
        return page * 2

    @node(output_name="pages")
    def split_pages(work_item_id: str) -> list[int]:
        return [1, 2]

    fan = Graph([derive_page], name="page").as_node(name="derive_pages").map_over("page").rename_inputs(page="pages")
    return Graph([split_pages, fan], name=name).with_runner(AsyncRunner())


async def settled_sweep(host, graph, ids, workflow_id):
    receipt = await submit_ids(host, graph, ids, workflow_id)
    async with worker(host):
        await host.client.follow(receipt.batch_ref, deadline=20)
    return receipt


async def test_the_census_reports_the_highest_attempt_each_run_reached(home, ledger):
    graph = flaky_graph(tries_needed={"work-flaky": 3})
    host = serve(graph, home=home, deployment_version="v1")
    await settled_sweep(host, graph, ["work-flaky", "work-clean"], "sweep-census")
    read = RunHomeReadModel(host.client)

    census = await read.retry_census()

    # Three tries for the flaky item, one for the item that settled first go.
    # Both are PRESENT: they were attempt-managed, and "managed, succeeded
    # immediately" is a different fact from "never opened a series".
    assert census == {"sweep-census:work-flaky": 3, "sweep-census:work-clean": 1}


async def test_run_ids_and_definition_each_narrow_the_census(home, ledger):
    graph = flaky_graph(tries_needed={"work-flaky": 2})
    other = flaky_graph(name="other-flaky", tries_needed={"work-flaky": 3})
    host = serve(graph, home=home, deployment_version="v1")
    other_host = serve(other, home=home, deployment_version="v1")
    await settled_sweep(host, graph, ["work-flaky"], "sweep-one")
    await settled_sweep(other_host, other, ["work-flaky"], "sweep-other")
    read = RunHomeReadModel(host.client)

    assert await read.retry_census() == {"sweep-one:work-flaky": 2, "sweep-other:work-flaky": 3}
    assert await read.retry_census(["sweep-one:work-flaky"]) == {"sweep-one:work-flaky": 2}
    assert await read.retry_census(definition="other-flaky") == {"sweep-other:work-flaky": 3}
    assert await read.retry_census(["sweep-one:work-flaky"], definition="other-flaky") == {}
    assert await read.retry_census(["never-submitted"]) == {}
    # An EMPTY selection asks about no Run, and is answered as such — never
    # widened back out to the whole Home.
    assert await read.retry_census([]) == {}

    with pytest.raises(TypeError, match="definition must be a Definition name string"):
        await read.retry_census(definition=object())
    with pytest.raises(TypeError, match="run_ids must be a sequence of run id strings"):
        await read.retry_census("sweep-one:work-flaky")
    with pytest.raises(TypeError, match="run_ids must contain run id strings"):
        await read.retry_census([object()])


async def test_a_home_whose_nodes_never_retried_answers_an_empty_census(home, ledger):
    """No attempt-managed node anywhere: an empty ledger, not a fabricated 1."""
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    await settled_sweep(host, graph, ["work-clean"], "sweep-unmanaged")
    read = RunHomeReadModel(host.client)

    assert await read.retry_census() == {}
    assert await read.retry_census(["sweep-unmanaged:work-clean"]) == {}


async def test_a_nested_run_s_retries_answer_under_the_run_that_executed_them(home, ledger):
    """A page's fourth try is the PAGE's, never silently the document's."""
    graph = fan_out_flaky_graph()
    host = serve(graph, home=home, deployment_version="v1")
    await settled_sweep(host, graph, ["doc-1"], "sweep-fanout")
    read = RunHomeReadModel(host.client)

    census = await read.retry_census()

    assert "sweep-fanout:doc-1" not in census  # the outer run retried nothing
    assert sorted(census.values()) == [1, 3]  # one page clean, one on its third try
    # Naming the Definition selects Host Runs, and no Host Run holds these.
    assert await read.retry_census(definition=graph.name) == {}


def sync_runs(home, graph, tries: dict[str, int]) -> list[str]:
    """Drive the flaky graph straight through SyncRunner into this Home."""
    runner = SyncRunner(checkpointer=home)
    for item in tries:
        runner.run(graph, {"work_item_id": item}, workflow_id=item)
    return list(tries)


def test_sync_census_mirrors_the_async_one_and_asks_one_question_per_window(home, ledger):
    tries = {"work-flaky": 4, "work-clean": 1}
    run_ids = sync_runs(home, flaky_graph(tries_needed=tries, sync=True), tries)
    read = RunHomeReadModel(RunHomeClient(home))

    assert read.retry_census_sync() == tries
    assert read.retry_census_sync(run_ids) == tries
    assert read.retry_census_sync(["work-flaky"]) == {"work-flaky": 4}

    # THE cost promise: narrowing by run id is one statement for the window,
    # never one per Run. A census that walked the selection would show two.
    statements: list[str] = []
    connection = home._sync_db()
    connection.set_trace_callback(statements.append)
    try:
        read.retry_census_sync(run_ids)
    finally:
        connection.set_trace_callback(None)
    assert len(statements) == 1


def test_the_census_writes_nothing_to_the_run_home(home, ledger):
    """A read model reads: an operator surface never migrates a live store."""
    tries = {"work-flaky": 2}
    sync_runs(home, flaky_graph(tries_needed=tries, sync=True), tries)
    read = RunHomeReadModel(RunHomeClient(home))
    before = home._sync_db().total_changes

    read.retry_census_sync()
    read.retry_census_sync(["work-flaky"], definition="flaky")

    assert home._sync_db().total_changes == before
