# 08 — Persist pending-node boundaries before sibling execution

**What to build:** Persist enough node-boundary intent that a process death
between sibling executions cannot forget unfinished siblings or make recovery
infer work from an incomplete superstep record.

**Blocked by:** 02 — Submit, execute, and watch one Run through the local Host.

**Status:** landed in commit `9e7546e2`; acceptance box 2 completed by #330
(per-node settlement marker, 2026-09-13 — see the note).

- [x] Every runnable sibling remains durably attributable before any sibling can cause external work
- [x] A real kill between sibling boundaries preserves completed facts and leaves unfinished siblings recoverable
- [x] Nested graph and loop behavior retain the same parent-facing execution identity
- [x] Sync and async runners expose the same recovery result
- [x] Existing checkpoint resume behavior remains compatible

## Note on box 2 — what is and is not delivered

**Delivered.** After a real SIGKILL mid-superstep, every runnable sibling of the
interrupted superstep is durably visible as a named `PENDING` boundary and is
recoverable: recovery reads the unfinished siblings instead of inferring them
from silence, and nothing is classified `UNKNOWN_EFFECT`. Facts committed in
*earlier, completed* supersteps survive the kill and are not re-executed.

**Not delivered by this ticket.** "Preserves completed facts" did not hold
*within* the killed superstep. StepRecords are committed per superstep, not per
node (`async_/runner.py` `_save_superstep_records`), so a sibling that ran to
completion inside the killed superstep has no StepRecord. Its boundary was
therefore derived as `PENDING` — byte-identical to a sibling that never
started — and it re-executes on restart. No boundary in a killed superstep can
truthfully read `COMMITTED`.

**Decision taken (2026-09-13, #330): the per-node settlement marker.** Of the
three options below the line, the marker was chosen: the schema seam already
existed, PRD 0014 needs exactly this signal, and it renegotiates neither
journal-write batching nor `CheckpointPolicy.durability` timing.

What shipped:

- `pending_nodes` gains a nullable `settled_at` (schema v9, one guarded ALTER;
  a v8 database migrates in place and reads NULL, which is exactly what a v8
  process knew). It is distinct from `dispatched_at`, which stays the PRD 0014
  effect seam.
- Each node marks its own boundary the instant its result is in hand — before
  that result is folded into shared state, because the async fold cannot start
  until every sibling has returned and a mark written there would never survive
  the kill it exists to describe. Sync and async settle at the same instant.
- `derive_boundary_state` gains a fourth state, `SETTLED_UNRECORDED`, ranked
  under `COMMITTED` and above `UNKNOWN_EFFECT`: a boundary that settled is not
  an unknown effect, because the node returned.
- **StepRecord commit timing is unchanged** — still per superstep. It follows
  that no boundary in a killed superstep reads `COMMITTED`; what changed is
  that "ran, record lost" and "never ran" are now distinguishable.
- Resume is deliberately unchanged: a `SETTLED_UNRECORDED` pure node has no
  recorded output to restore, so it re-executes exactly as a `PENDING` one did.
  That re-dispatch is still what PRD 0013 tolerates ("this only wastes
  effort"); the marker buys the READING, which is what an effectful node needs.

Pinned by
`tests/test_host/test_ticket08_pending_boundaries.py::TestRealKillBetweenSiblingBoundaries::test_sibling_completed_inside_the_killed_superstep_is_readable_as_settled`
and by `TestPerNodeSettlement` in the same file. PRD 0013's "After" block
carries the amendment; its Requirements list is left as the historical record
of what this ticket fixed.

**The options that were on the table:** add a per-node settlement marker (e.g.
`settled_at` on `pending_nodes`) — chosen; move StepRecord commit timing to
per-node (N transactions per superstep instead of one, and
`CheckpointPolicy.durability` timing would need re-examining); or amend PRD
0013's "After" block only, leaving ticket 09 (PRD 0014, effect identity
reservation) to invent its own marker. Ticket 09 was on this decision's
critical path and is now unblocked.
