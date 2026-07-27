# 0013 — Persist pending-node boundaries before sibling execution

status: accepted-intent (required by the amendment package build order; folded 2026-07-24, Durable Host V1 ticket 01; implementation is ticket 08)

## Why this exists

Today a superstep's record is written when the superstep completes. A
process that dies mid-superstep — after one sibling node committed and
before its siblings ran — leaves a journal gap: the completed sibling is
recorded, the unfinished siblings are not, and recovery must infer what was
pending from an incomplete record (canon grill: mid-superstep sibling
loss). For repeat-safe work this only wastes effort; once siblings may
cause external effects (PRD 0014), inferring pending work after the fact is
unsound.

## Fixed acceptance contract

Before (today — pending siblings die with the process):

```python
# superstep: [fetch_protocol, extract_entities, notify_review]
# fetch_protocol commits; process dies before extract_entities runs.
# The journal shows one completed node and no durable record that
# extract_entities and notify_review were ever pending.
```

After:

```python
# Before any sibling in a superstep can cause external work, the Run Home
# durably records the superstep's runnable node boundaries as pending.
# After a kill between sibling boundaries, recovery reads:
#   fetch_protocol    → committed (StepRecord)
#   extract_entities  → pending (never started; safe to dispatch)
#   notify_review     → pending (never started; safe to dispatch)
# Unfinished siblings stay attributable; nothing is inferred from silence.
```

Requirements:

- Every runnable sibling boundary is persisted as pending before any
  sibling in that superstep can cause external work.
- A pending boundary is intent, not execution truth: StepRecords remain the
  sole execution journal, and a pending record never claims a node ran.
- Recovery distinguishes three states per boundary without guessing:
  committed (StepRecord present), pending (recorded, never dispatched or
  never settled), and unknown effect (PRD 0014, declared effectful nodes
  only).
- A real kill between sibling boundaries preserves completed facts and
  leaves unfinished siblings recoverable and visible.
- Nested graphs and loops retain the same parent-facing execution identity;
  pending records use the same node addressing as the pause slot
  (PRD 0010).
- Sync and async runners expose the same recovery result; existing
  checkpoint resume behavior remains compatible.

## Test plan (red first)

- Kill between two sibling boundaries in one superstep → restart →
  completed sibling not re-executed; pending siblings dispatch exactly
  once.
- Nested-graph sibling boundaries: parent-facing addresses match across the
  kill.
- Loop graph: pending records from an interrupted iteration do not leak
  into the next iteration's identity.
- Sync + async parity; CI-equivalent run green.

## Out of scope

Effect identity reservation itself (PRD 0014), lease/epoch fencing
(Postgres tier), any change to StepRecord semantics as the execution
journal.
