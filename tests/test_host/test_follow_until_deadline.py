"""Issue #384 — "follow this until it stops, or fail loudly with what it saw".

Every consumer of a ref — a test, a notebook cell, a load simulation — wanted
the same twelve lines over `client.watch`: drain until the ref arrives, then
read the view, and raise with the last observed state if it took too long.
The repo's own suites carried it as `batch_where`, and the #382 adopter
carried it as a local `settled()`.

`client.follow(ref, until=..., deadline=...)` is that verb, and it is the
stream's wait, not a second poll loop: `until` is `watch`'s own two-value
vocabulary, the arrival rule is `watch`'s, and the deadline only decides how
long the caller is willing to hold the stream open. What it adds is the two
things a wait loop had to hand-roll: the final view as the RETURN value, and
a typed refusal carrying the last one when the deadline expires.
"""

from __future__ import annotations

import asyncio

import pytest

from hypergraph import BatchRef, FollowDeadlineExpired, HostError, RunRef, serve
from tests.test_host._batch_interrupt import answer_item, batch_where, paused_items, submit_ids, worker
from tests.test_host._ingestion_fixture import ingestion_graph

pytest.importorskip("aiosqlite")


async def test_follow_returns_the_settled_batch_view(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await submit_ids(host, graph, ["work-clean", "work-clean-2"], "drop-follow-settled")

    async with worker(host):
        view = await host.client.follow(receipt.batch_ref, deadline=25)

    # The thing a caller asserts on, not a stream of updates it has to fold.
    assert view.settled
    assert view.counts["completed"] == 2
    assert list(view.outcomes) == ["work-clean", "work-clean-2"]


async def test_follow_raises_at_its_deadline_with_the_last_view_attached(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await submit_ids(host, graph, ["work-dup-1", "work-clean"], "drop-follow-deadline")

    async with worker(host):
        await batch_where(host.client, receipt.batch_ref, lambda value: len(paused_items(value)) == 1 and value.counts["completed"] == 1)

        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(FollowDeadlineExpired) as raised:
            await host.client.follow(receipt.batch_ref, deadline=0.2)
        elapsed = loop.time() - started

    # It gives up at its own deadline — a gate never answers itself.
    assert 0.2 <= elapsed < 5.0
    error = raised.value
    assert isinstance(error, HostError)
    assert error.ref == receipt.batch_ref
    assert error.deadline == 0.2
    assert error.until == "settled"

    # "What was it doing when it ran out" — attached, and in the message.
    assert error.view.counts["paused"] == 1 and error.view.counts["completed"] == 1
    assert "1 completed, 1 paused of 2 items" in str(error)
    assert "until='resting'" in str(error)


async def test_follow_until_resting_returns_while_the_gate_is_open(home, ledger):
    """The #386 human-gate case: resting is reachable, settled is not."""
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await submit_ids(host, graph, ["work-dup-1", "work-clean"], "drop-follow-resting")

    async with worker(host):
        view = await host.client.follow(receipt.batch_ref, until="resting", deadline=25)

    # Nothing is running or queued, and nothing was invented: the parked
    # child is at rest, still not settled, still answerable.
    assert view.resting and not view.settled
    assert len(paused_items(view)) == 1
    assert view.counts["active"] == 0 and view.counts["queued"] == 0


async def test_follow_returns_the_run_view_for_a_run_ref(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await submit_ids(host, graph, ["work-dup-1", "work-clean"], "drop-follow-run")

    async with worker(host):
        batch = await batch_where(host.client, receipt.batch_ref, lambda value: len(paused_items(value)) == 1)

        clean = await host.client.follow(batch.items["work-clean"].run_ref, deadline=25)
        parked = await host.client.follow(batch.items["work-dup-1"].run_ref, until="resting", deadline=25)

    assert clean.status.value == "completed" and clean.waiting is None
    # Resting ended the WAIT, not the run: it is still parked on a person.
    assert parked.waiting.value == "paused"


async def test_answering_the_gate_lets_a_settled_follow_return(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-follow-answered")

    async with worker(host):
        parked = await host.client.follow(receipt.batch_ref, until="resting", deadline=25)
        await answer_item(host.client, parked.items["work-dup-1"], "create_new")
        view = await host.client.follow(receipt.batch_ref, deadline=25)

    assert not parked.settled
    assert view.settled and view.outcomes["work-dup-1"] == "completed"


async def test_follow_is_honest_about_a_ref_this_home_does_not_know(home):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")

    # Nothing to wait for, so nothing is waited for — the same honest None
    # get() returns, delivered immediately rather than after the deadline.
    assert await host.client.follow(BatchRef(home=home.uri, batch_id="never-submitted"), deadline=5) is None
    assert await host.client.follow(RunRef(home=home.uri, run_id="never-submitted"), deadline=5) is None


async def test_follow_refuses_a_third_arrival_and_a_non_ref(home):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await submit_ids(host, graph, ["work-clean"], "drop-follow-refusals")

    # The same closed vocabulary watch() enforces, named for THIS verb.
    with pytest.raises(ValueError, match=r"follow\(\) until must be 'settled' or 'resting'"):
        await host.client.follow(receipt.batch_ref, until="done")

    with pytest.raises(TypeError, match=r"follow\(\) expects a RunRef or BatchRef, got str"):
        await host.client.follow(receipt.batch_ref.batch_id)
