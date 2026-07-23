# 06 — Enforce Batch tolerance and item-scoped rerun

**What to build:** Let a Batch pin optional count and percentage failure
tolerances, stop admitting new children when either is exceeded, and rerun
named source items without changing their Definition identity or inputs.

**Blocked by:** 05 — Submit and watch an immutable durable Batch.

**Status:** ready-for-agent

- [ ] Count and percentage tolerances trip only when failures strictly exceed their thresholds
- [ ] Percentage always uses total logical manifest items as its denominator
- [ ] Failed and recovery-exhausted children count while paused, queued, delayed, and unstarted children do not
- [ ] A tripped Batch lets claimed children settle, marks the rest unstarted, and remains partial
- [ ] Subset rerun accepts only source item keys and records new Run or Batch lineage without input overrides
