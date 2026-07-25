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
- Anything derived from `steps` must follow those steps through retention
  compaction. Dropping a step without its boundary would silently
  re-classify settled work as pending.

## Sync, Async, and Versions

- Keep memory and SQLite retention semantics aligned unless a test names the
  intentional backend difference.
- Keep sync and async checkpointer paths behaviorally aligned.
- Tests must pass on Python 3.10. Avoid test helpers that only exist in newer
  stdlib `sqlite3` APIs unless the test guards or falls back explicitly.
