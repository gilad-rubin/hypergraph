"""Durable Host V1 — ticket 13 / PRD 0010: typed pause slots and settlement.

Covers: a paused run persists the graph-derived answer contract, the
occurrence options, the answer port, and a unique pause id in the SAME
transaction as the paused step's records and the ``PAUSED`` transition; a
human answer validates one typed value before an atomic compare-and-set; a
rejected value never consumes the occurrence; double, stale, and
answer-versus-stop races produce distinct truthful outcomes decided by commit
order; loop, nested-graph, Memory and SQLite behavior agree across restart.

Two scope notes, both deliberate:

- **Sync vs async is a CHECKPOINTER axis here, not a runner axis.** No shipped
  runner other than ``AsyncRunner`` declares ``supports_interrupts``
  (``SyncRunner`` raises ``IncompatibleRunnerError`` on a graph with an
  InterruptNode), so "sync and async paths behave identically" is proved
  against the checkpointer's sync mirrors — ``record_pause_sync`` /
  ``get_pause_slot_sync`` / ``settle_pause_sync`` — which is what PRD 0010
  actually requires.
- **Settlement writes durable resume input; it does not resume the run.**
  Worker loops are out of scope for this ticket (PRD 0010 "Out of scope"), so
  the loop tests resume through the runner the way Tier 0 does today.
"""

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import ClassVar, NamedTuple, TypedDict

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
    interrupt,
    node,
    route,
)
from hypergraph.checkpointers import (
    Checkpointer,
    MemoryCheckpointer,
    PauseSlot,
    Run,
    RunTotals,
    SqliteCheckpointer,
    StepRecord,
    StepStatus,
    WorkflowStatus,
    node_address,
)
from hypergraph.checkpointers._answer_schema import (
    UNRENDERABLE_KEY,
    is_unconstrained,
    render_answer_schema,
    validate_answer,
)
from hypergraph.host import HostError
from hypergraph.host.refs import BatchRef
from hypergraph.runners._shared.pause_slots import supports_pause_slots

aiosqlite = pytest.importorskip("aiosqlite")


# === Question types (the structural ask seam) ===


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


class Score:
    """A declared answer type the JSON-Schema renderer cannot express."""


@dataclass(frozen=True)
class Verdict:
    """A declared answer type with an honest JSON form: an object."""

    approved: bool
    note: str = ""


@dataclass(frozen=True)
class Opaque:
    answer_type: ClassVar[object] = Score
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple = ()


@dataclass(frozen=True)
class Adjudicate:
    answer_type: ClassVar[object] = Verdict
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple = ()


# === Graphs ===


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


def _pick_graph() -> Graph:
    @node(output_name="draft")
    def draft(claim_id: str) -> str:
        return f"draft-{claim_id}"

    @interrupt(answer_name="route_to")
    def triage(draft: str) -> Pick:
        return Pick(prompt="Where does this go?", options=("billing", "fraud"), evidence=(draft,))

    @node(output_name="final")
    def deliver(route_to: str) -> str:
        return f"sent-{route_to}"

    return Graph([draft, triage, deliver], name="triage")


def _opaque_graph() -> Graph:
    @node(output_name="draft")
    def draft(claim_id: str) -> str:
        return f"draft-{claim_id}"

    @interrupt(answer_name="rating")
    def rate(draft: str) -> Opaque:
        return Opaque(prompt="Rate it", evidence=(draft,))

    return Graph([draft, rate], name="rating")


def _verdict_graph() -> Graph:
    @node(output_name="draft")
    def draft(claim_id: str) -> str:
        return f"draft-{claim_id}"

    @interrupt(answer_name="verdict")
    def adjudicate(draft: str) -> Adjudicate:
        return Adjudicate(prompt="Adjudicate", evidence=(draft,))

    return Graph([draft, adjudicate], name="verdict")


def _loop_graph() -> Graph:
    """A real loop: the same interrupt node pauses on two distinct supersteps.

    ``ask`` takes no required input, so each resume delivers only the answer
    port — the shape a durable answer actually replays.
    """

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


def _nested_graph() -> Graph:
    @node(output_name="inner_draft")
    def inner_prep(seed: int) -> str:
        return f"d{seed}"

    @interrupt(answer_name="verdict")
    def inner_ask(inner_draft: str) -> Pick:
        return Pick(prompt="ok?", options=("yes", "no"), evidence=(inner_draft,))

    inner = Graph([inner_prep, inner_ask], name="inner")

    @node(output_name="seed")
    def seed_node(x: int) -> int:
        return x

    return Graph([seed_node, inner.as_node(name="review")], name="outer")


# === Fixtures and helpers ===


def _home_uri(tmp_path, filename: str = "runs.db") -> str:
    return f"file:{tmp_path / filename}"


@pytest_asyncio.fixture
async def home(tmp_path):
    h = RunHome.open(_home_uri(tmp_path))
    yield h
    await h.close()


@pytest_asyncio.fixture
async def sqlite_cp(tmp_path):
    cp = SqliteCheckpointer(str(tmp_path / "cp.db"))
    yield cp
    await cp.close()


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def backend(request, tmp_path):
    """Both backends implement the seam; every shared rule runs on both."""
    if request.param == "memory":
        yield MemoryCheckpointer()
        return
    cp = SqliteCheckpointer(str(tmp_path / f"{request.param}.db"))
    yield cp
    await cp.close()


def _read_slot_rows(db_path: str, run_id: str) -> list[tuple]:
    """Read committed pause_slots rows over a FRESH connection.

    A fresh connection can only see what reached the database file, so this is
    durability evidence rather than in-process bookkeeping.
    """
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT pause_id, response_key, answer_schema, settled_at, answer FROM pause_slots WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()


def _read_run_status(db_path: str, run_id: str) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
    except sqlite3.OperationalError:
        # A child process that has not created the schema yet.
        return None
    finally:
        conn.close()
    return None if row is None else str(row[0])


async def _pause_once(checkpointer, graph=None, workflow_id="refund-c-42", inputs=None) -> PauseSlot:
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


# === 1. The slot persists the whole answer contract with the pause ===


class TestSlotPersistsTheAnswerContract:
    """Checkbox 1: schema, options, answer port, and a unique id per occurrence."""

    async def test_paused_run_exposes_the_prd_acceptance_contract(self, backend):
        slot = await _pause_once(backend)

        assert slot.pause_id == "refund-c-42:1:approval"
        assert slot.pause_id == node_address("refund-c-42", 1, "approval")
        assert slot.response_key == "approved"
        assert slot.question == {
            "prompt": "Approve this refund?",
            "options": ["yes", "no"],
            "evidence": ["draft-c-42"],
            "answer_type": "builtins.bool",
        }
        # bool cannot accept the display options, so they stay display data.
        assert slot.answer_schema == {"type": "boolean"}
        assert slot.options == ("yes", "no")
        assert slot.is_open and slot.settled_at is None and slot.answer is None

    async def test_pause_id_is_the_paused_step_address(self, backend):
        slot = await _pause_once(backend)
        steps = await backend.get_steps("refund-c-42")
        paused = [step for step in steps if step.status is StepStatus.PAUSED]
        assert [node_address(step.run_id, step.superstep, step.node_name) for step in paused] == [slot.pause_id]

    async def test_options_narrow_the_schema_when_the_declared_type_accepts_them(self, backend):
        slot = await _pause_once(backend, _pick_graph(), workflow_id="triage-1")
        assert slot.answer_schema == {"type": "string", "enum": ["billing", "fraud"]}

        with pytest.raises(AnswerRejectedError, match="does not satisfy its answer schema"):
            await backend.settle_pause("triage-1", pause_id=slot.pause_id, value="legal")
        settled = await backend.settle_pause("triage-1", pause_id=slot.pause_id, value="fraud")
        assert settled.answer == "fraud"

    async def test_an_unrenderable_answer_type_says_so_instead_of_reading_empty(self, backend):
        slot = await _pause_once(backend, _opaque_graph(), workflow_id="rating-1")
        # The renderer cannot express `Score`, so it invents no constraint —
        # but it RECORDS that it gave up, naming the type. An operator reading
        # this slot can tell it apart from a handler that declared nothing.
        assert slot.answer_schema == {UNRENDERABLE_KEY: f"{Score.__module__}.Score"}
        assert is_unconstrained(slot.answer_schema)
        assert slot.question["answer_type"].endswith("Score")

        # A declared `Any` stores the plain empty schema — equally
        # unconstrained, and visibly a different thing.
        assert render_answer_schema(None) == {}
        assert UNRENDERABLE_KEY not in render_answer_schema(None)

        settled = await backend.settle_pause("rating-1", pause_id=slot.pause_id, value={"stars": 4})
        assert settled.answer == {"stars": 4}

    async def test_a_value_the_journal_cannot_hold_is_rejected(self, backend):
        slot = await _pause_once(backend, _opaque_graph(), workflow_id="rating-2")
        with pytest.raises(AnswerRejectedError, match="not JSON-serializable"):
            await backend.settle_pause("rating-2", pause_id=slot.pause_id, value=object())
        current = await backend.get_pause_slot("rating-2")
        assert current.is_open

    async def test_an_unrenderable_type_tells_the_caller_what_to_send_instead(self, backend):
        """The accepted set is the JSON *form* of the declared type.

        A `Score` instance is not JSON-safe, so it can never be a durable
        answer. The refusal has to say that out loud, naming the type the
        renderer could not express — otherwise the caller reads
        "not JSON-serializable" and has no idea the declared type is
        unreachable by construction.
        """
        slot = await _pause_once(backend, _opaque_graph(), workflow_id="rating-3")
        with pytest.raises(AnswerRejectedError) as caught:
            await backend.settle_pause("rating-3", pause_id=slot.pause_id, value=Score())
        assert "could not be rendered as JSON Schema" in str(caught.value)
        assert "Score" in str(caught.value)

    async def test_a_dataclass_answer_type_renders_as_an_object(self, backend):
        """Dataclasses are the domain-class case, and they have an honest JSON form."""
        slot = await _pause_once(backend, _verdict_graph(), workflow_id="verdict-1")
        assert slot.answer_schema == {"type": "object"}

        with pytest.raises(AnswerRejectedError, match="expected type 'object'"):
            await backend.settle_pause("verdict-1", pause_id=slot.pause_id, value="approve")
        settled = await backend.settle_pause("verdict-1", pause_id=slot.pause_id, value={"approved": True, "note": "ok"})
        assert settled.answer == {"approved": True, "note": "ok"}

    async def test_question_evidence_never_carries_the_live_object(self, backend):
        class Untouchable:
            pass

        @node(output_name="payload")
        def build() -> object:
            return Untouchable()

        @interrupt(answer_name="ok")
        def ask(payload: object) -> Confirm:
            return Confirm(prompt="fine?", evidence=(payload,))

        graph = Graph([build, ask], name="opaque-evidence")
        result = await AsyncRunner(checkpointer=backend).run(graph, {}, workflow_id="wf-ev")
        assert result.paused
        slot = (await backend.get_run_async("wf-ev")).pause_slot
        assert slot.question["evidence"] == [{"__unserializable__": "Untouchable"}]
        # Whatever the projection holds, it round-trips through JSON.
        json.dumps(slot.question)


# === 2. Slot, step records, and PAUSED are one transaction ===


class TestAtomicPauseCommit:
    """Checkbox 1: no window in which the run is paused but the slot is missing."""

    async def test_a_fresh_connection_sees_the_slot_with_the_paused_status(self, tmp_path):
        db_path = str(tmp_path / "atomic.db")
        cp = SqliteCheckpointer(db_path)
        try:
            await _pause_once(cp)
        finally:
            await cp.close()

        assert _read_run_status(db_path, "refund-c-42") == "paused"
        rows = _read_slot_rows(db_path, "refund-c-42")
        assert [(row[0], row[1], row[3]) for row in rows] == [("refund-c-42:1:approval", "approved", None)]

    @staticmethod
    def _paused_step_names(db_path: str, run_id: str) -> list[str]:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT node_name FROM steps WHERE run_id = ? AND status = 'paused'", (run_id,)).fetchall()
        finally:
            conn.close()
        return [str(row[0]) for row in rows]

    async def test_the_pause_commit_is_all_or_nothing(self, tmp_path, monkeypatch):
        """Kill the LAST write of the pause transaction; the earlier ones roll back.

        Injected at the seam that owns the transaction, so the runner's own
        unwinding cannot repaint the evidence. If the slot, the step records,
        and the status flip were three transactions, the first two would
        survive here — which is exactly the window "no run is paused without
        its slot" forbids.
        """
        db_path = str(tmp_path / "rollback.db")
        cp = SqliteCheckpointer(db_path)

        import hypergraph.checkpointers.sqlite as sqlite_module

        def _boom(*args, **kwargs):
            raise RuntimeError("commit interrupted")

        try:
            await cp.create_run("wf-atomic", graph_name="refund")
            slot = PauseSlot(
                run_id="wf-atomic",
                superstep=1,
                node_name="approval",
                response_key="approved",
                answer_schema={"type": "boolean"},
            )
            record = StepRecord(
                run_id="wf-atomic",
                superstep=1,
                node_name="approval",
                index=0,
                status=StepStatus.PAUSED,
                input_versions={},
            )
            monkeypatch.setattr(sqlite_module, "_run_status_update", _boom)
            with pytest.raises(RuntimeError, match="commit interrupted"):
                await cp.record_pause(slot, step_records=(record,))
            monkeypatch.undo()

            assert _read_slot_rows(db_path, "wf-atomic") == []
            assert _read_run_status(db_path, "wf-atomic") == "active"
            assert self._paused_step_names(db_path, "wf-atomic") == []

            # And everything handed to the call lands together when nothing fails.
            await cp.record_pause(slot, step_records=(record,))
            assert _read_run_status(db_path, "wf-atomic") == "paused"
            assert self._paused_step_names(db_path, "wf-atomic") == ["approval"]
            assert len(_read_slot_rows(db_path, "wf-atomic")) == 1
        finally:
            await cp.close()

    async def test_buffered_step_records_commit_with_the_slot_and_the_status(self, tmp_path):
        """``durability="exit"`` buffers the paused step record to the pause,
        so the runner really does hand all three facts to one transaction."""
        db_path = str(tmp_path / "exit.db")
        cp = SqliteCheckpointer(db_path, durability="exit", retention="latest")
        try:
            await _pause_once(cp)
        finally:
            await cp.close()

        assert _read_run_status(db_path, "refund-c-42") == "paused"
        assert self._paused_step_names(db_path, "refund-c-42") == ["approval"]
        assert len(_read_slot_rows(db_path, "refund-c-42")) == 1

    async def test_the_durable_pause_resets_the_recovery_brake(self, home):
        """A committed pause is committed progress (PRD 0017), and the pause
        fact that says so is the same transaction's status update."""
        slot = await _pause_once(home)
        updates = await home._read_run_updates("refund-c-42")
        statuses = [json.loads(payload) for _seq, kind, payload, _at in updates if kind == "status"]
        assert {"status": "paused", "pause_id": slot.pause_id} in statuses


# === 2b. Re-recording one pause address: first record wins, on both backends ===


#: Every stored field a replay must not rewrite (``settled_at``/``answer``
#: get their own test).
_PRESERVED_SLOT_FIELDS = (
    "run_id",
    "superstep",
    "node_name",
    "node_path",
    "response_key",
    "question",
    "answer_schema",
    "options",
    "created_at",
)


def _original_slot(run_id: str) -> PauseSlot:
    return PauseSlot(
        run_id=run_id,
        superstep=1,
        node_name="approval",
        node_path="approval",
        response_key="approved",
        question={"prompt": "Approve this refund?", "options": ["yes", "no"], "evidence": [], "answer_type": "builtins.bool"},
        answer_schema={"type": "boolean"},
        options=("yes", "no"),
    )


def _replayed_slot(run_id: str) -> PauseSlot:
    """The SAME address carrying a different contract in every field."""
    return PauseSlot(
        run_id=run_id,
        superstep=1,
        node_name="approval",
        node_path="rewritten/approval",
        response_key="rewritten_key",
        question={"prompt": "REWRITTEN", "options": None, "evidence": ["late"], "answer_type": "builtins.str"},
        answer_schema={"type": "string"},
        options=("maybe",),
    )


class TestReRecordingOneAddress:
    """First record wins, field for field, identically on both backends.

    ``pause_id`` IS the occurrence, so a resume that replays the pause must
    not rewrite what was asked. SQLite enforces this with
    ``ON CONFLICT(pause_id) DO NOTHING``; Memory has to agree, or the same
    replay would yield a different stored question depending on which
    backend is behind the Run Home.
    """

    async def test_a_replayed_address_leaves_every_stored_field_alone(self, backend):
        await backend.create_run("wf-replay", graph_name="refund")
        first = _original_slot("wf-replay")
        await backend.record_pause(first)

        await backend.record_pause(_replayed_slot("wf-replay"))

        stored = await backend.get_pause_slot("wf-replay")
        for field in _PRESERVED_SLOT_FIELDS:
            assert getattr(stored, field) == getattr(first, field), field
        assert stored.is_open

    async def test_a_replayed_address_never_reopens_a_settled_occurrence(self, backend):
        await backend.create_run("wf-replay-settled", graph_name="refund")
        first = _original_slot("wf-replay-settled")
        await backend.record_pause(first)
        settled = await backend.settle_pause("wf-replay-settled", pause_id=first.pause_id, value=True)

        await backend.record_pause(_replayed_slot("wf-replay-settled"))

        stored = await backend.get_pause_slot("wf-replay-settled")
        assert stored.settled_at == settled.settled_at
        assert stored.answer is True
        assert stored.answer_schema == {"type": "boolean"}

    async def test_a_replay_adds_no_occurrence_but_a_later_pause_does(self, backend):
        await backend.create_run("wf-replay-count", graph_name="refund")
        first = _original_slot("wf-replay-count")
        await backend.record_pause(first)
        await backend.record_pause(_replayed_slot("wf-replay-count"))
        assert (await backend.get_pause_slot("wf-replay-count")).pause_id == first.pause_id

        # A later superstep is a DIFFERENT address, so it really does append.
        later = replace(first, superstep=3)
        await backend.record_pause(later)
        assert (await backend.get_pause_slot("wf-replay-count")).pause_id == later.pause_id
        assert await backend.get_pause_slot("wf-replay-count", pause_id=first.pause_id) is not None


# === 3. Human answer: one typed value, then atomic settlement ===


class TestHumanAnswer:
    """Checkboxes 2 and 3."""

    async def test_settlement_writes_the_resume_input_for_the_answer_port(self, backend):
        slot = await _pause_once(backend)
        settled = await backend.settle_pause("refund-c-42", pause_id=slot.pause_id, value=True)

        assert settled.pause_id == slot.pause_id
        assert settled.response_key == "approved"
        assert settled.answer is True
        assert settled.settled_at is not None and not settled.is_open
        assert (await backend.get_pause_slot("refund-c-42")).answer is True

        # The settled value IS the resume input: replaying it through the
        # answer port completes the run.
        result = await AsyncRunner(checkpointer=backend).run(
            _approval_graph(),
            {settled.response_key: settled.answer},
            workflow_id="refund-c-42",
        )
        assert result.status.value == "completed"
        assert result["final"] == "settled-True"

    async def test_a_wrong_typed_value_leaves_the_same_occurrence_settleable(self, backend):
        slot = await _pause_once(backend)

        with pytest.raises(AnswerRejectedError) as rejected:
            await backend.settle_pause("refund-c-42", pause_id=slot.pause_id, value="yes")
        assert rejected.value.pause_id == slot.pause_id
        assert rejected.value.issues

        still_open = await backend.get_pause_slot("refund-c-42")
        assert still_open.is_open and still_open.answer is None
        # Rejection never consumed the occurrence.
        settled = await backend.settle_pause("refund-c-42", pause_id=slot.pause_id, value=False)
        assert settled.answer is False

    async def test_an_answer_must_name_the_occurrence(self, backend):
        slot = await _pause_once(backend)
        with pytest.raises(AnswerRejectedError, match="must name the pause occurrence"):
            await backend.settle_pause("refund-c-42", value=True)
        assert (await backend.get_pause_slot("refund-c-42")).is_open
        assert slot.pause_id in str((await backend.get_pause_slot("refund-c-42")).pause_id)

    async def test_an_unknown_pause_id_is_rejected_not_called_stale(self, backend):
        await _pause_once(backend)
        with pytest.raises(AnswerRejectedError, match="Unknown pause_id"):
            await backend.settle_pause("refund-c-42", pause_id="refund-c-42:99:approval", value=True)
        assert (await backend.get_pause_slot("refund-c-42")).is_open

    async def test_a_run_with_no_durable_pause_cannot_be_answered(self, backend):
        await backend.create_run("plain", graph_name="none")
        with pytest.raises(AnswerRejectedError, match="no durable pause"):
            await backend.settle_pause("plain", pause_id="plain:0:x", value=True)


# === 4. Double, stale, and answer-versus-stop ===


class TestDistinctRefusals:
    """Checkbox 4: three refusals that never blur into one another."""

    async def test_double_settle_keeps_the_first_answer(self, backend):
        slot = await _pause_once(backend)
        await backend.settle_pause("refund-c-42", pause_id=slot.pause_id, value=True)

        with pytest.raises(PauseAlreadySettledError) as second:
            await backend.settle_pause("refund-c-42", pause_id=slot.pause_id, value=False)
        assert second.value.pause_id == slot.pause_id
        assert (await backend.get_pause_slot("refund-c-42")).answer is True

    async def test_concurrent_answers_elect_exactly_one_winner(self, sqlite_cp):
        slot = await _pause_once(sqlite_cp)
        values = [True, False, True, False]

        results = await asyncio.gather(
            *(sqlite_cp.settle_pause("refund-c-42", pause_id=slot.pause_id, value=value) for value in values),
            return_exceptions=True,
        )
        winners = [item for item in results if isinstance(item, PauseSlot)]
        losers = [item for item in results if isinstance(item, BaseException)]
        assert len(winners) == 1
        assert all(isinstance(item, PauseAlreadySettledError) for item in losers)
        assert (await sqlite_cp.get_pause_slot("refund-c-42")).answer == winners[0].answer

    async def test_a_loop_supersedes_its_earlier_occurrence(self, backend):
        graph = _loop_graph()
        runner = AsyncRunner(checkpointer=backend)

        assert (await runner.run(graph, {}, workflow_id="wf-loop")).paused
        first = (await backend.get_run_async("wf-loop")).pause_slot
        await backend.settle_pause("wf-loop", pause_id=first.pause_id, value="first")

        assert (await runner.run(graph, {"reply": "first"}, workflow_id="wf-loop")).paused
        second = (await backend.get_run_async("wf-loop")).pause_slot

        # Repeated pauses in loops produce distinct ids — the superstep moved.
        assert first.pause_id == "wf-loop:0:ask"
        assert second.pause_id == "wf-loop:4:ask"
        assert second.question["prompt"] == "turn 1"

        with pytest.raises(StalePauseError) as stale:
            await backend.settle_pause("wf-loop", pause_id=first.pause_id, value="late")
        assert stale.value.pause_id == first.pause_id
        assert stale.value.current_pause_id == second.pause_id

        settled = await backend.settle_pause("wf-loop", pause_id=second.pause_id, value="second")
        assert settled.answer == "second"
        # The first occurrence keeps the answer that settled it.
        assert (await backend.get_pause_slot("wf-loop", pause_id=first.pause_id)).answer == "first"

    async def test_answer_and_stop_resolve_by_commit_order(self, home):
        """Answer first: the answer stands and a later stop is still accepted."""
        slot = await _pause_once(home)
        client = RunHomeClient(home)
        ref = RunRef(home=home.uri, run_id="refund-c-42")

        settled = await client.answer(ref, pause_id=slot.pause_id, value=True)
        assert settled.answer is True

        receipt = await client.stop(ref, info="operator changed their mind")
        assert receipt.duplicate is False
        assert (await client.get_run_slot(ref)).answer is True

    async def test_a_stop_that_commits_first_beats_the_answer(self, home):
        """Stop first: once its terminal transition is committed, the answer
        is refused and the occurrence is left exactly as it was."""
        slot = await _pause_once(home)
        client = RunHomeClient(home)
        ref = RunRef(home=home.uri, run_id="refund-c-42")

        await client.stop(ref, info="cancelled")
        # The transition a worker commits when it applies that stop.
        await home.update_run_status("refund-c-42", WorkflowStatus.STOPPED)

        with pytest.raises(AnswerRejectedError, match="is stopped, not paused"):
            await client.answer(ref, pause_id=slot.pause_id, value=True)
        assert (await client.get_run_slot(ref)).is_open

    async def test_a_real_answer_stop_race_settles_one_way_or_the_other(self, home):
        """Both writers contend on the same store; the invariant holds either way."""
        slot = await _pause_once(home)
        client = RunHomeClient(home)
        ref = RunRef(home=home.uri, run_id="refund-c-42")
        ready = asyncio.Event()

        async def answer():
            await ready.wait()
            return await client.answer(ref, pause_id=slot.pause_id, value=True)

        async def stop():
            await ready.wait()
            await home.update_run_status("refund-c-42", WorkflowStatus.STOPPED)
            return "stopped"

        task_answer = asyncio.create_task(answer())
        task_stop = asyncio.create_task(stop())
        ready.set()
        answered, _ = await asyncio.gather(task_answer, task_stop, return_exceptions=True)

        final = await client.get_run_slot(ref)
        if isinstance(answered, BaseException):
            # The stop's terminal transition committed first.
            assert isinstance(answered, AnswerRejectedError)
            assert final.is_open and final.answer is None
        else:
            assert final.answer is True


# === 5. Nested graphs: the slot carries the parent-facing address ===


class TestNestedProjection:
    """Checkbox 5: boundary projection rules, unchanged across restart."""

    async def test_the_parent_slot_names_the_parent_facing_port(self, tmp_path):
        db_path = str(tmp_path / "nested.db")
        cp = SqliteCheckpointer(db_path)
        try:
            result = await AsyncRunner(checkpointer=cp).run(_nested_graph(), {"x": 3}, workflow_id="wf-nested")
            assert result.paused
            parent = await cp.get_run_async("wf-nested")
            child = await cp.get_run_async("wf-nested/review")
            parent_steps = await cp.get_steps("wf-nested")
        finally:
            await cp.close()

        slot = parent.pause_slot
        # The parent-facing address: the delegating GraphNode in THIS run,
        # exactly the address its paused StepRecord carries.
        assert slot.pause_id == node_address("wf-nested", 1, "review")
        assert slot.node_name == "review"
        assert slot.node_path == "review/inner_ask"
        assert slot.response_key == "verdict"
        paused = [step for step in parent_steps if step.status is StepStatus.PAUSED]
        assert [node_address(s.run_id, s.superstep, s.node_name) for s in paused] == [slot.pause_id]

        # The child run records its own occurrence under the child workflow id.
        assert child.pause_slot.pause_id == node_address("wf-nested/review", 1, "inner_ask")
        assert child.pause_slot.response_key == "verdict"

        # Reopening the same file is the whole point: nothing was in-process.
        reopened = SqliteCheckpointer(db_path)
        try:
            after = await reopened.get_run_async("wf-nested")
            assert after.pause_slot.pause_id == slot.pause_id
            assert after.pause_slot.node_path == "review/inner_ask"
            settled = await reopened.settle_pause("wf-nested", pause_id=slot.pause_id, value="yes")
            assert settled.answer == "yes"
        finally:
            await reopened.close()

    async def test_the_nested_answer_schema_comes_from_the_inner_node(self, sqlite_cp):
        result = await AsyncRunner(checkpointer=sqlite_cp).run(_nested_graph(), {"x": 1}, workflow_id="wf-n2")
        assert result.paused
        slot = (await sqlite_cp.get_run_async("wf-n2")).pause_slot
        assert slot.answer_schema == {"type": "string", "enum": ["yes", "no"]}


# === 6. Sync and async checkpointer paths ===


class TestSyncAsyncParity:
    """PRD 0010: "Sync and async checkpointer paths behave identically"."""

    async def test_the_sync_mirrors_read_and_settle_the_same_occurrence(self, sqlite_cp):
        slot = await _pause_once(sqlite_cp)

        assert sqlite_cp.get_pause_slot_sync("refund-c-42") == slot
        assert sqlite_cp.get_run("refund-c-42").pause_slot == slot

        with pytest.raises(AnswerRejectedError):
            sqlite_cp.settle_pause_sync("refund-c-42", pause_id=slot.pause_id, value="nope")

        settled = sqlite_cp.settle_pause_sync("refund-c-42", pause_id=slot.pause_id, value=True)
        assert settled.answer is True
        assert (await sqlite_cp.get_pause_slot("refund-c-42")).answer is True

        with pytest.raises(PauseAlreadySettledError):
            sqlite_cp.settle_pause_sync("refund-c-42", pause_id=slot.pause_id, value=False)

    async def test_the_sync_mirror_reports_staleness_like_the_async_path(self, sqlite_cp):
        graph = _loop_graph()
        runner = AsyncRunner(checkpointer=sqlite_cp)
        assert (await runner.run(graph, {}, workflow_id="wf-loop")).paused
        first = sqlite_cp.get_pause_slot_sync("wf-loop")
        sqlite_cp.settle_pause_sync("wf-loop", pause_id=first.pause_id, value="first")
        assert (await runner.run(graph, {"reply": "first"}, workflow_id="wf-loop")).paused
        second = sqlite_cp.get_pause_slot_sync("wf-loop")

        assert second.pause_id != first.pause_id
        with pytest.raises(StalePauseError) as stale:
            sqlite_cp.settle_pause_sync("wf-loop", pause_id=first.pause_id, value="late")
        assert stale.value.current_pause_id == second.pause_id
        assert sqlite_cp.settle_pause_sync("wf-loop", pause_id=second.pause_id, value="second").answer == "second"

    async def test_record_pause_sync_commits_the_same_three_facts(self, tmp_path):
        """The sync write path is the template mirror; no shipped sync runner
        pauses yet, so it is proved directly against the backend seam."""
        db_path = str(tmp_path / "syncwrite.db")
        cp = SqliteCheckpointer(db_path)
        try:
            cp.create_run_sync("wf-sync", graph_name="manual")
            slot = PauseSlot(
                run_id="wf-sync",
                superstep=2,
                node_name="approval",
                response_key="approved",
                question={"prompt": "ok?", "options": None, "evidence": [], "answer_type": "builtins.bool"},
                answer_schema={"type": "boolean"},
            )
            record = StepRecord(
                run_id="wf-sync",
                superstep=2,
                node_name="approval",
                index=0,
                status=StepStatus.PAUSED,
                input_versions={},
            )
            cp.record_pause_sync(slot, step_records=(record,), totals=RunTotals(node_count=1, error_count=0))
        finally:
            await cp.close()

        assert _read_run_status(db_path, "wf-sync") == "paused"
        rows = _read_slot_rows(db_path, "wf-sync")
        assert [(row[0], row[1], row[3]) for row in rows] == [("wf-sync:2:approval", "approved", None)]

    async def test_the_sync_write_path_also_lets_the_first_record_win(self, sqlite_cp):
        """Re-record parity: the sync mirror keeps the same first-record-wins rule."""
        sqlite_cp.create_run_sync("wf-sync-replay", graph_name="manual")
        first = _original_slot("wf-sync-replay")
        sqlite_cp.record_pause_sync(first)

        sqlite_cp.record_pause_sync(_replayed_slot("wf-sync-replay"))

        stored = sqlite_cp.get_pause_slot_sync("wf-sync-replay")
        for field in _PRESERVED_SLOT_FIELDS:
            assert getattr(stored, field) == getattr(first, field), field
        assert stored == await sqlite_cp.get_pause_slot("wf-sync-replay")


# === 7. RunHomeClient.answer ===


class TestClientSurface:
    """Checkbox 2 through the public client."""

    async def test_answer_settles_the_observed_occurrence(self, home):
        slot = await _pause_once(home)
        client = RunHomeClient(home)
        ref = RunRef(home=home.uri, run_id="refund-c-42")

        observed = await client.get_run_slot(ref)
        assert observed == slot
        view = await client.get(ref)
        assert view.status is WorkflowStatus.PAUSED

        settled = await client.answer(ref, pause_id=observed.pause_id, value=True)
        assert settled.answer is True
        assert client.get_run_slot_sync(ref).answer is True

    async def test_the_sync_client_mirror_settles_identically(self, home):
        slot = await _pause_once(home, workflow_id="refund-sync")
        client = RunHomeClient(home)
        ref = RunRef(home=home.uri, run_id="refund-sync")
        settled = client.answer_sync(ref, pause_id=slot.pause_id, value=False)
        assert settled.answer is False

    async def test_a_batch_ref_is_refused_loudly(self, home):
        client = RunHomeClient(home)
        with pytest.raises(TypeError, match="answer\\(\\) expects a RunRef"):
            await client.answer(BatchRef(home=home.uri, batch_id="b"), pause_id="x", value=1)

    async def test_every_settlement_refusal_is_a_host_error(self, home):
        """A caller wrapping client calls in ``except HostError`` sees these.

        They are raised deep in the checkpointer but they SURFACE through
        ``client.answer``, so they are durable-host refusals like any other.
        ``RuntimeError`` stays in the bases for clauses that already target it.
        """
        slot = await _pause_once(home, workflow_id="refund-host-error")
        client = RunHomeClient(home)
        ref = RunRef(home=home.uri, run_id="refund-host-error")

        with pytest.raises(HostError):
            await client.answer(ref, pause_id=slot.pause_id, value="not-a-bool")
        with pytest.raises(HostError):
            client.answer_sync(ref, pause_id="refund-host-error:99:approval", value=True)

        await client.answer(ref, pause_id=slot.pause_id, value=True)
        with pytest.raises(HostError):
            await client.answer(ref, pause_id=slot.pause_id, value=False)

        for refusal in (AnswerRejectedError, PauseAlreadySettledError, StalePauseError):
            assert issubclass(refusal, HostError)
            assert issubclass(refusal, RuntimeError)

    async def test_watch_replays_the_pause_and_the_answer_as_durable_facts(self, home):
        slot = await _pause_once(home)
        client = RunHomeClient(home)
        ref = RunRef(home=home.uri, run_id="refund-c-42")
        await client.answer(ref, pause_id=slot.pause_id, value=True)

        rows = await home._read_run_updates("refund-c-42")
        facts = [(kind, json.loads(payload)) for _seq, kind, payload, _at in rows]
        assert ("status", {"status": "paused", "pause_id": slot.pause_id}) in facts
        assert ("answer", {"pause_id": slot.pause_id, "response_key": "approved"}) in facts


# === 8. Backends without the seam keep working ===


class TestOptionalSeam:
    async def test_a_checkpointer_without_the_seam_still_pauses(self):
        class BareCheckpointer(Checkpointer):
            def __init__(self):
                super().__init__()
                self.steps: list = []
                self.runs: dict = {}

            async def save_step(self, record):
                self.steps.append(record)

            async def create_run(self, run_id, **kwargs):
                run = Run(id=run_id, status=WorkflowStatus.ACTIVE)
                self.runs[run_id] = run
                return run

            async def update_run_status(self, run_id, status, **kwargs):
                self.runs[run_id].status = status

            async def get_state(self, run_id, *, superstep=None):
                return {}

            async def get_steps(self, run_id, *, superstep=None, show_internal=False):
                return list(self.steps)

            async def get_run_async(self, run_id):
                return self.runs.get(run_id)

            async def list_runs(self, **kwargs):
                return list(self.runs.values())

        bare = BareCheckpointer()
        assert supports_pause_slots(bare, sync=False) is False
        assert supports_pause_slots(None, sync=False) is False
        assert supports_pause_slots(MemoryCheckpointer(), sync=False) is True

        result = await AsyncRunner(checkpointer=bare).run(_approval_graph(), {"claim_id": "c-1"}, workflow_id="bare-1")
        assert result.paused
        assert bare.runs["bare-1"].status is WorkflowStatus.PAUSED
        assert any(step.status is StepStatus.PAUSED for step in bare.steps)


# === 9. The answer-schema renderer ===


class _Ticket(TypedDict):
    subject: str


class _Coordinate(NamedTuple):
    x: int
    y: int


class _Tier(Enum):
    GOLD = "gold"
    SILVER = "silver"


class TestAnswerSchemaRendering:
    @pytest.mark.parametrize(
        ("answer_type", "expected"),
        [
            (bool, {"type": "boolean"}),
            (int, {"type": "integer"}),
            (float, {"type": "number"}),
            (str, {"type": "string"}),
            (list, {"type": "array"}),
            (dict, {"type": "object"}),
            (list[str], {"type": "array"}),
            (str | None, {"anyOf": [{"type": "string"}, {"type": "null"}]}),
            # Types with an honest JSON form all render to one.
            (Verdict, {"type": "object"}),
            (_Ticket, {"type": "object"}),
            (_Coordinate, {"type": "array"}),
            (_Tier, {"enum": ["gold", "silver"]}),
            # Only a type with no expressible JSON form falls through — and
            # it says which type, rather than reading like a bare `Any`.
            (Score, {UNRENDERABLE_KEY: f"{Score.__module__}.Score"}),
            (None, {}),
        ],
    )
    def test_render(self, answer_type, expected):
        assert render_answer_schema(answer_type) == expected

    def test_a_union_with_an_unrenderable_member_names_the_union(self):
        rendered = render_answer_schema(str | Score)
        assert list(rendered) == [UNRENDERABLE_KEY]
        assert "Score" in rendered[UNRENDERABLE_KEY]

    def test_integer_rejects_bool_and_number_accepts_int(self):
        assert validate_answer({"type": "integer"}, True)
        assert validate_answer({"type": "integer"}, 3) == ()
        assert validate_answer({"type": "number"}, 3) == ()

    def test_options_only_narrow_a_schema_that_accepts_them(self):
        assert render_answer_schema(str, ("a", "b")) == {"type": "string", "enum": ["a", "b"]}
        assert render_answer_schema(bool, ("yes", "no")) == {"type": "boolean"}
        # An unconstrained contract cannot know whether the declared type
        # accepts these labels, so it never narrows on them.
        assert render_answer_schema(Score, ("a", "b")) == {UNRENDERABLE_KEY: f"{Score.__module__}.Score"}
        assert render_answer_schema(None, ("a", "b")) == {}

    def test_an_unconstrained_schema_accepts_anything_json_safe(self):
        for schema in ({}, {UNRENDERABLE_KEY: "pkg.Score"}):
            assert is_unconstrained(schema)
            assert validate_answer(schema, {"deep": [1, 2]}) == ()
            assert validate_answer(schema, object())


# === 10. Real process kill: a fresh process reads the slot and settles it ===


_PAUSE_KILL_SCRIPT = """
import asyncio
from dataclasses import dataclass
from typing import ClassVar

from hypergraph import AsyncRunner, Graph, RunHome, interrupt, node, serve

MARKER = {marker!r}


@dataclass(frozen=True)
class Confirm:
    answer_type: ClassVar[object] = bool
    prompt: str
    options: tuple = ("yes", "no")
    evidence: tuple = ()


@node(output_name="draft")
def draft(claim_id: str) -> str:
    return "draft-" + claim_id


@interrupt(answer_name="approved")
def approval(draft: str) -> Confirm:
    with open(MARKER, "a") as handle:
        handle.write("asked\\n")
    return Confirm(prompt="Approve this refund?", evidence=(draft,))


@node(output_name="final")
def settle(approved: bool) -> str:
    return "settled-" + str(approved)


graph = Graph([draft, approval, settle], name="refund").with_runner(AsyncRunner())
home = RunHome.open({uri!r})
host = serve(graph, home=home, deployment_version="v1")
host.submit_sync(graph, {{"claim_id": "c-42"}}, workflow_id="wf-kill")
asyncio.run(host.work_forever("w-child", poll_interval=0.02))
"""

_FRESH_READER_SCRIPT = """
import asyncio

from hypergraph import RunHome, RunHomeClient, RunRef


async def main():
    home = RunHome.open({uri!r})
    try:
        client = RunHomeClient(home)
        ref = RunRef(home=home.uri, run_id="wf-kill")
        slot = await client.get_run_slot(ref)
        assert slot is not None, "a killed pause left no durable slot"
        with open({out!r}, "w") as handle:
            handle.write(slot.pause_id + "|" + slot.response_key + "|" + str(slot.answer_schema))
        await client.answer(ref, pause_id=slot.pause_id, value=True)
    finally:
        await home.close()


asyncio.run(main())
"""


def _wait_for_paused(db_path: str, run_id: str, proc, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _read_run_status(db_path, run_id) == "paused":
            return
        if proc.poll() is not None:
            raise AssertionError("child worker exited before the run paused")
        time.sleep(0.05)
    raise AssertionError("child never committed a paused run")


class TestRealProcessKill:
    """PRD 0010 test plan: crash between pause execution and any observation."""

    async def test_a_fresh_process_reads_the_killed_run_s_slot_and_settles_it(self, tmp_path):
        db_path = str(tmp_path / "kill.db")
        uri = f"file:{db_path}"
        marker = tmp_path / "marker.txt"
        out = tmp_path / "slot.txt"

        proc = subprocess.Popen([sys.executable, "-c", _PAUSE_KILL_SCRIPT.format(marker=str(marker), uri=uri)])
        try:
            _wait_for_paused(db_path, "wf-kill", proc)
            proc.kill()
            proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()

        assert marker.exists() and "asked" in marker.read_text()

        # A DIFFERENT process, with no memory of the run, answers it.
        reader = subprocess.run(
            [sys.executable, "-c", _FRESH_READER_SCRIPT.format(uri=uri, out=str(out))],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert reader.returncode == 0, reader.stderr

        pause_id, response_key, schema = out.read_text().split("|")
        assert pause_id == node_address("wf-kill", 1, "approval")
        assert response_key == "approved"
        assert schema == "{'type': 'boolean'}"

        rows = _read_slot_rows(db_path, "wf-kill")
        assert len(rows) == 1
        assert rows[0][0] == pause_id
        assert rows[0][3] is not None  # settled_at
        assert json.loads(rows[0][4]) is True
