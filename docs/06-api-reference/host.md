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
Durable Batch, stop, rerun, pause answers, and delayed starts land in later
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

## Submitting a Run

```python
receipt = await host.submit("refund", {"claim_id": "c-42"},
                            workflow_id="refund-c-42")
receipt.run_ref        # RunRef — inert, JSON-serializable address
receipt.duplicate      # True when an identical nonterminal submission existed
```

The submission commits to the Run Home **before** any execution: process
loss after `submit()` returns cannot erase durable intent. Resubmitting a
nonterminal `workflow_id` returns the existing receipt with
`duplicate=True`; reusing a terminal one raises `AlreadyTerminalError`.
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
| `HostError` | base class for host-specific errors |
