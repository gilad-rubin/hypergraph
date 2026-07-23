# 0011 — Local Durable Host and SQLite Run Home

status: draft (blocked on PRD 0010 and ADR 0005/0006/0007 acceptance)

## What this delivers (Tier 1)

One machine, zero extra infrastructure: a nightly run that survives reboots
and continues where it stopped; a parked human question that survives
restarts and deploys; submit/answer/stop/watch from any process on the
machine — with the *same user code* that will later run against a shared
Postgres Run Home. Explicit non-promises: no fleet failover, no shared-disk
SQLite, no exactly-once side effects, no full live-event replay.

## Fixed acceptance contract

Before (today — intent lives in process memory):

```python
result = await runner.run(triage_graph, {"corpus": "main"},
                          workflow_id="triage-2026-07-21")
# reboot mid-run: nothing re-invokes it; stop() only works in-process;
# a second process starting the same workflow_id races the first.
```

After:

```python
from hypergraph import AsyncRunner, SyncRunner, serve, RunHome

triage = triage_graph.with_runner(SyncRunner())     # NEW: root-graph binding
refund = refund_graph.with_runner(AsyncRunner())

host = serve(refund, triage, home=RunHome.open("file:./runs.db"))

receipt = await host.submit("refund", {"claim_id": "c-42"},
                            workflow_id="refund-c-42")
dup = await host.submit("refund", {"claim_id": "c-42"},
                        workflow_id="refund-c-42")
assert dup.workflow_id == receipt.workflow_id and dup.duplicate  # name dedupes

await host.answer("refund-c-42", {"approved": True})   # settles the durable pause slot
await host.stop("triage-2026-07-21")                    # from any process on the machine

async for update in host.watch("refund-c-42"):          # durable replay, then live tail
    ...

await host.work_forever(worker_id="labbox")             # the worker loop entrypoint
# reboot + systemd/cron restart => restart scan re-adopts unfinished runs;
# nobody re-submits anything.
```

Requirements:

- **Run Home = the existing SQLite checkpointer plus coordination tables**
  (host commands with per-workflow sequence; claim/required_version fields
  adjacent to runs). Steps stay the sole execution journal; host
  coordination facts never enter `RunStatus`/`WorkflowStatus` (ADR 0005).
- **One exclusive worker per Home**, enforced by an OS-level lock at
  `work_forever()` startup; second worker fails loudly. No lease/epoch
  contract is exposed in this tier (ADR 0006 tier boundary). Intake and
  observation from other processes remain legal.
- **Verbs:** `submit` (dedup on workflow_id, use-existing), `answer`
  (delegates to PRD 0010 settlement; occurrence-checked), `stop` (durable
  stop intent; the worker observes at superstep boundaries via the existing
  cooperative stop; races resolve by command commit order with truthful
  rejections), `watch` (StepRecord + command replay, then best-effort live
  preview — never promised as full event replay). No `host.run()` in v1
  (ADR 0005).
- **Runner binding:** `Graph.with_runner()` (new, root-level) declares the
  per-graph runner; `serve()` binds each runner to the Home's checkpointer
  by cloning (`runner.with_checkpointer(...)`), never mutating the supplied
  instance. Daft (or any runner lacking checkpoint/event capability) fails
  at `serve()` construction. Checkpoint policy is forced synchronous for
  Home-bound runners; `"exit"` durability is rejected.
- **Version refusal:** each submit pins `required_version` (identity per
  ADR 0007's resolution); the worker claims only runs it can serve;
  version-incompatible runs are queryable, never guessed at.
- **Restart scan:** on `work_forever()` start, unfinished non-paused runs
  re-enter execution via normal checkpointer resume; paused runs wait for
  answers. At-least-once is documented honestly (a node that completed but
  wasn't yet persisted may re-run — narrowing that window is PRD 0013).

## Test plan (red first)

- kill -9 mid-run → restart → run completes without re-submission; completed
  steps not re-executed (journal skip), modulo the documented window.
- Second `work_forever()` on the same Home → loud startup failure.
- submit/submit dedup; answer via host on a run parked before a (simulated)
  deploy; stop from a second process; stop-vs-answer race both orders,
  truthful loser.
- `watch()` from a process that started nothing: full history then live tail.
- Daft binding → construction error. Version-incompatible run → visible via
  query, untouched by the worker.
- Sync + async runner parity for every verb; CI-equivalent run green.

## Out of scope

Scheduled answers (PRD 0012), node-boundary commits (PRD 0013), effect
identity (PRD 0014), Postgres/leases/epochs (PRD 0015/0016), any HTTP
surface, any dashboard.
