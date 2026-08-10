"""Watching durable submissions on a Run Home — the console over read models.

``Host.submit`` accepts ONE durable Run; ``Host.submit_batch`` accepts a
whole manifest. Either way the work is executed by whichever worker holds
it, possibly in another process, so the notebook that submitted has no event
stream to fold. What it has is durable truth: the Run Home read models. This
module is the console over those reads — the same design
``hypergraph.events.console`` renders from events, derived instead from
``RunHomeReadModel.get_batch`` / ``get_run`` (census and condition words),
``list_runs`` (timings and open questions), ``client.result`` (failure
evidence), and ``retry_census`` (the durable attempt ledger).

Both submission shapes fold into the SAME picture, because a reader watching
work does not care which verb accepted it: a lone Run is a submission of one
item, a Batch is a submission of many, and ``watch_submissions`` takes a
mixed list of either ref.

Three rules, each learned on a live run:

* **The view is BOUNDED.** The only per-item list rendered is the items
  actually RUNNING — held down by the admission gate — and everything else is
  an exceptions-only list of what somebody has to act on. A healthy
  770-item Batch renders the same fifteen lines a three-item one does.
  The READS are O(items across the watched submissions) per poll: four read
  model calls per Batch (three per lone Run) — never one round trip per item.
* **Parked is at REST.** An item paused on a human gate waits for an answer
  only an operator can give, so a watch that waited for it to SETTLE would
  never return. ``watch_submissions`` stops at resting, not settled.
* **Drawing and watching end separately.** ``max_watch_minutes`` stops the
  PICTURE; the work continues and the poll drops to a heartbeat.
  ``stop_after_minutes`` is the only thing that abandons the watch.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from hypergraph.host.client import RunHomeClient
from hypergraph.host.read_models import RunHomeReadModel
from hypergraph.host.refs import BatchRef, RunRef
from hypergraph.host.views import BATCH_COUNT_KEYS, TERMINAL_STATUS_VALUES, RunQuery

logger = logging.getLogger(__name__)

#: What a reader acts on as a failure. ``partial`` is hypergraph's word for a
#: run that settled with steps unfinished; for ONE item of a Batch that is a
#: failure — it surfaces even when nothing recorded an error string for it.
FAILED_STATUSES = frozenset({"failed", "partial"})

#: The kind an item parked on an unnamed gate is filed under. A named gate
#: uses the NODE that authored the question, so the graph supplies the word.
UNNAMED_GATE = "awaiting-a-decision"

#: How often the watch polls once it has stopped DRAWING. Passing the drawing
#: deadline stops the picture, never the work, so the poll drops to a
#: heartbeat.
QUIET_REFRESH_SECONDS = 30.0


@dataclass(frozen=True)
class ItemProgress:
    """One item of a watched submission, as the Run Home currently has it.

    An item is whatever the Batch was keyed by — or, for a lone submitted
    Run, the Run itself. ``item_key`` is that label VERBATIM and is what a
    reader is shown.
    """

    item_key: str
    workflow_id: str
    #: The Run Home's own condition word for this item. Never re-derived.
    word: str
    status: str
    #: Seconds from the run's start to its settlement, or to now.
    elapsed: float
    #: The highest attempt number any node of this run reached. 1 is the
    #: happy path; more means something below retried.
    retries: int
    error: str
    question: str
    #: The NODE whose interrupt this item is parked on, when it is parked.
    gate: str = ""

    @property
    def label(self) -> str:
        """What a reader is shown for this item — its manifest key."""
        return self.item_key

    @property
    def settled(self) -> bool:
        return self.status in TERMINAL_STATUS_VALUES

    @property
    def parked(self) -> bool:
        return self.status == "paused"

    @property
    def running(self) -> bool:
        """Actually executing — not queued behind the admission gate.

        Counting queued items as in flight would make the live table as long
        as the Batch: the bound this whole view rests on is the number of
        runs the host is actually executing, not the number it was handed.
        """
        return self.status == "running"


@dataclass(frozen=True)
class SubmissionProgress:
    """One watched submission's census, plus every item's condition.

    A Batch submission holds its manifest's items; a Run submission holds
    exactly one — itself. Nothing downstream branches on which, because a
    reader watching work does not care which verb accepted it.
    """

    #: What was submitted: a ``BatchRef`` from ``submit_batch``, or a
    #: ``RunRef`` from ``submit``.
    ref: BatchRef | RunRef
    definition: str
    settled: bool
    counts: dict[str, int]
    items: list[ItemProgress]

    @property
    def is_batch(self) -> bool:
        return isinstance(self.ref, BatchRef)

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def done(self) -> int:
        return sum(1 for item in self.items if item.settled)

    @property
    def failed(self) -> int:
        return sum(1 for item in self.items if item.status in FAILED_STATUSES)

    @property
    def parked(self) -> list[ItemProgress]:
        return [item for item in self.items if item.parked]

    @property
    def resting(self) -> bool:
        """Nothing more will happen here without a HUMAN — the stop rule.

        Settled OR parked, and the parked half is the whole point: an item on
        a human gate waits for an answer only an operator can give, so a
        watch that waited for it to SETTLE would never return. The
        submission's own settled flag still wins on its own, which is what
        makes an empty manifest resting rather than an infinite wait.
        """
        return self.settled or (bool(self.items) and all(item.settled or item.parked for item in self.items))

    @property
    def running(self) -> list[ItemProgress]:
        """The items in flight, longest-running first.

        The ONLY per-item list a live view may render: it is bounded by the
        number of runs the host is executing, while ``items`` grows with the
        submission.
        """
        return sorted(
            (item for item in self.items if item.running),
            key=lambda item: item.elapsed,
            reverse=True,
        )

    def median_seconds(self) -> float:
        """Median elapsed time of the items that have settled, or 0."""
        done = sorted(item.elapsed for item in self.items if item.settled and item.elapsed)
        if not done:
            return 0.0
        middle = len(done) // 2
        return done[middle] if len(done) % 2 else (done[middle - 1] + done[middle]) / 2


@dataclass(frozen=True)
class Attention:
    """One thing worth a human's attention, and why it is one."""

    kind: str
    item: ItemProgress
    note: str


def exceptions(progress: SubmissionProgress, *, slow_after_seconds: float) -> list[Attention]:
    """Only what is wrong: failures, parked gates, retries, and stragglers.

    The accumulating half of a bounded live view. A healthy run produces an
    empty list however many items it holds, and anything that appears here is
    a thing somebody has to do something about.
    """
    found: list[Attention] = []
    for item in progress.items:
        if item.status in FAILED_STATUSES or (item.error and item.settled):
            found.append(Attention("failed", item, item.error or _why_it_failed(item)))
        elif item.parked:
            found.append(Attention(item.gate or UNNAMED_GATE, item, item.question or "awaiting a decision"))
        elif item.retries > 1:
            found.append(Attention("retried", item, f"attempt {item.retries} — something below retried"))
        elif item.running and slow_after_seconds and item.elapsed > slow_after_seconds:
            found.append(Attention("slow", item, f"{item.elapsed:,.0f}s elapsed"))
    return found


def _why_it_failed(item: ItemProgress) -> str:
    """What to say when a settled item failed and nothing recorded evidence."""
    if item.status == "partial":
        return "settled partially — some of its steps never finished"
    return "no failure evidence recorded"


@dataclass(frozen=True)
class WatchSnapshot:
    """Every outstanding submission as ONE picture.

    Whatever executes the work does not know about Batch boundaries — or
    that a lone Run was submitted by a different verb — so neither does the
    reader's view: one header, one in-flight table, one exceptions list,
    however many submissions are outstanding and whichever shape they are.
    Every number below is folded from the submissions' own reads — this
    holds no counter of its own.
    """

    submissions: list[SubmissionProgress] = field(default_factory=list)
    #: A running item is a straggler past this multiple of the median.
    slow_multiple: float = 3.0
    #: Submissions adopted from an earlier process rather than made here.
    adopted: int = 0
    #: Is the caller still submitting? Nothing may rest until it stops.
    submitting: bool = False
    #: One line about WHO is executing, when it is not this process.
    note: str = ""

    @property
    def total(self) -> int:
        return sum(one.total for one in self.submissions)

    @property
    def done(self) -> int:
        return sum(one.done for one in self.submissions)

    @property
    def failed(self) -> int:
        return sum(one.failed for one in self.submissions)

    @property
    def retried(self) -> int:
        """Items whose durable attempt ledger shows more than one attempt."""
        return sum(1 for one in self.submissions for item in one.items if item.retries > 1)

    @property
    def settled_submissions(self) -> int:
        return sum(1 for one in self.submissions if one.settled)

    @property
    def counts(self) -> dict[str, int]:
        merged: dict[str, int] = {}
        for one in self.submissions:
            for word, number in one.counts.items():
                merged[word] = merged.get(word, 0) + number
        return merged

    @property
    def slow_after(self) -> float:
        """Seconds past which a running item is called a straggler."""
        return self.slow_multiple * max((one.median_seconds() for one in self.submissions), default=0.0)

    @property
    def running(self) -> list[ItemProgress]:
        return sorted(
            (item for one in self.submissions for item in one.running),
            key=lambda item: item.elapsed,
            reverse=True,
        )

    @property
    def parked(self) -> list[ItemProgress]:
        return [item for one in self.submissions for item in one.parked]

    @property
    def exceptions(self) -> list[Attention]:
        return [trouble for one in self.submissions for trouble in exceptions(one, slow_after_seconds=self.slow_after)]

    @property
    def resting(self) -> bool:
        """Every submission resting. Nothing read yet is unknown, not resting."""
        return bool(self.submissions) and all(one.resting for one in self.submissions)


def _seconds_since(moment: datetime | None) -> float:
    if moment is None:
        return 0.0
    now = datetime.now(timezone.utc)
    when = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return max(0.0, (now - when).total_seconds())


def _elapsed(row: Any) -> float:
    started = getattr(row, "started_at", None) or getattr(row, "accepted_at", None)
    if started is None:
        return 0.0
    settled = getattr(row, "settled_at", None)
    if settled is None:
        return _seconds_since(started)
    start = started if started.tzinfo else started.replace(tzinfo=timezone.utc)
    end = settled if settled.tzinfo else settled.replace(tzinfo=timezone.utc)
    return max(0.0, (end - start).total_seconds())


def _failure_text(outcome: Any) -> str:
    """The durable failure evidence for one item, in the Run Home's words."""
    failure = getattr(outcome, "failure", None)
    if failure is None:
        return ""
    where = f" in {failure.node_name}" if failure.node_name else ""
    return f"{failure.error}{where}"


def _question(row: Any) -> str:
    pause = getattr(row, "pause", None)
    if pause is None:
        return ""
    ask = getattr(pause, "ask", None) or {}
    return str(ask.get("prompt") or "awaiting a decision")


def _gate(row: Any) -> str:
    """The NODE this run is parked on — the graph's own name for the question."""
    pause = getattr(row, "pause", None)
    return "" if pause is None else str(getattr(pause, "node_name", "") or "")


def _bucket(status: str) -> str:
    """Put ONE Run's coarse status in the Batch census's own bucket word.

    A lone Run has no manifest to count, but the picture merges its census
    with every Batch's — so it must speak the same closed vocabulary. Only
    "running" needs translating: a Batch calls an executing child ``active``.
    """
    word = "active" if status == "running" else status
    return word if word in BATCH_COUNT_KEYS else "queued"


class SubmissionWatcher:
    """Live progress for durable submissions, from durable truth only.

    Wraps one ``RunHomeClient``, and reads whichever shape the ref names.
    For a ``BatchRef``, four read model calls and no tracker of its own:
    ``get_batch`` (census and per-item condition words), ``list_runs``
    (timings and open questions), ``client.result`` (durable failure
    evidence), and ``retry_census`` (the attempt ledger, one statement per
    poll — a run that looks merely slow is told apart from one that has been
    failing and retrying). For a ``RunRef`` the same picture costs three:
    ``get_run`` carries what ``get_batch`` and ``list_runs`` split.

    Deliberately absent: "which node is it on right now". The read models do
    not expose per-run pending node boundaries at a cost a poll may pay, so
    the view reports each item's condition, status, elapsed time, and attempt
    count — never a guess about its current node.
    """

    def __init__(self, client: RunHomeClient) -> None:
        self._client = client
        self._read = RunHomeReadModel(client)

    async def progress(self, ref: BatchRef | RunRef) -> SubmissionProgress | None:
        """Fold ONE submission — Batch or lone Run — into one census."""
        if isinstance(ref, RunRef):
            return await self._run_progress(ref)
        if isinstance(ref, BatchRef):
            return await self._batch_progress(ref)
        raise TypeError(f"watching expects a BatchRef or RunRef, got {type(ref).__name__}.")

    async def _batch_progress(self, ref: BatchRef) -> SubmissionProgress | None:
        census = await self._read.get_batch(ref)
        if census is None:
            return None
        rows = {row.workflow_id: row for row in await self._read.list_runs(RunQuery(batch=ref, limit=max(len(census.items), 1)))}
        outcome = await self._client.result(ref)
        results = getattr(outcome, "items", {}) or {}
        run_ids = [item.workflow_id for item in census.items.values()]
        attempts = await self._read.retry_census(run_ids) if run_ids else {}
        items = []
        for key, item in census.items.items():
            row = rows.get(item.workflow_id)
            items.append(
                ItemProgress(
                    item_key=str(key),
                    workflow_id=item.workflow_id,
                    word=str(item.word).removeprefix("waiting: "),
                    status=("unstarted" if row is None else str(row.status or "unstarted")),
                    elapsed=_elapsed(row),
                    retries=attempts.get(item.workflow_id, 1),
                    error=_failure_text(results.get(key)),
                    question=_question(row),
                    gate=_gate(row),
                )
            )
        return SubmissionProgress(
            ref=ref,
            definition=census.definition_id.name,
            settled=bool(census.settled),
            counts=dict(census.counts),
            items=items,
        )

    async def _run_progress(self, ref: RunRef) -> SubmissionProgress | None:
        """One submitted Run, as the one item of its own picture.

        ``get_run`` already carries the condition word, the timings and the
        open question that a Batch needs two reads for, so this costs three
        statements: the run, its outcome, and its attempt ledger.
        """
        row = await self._read.get_run(ref)
        if row is None:
            return None
        outcome = await self._client.result(ref)
        attempts = await self._read.retry_census([ref.run_id])
        status = str(row.status or "unstarted")
        item = ItemProgress(
            item_key=row.workflow_id,
            workflow_id=row.workflow_id,
            word=str(row.condition).removeprefix("waiting: "),
            status=status,
            elapsed=_elapsed(row),
            retries=attempts.get(row.workflow_id, 1),
            error=_failure_text(outcome),
            question=_question(row),
            gate=_gate(row),
        )
        return SubmissionProgress(
            ref=ref,
            definition=getattr(row.definition_id, "name", "") or ref.run_id,
            settled=item.settled,
            counts={_bucket(status): 1},
            items=[item],
        )


def _as_watcher(source: SubmissionWatcher | RunHomeClient) -> SubmissionWatcher:
    if isinstance(source, SubmissionWatcher):
        return source
    return SubmissionWatcher(source)


def _as_ref(value: Any) -> BatchRef | RunRef:
    """Accept a ref, or the receipt a submit verb just handed the caller.

    ``receipt.batch_ref`` / ``receipt.run_ref`` is what a notebook actually
    holds one line after submitting, so taking the receipt itself removes a
    papercut without widening what is watched.
    """
    if isinstance(value, (BatchRef, RunRef)):
        return value
    ref = getattr(value, "batch_ref", None) or getattr(value, "run_ref", None)
    if isinstance(ref, (BatchRef, RunRef)):
        return ref
    raise TypeError(f"watching expects a BatchRef, a RunRef, or a submit receipt, got {type(value).__name__}.")


async def watch_snapshot(
    source: SubmissionWatcher | RunHomeClient,
    refs: Sequence[Any],
    *,
    slow_multiple: float = 3.0,
    adopted: int = 0,
    submitting: bool = False,
    note: str = "",
) -> WatchSnapshot:
    """One read of every outstanding submission, folded into one picture."""
    watcher = _as_watcher(source)
    progress = [await watcher.progress(_as_ref(ref)) for ref in list(refs)]
    return WatchSnapshot(
        submissions=[one for one in progress if one is not None],
        slow_multiple=slow_multiple,
        adopted=adopted,
        submitting=submitting,
        note=note,
    )


async def watch_submissions(
    source: SubmissionWatcher | RunHomeClient,
    refs: Sequence[Any],
    *,
    refresh_seconds: float = 3.0,
    max_watch_minutes: float = 90.0,
    slow_multiple: float = 3.0,
    stop_after_minutes: float | None = None,
    adopted: int = 0,
    note: str = "",
    until_submitted: asyncio.Event | None = None,
    draw: Callable[[WatchSnapshot], None] | None = None,
) -> WatchSnapshot:
    """Watch durable submissions until nothing is left this process can advance.

    The shared verb behind every "submit things and see them happen"
    surface, for both submit verbs: ``refs`` is any mix of ``BatchRef``
    (from ``submit_batch``), ``RunRef`` (from ``submit``), and the receipts
    that carry them. It returns the resting snapshot, which is the frame a
    notebook cell renders into a stored output.

    Three rules, each of them learned on a live run:

    * **Resting, not settled.** An item parked on a human gate is at rest —
      waiting for it to settle would wait on a question only an operator can
      answer.
    * **Drawing and watching end separately.** ``max_watch_minutes`` stops
      the PICTURE; the work continues and the poll drops to a heartbeat,
      because whoever called this still needs every submission to rest.
      ``stop_after_minutes`` is the only thing that abandons the watch, and
      a headless caller sets it so it cannot hang forever.
    * **``refs`` is read live.** A caller still submitting passes the list it
      is appending to, plus ``until_submitted``, and the picture is drawn
      from the FIRST receipt rather than after the last one.
    """
    watcher = _as_watcher(source)
    started = time.monotonic()
    drawing_until = started + max_watch_minutes * 60
    abandon_at = None if stop_after_minutes is None else started + stop_after_minutes * 60
    while True:
        snapshot = await watch_snapshot(
            watcher,
            refs,
            slow_multiple=slow_multiple,
            adopted=adopted,
            submitting=until_submitted is not None and not until_submitted.is_set(),
            note=note,
        )
        now = time.monotonic()
        if draw is not None and snapshot.submissions and now <= drawing_until:
            draw(snapshot)
        if not snapshot.submitting and (not list(refs) or snapshot.resting):
            return snapshot
        if abandon_at is not None and now > abandon_at:
            logger.error(
                "stopped watching after %.0f minutes — the runs stay durable and a re-run picks up whatever did not finish",
                stop_after_minutes,
            )
            return snapshot
        await asyncio.sleep(refresh_seconds if now <= drawing_until else QUIET_REFRESH_SECONDS)


# ---------------------------------------------------------------------------
# rendering — the console design over a snapshot
# ---------------------------------------------------------------------------


def render_snapshot(
    snapshot: WatchSnapshot,
    *,
    uid: str = "",
    elapsed_s: float | None = None,
    item_label: str = "items",
) -> str:
    """One console frame over a snapshot. Pure function, bounded output.

    The same visual language ``hypergraph.events.console.render_console``
    renders from events: header with the run-level line, stat tiles, the
    segmented progress bar, the bounded in-flight table, and the
    needs-attention list — down to the palette, which resolves light or dark
    through hypergraph's own widget theme wrapper rather than a second
    detector. ``item_label`` names the unit ("items" by default);
    ``elapsed_s`` is the watch's own clock, when the caller keeps one.

    No collapse state to keep: this view has no tree to collapse, so it
    carries the theme wrapper and nothing else.
    """
    from hypergraph._repr import theme_wrap
    from hypergraph.events.console import _CSS, _esc, _fmt_s, _n_unit

    uid = uid or ("hgw" + uuid4().hex[:8])
    # ``item_label`` is caller text and lands in many attributes; escape it
    # once here, exactly as the event console escapes its ``item_labels``.
    unit = _esc(item_label)
    one = unit[:-1] if unit.endswith("s") else unit
    counts = snapshot.counts
    total = snapshot.total
    failed = snapshot.failed
    done_clean = max(0, snapshot.done - failed)
    running_n = counts.get("active", 0)
    parked_n = counts.get("paused", 0)
    queued_n = max(0, total - snapshot.done - running_n - parked_n)
    resting = snapshot.resting

    held = snapshot.submissions
    titles = sorted({one.definition for one in held})
    title = titles[0] if len(titles) == 1 else f"{len(held)} submissions"
    subtitle = (
        f"{_n_unit(total, unit)} · {len(held)} durable "
        + ("submission" if len(held) == 1 else "submissions")
        + (f" · {snapshot.adopted} adopted" if snapshot.adopted else "")
        + (" · still submitting" if snapshot.submitting else "")
    )

    if resting and not snapshot.submitting:
        word = "Settled" if all(one.settled for one in held) else "Resting"
        state = (
            '<span class="bc-live"><i class="dot" data-tone="saved"></i>'
            f"<b>{word} snapshot</b>" + (f" · {_fmt_s(elapsed_s)} watched" if elapsed_s is not None else "") + "</span>"
            f'<span class="bc-chip" data-tone="settled">{word}</span>'
        )
    else:
        state = (
            '<span class="bc-live"><i class="dot" data-tone="live"></i>'
            "<b>Live</b>" + (f" · {_fmt_s(elapsed_s)} elapsed" if elapsed_s is not None else "") + "</span>"
            '<span class="bc-chip" data-tone="running">Running</span>'
        )

    header_line = (
        f'<span class="bc-sub bc-agg"><b>{snapshot.done:,}/{total:,}</b> {unit}'
        f" · <b>{failed:,}</b> failed"
        f" · <b>{snapshot.retried:,}</b> retried"
        f" · <b>{parked_n:,}</b> parked</span>"
    )

    stats = [
        (
            f"{snapshot.done:,}",
            f"/ {total:,}",
            f"{unit.capitalize()} settled",
            "",
            f"{snapshot.done} of {total} {unit} are accounted for good.",
        ),
        (
            f"{running_n:,}",
            "",
            "In flight",
            "",
            f"{running_n} {unit} are executing right now.",
        ),
        (
            f"{parked_n:,}",
            "",
            "Parked",
            ' data-tone="warn"' if parked_n else "",
            f"{parked_n} {unit} wait on a human decision. They are at rest, not stuck.",
        ),
        (
            f"{failed:,}",
            "",
            f"{unit.capitalize()} failed",
            ' data-tone="bad"' if failed else "",
            f"{failed} {unit} ended in failure. Every one is listed under Needs attention.",
        ),
    ]
    stats_html = "".join(
        f'<div class="stat"{tone} title="{_esc(title_)}">'
        f'<div class="stat-v">{v}{f"<small>{suffix}</small>" if suffix else ""}</div>'
        f'<div class="stat-l">{label}</div></div>'
        for v, suffix, label, tone, title_ in stats
    )

    seg = lambda k, n, label: (  # noqa: E731
        f'<span class="seg" data-k="{k}" style="flex:{n} 1 0" title="{label}"></span>' if n else ""
    )
    key = lambda color, word, n, title_: (  # noqa: E731
        f'<span title="{title_}"><i class="key" style="background:{color}"></i><b>{n:,}</b> <i>{one if n == 1 else unit}</i> {word}</span>'
    )
    progress_html = f"""
    <div class="segs" role="img" aria-label="{done_clean} {unit} done, {running_n} running, {parked_n} parked, {failed} failed, {queued_n} queued, of {total} {unit}">
      {seg("done", done_clean, f"{done_clean} {unit} settled cleanly")}
      {seg("running", running_n, f"{running_n} {unit} executing right now")}
      {seg("paused", parked_n, f"{parked_n} {unit} parked on a human decision")}
      {seg("failed", failed, f"{failed} {unit} failed")}
      {seg("queued", queued_n, f"{queued_n} {unit} queued behind the admission gate")}
    </div>
    <div class="legend">
      {key("var(--ok)", "settled", done_clean, f"{done_clean} {unit} settled cleanly")}
      {key("var(--accent)", "running", running_n, f"{running_n} {unit} in flight right now") if running_n else ""}
      {key("var(--warn)", "parked", parked_n, f"{parked_n} {unit} wait on a human decision") if parked_n else ""}
      {key("var(--bar-track)", "queued", queued_n, f"{queued_n} {unit} not admitted yet") if queued_n else ""}
      {key("var(--bad)", "failed", failed, f"{failed} {unit} ended in failure")}
    </div>"""

    running = snapshot.running
    if running:
        rows = "".join(
            f'<tr><td class="k">{_esc(item.label)}</td>'
            f"<td>{_esc(item.word)}</td>"
            f'<td class="n">{_fmt_s(item.elapsed)}</td>'
            f'<td class="n">{item.retries}</td></tr>'
            for item in running
        )
        flight_html = (
            f'<div class="sec"><h3>In flight · {len(running)}</h3>'
            f'<span class="hint">bounded by the admission gate · longest first</span></div>'
            '<table class="flight"><tr><th>Item</th><th>Condition</th>'
            "<th>Elapsed</th><th>Attempt</th></tr>"
            f"{rows}</table>"
        )
    else:
        flight_html = '<div class="sec"><h3>In flight · 0</h3><span class="hint">nothing executing right now</span></div>'

    trouble = snapshot.exceptions
    if trouble:
        tone = {"failed": "", "retried": "info", "slow": "info"}
        rows = "".join(
            '<div class="fail"><span class="fail-dot" data-tone="' + (tone.get(t.kind, "gate")) + '" aria-hidden="true"></span>'
            f'<span class="fail-doc">{_esc(t.item.label)}</span>'
            f'<span class="fail-step">{_esc(t.kind)}</span>'
            f'<span class="fail-msg" title="{_esc(t.note)}">{_esc(t.note)}</span></div>'
            for t in trouble[:20]
        )
        more = " · first 20 shown" if len(trouble) > 20 else ""
        attention_html = (
            f'<div class="fails"><div class="sec"><h3>Needs attention · <span class="count">'
            f'{len(trouble)}</span></h3><span class="hint">'
            f"failures, open gates, retries and stragglers{more}</span></div>" + rows + "</div>"
        )
    else:
        attention_html = '<div class="fails"><div class="sec"><h3>Needs attention · 0</h3><span class="hint">nothing to act on</span></div></div>'

    note_html = f'<div class="notice"><b>executing elsewhere</b><span class="notice-text">{_esc(snapshot.note)}</span></div>' if snapshot.note else ""

    if resting and not snapshot.submitting:
        foot_left = "Snapshot saved · durable truth from the Run Home"
        foot_right = f"{snapshot.retried:,} {unit} retried · {_n_unit(failed, unit)} kept"
    else:
        foot_left = "Live · folded from Run Home read models"
        foot_right = f"{running_n:,} {unit} running · straggler past {snapshot.slow_after:,.0f}s"

    css = _CSS.replace("$UID", uid)
    frame = f"""<div id="{uid}">
<style>{css}</style>
<section class="bc" aria-label="Durable submission console">
  <div class="bc-head">
    <div>
      <div class="eyebrow">Hypergraph durable submissions</div>
      <span class="bc-title">{_esc(title)}</span>
      <span class="bc-sub">{_esc(subtitle)}</span>
      {header_line}
    </div>
    <div class="bc-status">{state}</div>
  </div>
  <div class="stats">{stats_html}</div>
  <div class="prog">{progress_html}</div>
  {note_html}
  <div class="bc-body">
    <div class="bc-main">
      {flight_html}
    </div>
  </div>
  {attention_html}
  <div class="bc-foot"><span>{foot_left}</span><span>{foot_right}</span></div>
</section>
</div>"""
    return theme_wrap(frame, state_key=f"watch:{uid}")


def snapshot_line(snapshot: WatchSnapshot) -> str:
    """The same facts as one log line — the headless rendering."""
    counts = " · ".join(f"{word} {number}" for word, number in sorted(snapshot.counts.items()) if number) or "-"
    trouble = snapshot.exceptions
    adopted = f" ({snapshot.adopted} adopted)" if snapshot.adopted else ""
    return (
        f"{len(snapshot.submissions)} submission(s){adopted} · items "
        f"{snapshot.done}/{snapshot.total} settled · in flight {len(snapshot.running)} · "
        f"{counts}" + (f" · needs attention {len(trouble)}" if trouble else "") + (f" · {snapshot.note}" if snapshot.note else "")
    )


class ConsolePanel:
    """The live console in ONE in-place IPython display handle.

    Drawn once and updated after that — never appended, never cleared. The
    last frame drawn (the resting one, when ``watch_submissions`` returns)
    is what a saved notebook keeps.
    """

    def __init__(self, *, item_label: str = "items") -> None:
        self._uid = "hgw" + uuid4().hex[:8]
        self._item_label = item_label
        self._handle: Any = None
        self._started = time.monotonic()
        self.frames = 0

    def __call__(self, snapshot: WatchSnapshot) -> None:
        from IPython.display import HTML, display

        frame = HTML(
            render_snapshot(
                snapshot,
                uid=self._uid,
                elapsed_s=time.monotonic() - self._started,
                item_label=self._item_label,
            )
        )
        if self._handle is None:
            self._handle = display(frame, display_id=self._uid)
        else:
            self._handle.update(frame)
        self.frames += 1


class LogPanel:
    """The live view as a log line, at most once every ``every_seconds``.

    What a headless run draws with. The watch ticks every few seconds so the
    stop rule stays responsive; the log does not need to.
    """

    def __init__(self, every_seconds: float = 60.0) -> None:
        self._every = every_seconds
        self._last = float("-inf")

    def __call__(self, snapshot: WatchSnapshot) -> None:
        now = time.monotonic()
        if now - self._last < self._every:
            return
        self._last = now
        logger.info("%s", snapshot_line(snapshot))
