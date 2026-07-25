# 08 — Persist pending-node boundaries before sibling execution

**What to build:** Persist enough node-boundary intent that a process death
between sibling executions cannot forget unfinished siblings or make recovery
infer work from an incomplete superstep record.

**Blocked by:** 02 — Submit, execute, and watch one Run through the local Host.

**Status:** landed in commit `9e7546e2` — acceptance box 2 is only partially
delivered and stays unticked pending a maintainer decision (see the note).

- [x] Every runnable sibling remains durably attributable before any sibling can cause external work
- [ ] A real kill between sibling boundaries preserves completed facts and leaves unfinished siblings recoverable
- [x] Nested graph and loop behavior retain the same parent-facing execution identity
- [x] Sync and async runners expose the same recovery result
- [x] Existing checkpoint resume behavior remains compatible

## Note on box 2 — what is and is not delivered

**Delivered.** After a real SIGKILL mid-superstep, every runnable sibling of the
interrupted superstep is durably visible as a named `PENDING` boundary and is
recoverable: recovery reads the unfinished siblings instead of inferring them
from silence, and nothing is classified `UNKNOWN_EFFECT`. Facts committed in
*earlier, completed* supersteps survive the kill and are not re-executed.

**Not delivered.** "Preserves completed facts" does not hold *within* the killed
superstep. StepRecords are committed per superstep, not per node
(`async_/runner.py` `_save_superstep_records`), so a sibling that ran to
completion inside the killed superstep has no StepRecord. Its boundary is
therefore derived as `PENDING`, and it re-executes on restart. No boundary in a
killed superstep can truthfully read `COMMITTED`.

This is tolerated for repeat-safe work (PRD 0013: "this only wastes effort") and
is pinned by
`tests/test_host/test_ticket08_pending_boundaries.py::TestRealKillBetweenSiblingBoundaries::test_sibling_completed_inside_the_killed_superstep_re_executes`,
which documents the behavior rather than implying a stronger guarantee.

**Open maintainer decision** (deliberately not taken here): whether to add a
per-node settlement marker (e.g. `settled_at` on `pending_nodes`), move
StepRecord commit timing to per-node, or amend PRD 0013's "After" block to match
per-superstep reality. Ticket 09 (PRD 0014, effect identity reservation) needs
such a marker, so the decision is on its critical path. The schema and commit
timing are unchanged by this ticket's review pass.
