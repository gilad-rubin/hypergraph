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

- `record_pause` is atomic by contract: the slot, the paused step's records,
  and the run's transition to `PAUSED` commit as ONE transaction. Never split
  them, and never add a write between them.
- Settlement is a compare-and-set on the CURRENT `pause_id`. Every refusal
  (`AnswerRejectedError` / `PauseAlreadySettledError` / `StalePauseError`)
  must be raised before any write, so a refused answer leaves the occurrence
  open.
- The decision cascade lives once, in `base._check_settlement`. Backends
  supply the rows and perform the CAS; they must not re-derive which refusal
  applies, or Memory and SQLite would drift.
- The answer contract is data, never a callable: render `answer_type` through
  `_answer_schema.render_answer_schema` and check values with
  `validate_answer`. A type the renderer cannot express becomes the empty
  schema — never a guessed constraint.

## Sync, Async, and Versions

- Keep memory and SQLite retention semantics aligned unless a test names the
  intentional backend difference.
- Keep sync and async checkpointer paths behaviorally aligned.
- Tests must pass on Python 3.10. Avoid test helpers that only exist in newer
  stdlib `sqlite3` APIs unless the test guards or falls back explicitly.
