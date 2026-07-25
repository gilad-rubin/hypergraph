# Checkpointers Agent Guide

Persistent run history is user-visible state. Prefer convergent compaction over
row-count shortcuts.

## Retention and Baselines

- Baseline records are state-carrying records, not ordinary node executions.
  Retention compaction must fold existing baselines into new baselines and keep
  baseline rows out of normal `max_superstep` window calculations.
- Compaction should converge if it runs repeatedly or after a partial run. Tests
  should prove that reconstructible state is preserved after pruning.
- SQLite deletes and updates can touch many rows. Batch parameterized `IN`
  operations below SQLite bind limits rather than building one huge statement.

## Derived Records

- A pending node boundary is intent; its `BoundaryState` is derived by
  joining `steps` on `(run_id, superstep, node_name)`. Never store the
  derived state, and never let a boundary row assert that a node ran.
- The state cascade itself lives once, in `types.derive_boundary_state`.
  Backends shape rows and call it; they must not re-derive the states, or a
  new state would have to be added in every backend.
- Anything derived from `steps` must follow those steps through retention
  compaction. Dropping a step without its boundary would silently
  re-classify settled work as pending.
- A durable pause slot is NOT derived: it carries the question projection and
  the human answer, so retention must not prune it with its step. Keep new
  durable records on the right side of this line before wiring compaction.

## Durable Pause Slots

- `record_pause` is atomic by contract: everything handed to it — the slot,
  any still-buffered step records, and the run's transition to `PAUSED` —
  commits as ONE transaction. Never split them, and never add a write between
  them. State the guarantee as the invariant, not as a write count: the
  paused StepRecord is always `<=` the slot and never after it, so no reader
  observes a committed `PAUSED` run without its slot. (Only `durability="exit"`
  still has the step record buffered at this point; `"sync"`/`"async"`
  committed it earlier through the ordinary step path.)
- First record wins per `pause_id`, in EVERY backend: the address IS the
  occurrence, so a replay must leave the whole stored row alone — question,
  schema, options, answer port, `created_at`, and any settlement.
- Settlement is a compare-and-set on the CURRENT `pause_id`. Every refusal
  (`AnswerRejectedError` / `PauseAlreadySettledError` / `StalePauseError`)
  must be raised before any write, so a refused answer leaves the occurrence
  open. All three are `HostError`s: they surface through
  `RunHomeClient.answer`, so `except HostError` must catch them.
- The decision cascade lives once, in `base._check_settlement`. Backends
  supply the rows and perform the CAS; they must not re-derive which refusal
  applies, or Memory and SQLite would drift.
- The answer contract is data, never a callable: render `answer_type` through
  `_answer_schema.render_answer_schema` and check values with
  `validate_answer`. The schema describes the JSON *form* of the declared
  type (a settled answer is durable resume input, so it is always JSON-safe)
  and never claims a keyword `validate_answer` does not check. A type the
  renderer cannot express is recorded as such (`UNRENDERABLE_KEY`) rather
  than silently degrading to `{}` — never a guessed constraint, and never a
  pause-time raise.

## Sync, Async, and Versions

- Keep memory and SQLite retention semantics aligned unless a test names the
  intentional backend difference.
- Keep sync and async checkpointer paths behaviorally aligned.
- Tests must pass on Python 3.10. Avoid test helpers that only exist in newer
  stdlib `sqlite3` APIs unless the test guards or falls back explicitly.
