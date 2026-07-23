# Durable-execution landscape: why engines can't be hypergraph's durability mixin, and what can

- Date: 2026-07-21
- Issue: none yet; research capture from the durable-serving design conversation
- Implementation: none; research capture only
- Measured revision: `86618f7d80db841874239ac12872bc3098e79f5b`
- Status: investigation complete; conclusions feed the durable-host ADRs/PRDs and the clean-room experiment (`2026-07-21-cleanroom-*-durable-host.md`)

> **Intent, not canon.** This note records external-system facts as verified on
> the date above, and the reasoning they grounded. Vendor behavior changes;
> re-verify before relying on a specific claim.

## The two durability philosophies

Every durable-execution product uses one of two mechanisms:

1. **Replay-based** (Temporal, DBOS, Restate, Rivet Workflows, durable-workflow/sdk-python):
   journal every step of arbitrary imperative code; on crash, re-execute the
   function from the top, feeding recorded results back until caught up.
   Requires determinism contracts, version gates — and crucially, the engine
   must own control flow to replay it. **This is why they cannot be a mixin:
   to replay your control flow, they must be the runner.**
2. **State-based** (Rivet Actor state, LangGraph checkpoints, hypergraph's
   checkpointer): persist the state itself; on crash, load state and continue.
   No replay, no determinism contract — works when the system knows what "the
   state" is. A graph with named values and superstep boundaries knows exactly
   that. Hypergraph is already this philosophy; replay would solve a problem
   it does not have.

## Rivet actors (rivet.dev) — concept source, not a dependency

Long-lived addressable objects: direct state persistence (no replay),
single-writer per key, hibernation (~30s idle → sleep; wake on message),
actions + events + queues. Apache-2.0, Rust engine, self-hostable; SDKs are
TypeScript-first (Python experimental, client-only). Verdict: unusable as a
Python dependency; highly usable as a concept map — the durable-host design's
"parked runs are rows at rest" and wake-on-command are actor lifecycle ideas
over hypergraph's own journal. Rivet splits durability into actor-state
(state-based) vs workflows (replay) — independent validation that the two
philosophies are separable products.

## DBOS rentability (verified against dbos-transact-py source + docs, 2026-07-21)

- Process **singleton**: one `DBOS()` per program lifetime; one config,
  executor identity, app version, recovery domain, system schema.
  `launch()` starts ~10 background threads plus an admin HTTP server
  (default port 3001). Two subsystems in one process cannot use DBOS
  independently.
- Startup recovery selects `PENDING` workflows by `executor_id +
  application_version` only — cannot be scoped to a subset of workflows in a
  shared app.
- **Fleet failover (dead-worker detection + reassignment) is not in the OSS
  library** — it requires Conductor, which is proprietary-licensed for
  self-hosted production use.
- Honest integration shapes: (rejected) `DBOSCheckpointer` — the Checkpointer
  seam is passive storage, DBOS degenerates to a Postgres driver;
  (rejected) node-per-step engine-runner — double journal + determinism
  contract + replay event dupes; (rejected as "supervisor mixin") one DBOS
  workflow wrapping the run — workflow bodies must be deterministic, and the
  singleton/recovery-scope facts above make "component" framing dishonest;
  (viable, deferred) DBOS **as the host's internal engine in a dedicated
  worker process** — rents queues/flow-control/timers/dashboard if a
  deployment ever needs them, behind hypergraph's own host API.

## Restate, and the rest of the field

- **Restate**: Rust server + Python SDK; virtual objects = keyed single-writer
  actors with K/V state; journaling at `ctx.run()` granularity. Closest
  external candidate for durability-without-ceding-scheduling, but: server
  dependency, journal duplication beside hypergraph's checkpointer, and
  hypergraph's differentiators (fork/lineage/inspection) live only in its own
  journal. Server is BSL-licensed.
- **durable-workflow/sdk-python**: a Temporal clone over HTTP/JSON (polyglot
  PHP/Python workers). Beta, ~1 star. Same replay family; strictly dominated
  by Temporal/DBOS if that family were ever chosen.
- **Procrastinate** (MIT, Postgres task queue): per-key locks, worker
  heartbeats, stalled-job retry — closest OSS library to the coordination
  slot, but no fencing token, and a worker dying while holding a per-key lock
  can wedge the key (procrastinate issue #1446). Verdict: adapt-only-if-
  desperate; it doesn't remove the hard correctness work.
- **Hatchet** (MIT): dynamic concurrency keys, TTL idempotency, at-least-once
  recovery — but a full second control plane (API server + engine + dashboard
  + Postgres [+ RabbitMQ]) that persists its own workflow transitions.
- **NATS JetStream / Redis Streams**: durable transport with redelivery;
  no keyed scheduling, no write fencing — transport only.
- **APScheduler 4**: acquisition leases exist but pre-release ("do not use in
  production"); per-task not per-key concurrency; no fence.

## Framework API conventions (adopted into the host API)

- **Temporal**: "the Workflow Id acts as an idempotency key";
  `WorkflowIDConflictPolicy.USE_EXISTING` returns the running workflow's
  handle → start dedupes on workflow_id, no client-managed keys.
- **Restate**: `Idempotency-Key` is an HTTP header — a transport concern.
- **Inngest**: event-id dedup at send; per-function idempotency as config
  (CEL), never a per-call API parameter.
- **Prefect 3**: declare flows, one `serve(deploy_a, deploy_b)` call —
  no imperative registration. **Inngest**: `serve(app, client, [fn1, fn2])`.
  **Temporal**: `Worker(workflows=[...])`. → declare `graph.with_runner(...)`,
  one list-taking `serve()`.

## What the engines have that the native design defers (gap analysis)

Critical and adopted now: **durable timers** (`wake_at` on the run record —
"escalate if unanswered in 72h", "retry payment tomorrow").
Grows-with-scale, deferred with named triggers: fleet-wide flow control
(global rate limits/concurrency — the first feature that genuinely bites at
scale), queue priorities, ops dashboard, >1-Postgres scale ceiling, engine HA
(rented as managed-Postgres HA). Covered differently, not gaps: replay,
child workflows (nested graphs are native), signals (answer/pause + run
values), history patching (version pin + fork).

## Sources

Rivet: rivet.dev/docs/actors (state, lifecycle, workflows, design patterns);
github.com/rivet-dev/rivet. DBOS: docs.dbos.dev (dbos-class, workflow
communication, workflow-recovery, hosting-conductor), github.com/dbos-inc/
dbos-transact-py. Restate: docs.restate.dev/foundations/key-concepts,
restate.dev blog (durable AI loops). Temporal: docs.temporal.io
workflow-execution/workflowid-runid. Prefect: docs.prefect.io/v3/concepts/
deployments. Inngest: inngest.com/docs/events, docs/guides/handling-
idempotency. Procrastinate: procrastinate.readthedocs.io, issue #1446.
Hatchet: docs.hatchet.run (architecture-and-guarantees, concurrency,
idempotency, self-hosting). Pydantic AI durable integrations:
ai.pydantic.dev/durable_execution. LangGraph: docs.langchain.com persistence,
langsmith/agent-server.
