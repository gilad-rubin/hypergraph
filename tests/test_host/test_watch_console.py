"""The durable submission watch: read-model fold, resting stop, console frame.

Issue #392: a notebook that called ``Host.submit`` or ``Host.submit_batch``
watches its work live through ``hypergraph.host.watch`` — folded from Run
Home read models (the executing worker may be another process), bounded
rendering, and the resting stop rule (a parked item ends the watch; settling
it would wait on a person).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from hypergraph import AsyncRunner, Graph, RetryPolicy, node, serve
from hypergraph.host.read_models import RunHomeReadModel
from hypergraph.host.refs import BatchRef
from hypergraph.host.watch import (
    ConsolePanel,
    ItemProgress,
    LogPanel,
    SubmissionProgress,
    SubmissionWatcher,
    WatchSnapshot,
    render_snapshot,
    snapshot_line,
    watch_snapshot,
    watch_submissions,
)
from tests.test_host._batch_interrupt import answer_item, until, worker
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


# ---------------------------------------------------------------------------
# the pace: settled work per second of wall clock, and what is left at it
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def _item(
    key: str,
    status: str,
    *,
    start: float | None = 0.0,
    elapsed: float = 0.0,
    gate: str = "",
    ever_parked: bool | None = None,
) -> ItemProgress:
    """One item with the instants the Run Home would have read for it.

    ``ever_parked`` defaults to "whatever the status says now", which is
    what the Run Home reports for an item nobody has answered yet; pass it
    explicitly for the one case the status can no longer tell you — an item
    a person has already answered and which then settled.
    """
    started = None if start is None else _T0 + timedelta(seconds=start)
    settled = started + timedelta(seconds=elapsed) if started is not None and status in {"completed", "failed", "partial"} else None
    return ItemProgress(
        item_key=key,
        workflow_id=key,
        word=status,
        status=status,
        elapsed=elapsed,
        retries=1,
        error="",
        question="",
        gate=gate,
        started_at=started,
        settled_at=settled,
        ever_parked=(status == "paused") if ever_parked is None else ever_parked,
    )


def _batch(items: list[ItemProgress]) -> SubmissionProgress:
    return SubmissionProgress(ref=BatchRef(batch_id="b", home="h"), definition="ingest", settled=False, counts={}, items=items)


def test_five_of_ten_items_settled_in_five_seconds_is_one_per_second() -> None:
    """The arithmetic every consumer was writing itself, done once.

    Ten items, five of them settled across a five-second window: one item
    per second, and five still to go — so five seconds left.
    """
    settled = [_item(f"done-{n}", "completed", start=0.0, elapsed=5.0) for n in range(5)]
    queued = [_item(f"queued-{n}", "unstarted", start=None) for n in range(5)]
    batch = _batch(settled + queued)

    assert batch.total == 10 and batch.done == 5
    assert batch.rate == 1.0
    assert batch.eta_seconds == 5.0
    # The same fold survives being merged into the multi-submission picture.
    snapshot = WatchSnapshot(submissions=[batch])
    assert snapshot.rate == 1.0 and snapshot.eta_seconds == 5.0


def test_the_rate_is_wall_clock_not_one_over_the_median_item() -> None:
    """Five items that each took 5s but ran four-up are not 0.2 items/s.

    ``1 / median_seconds()`` ignores how many children the admission cap
    runs at once and is wrong by exactly that factor.
    """
    batch = _batch([_item(f"done-{n}", "completed", start=0.0, elapsed=5.0) for n in range(4)] + [_item("late", "completed", start=5.0, elapsed=5.0)])

    assert batch.median_seconds() == 5.0  # each item took five seconds
    assert batch.rate == 0.5  # but five settled in ten seconds of wall clock
    assert batch.eta_seconds == 0.0  # nothing left that can move on its own


def test_a_parked_item_neither_lengthens_the_eta_nor_stalls_the_rate() -> None:
    """A human thinking is not throughput, and not remaining work either."""
    settled = [_item(f"done-{n}", "completed", start=0.0, elapsed=5.0) for n in range(5)]
    queued = [_item(f"queued-{n}", "unstarted", start=None) for n in range(4)]

    thinking = _batch(settled + queued + [_item("gate", "paused", start=0.0, elapsed=5.0, gate="review")])
    still_thinking = _batch(settled + queued + [_item("gate", "paused", start=0.0, elapsed=5_000.0, gate="review")])

    # Four items remain that can move on their own; the parked one is not
    # one of them, so the ETA is 4s rather than the 5s it would be if a
    # person's open question counted as remaining work.
    assert thinking.rate == 1.0 and thinking.eta_seconds == 4.0
    # And an hour and a half of silence on that gate changes NEITHER number.
    assert still_thinking.rate == thinking.rate
    assert still_thinking.eta_seconds == thinking.eta_seconds
    assert len(thinking.parked) == 1  # it stays its own number


def test_answering_a_gate_does_not_drop_the_reported_rate() -> None:
    """The same lie, one step later: a person ANSWERS, and the pace tanks.

    Excluding an item only while its gate is open is not enough. The moment
    somebody answers, that item settles with a timestamp however many hours
    past where the machine actually is — and if it is allowed back into the
    span, the rate collapses in the RESTING frame a saved notebook keeps.
    So "ever parked" is the test, not "parked right now".
    """
    # 100 items the machine moved by itself: one every 10s, last at 1000s.
    machine = [_item(f"done-{n}", "completed", start=n * 10.0, elapsed=10.0) for n in range(100)]
    gated = "review"

    open_gate = _batch([*machine, _item("gate", "paused", start=0.0, elapsed=1000.0, gate=gated)])
    # A person answers 8 hours in; the item then settles at t=30000s.
    answered = _batch([*machine, _item("gate", "completed", start=0.0, elapsed=30_000.0, ever_parked=True)])

    machine_pace = 100 / 1000.0  # 0.1 items/s — 360 items/h
    assert open_gate.rate == machine_pace
    # THE repair: answering changes nothing about the machine's pace.
    assert answered.rate == machine_pace
    assert answered.rate == open_gate.rate
    # Had the answered item re-entered the span it would have read 101
    # settled over 30,000s — 12.1 items/h against a true 360.
    assert answered.rate * 3600 == 360.0
    assert round((101 / 30_000.0) * 3600, 1) == 12.1  # what the console used to say

    # The answered item is no longer remaining work either — it settled.
    assert answered.done == 101 and answered.eta_seconds == 0.0
    # While the gate was open it was at rest, so it was not remaining work
    # and the 100 settled items left nothing to wait for.
    assert open_gate.eta_seconds == 0.0


def test_a_submission_every_item_of_which_was_gated_reports_no_pace() -> None:
    """Nothing the machine moved alone means no machine pace to report."""
    batch = _batch([_item(f"gated-{n}", "completed", start=0.0, elapsed=90.0, ever_parked=True) for n in range(5)])

    assert batch.done == 5  # they really did settle
    assert batch.rate is None and batch.eta_seconds is None  # but not on their own


def test_nothing_settled_yet_reports_none_and_the_console_prints_no_pace() -> None:
    """The honest failure path: no number beats a made-up one.

    Under the threshold both properties are ``None`` and BOTH renderers
    leave the pace out — never "∞ items/h".
    """
    running = _batch([_item(f"live-{n}", "running", start=0.0, elapsed=3.0) for n in range(3)])
    assert running.rate is None and running.eta_seconds is None

    # A settled item whose whole window has no width is just as unreportable.
    instant = _batch([_item("done", "completed", start=0.0, elapsed=0.0)])
    assert instant.rate is None and instant.eta_seconds is None

    snapshot = WatchSnapshot(submissions=[running])
    assert snapshot.rate is None and snapshot.eta_seconds is None
    assert "items/h" not in render_snapshot(snapshot, uid="hgwnone")
    assert "items/h" not in snapshot_line(snapshot)
    assert "left" not in snapshot_line(snapshot)

    # Once one settles over a real window, both renderers say the pace.
    moving = WatchSnapshot(
        submissions=[_batch([_item("done", "completed", start=0.0, elapsed=2.0), _item("live", "running", start=0.0, elapsed=2.0)])]
    )
    assert moving.rate == 0.5
    assert "1,800.0</b> items/h" in render_snapshot(moving, uid="hgwpace")
    assert "1,800.0 items/h · ~2.0s left" in snapshot_line(moving)


async def test_the_watcher_reads_the_instants_the_pace_is_derived_from(home, ledger):
    """The wiring: real read-model rows, not hand-built items."""
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await _submit(host, graph, ["work-clean", "work-clean-2"], "wc-pace")

    async with worker(host):
        snapshot = await watch_submissions(host.client, [receipt.batch_ref], refresh_seconds=0.05, stop_after_minutes=1.0)

    [batch] = snapshot.submissions
    assert batch.done == 2
    for item in batch.items:
        assert item.started_at is not None and item.settled_at is not None
        assert item.settled_at >= item.started_at
    # A real Run Home, a real clock: the pace is a positive number of items
    # per second, and nothing is left to wait for.
    assert batch.rate is not None and batch.rate > 0
    assert batch.eta_seconds == 0.0
    assert snapshot.rate == batch.rate
    # Nobody was ever asked anything, so every item counts toward the pace.
    assert not any(item.ever_parked for item in batch.items)


async def test_an_answered_gate_is_still_remembered_as_ever_parked(home, ledger):
    """The durable fact the repair rests on, through a real answer.

    ``pause`` is the present tense and is None again once the answer lands.
    ``ever_paused`` is what outlives it — and without it the watcher could
    not tell a run a person unblocked from one the machine did alone.
    """
    graph = ingestion_graph()
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await _submit(host, graph, ["work-dup-1", "work-clean"], "wc-answered")
    read = RunHomeReadModel(host.client)
    watcher = SubmissionWatcher(host.client)

    async with worker(host):

        async def parked():
            census = await read.get_batch(receipt.batch_ref)
            item = census.items.get("work-dup-1")
            if item is None:
                return None
            row = await read.get_run(item.run_ref)
            return item if row is not None and row.status == "paused" else None

        item = await until(parked)
        await answer_item(host.client, item, "create_new")
        settled = await until(lambda: watch_snapshot(host.client, [receipt.batch_ref]))
        while not settled.submissions[0].settled:
            settled = await watch_snapshot(host.client, [receipt.batch_ref])
            await asyncio.sleep(0.05)

    progress = await watcher.progress(receipt.batch_ref)
    by_key = {one.item_key: one for one in progress.items}
    answered, clean = by_key["work-dup-1"], by_key["work-clean"]

    assert answered.settled and not answered.parked  # the gate is behind it
    assert answered.ever_parked  # but the Run Home still remembers
    assert not clean.ever_parked
    # So the pace is the clean item's alone, and the answered one never
    # drags it down however long the person took.
    assert progress.rate is not None
