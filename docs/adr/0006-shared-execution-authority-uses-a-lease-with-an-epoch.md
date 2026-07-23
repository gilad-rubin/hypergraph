# Shared execution authority uses a lease with an epoch

**Status:** Proposed on 2026-07-21, pending maintainer review (wayfinder map ticket). Derived independently by all three clean-room designs and the anchored review (`docs/research/2026-07-21-cleanroom-durable-host-experiment.md`); risk analysis in `docs/research/2026-07-21-durable-host-canon-grill.md`.

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
- **Tier boundary.** SQLite (local Run Home) does NOT advertise leases: one
  OS-level exclusive worker lock per Home; epoch fields may exist as private
  schema placeholders. The lease-with-epoch contract is the Postgres
  (shared) tier, accepted only via the eight-point kill-test matrix.

## Consequences

- No lock service, broker, or consensus tier: fencing rides the journal's
  own store; claimable rows are the queue (lease = visibility timeout).
- The word is **lease** — never "grant" (superposition's authority atom) and
  never "ExecutionGrant."
- Side-effect nodes still need stable effect identity (PRD 0014); the lease
  guarantees a single authoritative *writer*, not a single physical
  *executor*.
