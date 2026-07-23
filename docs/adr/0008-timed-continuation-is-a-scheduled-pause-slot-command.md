# Timed continuation is a scheduled pause-slot command, not a retry

**Status:** Proposed on 2026-07-21, pending maintainer review (wayfinder map ticket). Constrained by the locked retry/timeout contract (`docs/research/2026-07-14-retry-timeout-contract.md`) and the canon grill finding that a bare `wake_at` column is unsound.

## Context

"If Dana doesn't answer in 72 hours, auto-escalate" needs a run that wakes on
*time*, not only on an answer. A bare timestamp column cannot do this: it
supplies neither the answer port nor the values a resumed `InterruptNode`
requires, and a timer armed for pause A must never fire into a later pause B
(repeated pauses in loops make this race real). Separately, node-owned
`RetryPolicy` is locked canon — a host timer must not become a backdoor retry
mechanism.

## Decision

- **The durable unit is a scheduled answer:**
  `(workflow_id, pause_id, due_at, values)` — an ordinary host command that
  becomes applicable when store-authoritative time passes `due_at`.
- **Stale timers are rejected atomically.** Application carries the same
  CAS as a human answer: apply only if `pause_id` is still the current
  pause. An answered pause silently voids its timer.
- **Answer-versus-timer races resolve by commit order**, same as every host
  command race; the loser receives a truthful rejection.
- **Never a retry.** Scheduled answers do not touch attempt budgets, retry
  windows, jitter, or `retry_not_before`; "retry the payment in 24h" is a
  business-workflow continuation expressed in graph logic (a pause with a
  scheduled timeout answer), not an attempt-ledger operation. Cron and
  recurring schedules stay outside the host (OS/product side), per the
  locked boundary.

## Consequences

- One mechanism serves timeouts, reminders, and delayed continuations, with
  values that tell the graph *why* it woke (`{"approved": False,
  "timed_out": True}`).
- Requires durable pause slots first (PRD 0010); implemented as PRD 0012 on
  the local tier before any shared-tier work.
