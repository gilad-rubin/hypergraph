# 13 — Persist typed pause slots and settle answers

**What to build:** Make each interrupt occurrence a durable typed PauseSlot
and let RunHomeClient answer the observed occurrence atomically. Rejected,
double, and stale answers must leave execution truth intact across restart.

**Blocked by:** 02 — Submit, execute, and watch one Run through the local Host.

**Status:** done — landed in commit `9dab714e`

- [x] PauseSlot persists the graph-derived answer schema, occurrence options, answer port, and unique pause id with the paused transition
- [x] Human answer validates one typed value before atomic settlement
- [x] Rejected values leave the current PauseSlot open
- [x] Double and stale answers receive distinct truthful errors and answer-versus-stop races resolve by commit order
- [x] Loop, nested graph, sync, async, Memory, and SQLite behavior remain consistent across restart

Tests: `tests/test_host/test_ticket13_pause_slots.py` (58 tests, both
backends parametrized). Suite: 4402 passed, 15 skipped (baseline 4344/15).

## Reading of box 5's "sync, async" and "Memory"

`SyncRunner` declares no interrupt support (`supports_interrupts=False`), so a
graph with an `InterruptNode` raises `IncompatibleRunnerError` before it can
pause — there is no sync *runner* pause to compare against. Box 5 is therefore
proved on the axis PRD 0010 actually names: "sync and async **checkpointer
paths** behave identically". `record_pause_sync` / `get_pause_slot_sync` /
`settle_pause_sync` are proved against the same store as their async
counterparts (`TestSyncAsyncParity`), including the stale/double/rejected
cascade. The sync runner template calls the same `commit_pause_sync` helper so
the two templates cannot drift when a sync interrupt runner arrives; that
template branch is unexercised in-tree today.

**Memory's "across restart" clause is unprovable, not proven.** No test
restarts anything for `MemoryCheckpointer`, and none can: an in-process store
does not survive a restart, so that half of box 5 is vacuous rather than
demonstrated. What IS proved for Memory is every rule that does not need a
restart — the slot contract, the settlement cascade, the three refusals, loop
supersession, and (after the two-axis review) field-for-field first-record-wins
on re-record — all parametrized against SQLite by the `backend` fixture, so the
two backends cannot silently disagree. Restart itself is proved on SQLite only,
including a real `SIGKILL` in `TestRealProcessKill`. The box stays ticked
because everything provable in it holds; this note is the honest scope of the
"Memory ... across restart" phrase.

## Atomicity: what "one transaction" covers

`SqliteCheckpointer.record_pause` opens one `BEGIN IMMEDIATE` and commits the
buffered step records, the pause slot, and the runs-row transition to
`paused` together (`test_the_pause_commit_is_all_or_nothing` kills the last
write and shows the earlier ones roll back). Under `durability="exit"` the
paused StepRecord is genuinely buffered into that transaction
(`test_buffered_step_records_commit_with_the_slot_and_the_status`). Under
`durability="sync"`/`"async"` the paused StepRecord was already committed by
the ordinary per-superstep step path — untouched by this ticket per its
constraints — so the ordering is: step record, then slot + `PAUSED` together.
The invariant the PRD names holds in every mode: **no committed `PAUSED` run
without its slot.**

## Deliberately not built

- Scheduled/timed answers and `source_ref` on `answer()` (ticket 14, ADR
  0008). The slot shape carries what a scheduled answer needs — a stable
  `pause_id` to CAS on and a persisted schema to revalidate against — with no
  scheduling machinery.
- Re-delivering a settled answer to a worker. Settlement writes durable
  resume input; worker loops are out of scope for this ticket, so a settled
  run is resumed the Tier-0 way
  (`runner.run(graph, {slot.response_key: slot.answer}, workflow_id=...)`).
  Note that today a paused run's submission is already marked `finished` by
  the worker, so a paused run is not re-claimable at all — closing that is
  the same worker-loop slice.
