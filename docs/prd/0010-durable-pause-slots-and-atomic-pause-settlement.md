# 0010 — Durable pause slots and atomic pause settlement

status: accepted-intent (ADRs 0005/0008 accepted 2026-07-23 with amendments; A8 folded 2026-07-24, Durable Host V1 ticket 01; implementation is ticket 13)

## Why this is first for answering

Every durable-host verb that touches a paused run needs a durable pause
identity, and none exists today: `PauseInfo` (question value + response key)
lives only on the in-memory `RunResult`; the persisted paused StepRecord
carries neither the question projection nor the answer port. A crash after
the pause commits leaves no recoverable answer slot — so `answer()`,
scheduled answers (ADR 0008), and answer dedup are all impossible until this
lands. See `docs/research/2026-07-21-durable-host-canon-grill.md` finding 2.
Per A1, this PRD gates only the `answer()` verb and scheduled answers — not
the local Host wedge (submit/stop/watch/restart-recovery), which lands first
under PRD 0011.

## Fixed acceptance contract

Before (today — pause truth dies with the process):

```python
result = await runner.run(refund_graph, {"claim_id": "c-42"},
                          workflow_id="refund-c-42")
assert result.paused
result.pause.value          # question — memory only
result.pause.response_key   # answer port — memory only
# process exits; a new process cannot discover what was asked,
# which port answers it, or which pause occurrence is current.
```

After:

```python
run = await checkpointer.get_run_async("refund-c-42")
slot = run.pause_slot                       # durable, or None
assert slot.pause_id == "refund-c-42:8:approval"   # workflow:superstep:node
assert slot.response_key == "approved"
assert slot.question == {...}               # JSON-safe projection, not the live object
assert slot.answer_schema == {"type": "boolean"}   # graph-derived from answer_type
# Settling names the observed occurrence and validates one typed value
# before the atomic write:
await checkpointer.settle_pause("refund-c-42",
                                pause_id=slot.pause_id,
                                value=True)
# settle without pause_id, or with a value failing answer_schema →
# AnswerRejectedError; the slot stays open
# second settle of the same pause_id → PauseAlreadySettledError
# settle with a stale pause_id (a later pause is current) → StalePauseError
```

Requirements:

- `pause_id` identifies one durable interrupt occurrence
  (workflow / superstep / node address); repeated pauses in loops produce
  distinct ids.
- The pause slot is written in the same transaction as the paused step's
  records and the run-status transition to `PAUSED` — no window in which the
  run is paused but the slot is missing.
- The slot persists the graph-derived answer contract (A8): `answer_type`
  rendered as a JSON `answer_schema`, plus the occurrence options. There are
  no `serve()`-time validator callables; validation is schema-driven at
  settlement time.
- Every answer names the observed `pause_id` and carries exactly one typed
  `value`. A value failing `answer_schema` is rejected before any write and
  the slot stays open; rejection never consumes the pause occurrence.
- Settlement is a CAS on the current `pause_id`, in one transaction with the
  resume-input write; it never invents execution truth (the resumed run
  still flows through normal `is_resuming` semantics).
- The question stored is a JSON-safe projection; the live handler object
  never enters the journal.
- Nested-graph interrupts: the slot carries the parent-facing address
  (boundary projection rules per `runners/_shared/AGENTS.md`).
- Sync and async checkpointer paths behave identically; Memory and SQLite
  backends both implement it.

## Test plan (red first)

- Crash injection between pause execution and any later observation: a fresh
  process reads the slot and settles it.
- Loop graph pausing twice: late settle against the first `pause_id` raises
  `StalePauseError`; the second occurrence settles cleanly.
- Double settle: second caller gets `PauseAlreadySettledError`, first
  caller's value wins.
- Schema rejection: a wrong-typed value raises `AnswerRejectedError` and the
  same occurrence remains settleable afterwards.
- Nested interrupt: slot address matches the parent-facing port.
- CI-equivalent run green (`uv run pytest -W error -W 'ignore::pytest.PytestUnraisableExceptionWarning'`).

## Out of scope

Host verbs, worker loops, scheduled answers (PRD 0012), any Postgres work.
