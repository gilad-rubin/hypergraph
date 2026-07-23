# 0010 — Durable pause slots and atomic pause settlement

status: draft (blocked on ADR 0005/0008 acceptance)

## Why this is first

Every durable-host verb that touches a paused run needs a durable pause
identity, and none exists today: `PauseInfo` (question value + response key)
lives only on the in-memory `RunResult`; the persisted paused StepRecord
carries neither the question projection nor the answer port. A crash after
the pause commits leaves no recoverable answer slot — so `answer()`,
scheduled answers (ADR 0008), and answer dedup are all impossible until this
lands. See `docs/research/2026-07-21-durable-host-canon-grill.md` finding 2.

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
# Settling is atomic and occurrence-checked:
await checkpointer.settle_pause("refund-c-42",
                                pause_id=slot.pause_id,
                                values={"approved": True})
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
  caller's values win.
- Nested interrupt: slot address matches the parent-facing port.
- CI-equivalent run green (`uv run pytest -W error -W 'ignore::pytest.PytestUnraisableExceptionWarning'`).

## Out of scope

Host verbs, worker loops, scheduled answers (PRD 0012), any Postgres work.
