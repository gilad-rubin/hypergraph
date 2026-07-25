"""Durable Host V1 — ticket 14 / ADR 0008: scheduled pause answers + provenance.

Covers: one typed answer armed against ONE observed pause occurrence, durable
with its pause id, due time, and value; answer-versus-timer races decided by
the shared compare-and-set in commit order; an answered or superseded pause
making its timer inapplicable (refused at fire time and recorded, never
deleted); ``source_ref`` as audit data that no dedup or eligibility predicate
reads; and the deliberate absence of reminders, assignment, recurrence, cron,
and any caller-chosen scheduled verb.

Four scope notes, all deliberate:

- **The durable unit is a scheduled ANSWER, never a generic command.** ADR
  0008 and PRD 0017 both exclude cron, recurring schedules, and a
  scheduled-command framework, so ``TestExcludedSchedulingSurfacesAreAbsent``
  asserts their absence the way ticket 12 asserted the excluded overflow
  strategies.
- **One due-row scanner.** Delayed starts (``host_submissions.start_at``) and
  scheduled answers (``host_commands.due_at``) share ``home._due_clause`` and
  one store-authoritative ``now`` per worker pass. Tests drive that ``now``
  explicitly — no sleeping, no wall clock in the assertion.
- **Firing settles; it does not resume.** A settled answer is durable resume
  input, exactly as ticket 13 left it. Nothing here asserts or implies that an
  answered or timed-out run continues executing.
- **Memory/SQLite parity does not apply.** Host commands are Run Home
  coordination facts; ``MemoryCheckpointer`` has no host tables. The
  settlement path these timers fire through is the shared one ticket 13
  already proved on both backends. Sync/async parity DOES apply to
  scheduling, and is asserted; the due-row scan is async-only like every
  other worker-side scan on this Home.
"""

import asyncio
import inspect
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

import pytest
import pytest_asyncio

from hypergraph import (
    END,
    AnswerRejectedError,
    AsyncRunner,
    Graph,
    PauseAlreadySettledError,
    RunHome,
    RunHomeClient,
    RunRef,
    StalePauseError,
    SyncRunner,
    interrupt,
    node,
    route,
    serve,
)
from hypergraph.checkpointers.types import PauseSlot, WorkflowStatus
from hypergraph.host.batch import BatchTolerance
from hypergraph.host.home import (
    SCHEDULED_ANSWER_ALREADY_SETTLED,
    SCHEDULED_ANSWER_REJECTED,
    SCHEDULED_ANSWER_SETTLED,
    SCHEDULED_ANSWER_SUPERSEDED,
    _due_clause,
)
from hypergraph.host.refs import BatchRef, CommandReceipt

aiosqlite = pytest.importorskip("aiosqlite")

RUN_ID = "refund-c-42"


# === Question types and graphs ===


@dataclass(frozen=True)
class Confirm:
    answer_type: ClassVar[object] = bool
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple = ()


@dataclass(frozen=True)
class Pick:
    answer_type: ClassVar[object] = str
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple = ()


def _approval_graph() -> Graph:
    @node(output_name="draft")
    def draft(claim_id: str) -> str:
        return f"draft-{claim_id}"

    @interrupt(answer_name="approved")
    def approval(draft: str) -> Confirm:
        return Confirm(prompt="Approve this refund?", options=("yes", "no"), evidence=(draft,))

    @node(output_name="final")
    def settle(approved: bool) -> str:
        return f"settled-{approved}"

    return Graph([draft, approval, settle], name="refund")


def _loop_graph() -> Graph:
    """The same interrupt node pauses on two distinct supersteps."""

    @interrupt(answer_name="reply")
    def ask(turns: int = 0) -> Pick:
        return Pick(prompt=f"turn {turns}", options=None, evidence=())

    @node(output_name="turns")
    def record(reply: str, turns: int = 0) -> int:
        return turns + 1

    @route(targets=["ask", END])
    def again(turns: int = 0) -> str:
        return END if turns >= 2 else "ask"

    return Graph([ask, record, again], name="loop", entrypoint="ask")


def _plain_graph(name: str = "dbl") -> Graph:
    @node(output_name="out")
    def compute(x: int) -> int:
        return x + 1

    return Graph([compute], name=name).with_runner(SyncRunner())


# === Fixtures and helpers ===


def _home_uri(tmp_path, filename: str = "runs.db") -> str:
    return f"file:{tmp_path / filename}"


@pytest_asyncio.fixture
async def home(tmp_path):
    h = RunHome.open(_home_uri(tmp_path))
    yield h
    await h.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _future_iso(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def _past_iso(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _db_path(home_obj) -> str:
    return home_obj.uri[len("file:") :]


def _command_rows(home_obj, run_id: str = RUN_ID) -> list[tuple]:
    """Read committed host_commands rows over a FRESH connection.

    A fresh connection can only see what reached the database file, so this
    is durability evidence rather than in-process bookkeeping.
    """
    conn = sqlite3.connect(_db_path(home_obj))
    try:
        return conn.execute(
            "SELECT id, verb, pause_id, due_at, payload, source_ref, applied_at, outcome FROM host_commands WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()


def _scheduled_rows(home_obj, run_id: str = RUN_ID) -> list[tuple]:
    return [row for row in _command_rows(home_obj, run_id) if row[1] == "schedule_answer"]


async def _pause_once(checkpointer, graph=None, workflow_id: str = RUN_ID, inputs=None) -> PauseSlot:
    graph = graph if graph is not None else _approval_graph()
    result = await AsyncRunner(checkpointer=checkpointer).run(
        graph,
        inputs if inputs is not None else {"claim_id": "c-42"},
        workflow_id=workflow_id,
    )
    assert result.paused
    run = await checkpointer.get_run_async(workflow_id)
    assert run is not None and run.pause_slot is not None
    return run.pause_slot


def _ref(home_obj, run_id: str = RUN_ID) -> RunRef:
    return RunRef(home=home_obj.uri, run_id=run_id)


async def _first_command_update(client, ref: RunRef, verb: str, timeout: float = 5.0):
    """The first durable ``command`` update for ``verb``, read the public way."""

    async def _scan():
        async for update in client.watch(ref):
            if update.kind == "command" and update.payload.get("verb") == verb:
                return update
        raise AssertionError(f"watch() ended without a {verb!r} command update")

    return await asyncio.wait_for(_scan(), timeout=timeout)


# === 1. A scheduled answer persists with pause id, due time, and one value ===


class TestScheduledAnswerPersists:
    """Checkbox 1."""

    async def test_it_persists_the_occurrence_the_due_time_and_one_typed_value(self, home):
        slot = await _pause_once(home)
        client = RunHomeClient(home)
        due_at = _future_iso(hours=72)

        receipt = await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=False, due_at=due_at)

        assert isinstance(receipt, CommandReceipt)
        assert (receipt.verb, receipt.duplicate, receipt.run_ref) == ("schedule_answer", False, _ref(home))
        (row,) = _scheduled_rows(home)
        _id, verb, pause_id, stored_due, payload, source_ref, applied_at, outcome = row
        assert verb == "schedule_answer"
        assert pause_id == slot.pause_id == f"{RUN_ID}:1:approval"
        assert stored_due == due_at
        assert json.loads(payload) == {"pause_id": slot.pause_id, "value": False}
        assert (source_ref, applied_at, outcome) == (None, None, None)
        # Arming is not answering: the occurrence stays open.
        assert (await client.get_run_slot(_ref(home))).is_open

    async def test_it_survives_reopening_the_home_and_still_fires(self, tmp_path):
        uri = _home_uri(tmp_path, "restart.db")
        first = RunHome.open(uri)
        try:
            slot = await _pause_once(first)
            await RunHomeClient(first).schedule_answer(_ref(first), pause_id=slot.pause_id, value=True, due_at=_past_iso(minutes=5))
        finally:
            await first.close()

        second = RunHome.open(uri)
        try:
            # A process that never saw the schedule call finds it durable.
            (row,) = _scheduled_rows(second)
            assert row[2] == slot.pause_id and json.loads(row[4])["value"] is True
            assert [outcome for _id, outcome in await second._settle_due_answers(_now_iso())] == [SCHEDULED_ANSWER_SETTLED]
            settled = await second.get_pause_slot(RUN_ID)
            assert settled.answer is True and not settled.is_open
        finally:
            await second.close()

    @pytest.mark.parametrize(
        "due_at",
        [
            datetime(2999, 1, 1, 12, 0, 0),  # naive -> read as UTC
            datetime(2999, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            "2999-01-01T12:00:00Z",
            "2999-01-01T12:00:00+00:00",
            "2999-01-01T14:00:00+02:00",
        ],
    )
    async def test_every_due_time_shape_stores_the_same_instant(self, home, due_at):
        """One stored shape, because the due predicate is a `<=` string compare."""
        slot = await _pause_once(home)
        await RunHomeClient(home).schedule_answer(_ref(home), pause_id=slot.pause_id, value=True, due_at=due_at)
        assert _scheduled_rows(home)[0][3] == "2999-01-01T12:00:00+00:00"

    async def test_a_scheduled_answer_requires_a_due_time(self, home):
        slot = await _pause_once(home)
        client = RunHomeClient(home)
        with pytest.raises(TypeError):
            await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=True)  # type: ignore[call-arg]
        with pytest.raises(ValueError, match="How to fix"):
            await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=True, due_at=None)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="ISO 8601"):
            await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=True, due_at="friday-ish")
        assert _scheduled_rows(home) == []

    async def test_the_value_is_checked_against_the_slot_schema_before_anything_is_written(self, home):
        slot = await _pause_once(home)
        client = RunHomeClient(home)

        with pytest.raises(AnswerRejectedError) as rejected:
            await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value="maybe", due_at=_future_iso(days=3))

        assert rejected.value.pause_id == slot.pause_id and rejected.value.issues
        assert _scheduled_rows(home) == []  # an unarmable timer is never accepted
        assert (await client.get_run_slot(_ref(home))).is_open
        # The corrected value arms the same occurrence.
        assert (await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=True, due_at=_future_iso(days=3))).duplicate is False

    async def test_it_is_admitted_through_the_same_refusal_cascade_a_human_answer_gets(self, home):
        slot = await _pause_once(home)
        client = RunHomeClient(home)
        due = _future_iso(days=1)

        with pytest.raises(AnswerRejectedError, match="must name the pause occurrence"):
            await client.schedule_answer(_ref(home), value=True, due_at=due)
        with pytest.raises(AnswerRejectedError, match="Unknown pause_id"):
            await client.schedule_answer(_ref(home), pause_id=f"{RUN_ID}:99:approval", value=True, due_at=due)
        await client.answer(_ref(home), pause_id=slot.pause_id, value=True)
        with pytest.raises(PauseAlreadySettledError):
            await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=False, due_at=due)
        assert _scheduled_rows(home) == []

    async def test_a_run_with_no_durable_pause_can_schedule_nothing(self, home):
        """The only schedulable thing is an answer to a real open occurrence."""
        await home.create_run("plain", graph_name="none")
        with pytest.raises(AnswerRejectedError, match="no durable pause"):
            await RunHomeClient(home).schedule_answer(_ref(home, "plain"), pause_id="plain:0:x", value=True, due_at=_future_iso(days=1))
        assert _command_rows(home, "plain") == []

    async def test_one_timer_per_occurrence_first_one_wins(self, home):
        slot = await _pause_once(home)
        client = RunHomeClient(home)

        first = await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=True, due_at=_future_iso(days=1))
        second = await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=False, due_at=_future_iso(days=2))

        assert (first.duplicate, second.duplicate) == (False, True)
        rows = _scheduled_rows(home)
        assert len(rows) == 1
        assert json.loads(rows[0][4])["value"] is True  # the first timer owns its value

    async def test_a_scheduled_answer_takes_a_run_ref_only(self, home):
        with pytest.raises(TypeError, match="expects a RunRef"):
            await RunHomeClient(home).schedule_answer(
                BatchRef(home=home.uri, batch_id="b-1"),  # type: ignore[arg-type]
                pause_id="x",
                value=True,
                due_at=_future_iso(days=1),
            )


# === 2. The shared due-row scanner ===


class TestSharedDueRowScanner:
    """One store-authoritative `now`, one due predicate — never a second timer."""

    def test_there_is_exactly_one_due_predicate_in_the_home(self):
        assert _due_clause("start_at") == "(start_at IS NULL OR start_at <= ?)"
        assert _due_clause("due_at") == "(due_at IS NULL OR due_at <= ?)"
        source = Path(inspect.getfile(RunHome)).read_text()
        # A second hand-written due predicate would be a second timer concept.
        assert len(re.findall(r"IS NULL OR\b", source)) == 1
        assert "_due_clause('start_at')" in inspect.getsource(RunHome._claim_eligible)
        assert "_due_clause('due_at')" in inspect.getsource(RunHome._due_scheduled_answers)

    async def test_one_now_decides_both_delayed_starts_and_due_answers(self, home):
        host = serve(_plain_graph(), home=home, deployment_version="v1")
        due = "2999-01-01T00:00:00+00:00"
        await host.submit("dbl", {"x": 1}, workflow_id="wf-later", start_at=due)
        slot = await _pause_once(home)
        await host.client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=True, due_at=due)

        before = "2998-12-31T23:59:59+00:00"
        assert await home._claim_eligible(before, served=host._served_identities) == []
        assert await home._settle_due_answers(before) == []
        assert (await home.get_pause_slot(RUN_ID)).is_open

        after = "2999-01-02T00:00:00+00:00"
        assert [row["workflow_id"] for row in await home._claim_eligible(after, served=host._served_identities)] == ["wf-later"]
        assert [outcome for _id, outcome in await home._settle_due_answers(after)] == [SCHEDULED_ANSWER_SETTLED]

    async def test_a_timer_is_not_applied_before_its_due_time(self, home):
        slot = await _pause_once(home)
        await RunHomeClient(home).schedule_answer(_ref(home), pause_id=slot.pause_id, value=True, due_at=_future_iso(days=3))

        assert await home._settle_due_answers(_now_iso()) == []
        assert (await home.get_pause_slot(RUN_ID)).is_open
        assert _scheduled_rows(home)[0][6] is None  # unapplied

    async def test_a_due_timer_settles_the_occurrence_it_named(self, home):
        slot = await _pause_once(home)
        await RunHomeClient(home).schedule_answer(
            _ref(home),
            pause_id=slot.pause_id,
            value=False,
            due_at=_past_iso(hours=1),
            source_ref="review-console:req-91",
        )

        assert [outcome for _id, outcome in await home._settle_due_answers(_now_iso())] == [SCHEDULED_ANSWER_SETTLED]

        settled = await home.get_pause_slot(RUN_ID)
        assert settled.pause_id == slot.pause_id
        assert settled.answer is False and not settled.is_open
        row = _scheduled_rows(home)[0]
        assert row[6] is not None and row[7] == SCHEDULED_ANSWER_SETTLED
        assert row[5] == "review-console:req-91"  # provenance survives firing

    async def test_the_worker_loop_fires_due_answers_with_its_claim_scan(self, home):
        """The one scan is wired into work_forever, not only reachable in tests."""
        host = serve(_plain_graph(), home=home, deployment_version="v1")
        slot = await _pause_once(home)
        await host.client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=True, due_at=_past_iso(hours=1))

        worker = asyncio.create_task(host.work_forever("w-due", poll_interval=0.01))
        try:
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                if not (await home.get_pause_slot(RUN_ID)).is_open:
                    break
                await asyncio.sleep(0.01)
        finally:
            host.shutdown()
            await asyncio.wait_for(worker, timeout=10)

        assert (await home.get_pause_slot(RUN_ID)).answer is True
        assert _scheduled_rows(home)[0][7] == SCHEDULED_ANSWER_SETTLED
        assert host.worker_errors == []


# === 3. Answer-versus-timer races resolve by atomic commit order ===


class TestAnswerVersusTimerRaces:
    """Checkbox 2: no preference rule, no lock — the compare-and-set decides."""

    async def test_a_human_answer_that_commits_first_wins_and_the_timer_loses(self, home):
        slot = await _pause_once(home)
        client = RunHomeClient(home)
        await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=False, due_at=_past_iso(hours=1))

        settled = await client.answer(_ref(home), pause_id=slot.pause_id, value=True)
        assert settled.answer is True

        assert [outcome for _id, outcome in await home._settle_due_answers(_now_iso())] == [SCHEDULED_ANSWER_ALREADY_SETTLED]
        assert (await client.get_run_slot(_ref(home))).answer is True  # the human's value stands

    async def test_a_timer_that_commits_first_wins_and_the_human_loses(self, home):
        slot = await _pause_once(home)
        client = RunHomeClient(home)
        await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=False, due_at=_past_iso(hours=1))

        assert [outcome for _id, outcome in await home._settle_due_answers(_now_iso())] == [SCHEDULED_ANSWER_SETTLED]

        with pytest.raises(PauseAlreadySettledError) as late:
            await client.answer(_ref(home), pause_id=slot.pause_id, value=True)
        assert late.value.pause_id == slot.pause_id
        assert (await client.get_run_slot(_ref(home))).answer is False  # the timer's value stands

    async def test_a_real_race_elects_exactly_one_winner(self, home):
        """Three humans and one timer contend on the same occurrence at once."""
        slot = await _pause_once(home)
        client = RunHomeClient(home)
        ref = _ref(home)
        await client.schedule_answer(ref, pause_id=slot.pause_id, value=False, due_at=_past_iso(hours=1))
        gate = asyncio.Event()

        async def human():
            await gate.wait()
            return await client.answer(ref, pause_id=slot.pause_id, value=True)

        async def timer():
            await gate.wait()
            return await home._settle_due_answers(_now_iso())

        tasks = [asyncio.create_task(human()) for _ in range(3)]
        timer_task = asyncio.create_task(timer())
        gate.set()
        human_results = await asyncio.gather(*tasks, return_exceptions=True)
        ((_command_id, timer_outcome),) = await timer_task

        final = await client.get_run_slot(ref)
        assert not final.is_open and final.settled_at is not None
        human_winners = [item for item in human_results if isinstance(item, PauseSlot)]
        human_losers = [item for item in human_results if isinstance(item, BaseException)]
        assert all(isinstance(item, PauseAlreadySettledError) for item in human_losers)

        if final.answer is True:  # a human committed first
            assert (len(human_winners), timer_outcome) == (1, SCHEDULED_ANSWER_ALREADY_SETTLED)
        else:  # the timer committed first
            assert (final.answer, len(human_winners), timer_outcome) == (False, 0, SCHEDULED_ANSWER_SETTLED)
        assert len(human_winners) + len(human_losers) == 3
        assert _scheduled_rows(home)[0][7] == timer_outcome


# === 4. An answered or replaced pause makes its timer inapplicable ===


class TestVoidedTimers:
    """Checkbox 3: refused at fire time, recorded, and never deleted."""

    async def test_an_answered_pause_voids_its_timer_and_keeps_the_whole_row(self, home):
        slot = await _pause_once(home)
        client = RunHomeClient(home)
        due_at = _past_iso(hours=1)
        await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=False, due_at=due_at, source_ref="ops-console-7")
        await client.answer(_ref(home), pause_id=slot.pause_id, value=True)

        await home._settle_due_answers(_now_iso())

        rows = _scheduled_rows(home)
        assert len(rows) == 1  # "inapplicable" is recorded, never deleted
        _id, _verb, pause_id, stored_due, payload, source_ref, applied_at, outcome = rows[0]
        assert (pause_id, stored_due, source_ref) == (slot.pause_id, due_at, "ops-console-7")
        assert json.loads(payload)["value"] is False  # what it would have answered
        assert applied_at is not None and outcome == SCHEDULED_ANSWER_ALREADY_SETTLED
        assert (await client.get_run_slot(_ref(home))).answer is True

    async def test_a_later_occurrence_makes_the_earlier_timer_inapplicable(self, home):
        """A timer armed for pause A must never fire into a later pause B."""
        graph = _loop_graph()
        runner = AsyncRunner(checkpointer=home)
        client = RunHomeClient(home)
        ref = _ref(home, "wf-loop")

        assert (await runner.run(graph, {}, workflow_id="wf-loop")).paused
        first = (await home.get_run_async("wf-loop")).pause_slot
        await client.schedule_answer(ref, pause_id=first.pause_id, value="late", due_at=_past_iso(hours=1))

        # Resume through the runner without settling: the first occurrence is
        # replaced while still open, so this is supersession, not double-answer.
        assert (await runner.run(graph, {"reply": "first"}, workflow_id="wf-loop")).paused
        second = (await home.get_run_async("wf-loop")).pause_slot
        assert second.pause_id != first.pause_id

        assert [outcome for _id, outcome in await home._settle_due_answers(_now_iso())] == [SCHEDULED_ANSWER_SUPERSEDED]

        assert (await home.get_pause_slot("wf-loop", pause_id=first.pause_id)).is_open  # never settled
        assert (await client.get_run_slot(ref)).pause_id == second.pause_id
        assert (await client.get_run_slot(ref)).is_open  # the later question is untouched
        assert _scheduled_rows(home, "wf-loop")[0][7] == SCHEDULED_ANSWER_SUPERSEDED

    async def test_a_stopped_run_makes_its_timer_inapplicable(self, home):
        slot = await _pause_once(home)
        await RunHomeClient(home).schedule_answer(_ref(home), pause_id=slot.pause_id, value=True, due_at=_past_iso(hours=1))
        await home.update_run_status(RUN_ID, WorkflowStatus.STOPPED)

        assert [outcome for _id, outcome in await home._settle_due_answers(_now_iso())] == [SCHEDULED_ANSWER_REJECTED]
        assert (await home.get_pause_slot(RUN_ID)).is_open  # a refusal never consumes the occurrence

    async def test_a_fired_timer_never_fires_again(self, home):
        """The anti-recurrence proof: one arming, exactly one application."""
        slot = await _pause_once(home)
        client = RunHomeClient(home)
        await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=True, due_at=_past_iso(days=1))

        assert len(await home._settle_due_answers(_now_iso())) == 1
        for later in (_now_iso(), _future_iso(days=365), "2999-01-01T00:00:00+00:00"):
            assert await home._settle_due_answers(later) == []

        assert len(_scheduled_rows(home)) == 1
        settled = await client.get_run_slot(_ref(home))
        assert settled.answer is True and settled.settled_at is not None

    async def test_a_voided_timer_leaves_the_occurrence_answerable_by_a_human(self, home):
        """A superseded timer consumes nothing: the current pause still answers."""
        graph = _loop_graph()
        runner = AsyncRunner(checkpointer=home)
        client = RunHomeClient(home)
        ref = _ref(home, "wf-loop")

        assert (await runner.run(graph, {}, workflow_id="wf-loop")).paused
        first = (await home.get_run_async("wf-loop")).pause_slot
        await client.schedule_answer(ref, pause_id=first.pause_id, value="late", due_at=_past_iso(hours=1))
        assert (await runner.run(graph, {"reply": "first"}, workflow_id="wf-loop")).paused
        second = (await home.get_run_async("wf-loop")).pause_slot

        await home._settle_due_answers(_now_iso())

        settled = await client.answer(ref, pause_id=second.pause_id, value="second")
        assert settled.answer == "second"


# === 5. source_ref is audit data, never authentication and never dedup ===


class TestSourceRefIsAuditOnly:
    """Checkbox 4."""

    async def test_it_is_visible_on_the_command_row_and_in_the_durable_stream(self, home):
        slot = await _pause_once(home)
        client = RunHomeClient(home)
        due_at = _future_iso(days=3)

        await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=True, due_at=due_at, source_ref="review-console:req-91")

        assert _scheduled_rows(home)[0][5] == "review-console:req-91"
        update = await _first_command_update(client, _ref(home), "schedule_answer")
        assert update.durable is True
        assert update.payload == {
            "verb": "schedule_answer",
            "pause_id": slot.pause_id,
            "due_at": due_at,
            "source_ref": "review-console:req-91",
        }

    async def test_a_stop_carries_its_source_ref_into_the_same_stream(self, home):
        """One provenance rule for every command verb, not one per feature."""
        host = serve(_plain_graph(), home=home, deployment_version="v1")
        receipt = await host.submit("dbl", {"x": 1}, workflow_id="wf-stop")
        await host.client.stop(receipt.run_ref, info="recalled", source_ref="ops-console-7")

        update = await _first_command_update(host.client, receipt.run_ref, "stop")
        assert update.payload == {"verb": "stop", "info": "recalled", "source_ref": "ops-console-7"}

    async def test_scheduled_answers_dedup_identically_whatever_their_source_ref(self, home):
        slot = await _pause_once(home)
        client = RunHomeClient(home)
        due = _future_iso(days=1)

        first = await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=True, due_at=due, source_ref="console-a")
        differing = await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=True, due_at=due, source_ref="console-b")
        matching = await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=True, due_at=due, source_ref="console-a")
        absent = await client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=True, due_at=due)

        # A different caller label is not a different command.
        assert [r.duplicate for r in (first, differing, matching, absent)] == [False, True, True, True]
        rows = _scheduled_rows(home)
        assert len(rows) == 1 and rows[0][5] == "console-a"

    async def test_stops_dedup_identically_whatever_their_source_ref(self, home):
        host = serve(_plain_graph(), home=home, deployment_version="v1")
        receipt = await host.submit("dbl", {"x": 1}, workflow_id="wf-dedup")

        first = await host.client.stop(receipt.run_ref, info="one", source_ref="console-a")
        second = await host.client.stop(receipt.run_ref, info="two", source_ref="console-b")

        assert (first.duplicate, second.duplicate) == (False, True)
        assert len(_command_rows(home, "wf-dedup")) == 1

    async def test_submission_fingerprints_ignore_source_ref(self, home):
        host = serve(_plain_graph(), home=home, deployment_version="v1")
        first = await host.submit("dbl", {"x": 1}, workflow_id="wf-fp", source_ref="console-a")
        second = await host.submit("dbl", {"x": 1}, workflow_id="wf-fp", source_ref="console-b")

        # A differing source_ref is use-existing, never a WorkflowIdConflictError.
        assert (first.duplicate, second.duplicate) == (False, True)
        assert home._get_submission_sync("wf-fp")["source_ref"] == "console-a"

    async def test_source_ref_changes_nothing_but_the_audit_record(self, home):
        """Same command, two labels: identical admission, dedup, and outcome."""
        outcomes = {}
        for index, source_ref in enumerate((None, "console-x")):
            workflow_id = f"wf-src-{index}"
            slot = await _pause_once(home, workflow_id=workflow_id)
            client = RunHomeClient(home)
            ref = _ref(home, workflow_id)
            receipt = await client.schedule_answer(ref, pause_id=slot.pause_id, value=True, due_at=_past_iso(hours=1), source_ref=source_ref)
            fired = await home._settle_due_answers(_now_iso())
            outcomes[source_ref] = (receipt.duplicate, [outcome for _id, outcome in fired], (await client.get_run_slot(ref)).answer)
        assert outcomes[None] == outcomes["console-x"] == (False, [SCHEDULED_ANSWER_SETTLED], True)

    def test_no_dedup_or_eligibility_predicate_reads_source_ref(self):
        """Structural half: source_ref never appears in a WHERE clause."""
        from hypergraph.host import fingerprint as fingerprint_module

        files = [Path(inspect.getfile(RunHome))]
        files += [Path(inspect.getfile(fingerprint_module))]
        for path in files:
            for line in path.read_text().splitlines():
                if "WHERE" in line:
                    assert "source_ref" not in line, f"{path.name}: {line.strip()}"
        # And no fingerprint input can carry it.
        for name in ("start_fingerprint", "batch_fingerprint", "canonical_json"):
            params = inspect.signature(getattr(fingerprint_module, name)).parameters
            assert "source_ref" not in params

    def test_no_host_surface_treats_a_caller_label_as_identity(self):
        """There is no authentication in Hypergraph — that is the whole point."""
        import hypergraph
        import hypergraph.host as host_module

        forbidden = ("auth", "principal", "identity_token", "credential", "permission", "actor", "role")
        for module in (hypergraph, host_module):
            for exported in module.__all__:
                assert not [word for word in forbidden if word in exported.lower()], exported


# === 6. Excluded scheduling surfaces stay absent ===


class TestExcludedSchedulingSurfacesAreAbsent:
    """Checkbox 5: one pause-scoped verb, no scheduler, no reminders."""

    #: Cron, recurrence, reminders, assignment, escalation, and consensus are
    #: named Out of Scope by PRD 0017 and A11 in ADR 0008.
    FORBIDDEN = ("remind", "assign", "recur", "cron", "rrule", "escalat", "consensus", "cadence", "repeat_every", "claim_clock")

    def test_no_public_export_names_an_excluded_concept(self):
        import hypergraph
        import hypergraph.host as host_module

        for module in (hypergraph, host_module):
            exported = " ".join(module.__all__).lower()
            assert not [word for word in self.FORBIDDEN if word in exported], module.__name__

    def test_no_scheduling_surface_offers_recurrence_or_a_reminder(self):
        from hypergraph.host.host import Host

        surfaces = [
            RunHome.open,
            serve,
            Host.submit,
            Host.submit_sync,
            Host.submit_batch,
            Host.submit_batch_sync,
            Host.work_forever,
            RunHomeClient.stop,
            RunHomeClient.answer,
            RunHomeClient.schedule_answer,
            RunHomeClient.schedule_answer_sync,
            BatchTolerance.__init__,
        ]
        for surface in surfaces:
            names = " ".join(inspect.signature(surface).parameters).lower()
            assert not [word for word in self.FORBIDDEN if word in names], surface

    def test_scheduling_takes_exactly_one_occurrence_one_value_and_one_due_time(self):
        expected = ("self", "ref", "pause_id", "value", "due_at", "source_ref")
        assert tuple(inspect.signature(RunHomeClient.schedule_answer).parameters) == expected
        assert tuple(inspect.signature(RunHomeClient.schedule_answer_sync).parameters) == expected
        # No count, no interval, no end date, no timezone rule — those are the
        # parameters a recurring scheduler would need and this is not one.

    def test_no_public_method_accepts_a_caller_chosen_command_verb(self):
        from hypergraph.host.host import Host

        for surface in (RunHomeClient.stop, RunHomeClient.stop_sync, RunHomeClient.schedule_answer, Host.submit, Host.work_forever):
            assert "verb" not in inspect.signature(surface).parameters

    async def test_the_host_writes_exactly_two_command_verbs(self, home):
        host = serve(_plain_graph(), home=home, deployment_version="v1")
        receipt = await host.submit("dbl", {"x": 1}, workflow_id="wf-verbs")
        await host.client.stop(receipt.run_ref)
        slot = await _pause_once(home)
        await host.client.schedule_answer(_ref(home), pause_id=slot.pause_id, value=True, due_at=_future_iso(days=1))

        conn = sqlite3.connect(_db_path(home))
        try:
            verbs = {row[0] for row in conn.execute("SELECT DISTINCT verb FROM host_commands").fetchall()}
        finally:
            conn.close()
        assert verbs == {"stop", "schedule_answer"}

    def test_the_fire_outcome_vocabulary_is_closed_and_is_not_a_status(self):
        outcomes = {SCHEDULED_ANSWER_SETTLED, SCHEDULED_ANSWER_ALREADY_SETTLED, SCHEDULED_ANSWER_SUPERSEDED, SCHEDULED_ANSWER_REJECTED}
        assert outcomes == {"settled", "already_settled", "superseded", "rejected"}
        # A command outcome is audit data; it never enters execution truth.
        assert outcomes.isdisjoint({status.value for status in WorkflowStatus})

    async def test_nothing_non_interrupting_can_be_scheduled(self, home):
        """A reminder would need a schedulable thing that is not a pause answer."""
        host = serve(_plain_graph(), home=home, deployment_version="v1")
        receipt = await host.submit("dbl", {"x": 1}, workflow_id="wf-running")

        # A running (unpaused) run has nothing to schedule against.
        with pytest.raises(AnswerRejectedError, match="no durable pause"):
            await host.client.schedule_answer(receipt.run_ref, pause_id="wf-running:0:compute", value=True, due_at=_future_iso(days=1))
        assert _command_rows(home, "wf-running") == []


# === 7. Sync and async mirrors agree ===


class TestSyncAsyncParity:
    """Scheduling has a sync mirror; the worker-side due scan is async-only."""

    async def test_both_mirrors_persist_the_same_row(self, home):
        due = _future_iso(days=2)
        rows = {}
        for index, use_sync in enumerate((False, True)):
            workflow_id = f"wf-parity-{index}"
            slot = await _pause_once(home, workflow_id=workflow_id)
            client = RunHomeClient(home)
            ref = _ref(home, workflow_id)
            if use_sync:
                receipt = client.schedule_answer_sync(ref, pause_id=slot.pause_id, value=True, due_at=due, source_ref="c")
            else:
                receipt = await client.schedule_answer(ref, pause_id=slot.pause_id, value=True, due_at=due, source_ref="c")
            assert (receipt.verb, receipt.duplicate) == ("schedule_answer", False)
            row = _scheduled_rows(home, workflow_id)[0]
            rows[use_sync] = (row[1], row[3], json.loads(row[4])["value"], row[5], row[6], row[7])
        assert rows[False] == rows[True] == ("schedule_answer", due, True, "c", None, None)

    async def test_the_sync_mirror_dedupes_and_refuses_identically(self, home):
        slot = await _pause_once(home)
        client = RunHomeClient(home)
        due = _future_iso(days=1)

        with pytest.raises(AnswerRejectedError):
            client.schedule_answer_sync(_ref(home), pause_id=slot.pause_id, value="maybe", due_at=due)
        with pytest.raises(AnswerRejectedError, match="must name the pause occurrence"):
            client.schedule_answer_sync(_ref(home), value=True, due_at=due)
        assert client.schedule_answer_sync(_ref(home), pause_id=slot.pause_id, value=True, due_at=due).duplicate is False
        assert client.schedule_answer_sync(_ref(home), pause_id=slot.pause_id, value=False, due_at=due).duplicate is True
        with pytest.raises(TypeError, match="expects a RunRef"):
            client.schedule_answer_sync(BatchRef(home=home.uri, batch_id="b"), pause_id="x", value=True, due_at=due)  # type: ignore[arg-type]

    async def test_a_sync_scheduled_answer_is_fired_by_the_shared_scan(self, home):
        slot = await _pause_once(home)
        client = RunHomeClient(home)
        client.schedule_answer_sync(_ref(home), pause_id=slot.pause_id, value=True, due_at=_past_iso(hours=1))

        assert [outcome for _id, outcome in await home._settle_due_answers(_now_iso())] == [SCHEDULED_ANSWER_SETTLED]
        assert (await client.get_run_slot(_ref(home))).answer is True

    async def test_a_timer_that_lost_reports_the_same_refusal_a_human_would_get(self, home):
        """One cascade: the outcome string names the exception the human path raises."""
        graph = _loop_graph()
        runner = AsyncRunner(checkpointer=home)
        client = RunHomeClient(home)
        ref = _ref(home, "wf-loop")

        assert (await runner.run(graph, {}, workflow_id="wf-loop")).paused
        first = (await home.get_run_async("wf-loop")).pause_slot
        await client.schedule_answer(ref, pause_id=first.pause_id, value="late", due_at=_past_iso(hours=1))
        assert (await runner.run(graph, {"reply": "first"}, workflow_id="wf-loop")).paused

        assert [outcome for _id, outcome in await home._settle_due_answers(_now_iso())] == [SCHEDULED_ANSWER_SUPERSEDED]
        # A human answering that same superseded occurrence earns the error the
        # outcome string names — the timer is not judged by a softer rule.
        with pytest.raises(StalePauseError):
            await client.answer(ref, pause_id=first.pause_id, value="late")
