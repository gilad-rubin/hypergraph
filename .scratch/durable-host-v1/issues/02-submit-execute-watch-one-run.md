# 02 — Submit, execute, and watch one Run through the local Host

**What to build:** Let a caller bind one Definition, submit one Run into a
SQLite Run Home, receive a RunRef, watch it through RunHomeClient, and observe
the real runner complete it in a product-owned worker. Direct runner execution
must remain unchanged.

**Blocked by:** 01 — Lock the Durable Host contract and publish the decision prototype; explicit maintainer approval of that prototype.

**Status:** ready-for-agent

- [ ] One accepted submission persists before execution and completes through the Definition's runner
- [ ] RunHomeClient can get and watch the Run from a process that did not submit it
- [ ] Durable Run updates use a reconnectable cursor while live previews cannot advance it
- [ ] SQLite enforces one worker owner with explicit startup, bounded drain, and lock release
- [ ] Direct runner and process-local Execution handle behavior remain unchanged
