# Timed continuation is a scheduled pause-slot command, not a retry

**Status:** Accepted on 2026-07-23 at the durable-host amendment sitting
(decisions and provenance: `docs/research/2026-07-23-durable-host-amendments.md`;
delivery contract: `docs/prd/0017-durable-host-v1-program.md`). Amendments
A8 and A11 folded on 2026-07-24 (Durable Host V1 ticket 01). Constrained by
the locked retry/timeout contract (`docs/research/2026-07-14-retry-timeout-contract.md`)
and the canon grill finding that a bare `wake_at` column is unsound.

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
  `(workflow_id, pause_id, due_at, value)` — an ordinary host command that
  becomes applicable when store-authoritative time passes `due_at`.
- **One typed value, validated before settlement (A8).** The PauseSlot
  persists the graph-derived answer contract (`answer_type` → JSON
  `answer_schema` plus occurrence options). Every answer — human or
  scheduled — names the observed `pause_id`, validates one typed `value`
  against that schema before settlement, and leaves the slot open on
  rejection. No `serve()`-time validator callables.
- **Stale timers are rejected atomically.** Application carries the same
  CAS as a human answer: apply only if `pause_id` is still the current
  pause. An answered pause silently voids its timer.
- **Answer-versus-timer races resolve by commit order**, same as every host
  command race; the loser receives a truthful rejection.
- **Reminders are not scheduled answers (A11).** A non-interrupting
  reminder must never consume a workflow pause; reminders, human-task
  assignment, claim/availability clocks, escalation, and consensus live in
  the product or superposition layer, outside Hypergraph. This corrects the
  earlier framing in which one mechanism was said to serve reminders.
- **Never a retry.** Scheduled answers do not touch attempt budgets, retry
  windows, jitter, or `retry_not_before`; "retry the payment in 24h" is a
  business-workflow continuation expressed in graph logic (a pause with a
  scheduled timeout answer), not an attempt-ledger operation. Cron and
  recurring schedules stay outside the host (OS/product side), per the
  locked boundary.

## Consequences

- One mechanism serves timeouts and delayed continuations, with a value that
  tells the graph *why* it woke (`{"approved": False, "timed_out": True}`).
- Scheduled answers share a due-row scanner with delayed starts (`start_at`)
  but remain pause-scoped commands; recurring schedules and general
  scheduled commands are excluded.
- Requires durable pause slots first (PRD 0010); implemented as PRD 0012 on
  the local tier before any shared-tier work.
