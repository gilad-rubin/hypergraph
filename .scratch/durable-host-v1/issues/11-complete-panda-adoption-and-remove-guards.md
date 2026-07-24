# 11 — Complete Panda durable-host adoption and remove product guards

**What to build:** Move Panda ingestion and review background work fully onto
the durable Host, prove restart safety for repeat-safe and effectful paths,
and then remove the queue organs, temporary restart and duplicate guards,
BackgroundTasks, and reflective resource closer that Hypergraph now replaces.

**Blocked by:** 07 — Prove Panda integration on repeat-safe work; 08 — Persist pending-node boundaries before sibling execution; 09 — Reserve effect identity and surface unknown outcomes; 10 — Land stateful resource lifecycle for consumer cleanup. ALSO BLOCKED on the Panda prod drop: this ticket deletes protections prod is running on right now, so it waits for an explicit maintainer go-ahead after ticket 07's controlled proof.

**Status:** blocked-external (Panda prod drop + ticket 07)

**Guard-deletion scope (grown 2026-07-24):** beyond the issue-0025 guards and
BackgroundTasks, this ticket also owns removal of whatever guards Panda issue
0029 (`docs/issues/0029-durable-cancel-and-single-drive.md` on Panda main)
adds — a per-work-item drive registry and/or a persistence-seam change. No
guard is deleted without its equivalent Host proof: the 0029 cancel-stickiness
guard maps to durable stop (ticket 04), the 0029 double-retry guard maps to
start-fingerprint dedup (ticket 03), the drive-registry guard maps to durable
submission identity.

- [ ] Ingestion and review work use the Host worker lifecycle rather than product BackgroundTasks
- [ ] Real kill tests preserve completed work and surface unsafe ambiguous effects for review
- [ ] Temporary restart, duplicate, and queue bookkeeping are removed only after equivalent Host proof passes
- [ ] Issue-0029 guards (drive registry and/or persistence-seam change) are removed only after the ticket-07 proof shows the Host makes them unnecessary
- [ ] Reflective graph cleanup is removed in favor of scoped resource lifecycle
- [ ] The operator walkthrough shows what became possible and the production test suite passes
