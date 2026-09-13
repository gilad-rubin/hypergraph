"""Issue #405 — one LIVE run per subject, enforced at acceptance.

`workflow_id` names a SUBMISSION. `exclusive_key` names the SUBJECT the
submission is about — "review:doc-41" — and at most one live run may hold
one. Before this, "only one review of this document at a time" meant hosts
derived ids from the subject plus an ordinal and scanned the whole run list
before every submission: a full-table read AND a TOCTOU, because the scan
happened outside the acceptance transaction. Two doors minting in different
series could both be live for one subject.

The check now runs inside the `BEGIN IMMEDIATE` every submission already
opens, and a partial unique index over the live states is its backstop. A
collision ADOPTS — the caller gets the live run's receipt with
`duplicate=True`, exactly like `workflow_id` dedup — unless the values
differ, which is a typed conflict rather than a silent discard.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading

import pytest

from hypergraph import (
    Graph,
    RunHome,
    RunQuery,
    SyncRunner,
    WorkflowIdConflictError,
    node,
    serve,
)
from hypergraph.checkpointers._migrate import _SETTLED_SUBMISSION_STATE_VALUES
from hypergraph.host.views import SETTLED_SUBMISSION_STATES

pytest.importorskip("aiosqlite")


# === Helpers ===


def _graph(name: str = "review") -> Graph:
    @node(output_name="verdict")
    def decide(document_id: str) -> str:
        return f"reviewed {document_id}"

    return Graph([decide], name=name).with_runner(SyncRunner())


@contextlib.asynccontextmanager
async def _worker(host, worker_id: str = "w-405"):
    task = asyncio.create_task(host.work_forever(worker_id))
    try:
        yield task
    finally:
        host.shutdown()
        await asyncio.wait_for(task, timeout=25)


def _submission_rows(home: RunHome) -> list[tuple[str, str | None]]:
    db = home._sync_db()
    return [(str(row[0]), row[1]) for row in db.execute("SELECT workflow_id, exclusive_key FROM host_submissions ORDER BY workflow_id")]


# === Adoption: a second submit joins the live run instead of forking work ===


async def test_a_live_holder_is_adopted_and_the_receipt_names_it(home):
    graph = _graph()
    host = serve(graph, home=home, deployment_version="v1")

    first = await host.submit(graph, {"document_id": "doc-41"}, workflow_id="review-a", exclusive_key="review:doc-41")
    # A DIFFERENT door, minting from a different series — the case a
    # workflow_id convention cannot catch.
    second = await host.submit(graph, {"document_id": "doc-41"}, workflow_id="nightly-sweep-7", exclusive_key="review:doc-41")

    assert first.duplicate is False
    assert second.duplicate is True
    # The receipt points at the run that WON, not the id this call minted.
    assert second.workflow_id == "review-a"
    assert second.run_ref == first.run_ref
    # And nothing was written for the id the second caller brought.
    assert _submission_rows(home) == [("review-a", "review:doc-41")]


async def test_a_keyless_submission_is_never_adopted(home):
    """Only work that claims a subject is exclusive; everything else is not."""
    graph = _graph()
    host = serve(graph, home=home, deployment_version="v1")

    await host.submit(graph, {"document_id": "doc-41"}, workflow_id="keyed", exclusive_key="review:doc-41")
    plain = await host.submit(graph, {"document_id": "doc-41"}, workflow_id="keyless")

    assert plain.duplicate is False
    assert _submission_rows(home) == [("keyed", "review:doc-41"), ("keyless", None)]


async def test_differing_values_under_a_live_key_are_refused_by_name(home):
    """The failure path: adoption would discard what this caller asked for."""
    graph = _graph()
    host = serve(graph, home=home, deployment_version="v1")
    await host.submit(graph, {"document_id": "doc-41"}, workflow_id="review-a", exclusive_key="review:doc-41")

    with pytest.raises(WorkflowIdConflictError) as raised:
        await host.submit(graph, {"document_id": "doc-99"}, workflow_id="review-b", exclusive_key="review:doc-41")

    error = raised.value
    # It names the KEY, the run holding it, and which aspect differs — a
    # caller cannot act on "conflict" alone.
    assert "review:doc-41" in str(error)
    assert "review-a" in str(error)
    assert error.aspect == "inputs"
    assert error.workflow_id == "review-a"
    # Refused before any write: the holder is still the only row.
    assert _submission_rows(home) == [("review-a", "review:doc-41")]


async def test_an_empty_exclusive_key_is_refused_at_the_door(home):
    graph = _graph()
    host = serve(graph, home=home, deployment_version="v1")

    with pytest.raises(ValueError, match="non-empty string naming the subject"):
        await host.submit(graph, {"document_id": "doc-41"}, exclusive_key="   ")
    with pytest.raises(TypeError, match="exclusive_key must be a string"):
        await host.submit(graph, {"document_id": "doc-41"}, exclusive_key=7)
    assert _submission_rows(home) == []


# === Settlement frees the key ===


async def test_a_settled_subject_resubmitted_starts_a_new_run(home):
    """The constraint is one LIVE run per key, not one run ever."""
    graph = _graph()
    host = serve(graph, home=home, deployment_version="v1")

    first = await host.submit(graph, {"document_id": "doc-41"}, workflow_id="review-a", exclusive_key="review:doc-41")
    async with _worker(host):
        view = await host.client.follow(first.run_ref, deadline=25)
    assert view.status.value == "completed"
    assert home._get_submission_sync("review-a")["state"] in SETTLED_SUBMISSION_STATES

    # The key is free again: the next submission for this subject is NEW work.
    second = await host.submit(graph, {"document_id": "doc-41"}, workflow_id="review-b", exclusive_key="review:doc-41")
    assert second.duplicate is False
    assert second.workflow_id == "review-b"
    assert _submission_rows(home) == [("review-a", "review:doc-41"), ("review-b", "review:doc-41")]

    # Both are listed under the key, newest first, so the live holder leads.
    views = await host.client.list(RunQuery(key="review:doc-41"))
    assert [view.workflow_id for view in views] == ["review-b", "review-a"]


# === RunQuery(key=...) ===


async def test_run_query_key_returns_only_that_subjects_runs(home):
    graph = _graph()
    host = serve(graph, home=home, deployment_version="v1")
    await host.submit(graph, {"document_id": "doc-41"}, workflow_id="a", exclusive_key="review:doc-41")
    await host.submit(graph, {"document_id": "doc-99"}, workflow_id="b", exclusive_key="review:doc-99")
    await host.submit(graph, {"document_id": "doc-7"}, workflow_id="c")

    views = await host.client.list(RunQuery(key="review:doc-41"))
    assert [view.workflow_id for view in views] == ["a"]
    assert await host.client.list(RunQuery(key="review:nobody")) == []
    # The unkeyed listing still sees everything.
    assert {view.workflow_id for view in await host.client.list(RunQuery())} == {"a", "b", "c"}


async def test_the_key_filter_narrows_the_store_read_not_the_python_list(home):
    """#392 H5's first case: the index answers it, so the read is narrowed.

    A Python-side filter would still have read every submission row plus
    every bare Tier-0 run. Asking the Home directly is how that is visible:
    the keyed read returns ONE row where the unfiltered read returns four.
    """
    graph = _graph()
    host = serve(graph, home=home, deployment_version="v1")
    await host.submit(graph, {"document_id": "doc-41"}, workflow_id="a", exclusive_key="review:doc-41")
    await host.submit(graph, {"document_id": "doc-99"}, workflow_id="b", exclusive_key="review:doc-99")
    await host.submit(graph, {"document_id": "doc-7"}, workflow_id="c")
    # A Tier-0 run: a runs row with no submission, and therefore no key.
    SyncRunner().with_checkpointer(home).run(graph, document_id="doc-0", workflow_id="tier0")

    assert len(await home._list_run_rows()) == 4
    keyed = await home._list_run_rows(exclusive_key="review:doc-41")
    assert [row[0]["workflow_id"] for row in keyed] == ["a"]
    assert home._list_run_rows_sync(exclusive_key="review:doc-41") == keyed
    # A Tier-0 run can never hold a key, so the keyed read does not sweep for
    # it at all — while the unfiltered listing still reports it.
    assert [row[1].id for row in await home._list_run_rows() if row[0] is None] == ["tier0"]
    assert all(row[0] is not None for row in keyed)
    assert "tier0" not in {view.workflow_id for view in await host.client.list(RunQuery(key="review:doc-41"))}
    assert "tier0" in {view.workflow_id for view in await host.client.list(RunQuery())}


async def test_run_query_key_is_validated(home):
    host = serve(_graph(), home=home, deployment_version="v1")
    with pytest.raises(TypeError, match="RunQuery.key must be an exclusive_key string"):
        await host.client.list(RunQuery(key=7))
    with pytest.raises(ValueError, match="RunQuery.key must be a non-empty"):
        await host.client.list(RunQuery(key=" "))


# === The race two doors could actually lose ===


async def test_two_concurrent_submits_of_one_key_produce_one_live_run(tmp_path):
    """Two Homes, two connections, two threads released together.

    The pre-submit scan this replaces was a TOCTOU: both doors read "nobody
    holds it" and both wrote. Here each thread holds its own `RunHome` — its
    own sqlite connection, exactly as two worker processes would — and an
    `Event` releases them together, so the only thing serializing them is
    the acceptance transaction itself. Whichever commits first owns the key;
    the other adopts it and writes nothing.
    """
    uri = f"file:{tmp_path / 'runs.db'}"
    graph = _graph()
    homes = [RunHome.open(uri), RunHome.open(uri)]
    hosts = [serve(graph, home=home, deployment_version="v1") for home in homes]

    release = threading.Event()
    ready = threading.Barrier(len(hosts) + 1)
    receipts: dict[int, object] = {}
    failures: dict[int, BaseException] = {}

    def submit(index: int) -> None:
        ready.wait(timeout=10)
        release.wait(timeout=10)
        try:
            receipts[index] = hosts[index].submit_sync(
                graph,
                {"document_id": "doc-41"},
                workflow_id=f"door-{index}",
                exclusive_key="review:doc-41",
            )
        except BaseException as error:  # pragma: no cover - reported below
            failures[index] = error

    threads = [threading.Thread(target=submit, args=(index,)) for index in range(len(hosts))]
    for thread in threads:
        thread.start()
    try:
        ready.wait(timeout=10)
        release.set()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive()
        assert not failures, failures

        rows = _submission_rows(homes[0])
        assert len(rows) == 1, rows
        winner, key = rows[0]
        assert key == "review:doc-41"
        # Exactly one receipt says "I created this"; the other adopted it,
        # and BOTH name the same run.
        assert sorted(receipt.duplicate for receipt in receipts.values()) == [False, True]
        assert {receipt.workflow_id for receipt in receipts.values()} == {winner}
    finally:
        release.set()
        for open_home in homes:
            await open_home.close()


# === The backstop under the check ===


async def test_the_unique_index_refuses_a_second_live_holder(tmp_path):
    """A writer that got past the SELECT still cannot land a second holder."""
    home = RunHome.open(f"file:{tmp_path / 'runs.db'}")
    try:
        db = home._sync_db()
        insert = (
            "INSERT INTO host_submissions (workflow_id, definition_name, inputs_json, created_at, state, exclusive_key) "
            "VALUES (?, 'review', '{}', '2026-09-13T00:00:00+00:00', ?, 'review:doc-41')"
        )
        db.execute(insert, ("holder", "pending"))
        db.commit()

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert, ("intruder", "claimed"))
        db.rollback()

        # A settled holder is outside the index, so the key is free.
        db.execute("UPDATE host_submissions SET state = 'finished' WHERE workflow_id = 'holder'")
        db.execute(insert, ("next", "pending"))
        db.commit()
        assert [row[0] for row in _submission_rows(home)] == ["holder", "next"]
    finally:
        await home.close()


async def test_the_index_predicate_is_the_settled_vocabulary(tmp_path):
    """Adding a settled state must widen the index, not leave a stale list.

    `checkpointers/` cannot import `host/` — the dependency runs the other
    way — so the schema module restates the settled states rather than
    deriving them. THIS is what makes the restatement safe: the two spellings
    are pinned to each other here, and adding a settled state on one side
    only fails this test rather than silently leaving a live holder of a key
    that has actually settled.
    """
    assert set(_SETTLED_SUBMISSION_STATE_VALUES) == SETTLED_SUBMISSION_STATES

    home = RunHome.open(f"file:{tmp_path / 'runs.db'}")
    try:
        sql = home._sync_db().execute("SELECT sql FROM sqlite_master WHERE name = 'idx_host_submissions_exclusive'").fetchone()[0]
        expected = ", ".join(f"'{state}'" for state in sorted(SETTLED_SUBMISSION_STATES))
        assert f"state NOT IN ({expected})" in sql
        assert "exclusive_key IS NOT NULL" in sql
    finally:
        await home.close()


# === Batches keep their own identity ===


async def test_submit_batch_refuses_exclusive_key_by_name(home):
    """A Batch's exclusive identity is its required workflow_id (#346 owns the rest)."""
    graph = _graph()
    host = serve(graph, home=home, deployment_version="v1")

    with pytest.raises(TypeError, match="exclusive_key"):
        await host.submit_batch(
            graph,
            {"document_id": ["doc-41", "doc-99"]},
            map_over="document_id",
            identity="document_id",
            workflow_id="sweep-1",
            exclusive_key="sweep:corpus",
        )


# === Sync parity ===


async def test_sync_submit_adopts_and_lists_exactly_like_async(tmp_path):
    home = RunHome.open(f"file:{tmp_path / 'runs.db'}")
    try:
        graph = _graph()
        host = serve(graph, home=home, deployment_version="v1")
        first = host.submit_sync(graph, {"document_id": "doc-41"}, workflow_id="review-a", exclusive_key="review:doc-41")
        second = host.submit_sync(graph, {"document_id": "doc-41"}, workflow_id="review-b", exclusive_key="review:doc-41")

        assert (first.duplicate, second.duplicate) == (False, True)
        assert second.workflow_id == "review-a"
        assert [view.workflow_id for view in host.client.list_sync(RunQuery(key="review:doc-41"))] == ["review-a"]

        with pytest.raises(WorkflowIdConflictError):
            host.submit_sync(graph, {"document_id": "doc-99"}, workflow_id="review-c", exclusive_key="review:doc-41")
        assert _submission_rows(home) == [("review-a", "review:doc-41")]
    finally:
        await home.close()
