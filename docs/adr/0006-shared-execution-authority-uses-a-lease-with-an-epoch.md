# Shared execution authority uses a lease with an epoch

**Status:** Accepted on 2026-07-23 at the durable-host amendment sitting
(decisions and provenance: `docs/research/2026-07-23-durable-host-amendments.md`;
delivery contract: `docs/prd/0017-durable-host-v1-program.md`). Amendments
A6 and A10 folded on 2026-07-24 (Durable Host V1 ticket 01). Derived
independently by all three clean-room designs and the anchored review
(`docs/research/2026-07-21-cleanroom-durable-host-experiment.md`); risk
analysis in `docs/research/2026-07-21-durable-host-canon-grill.md`.

**The tier boundary below is superseded (2026-08-05).** SQLite now claims
with a lease: `host_submissions.claimed_by` / `lease_until`, renewed by the
holder and adopted on expiry, with `claim_seq` as the fence. The exclusive
worker lock and `WorkerLockError` are retired. Everything else in this ADR
stands, and the local tier now implements more of it than the boundary
allowed — see "Tier boundary" for what changed and what still does not hold
here.

## Context

Two processes must never both write one workflow's history. Remote death is
unprovable: a worker presumed dead may only be partitioned and can wake up
mid-write. The only sound primitive is revoking *authority to write* —
enforcement must therefore live at the moment of writing, not in a check
performed before it.

## Decision

- **Claiming a workflow takes a lease carrying a monotonic epoch.** Each
  takeover increments the epoch. Heartbeats renew `expires_at`; expiry makes
  the workflow claimable and proves nothing about the old worker.
- **Every mutation is fenced in-transaction.** Journal writes, attempt
  operations, command application, and scheduled-answer settlement all carry
  the writer's epoch and are rejected atomically when a newer epoch exists.
  Validate-then-write is forbidden — the check rides inside the same
  transaction as the write. Consequently the coordination store and the
  journal must share transactional authority (one database).
- **Authority propagates to nested runs.** A nested graph's child writes are
  fenced by the root claim's epoch; child run identity alone is
  insufficient.
- **Heartbeats are isolated.** Lease renewal must survive a blocking node:
  an independent thread/connection, never the runner's own event loop.
- **Attempt semantics under takeover.** A recovered `STARTED` attempt
  becomes `OUTCOME_UNKNOWN` (never an invented failure) and surfaces
  `AttemptOutcomeUnknownError` exactly as today. The old invocation may
  still be executing and may still complete external effects — only its
  commits are impossible. This supersedes the `resolve_stranded_attempts`
  precondition "the caller knows no prior invocation still runs" with:
  **"no prior lease may still commit."** Host re-dispatch is recovery, not a
  node retry; retry budgets, windows, and backoff are untouched.
- **Recovery has a finite brake (A6).** Each Run pins a recovery-attempt cap
  at first submit. Re-adoption without committed graph progress sets the
  coordination condition `recovery_exhausted` — never a `WorkflowStatus` —
  and reconciliation skips the Run; only `rerun()` into a new Run revives
  the work. The counter resets only on a committed StepRecord, a durable
  pause, or a terminal transition.
- **Effects reserve identity before dispatch (A10).** Every node that may
  cause an external effect declares that fact, and the Host reserves a
  durable effect identity before any provider call. An effect whose
  settlement was not witnessed surfaces `OUTCOME_UNKNOWN` and is never spent
  again automatically; it requires an explicit operator decision. The
  declaration and reservation contract is specified in PRD 0014.
- **Tier boundary.** *(Superseded 2026-08-05; the original read: "SQLite
  (local Run Home) does NOT advertise leases: one OS-level exclusive worker
  lock per Home; epoch fields may exist as private schema placeholders. The
  lease-with-epoch contract is the Postgres (shared) tier, accepted only via
  the eight-point kill-test matrix.")*

  SQLite now claims with a lease. `host_submissions.claimed_by` and
  `lease_until` are written by the claim compare-and-set, renewed by the
  holder at a third of the TTL, and adopted on expiry by any worker's poll
  pass; `claim_seq` is the epoch, and the transitions that speak for one
  execution compare-and-set on it. Several workers may share one Home, and
  `WorkerLockError` is retired.

  The lock was never the answer this ADR wanted — it made "is this
  half-finished Run dead?" unaskable rather than answered, and forbade a
  notebook from executing work only it could configure. Deferring the lease
  bought nothing once the local tier had a real second executor.

  **What still does NOT hold at this tier**, and is still the Postgres
  tier's work: the fence is at the SUBMISSION's transitions, not on every
  journal write. Step saves, attempt operations and status transitions are
  not epoch-checked, so a presumed-dead worker still executing can commit
  steps to a Run another worker has adopted. That is the documented
  at-least-once boundary — an adopted Run resumes from checkpoint state and
  a duplicated step is wasted work, never a second settlement — but it is
  not "every mutation is fenced in-transaction", and the eight-point
  kill-test matrix remains the gate for claiming that.

## Consequences

- No lock service, broker, or consensus tier: fencing rides the journal's
  own store; claimable rows are the queue (lease = visibility timeout).
- The word is **lease** — never "grant" (superposition's authority atom) and
  never "ExecutionGrant."
- Side-effect nodes still need stable effect identity (PRD 0014); the lease
  guarantees a single authoritative *writer*, not a single physical
  *executor*.
- Recovery exhaustion gives operators a visible, queryable parking state for
  poison work without inventing a new execution status.
