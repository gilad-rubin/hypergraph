"""The durable submission watch: read-model fold, resting stop, console frame.

Issue #392: a notebook that called ``Host.submit`` or ``Host.submit_batch``
watches its work live through ``hypergraph.host.watch`` — folded from Run
Home read models (the executing worker may be another process), bounded
rendering, and the resting stop rule (a parked item ends the watch; settling
it would wait on a person).
"""

from __future__ import annotations

import asyncio

import pytest

from hypergraph import AsyncRunner, Graph, RetryPolicy, node, serve
from hypergraph.host.watch import (
    ConsolePanel,
    LogPanel,
    SubmissionWatcher,
    render_snapshot,
    snapshot_line,
    watch_snapshot,
    watch_submissions,
)
from tests.test_host._batch_interrupt import worker
from tests.test_host._ingestion_fixture import ingestion_graph

pytest.importorskip("aiosqlite")


async def _submit(host, graph, ids, workflow_id):
    return await host.submit_batch(
        graph,
        {"work_item_id": list(ids)},
        map_over="work_item_id",
        identity="work_item_id",
        workflow_id=workflow_id,
    )


# ---------------------------------------------------------------------------
# submit: ONE durable Run is watched in the same console as a whole Batch
# ---------------------------------------------------------------------------


async def test_a_lone_submitted_run_is_watched_in_the_same_console(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await host.submit(graph, {"work_item_id": "work-clean"}, workflow_id="wc-one")

    async with worker(host):
        snapshot = await watch_submissions(host.client, [receipt.run_ref], refresh_seconds=0.05, stop_after_minutes=1.0)

    assert snapshot.resting and snapshot.total == 1 and snapshot.done == 1
    [one] = snapshot.submissions
    assert not one.is_batch and one.ref == receipt.run_ref
    assert one.definition == "ingest"
    [item] = one.items
    assert item.item_key == "wc-one" and item.settled and item.status == "completed"
    # The run's census speaks the Batch bucket vocabulary, so one picture
    # can merge a lone Run with a manifest without renaming anything.
    assert one.counts == {"completed": 1}

    html = render_snapshot(snapshot, uid="hgwone")
    assert "Hypergraph durable submissions" in html and "1 durable submission" in html


async def test_a_lone_run_parked_on_a_gate_rests_and_names_its_question(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await host.submit(graph, {"work_item_id": "work-dup-1"}, workflow_id="wc-one-park")

    async with worker(host):
        snapshot = await watch_submissions(host.client, [receipt.run_ref], refresh_seconds=0.05, stop_after_minutes=1.0)

    # Resting, not settled: the answer is a person's to give.
    assert snapshot.resting
    [one] = snapshot.submissions
    assert not one.settled and len(one.parked) == 1
    assert one.counts == {"paused": 1}
    [trouble] = snapshot.exceptions
    assert trouble.kind == "wait_for_duplicate_review"
    assert "duplicate" in trouble.item.question.lower()


async def test_run_and_batch_refs_mix_in_one_picture_and_receipts_are_accepted(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    batch = await _submit(host, graph, ["work-clean", "work-clean-2"], "wc-mixed-batch")
    single = await host.submit(graph, {"work_item_id": "work-clean-3"}, workflow_id="wc-mixed-run")

    async with worker(host):
        # The receipts themselves, not the refs: what a notebook actually holds.
        snapshot = await watch_submissions(host.client, [batch, single], refresh_seconds=0.05, stop_after_minutes=1.0)

    assert len(snapshot.submissions) == 2
    assert [one.is_batch for one in snapshot.submissions] == [True, False]
    assert snapshot.total == 3 and snapshot.done == 3 and snapshot.failed == 0
    assert "2 durable submissions" in render_snapshot(snapshot, uid="hgwmix")


async def test_an_unknown_ref_is_left_out_rather_than_invented(home, ledger):
    from dataclasses import replace

    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await host.submit(graph, {"work_item_id": "work-clean"}, workflow_id="wc-ghost")
    ghost = replace(receipt.run_ref, run_id="never-submitted")

    # A ref this Home never accepted contributes nothing — and an unread
    # submission is UNKNOWN, never "resting", so the watch keeps asking.
    snapshot = await watch_submissions(host.client, [ghost], refresh_seconds=0.01, stop_after_minutes=0.0)
    assert snapshot.submissions == [] and not snapshot.resting


async def test_the_fold_reads_census_conditions_failures_and_gates(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await _submit(host, graph, ["work-clean", "work-dup-1", "work-boom"], "wc-fold")

    watcher = SubmissionWatcher(host.client)
    async with worker(host):
        snapshot = await watch_submissions(watcher, [receipt.batch_ref], refresh_seconds=0.05, stop_after_minutes=1.0)

    assert snapshot.resting and not snapshot.submitting
    assert snapshot.total == 3
    [batch] = snapshot.submissions
    assert batch.definition == "ingest"
    by_key = {item.item_key: item for item in batch.items}

    clean = by_key["work-clean"]
    assert clean.settled and clean.status == "completed" and not clean.error

    parked = by_key["work-dup-1"]
    assert parked.parked and not parked.settled
    assert parked.gate == "wait_for_duplicate_review"
    assert "duplicate" in parked.question.lower()

    failed = by_key["work-boom"]
    assert failed.settled and failed.status in {"failed", "partial"}
    assert failed.error  # durable failure evidence, not a guess

    kinds = {trouble.kind for trouble in snapshot.exceptions}
    assert "failed" in kinds and "wait_for_duplicate_review" in kinds


async def test_resting_ends_the_watch_while_a_question_is_still_open(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await _submit(host, graph, ["work-dup-1"], "wc-resting")

    async with worker(host):
        snapshot = await watch_submissions(host.client, [receipt.batch_ref], refresh_seconds=0.05, stop_after_minutes=1.0)

    # The watch returned — but nothing is settled: the item rests on a person.
    assert snapshot.resting
    [batch] = snapshot.submissions
    assert not batch.settled and len(batch.parked) == 1


async def test_the_draw_hook_sees_the_picture_and_the_last_frame_is_resting(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await _submit(host, graph, ["work-clean", "work-clean-2"], "wc-draw")

    frames = []
    async with worker(host):
        snapshot = await watch_submissions(
            host.client,
            [receipt.batch_ref],
            refresh_seconds=0.05,
            stop_after_minutes=1.0,
            draw=frames.append,
        )

    assert frames, "the draw hook never saw a frame"
    assert frames[-1].resting == snapshot.resting
    assert snapshot.done == 2 and snapshot.failed == 0


async def test_refs_are_read_live_while_the_caller_is_still_submitting(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")

    refs: list = []
    submitted = asyncio.Event()

    async def submit_two() -> None:
        first = await _submit(host, graph, ["work-clean"], "wc-live-1")
        refs.append(first.batch_ref)
        second = await _submit(host, graph, ["work-clean-b"], "wc-live-2")
        refs.append(second.batch_ref)
        submitted.set()

    async with worker(host):
        submitter = asyncio.create_task(submit_two())
        snapshot = await watch_submissions(
            host.client,
            refs,
            refresh_seconds=0.05,
            stop_after_minutes=1.0,
            until_submitted=submitted,
        )
        await submitter

    assert len(snapshot.submissions) == 2 and snapshot.done == 2


async def test_the_retry_census_tells_a_retried_item_from_a_slow_one(home, ledger):
    attempts = {"count": 0}

    @node(output_name="value", retry=RetryPolicy(max_attempts=3, retry_on=(ValueError,), initial_delay=0.01))
    async def flaky(work_item_id: str) -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ValueError("first try refused")
        return work_item_id

    graph = Graph([flaky], name="flaky-set").with_runner(AsyncRunner())
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await _submit(host, graph, ["only-item"], "wc-retry")

    async with worker(host):
        snapshot = await watch_submissions(host.client, [receipt.batch_ref], refresh_seconds=0.05, stop_after_minutes=1.0)

    [batch] = snapshot.submissions
    [item] = batch.items
    assert item.settled and item.status == "completed"
    assert item.retries == 2  # the durable attempt ledger, not a guess
    assert snapshot.retried == 1
    assert any(trouble.kind == "retried" for trouble in snapshot.exceptions)


async def test_the_console_frame_is_bounded_and_names_the_truth(home, ledger):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await _submit(host, graph, ["work-clean", "work-dup-1", "work-boom"], "wc-frame")

    async with worker(host):
        snapshot = await watch_submissions(host.client, [receipt.batch_ref], refresh_seconds=0.05, stop_after_minutes=1.0)

    html = render_snapshot(snapshot, uid="hgwtest", elapsed_s=4.2)
    assert "Hypergraph durable submissions" in html
    # Same theme mechanism as the event console: hypergraph's own wrapper,
    # never a second detector and never a hardcoded palette.
    assert "color-scheme:light dark" in html and "data-vscode-theme-kind" in html
    assert html.count("light-dark(") >= 20
    assert "ingest" in html
    assert "Needs attention" in html and "work-boom" in html
    assert "parked" in html
    assert "\x00" not in html
    assert len(html.encode()) <= 50_000

    line = snapshot_line(snapshot)
    assert "1 submission(s)" in line and "needs attention" in line


async def test_the_console_panel_keeps_one_display_handle(home, ledger):
    from unittest.mock import MagicMock, patch

    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await _submit(host, graph, ["work-clean"], "wc-panel")

    handle = MagicMock()
    with patch("IPython.display.display", return_value=handle) as display:
        panel = ConsolePanel()
        async with worker(host):
            await watch_submissions(
                host.client,
                [receipt.batch_ref],
                refresh_seconds=0.05,
                stop_after_minutes=1.0,
                draw=panel,
            )

    assert display.call_count == 1  # ONE handle; every later frame updates it
    assert panel.frames >= 1
    if handle.update.called:
        assert "Hypergraph durable submissions" in handle.update.call_args.args[0].data


async def test_watch_snapshot_is_one_read_and_log_panel_throttles(home, ledger, caplog):
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await _submit(host, graph, ["work-clean"], "wc-line")

    async with worker(host):
        snapshot = await watch_submissions(host.client, [receipt.batch_ref], refresh_seconds=0.05, stop_after_minutes=1.0)

    one = await watch_snapshot(host.client, [receipt.batch_ref])
    assert one.done == snapshot.done == 1

    panel = LogPanel(every_seconds=0.0)
    with caplog.at_level("INFO", logger="hypergraph.host.watch"):
        panel(one)
    assert any("1/1 settled" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# two things the frame must never do: trust a label, or invent an answer
# ---------------------------------------------------------------------------


async def test_an_item_key_carrying_markup_cannot_break_the_frame(home, ledger):
    """Item keys are caller data — a Batch may legitimately be keyed by them."""
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    # A key that FAILS, so the frame has to print it in Needs attention.
    # (No "/" — an item key containing one never gets claimed; see the
    # separate finding. The escaping question is about "<", ">" and "&".)
    receipt = await _submit(host, graph, ["<img onerror=alert(1)> & co -boom", "work-clean"], "wc-esc")

    async with worker(host):
        snapshot = await watch_submissions(host.client, [receipt.batch_ref], refresh_seconds=0.05, stop_after_minutes=1.0)

    html = render_snapshot(snapshot, uid="hgwesc", item_label="<b>cases</b>")
    assert "<img onerror=alert(1)>" not in html
    assert "<b>cases</b>" not in html
    assert "&lt;img onerror=alert(1)&gt; &amp; co" in html
    assert "&lt;b&gt;cases&lt;/b&gt;" in html


async def test_a_batch_the_home_cannot_read_yet_is_polled_not_declared_done(home, ledger):
    """Unreadable is UNKNOWN. A submitted Batch whose rows are not visible
    yet must keep the watch waiting — declaring it settled would report a
    finished run for work that had not started.
    """
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await _submit(host, graph, ["work-clean"], "wc-unreadable")

    class SlowToAppear(SubmissionWatcher):
        """The Run Home answers None for the first few polls, then the truth."""

        def __init__(self, client):
            super().__init__(client)
            self.polls = 0

        async def progress(self, ref):
            self.polls += 1
            if self.polls <= 3:
                return None
            return await super().progress(ref)

    watcher = SlowToAppear(host.client)
    async with worker(host):
        snapshot = await watch_submissions(watcher, [receipt.batch_ref], refresh_seconds=0.05, stop_after_minutes=1.0)

    # It kept polling past the unreadable window rather than returning an
    # empty (and therefore "resting"-looking) picture.
    assert watcher.polls > 3
    assert snapshot.resting and snapshot.total == 1 and snapshot.done == 1


def test_an_empty_picture_is_never_resting() -> None:
    """The predicate itself: nothing read is unknown, not finished."""
    from hypergraph.host.watch import WatchSnapshot

    assert not WatchSnapshot().resting
