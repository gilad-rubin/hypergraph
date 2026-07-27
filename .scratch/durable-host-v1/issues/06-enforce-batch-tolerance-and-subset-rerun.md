# 06 — Enforce Batch tolerance and item-scoped rerun

**What to build:** Let a Batch pin optional count and percentage failure
tolerances, stop admitting new children when either is exceeded, and rerun
named source items without changing their Definition identity or inputs.

**Blocked by:** 05 — Submit and watch an immutable durable Batch.

**Status:** done — landed in commit `4c6f81c5`

- [x] Count and percentage tolerances trip only when failures strictly exceed their thresholds
- [x] Percentage always uses total logical manifest items as its denominator
- [x] Failed and recovery-exhausted children count while paused, queued, delayed, and unstarted children do not
- [x] A tripped Batch lets claimed children settle, marks the rest unstarted, and remains partial
- [x] Subset rerun accepts only source item keys and records new Run or Batch lineage without input overrides
