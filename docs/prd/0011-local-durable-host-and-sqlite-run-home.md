# 0011 — Local Durable Host and SQLite Run Home

status: accepted-intent (ADRs 0005–0007 accepted 2026-07-23 with amendments; A1, A5, A9, A12–A16 folded 2026-07-24, Durable Host V1 ticket 01; implementation starts with ticket 02)

## What this delivers (Tier 1)

One machine, zero extra infrastructure: a nightly run that survives reboots
and continues where it stopped; a parked human question that survives
restarts and deploys; submit/stop/watch from any process on the machine —
with the *same user code* that will later run against a shared Postgres Run
Home. Explicit non-promises: no fleet failover, no shared-disk SQLite, no
exactly-once side effects, no full live-event replay.

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

host = serve(refund, triage, home=RunHome.open("file:./runs.db"),
             deployment_version="2026.07.3")
client = host.client        # the one RunHomeClient; the Host never copies its verbs

receipt = await host.submit("refund", {"claim_id": "c-42"},
                            workflow_id="refund-c-42")
receipt.run_ref             # inert, serializable address — no status or control methods

dup = await host.submit("refund", {"claim_id": "c-42"},
                        workflow_id="refund-c-42")
assert dup.run_ref == receipt.run_ref and dup.duplicate  # fingerprint-identical → use-existing

await client.stop(receipt.run_ref)          # durable stop intent, from any process
view = await client.get(receipt.run_ref)    # RunView: persisted facts + why it waits

async for update in client.watch(receipt.run_ref, after=cursor):  # gap-free durable replay,
    ...                                       # then non-durable live preview

await host.work_forever(worker_id="labbox")             # the worker loop entrypoint
# reboot + systemd/cron restart => restart scan re-adopts unfinished runs;
# nobody re-submits anything.
```

The ownership seam (A13): the **Host** owns new submission (`submit`,
`submit_batch`), worker lifecycle (`work_forever`, bounded drain, lock
release), and explicit `fork` (migration needs the target Definition
loaded). The **RunHomeClient** owns everything about existing work — `get`,
`list`, `watch`, `stop`, `rerun`, and (once PRD 0010 lands) `answer` — for
`RunRef` and the applicable `BatchRef` operations. A process with no graph
code may construct a client and inspect or control existing work, but can
never submit (owner decision 5).

Requirements:

- **Run Home = the existing SQLite checkpointer plus coordination tables**
  (host commands with per-workflow sequence; claim and pinned Definition
  identity adjacent to runs). Steps stay the sole execution journal; host
  coordination facts never enter `RunStatus`/`WorkflowStatus` (ADR 0005).
- **One exclusive worker per Home**, enforced by an OS-level lock at
  `work_forever()` startup; second worker fails loudly. The worker
  lifecycle is explicit: startup, bounded drain on shutdown, and lock
  release. No lease/epoch contract is exposed in this tier (ADR 0006 tier
  boundary). Intake and observation from other processes remain legal.
- **Verbs and ownership (A13):** Host: `submit`, `submit_batch`, `fork`,
  `work_forever`. RunHomeClient: `get`, `list` (via `RunQuery`), `watch`,
  `stop`, `rerun`, `answer` (gated on PRD 0010 per A1). The Host exposes
  the client as `host.client` and adds no pass-through convenience copies.
  No `host.run()` in v1 (ADR 0005).
- **Submission dedup (A5):** the start fingerprint covers the complete
  pinned `DefinitionId(name, deployment_version, structural_hash)`,
  normalized inputs, effective Batch configuration, and requested
  `start_at`. A fingerprint-identical nonterminal resubmission returns the
  existing Run (`duplicate=True`); terminal reuse raises a typed
  `already_terminal` conflict; a fingerprint mismatch on the same
  `workflow_id` raises a typed `workflow_id_conflict`.
- **Rerun and fork (A16):** `client.rerun(ref)` creates a new Run under a
  new workflow id with the source's pinned Definition identity and inputs,
  records `retry_of`, and accepts no input override. `host.fork(ref,
  into=..., reason=...)` performs compatibility-checked migration to a
  loaded Definition, recording `forked_from` and the reason. The lineage
  relations never merge.
- **Durable watch (A9):** every Run mutation receives a monotonic per-Run
  durable sequence in its transaction. `client.watch(ref, after=cursor)`
  replays committed facts in sequence order — no gaps, no repeats — then
  tails live previews marked non-durable; previews never advance the
  cursor. Full contract: PRD 0018.
- **Delayed start (A14):** `submit(start_at=…)` persists and fingerprints
  the Run immediately; it becomes claim-eligible only when
  store-authoritative time passes `start_at`; a past time is immediately
  eligible; stop-before-due prevents execution.
- **Admission (A3/A15):** the Run Home owns a runtime-tunable
  `max_active_runs`; over-limit Runs wait in claim order and paused,
  delayed, version-incompatible, and recovery-exhausted Runs consume no
  slot. Provider-resource limits are injected separately; waiting on a
  provider permit never counts as failure.
- **Runner binding:** `Graph.with_runner()` (new, root-level) declares the
  per-graph runner; `serve()` binds each runner to the Home's checkpointer
  by cloning (`runner.with_checkpointer(...)`), never mutating the supplied
  instance. Daft (or any runner lacking checkpoint/event capability) fails
  at `serve()` construction. Checkpoint policy is forced synchronous for
  Home-bound runners; `"exit"` durability is rejected.
- **Version refusal (ADR 0007):** each submit pins the complete
  `DefinitionId`; the worker claims only runs it serves exactly or via an
  explicit `accepts=(...)` declaration; version-incompatible runs are
  queryable, never guessed at.
- **Restart scan:** on `work_forever()` start, unfinished non-paused runs
  re-enter execution via normal checkpointer resume; paused runs wait for
  answers. Repeated recovery without committed graph progress hits the
  pinned recovery cap and parks the Run as `recovery_exhausted` — a
  coordination condition, never a WorkflowStatus (A6). At-least-once is
  documented honestly (a node that completed but wasn't yet persisted may
  re-run — narrowing that window is PRD 0013).

## Test plan (red first)

- kill -9 mid-run → restart → run completes without re-submission; completed
  steps not re-executed (journal skip), modulo the documented window.
- Second `work_forever()` on the same Home → loud startup failure.
- submit/submit fingerprint dedup; terminal-reuse and fingerprint-mismatch
  conflicts are distinct typed errors; rerun lineage and fork lineage stay
  separate; stop from a second process; stop-vs-completion race both
  orders, truthful loser.
- `watch()` from a process that started nothing: gap-free durable replay
  from a stored cursor, then live tail; previews never advance the cursor.
- Daft binding → construction error. Version-incompatible run → visible via
  query, untouched by the worker; an `accepts=(...)` declaration drains it.
- Runtime `max_active_runs` change with queued work; over-limit order
  preserved; provider-permit waits do not alter failure state.
- Sync + async runner parity for every verb; CI-equivalent run green.

## Out of scope

Durable pause slots (PRD 0010; gates only `answer()`), scheduled answers
(PRD 0012), node-boundary commits (PRD 0013), effect identity (PRD 0014),
Postgres/leases/epochs (PRD 0015/0016), any HTTP surface, any dashboard.
The durable Batch contract rides on this Run Home and is specified in
PRD 0019.
