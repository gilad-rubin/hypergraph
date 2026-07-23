# Durable hosts accept commands; runners retain execution semantics

**Status:** Proposed on 2026-07-21, pending maintainer review (wayfinder map ticket). Grounded in the three-model clean-room convergence (`docs/research/2026-07-21-cleanroom-durable-host-experiment.md`) and the canon grill (`docs/research/2026-07-21-durable-host-canon-grill.md`). Extends — never supersedes — ADR 0004: handles stay process-local live control.

## Context

Everything that makes `runner.run()` safe lives in one process's memory: the
active-run registry, the stop signal, event delivery, duplicate-run
prevention. Webhooks retry, processes crash, humans park runs for a week, and
deploys happen with runs in flight. The missing layer is a durable home for
*intent and authority* — not a second execution engine.

## Decision

- **One new composition root, additive.** A `serve(...)` call takes graphs —
  each carrying its own runner via a declarative binding — plus one **Run
  Home** (the single store owning runs, steps, host commands, and claims).
  Direct `runner.run()` / `start_run()` remain first-class and unchanged
  (Tier 0); the host is never a toll booth.
- **The Run Home is the existing checkpointer plus coordination facts —
  never a second execution journal.** Steps remain the source of truth.
  Coordination facts (command sequence, claim, required version, scheduled
  answers) live in adjacent structures; no independently written terminal
  status. Host coordination states never enter `RunStatus` or
  `WorkflowStatus`.
- **Durable command intake with name-based dedup.** `submit()` dedupes on
  `workflow_id` (use-existing semantics); `answer()` dedupes on the durable
  `pause_id` (one answer per pause occurrence, atomically checked);
  `stop()` is idempotent. Callers never manage idempotency keys; a
  `CommandReceipt` records acceptance/application only — never terminal
  execution truth.
- **`host.run()` is excluded from v1.** `RunResult` carries local-only
  evidence (exact exception objects, `RunLog`, checkpoint-write evidence)
  that a detached worker cannot reconstruct truthfully. Durable serving is
  `submit()` + `watch()`. A future durable result projection requires a new
  ADR that explicitly extends ADR 0004.
- **`watch()` is durable replay plus live preview.** History is replayed
  from StepRecords and host commands; live events remain best-effort
  preview, per the existing event-processor contract. Full event replay is
  not promised.
- **Runner binding clones, never mutates.** `serve()` binds each supplied
  runner to the Home's checkpointer via an immutable cloning contract; a
  runner that cannot satisfy host requirements (today: Daft — no
  checkpointing, no events) fails at construction, loudly.

## Considered and rejected

- External engines as the durability layer (Temporal/DBOS/Restate — replay
  engines must own control flow; DBOS is a process singleton whose OSS
  recovery has no fleet failover): `docs/research/2026-07-21-durable-execution-landscape.md`.
- A durable `ExecutionHandle`: rejected by ADR 0004; receipts + watch are
  the durable surfaces.
- Engine-backed host internals (DBOS as dedicated worker) remain a possible
  Tier 3 implementation of THIS contract if a deployment ever needs
  fleet-wide flow control; no compatibility promise is made now.

## Consequences

- The notebook→production path is construction-time only: the same graphs
  and verbs run against a local (SQLite) or shared (Postgres) Run Home.
- The host is fully usable without superposition; sp joins later by
  reference (`docs/research/2026-07-21-superposition-relationship.md`).
- PRD 0010 (durable pause slots) gates all of this: today the pause
  question and answer port exist only in memory.
