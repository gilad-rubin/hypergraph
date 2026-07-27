# 04 — Stop, restart, and exhaust recovery safely

**What to build:** Let another process stop a Run durably, let repeat-safe
unfinished work continue after a real worker death, and stop poison work from
being adopted forever when repeated recovery makes no graph progress.

**Blocked by:** 03 — Make Run identity, deduplication, rerun, and fork truthful.

**Status:** done — landed in commit `12a4c9dc`; two-axis review findings repaired in `a8836312`.

- [x] Durable stop works from a detached client and races with completion by committed order
- [x] A real child-process kill followed by Run Home reopen continues repeat-safe work without resubmission
- [x] Committed steps stay complete across restart
- [x] A pinned recovery cap produces a visible recovery-exhausted condition without adding a WorkflowStatus
- [x] Run queries distinguish queued, scheduled, paused, incompatible, and recovery-exhausted work
