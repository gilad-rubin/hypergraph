# Durable Host

A durable host lets one process ask for work durably and leave: submission is
persisted before execution, a product-owned worker executes it, and any other
process can inspect and follow the run through a small client. It is additive
— direct `runner.run()` / `start_run()` (Tier 0) are unchanged, and the host
is never required.

{% hint style="info" %}
This page covers the local Tier 1 host: `serve()`, `RunHome`, `Host`, and
`RunHomeClient`. For direct execution semantics see [Runners](runners.md);
for process-local background handles see
[Control Work After It Starts](../05-how-to/control-background-execution.md).
Durable Batch, stop, pause answers, and delayed starts land in later
releases of this surface.
{% endhint %}

## Serving Definitions

Bind each root graph to its runner, then `serve()` the Definitions against
one Run Home:

```python
from hypergraph import AsyncRunner, SyncRunner, serve, RunHome

triage = triage_graph.with_runner(SyncRunner())     # Graph.with_runner: root binding
refund = refund_graph.with_runner(AsyncRunner())

host = serve(refund, triage,
             home=RunHome.open("file:./runs.db"),
             deployment_version="2026.07.3")
```

`serve()` clones each bound runner onto the Home's checkpointer via
`runner.with_checkpointer(...)` — the runner instance you passed is never
mutated. Every graph must have a `name` and a bound runner; a runner without
checkpoint/event capability (for example `DaftRunner`) fails loudly at
construction.

## Definition Identity and `accepts=`

Every submission pins the complete Definition identity as a typed
`DefinitionId(name, deployment_version, structural_hash)` value (ADR 0007);
it travels with the submission and shows up on `RunView.definition_id`.

A worker claims a submission only when its pinned identity matches a served
Definition exactly, or matches a prior identity the deployment explicitly
declares with `accepts=`:

```python
from hypergraph import DefinitionId

host = serve(ingest, home=RunHome.open("file:./runs.db"),
             deployment_version="2026.07.3",
             accepts=(DefinitionId("ingest", "2026.06.1", "44de…"),))
#            → this worker now drains runs parked under the old identity
```

Without a matching declaration the worker refuses the submission: it stays
persisted and unclaimed, and its view reports
`WaitingCondition.VERSION_INCOMPATIBLE`. An `accepts=` entry names a complete
identity — one whose structural hash matches nothing simply never drains.
Every `accepts=` entry must be a `DefinitionId` (anything else is a
`TypeError` at `serve()`).

## Submitting a Run

```python
receipt = await host.submit("refund", {"claim_id": "c-42"},
                            workflow_id="refund-c-42")
receipt.run_ref        # RunRef — inert, JSON-serializable address
receipt.duplicate      # True when an identical nonterminal submission existed
```

The submission commits to the Run Home **before** any execution: process
loss after `submit()` returns cannot erase durable intent. Each submission
also records a **start fingerprint** over the pinned `DefinitionId`, the
normalized inputs, and the requested `start_at`. Resubmitting the same
`workflow_id`:

- **fingerprint-identical and nonterminal** → returns the existing receipt
  with `duplicate=True` (webhook-style retries are safe; nothing new is
  written);
- **fingerprint mismatch** (different identity, inputs, or `start_at`) →
  `WorkflowIdConflictError`;
- **terminal history** → `AlreadyTerminalError`, whether or not the
  fingerprint matches. Completed history never changes identity; use
  `client.rerun()` to repeat a settled run.

`host.submit_sync(...)` is the synchronous mirror.

`RunRef` carries identity only — `home` and `run_id`, plus
`to_dict()`/`from_dict()`. It has no status, result, or control methods and
is never a durable handle: live control stays process-local (ADR 0004).

## Inspecting and Watching

`host.client` is the one `RunHomeClient` for the Home. A client can also be
constructed directly — no graph code required — which is how a small
operator process inspects work it never submitted:

```python
from hypergraph import RunHome, RunHomeClient, WaitingCondition

client = RunHomeClient(RunHome.open("file:./runs.db"))

view = await client.get(receipt.run_ref)   # RunView: persisted facts
view.status                                # WorkflowStatus | None
view.waiting                               # WaitingCondition | None (typed,
                                           # never a WorkflowStatus)

cursor = None
async for update in client.watch(receipt.run_ref, after=cursor):
    if update.durable:
        cursor = update.cursor             # "seq:N" — store this
```

Every committed Run fact carries a monotonic per-Run durable sequence.
`watch(ref, after=cursor)` replays every fact after the cursor in order — no
gaps, no repeats, safe across process restarts — then tails live previews.
Previews arrive with `update.durable is False`, repeat the last durable
cursor, and **never advance it**; store cursors from durable updates only.
`get_sync()` is the synchronous mirror of `get()`.

`WaitingCondition` is a closed enum — `QUEUED`, `SCHEDULED`, `PAUSED`,
`VERSION_INCOMPATIBLE`, `ADMISSION_LIMITED`, `RECOVERY_EXHAUSTED` — so
waiting work never looks alike and callers branch on typed values. `waiting`
is `None` while a Run executes or is terminal.

## Rerun: Repeat a Settled Run

`client.rerun(ref)` repeats a terminal run under a new workflow id with
retry lineage — it lives on the client because it needs **no loaded
Definition code**: the new submission carries the source's pinned
`DefinitionId` and inputs verbatim.

```python
rerun_receipt = await client.rerun(receipt.run_ref)
rerun_receipt.workflow_id      # "refund-c-42-retry-1"
```

The worker executes the rerun with `retry_from` lineage: the new runs row
records `retry_of`/`retry_index`, and completed-step checkpoints from the
source are reused. There is deliberately **no input override** — the
signature is `rerun(ref)` and passing `inputs=` is a `TypeError`; changed
inputs use a normal new `submit()`. The source must exist and be terminal,
else `RerunError`. `client.rerun_sync(...)` is the synchronous mirror.

## Fork: Migrate to New Code

`host.fork(ref, into=..., reason=...)` migrates an existing run to a served
Definition — it lives on the Host because migration targets **loaded
Definition code**. The new run is seeded from the source's recorded history,
pinned to the target's `DefinitionId`, and records `forked_from` lineage
plus your reason on the submission:

```python
fork_receipt = await host.fork(run_ref, into="refund",
                               reason="2026.07.3 schema migration, approved by ops")
fork_receipt.workflow_id       # "refund-c-42-fork-a1b2c3"
```

Compatibility is checked **at fork time**: the target Definition's
`structural_hash` must equal the source submission's pinned hash (history
seeding requires restorable checkpoints), else `ForkCompatibilityError`
naming both identities. `reason` must be a non-empty string and `into` must
name a served Definition (`ValueError` otherwise). `host.fork_sync(...)` is
the synchronous mirror.

Rerun and fork lineage never merge: `RunView.retry_of` and
`RunView.forked_from` are mutually exclusive, so queries can always tell
repetition from migration.

## The Worker

One product-owned process executes submitted work:

```python
await host.work_forever(worker_id="labbox")   # blocks; explicit lifecycle
host.shutdown()                               # from another task: bounded drain
```

Startup takes an OS-level exclusive lock on the Home; a second worker on the
same Home fails immediately with `WorkerLockError`. `shutdown()` stops new
claims and lets in-flight runs finish within `drain_timeout` before the lock
is released. On startup the worker re-adopts submissions whose previous
worker died mid-run, so unfinished work continues without resubmission.
Process supervision (systemd, FastAPI lifespan, cron) restarts the worker —
Hypergraph runs no control-plane server.

## Errors

| Error | Raised when |
|---|---|
| `WorkerLockError` | a second worker starts on the same Run Home |
| `AlreadyTerminalError` | a terminal `workflow_id` is reused |
| `WorkflowIdConflictError` | a nonterminal `workflow_id` is reused with a different start fingerprint |
| `ForkCompatibilityError` | `host.fork()` targets a structurally incompatible Definition |
| `RerunError` | `client.rerun()` names a missing or nonterminal source |
| `HostError` | base class for host-specific errors |
