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
Durable pause answers land in a later release of this surface.
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
mutated, and the clone's nested-graph executors are rebuilt against the
clone so GraphNode child workflows persist to the same Run Home. Every
graph must have a `name` and a bound runner (readable back via the
read-only `graph.bound_runner` property); a runner without checkpoint/event
capability (for example `DaftRunner`) fails loudly at construction.

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
persisted and unclaimed, its view reports
`WaitingCondition.VERSION_INCOMPATIBLE`, and the worker logs a warning
naming the pinned identity it cannot serve. Every `accepts=` entry must be
a `DefinitionId` (anything else is a `TypeError` at `serve()`), and each
entry is validated structurally at `serve()` time: it must name a
Definition this host serves and its structural hash must equal the served
Definition's hash — anything else is a `ValueError`, because an
undrainable declaration would park submissions forever (ADR 0007).

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

`submit()` also accepts `start_at` (a `datetime` or ISO string) for a
one-shot delayed start — no external timer service — see
[Delayed Start](#delayed-start).

`submit()` also accepts `recovery_cap` (default `3`): how many progressless
crash re-adoptions park the run as recovery-exhausted instead of resuming
it — see [Crash Recovery and the Recovery Brake](#crash-recovery-and-the-recovery-brake).
It is a recovery budget, not an identity fact, so it is **not** part of the
start fingerprint: an identical resubmission with a different cap dedupes and
keeps the first cap. The cap must be an `int >= 0` (`0` brakes on the first
progressless re-adoption).

`RunRef` carries identity only — `home` and `run_id`, plus
`to_dict()`/`from_dict()`. It has no status, result, or control methods and
is never a durable handle: live control stays process-local (ADR 0004).

## Submitting a Batch

A durable Batch is an **immutable manifest** of unique logical item keys,
each mapped to one independent child Run with its own pinned inputs (PRD
0019) — never a durable parent `MapResult`:

```python
from hypergraph import BatchTolerance

receipt = await host.submit_batch(
    "ingest",
    items={"protocol-17": {"doc": ...}, "protocol-18": {"doc": ...}},
    workflow_id="schneider-drop-42",
    tolerance=BatchTolerance(max_failed=2, max_failed_percent=25),  # optional
)
receipt.batch_ref          # BatchRef — inert, JSON-serializable address
receipt.duplicate          # True when an identical nonterminal Batch existed
```

**One transaction** persists all of it — the manifest row (Definition
identity, item keys with pinned inputs, the tolerance declaration, the
start intent), one child submission per item key (child workflow id
`<workflow_id>:<item_key>`), and the `manifest` fact at per-Batch durable
sequence `bseq=1`. A kill anywhere inside acceptance leaves the Batch fully
absent, never half-accepted. Duplicate or empty item keys are rejected at
submission (`ValueError`); the mapping order of `items` is the manifest
order used for keyed outcomes.

Children are **ordinary Runs**: they flow through the same
claim/execute/stop/recovery machinery as submitted runs — each gets a
`RunView`, a run-level `watch`, durable stop, and crash recovery — with
their Batch membership recorded. `host.submit_batch_sync(...)` is the
synchronous mirror.

Dedup mirrors `submit`, over a start fingerprint covering the pinned
Definition identity, the normalized manifest, the pinned tolerance, and
`start_at`. Resubmitting the same `workflow_id`:

- **fingerprint-identical and nonterminal** → the existing receipt with
  `duplicate=True`, naming the same `BatchRef`;
- **fingerprint mismatch** (different identity, items, tolerance, or
  `start_at`) → `WorkflowIdConflictError`;
- **fully settled Batch** → `AlreadyTerminalError`, whether or not the
  fingerprint matches.

Run and Batch workflow ids share one namespace: reusing an id owned by a
plain run submission (or a run id owned by a Batch) is a conflict too.

`BatchTolerance(max_failed=…, max_failed_percent=…)` pins optional failure
thresholds into the manifest at acceptance — never `serve()` configuration
— and is part of the dedup fingerprint. At least one threshold must be set;
`max_failed` is an `int >= 0`, `max_failed_percent` a whole `int` between 0
and 100 whose denominator is the fixed manifest item count. See
[Failure tolerance](#failure-tolerance-and-the-trip) for what happens when
one is exceeded.

`BatchSubmitReceipt` mirrors `SubmitReceipt` (inert, with `batch_ref`,
`workflow_id`, `duplicate`). `BatchRef` mirrors `RunRef`: identity only
(`home`, `batch_id`, plus `to_dict()`/`from_dict()`), no live methods,
never a durable handle.

## Inspecting and Watching a Batch

The same client verbs accept a `BatchRef`:

```python
view = await client.get(receipt.batch_ref)   # BatchView: keyed persisted facts
view.counts          # every manifest item in exactly one bucket:
                     # {"completed": 1, "failed": 0, "active": 0, "queued": 1,
                     #  "unstarted": 0, ...} — all keys always present
view.outcomes        # item key -> terminal status ("completed" / "failed" /
                     # ...) for settled children, None while in flight
view.unstarted_items # item keys whose child never executed — never
                     # invented results
view.settled         # True when no child is active or queued
view.tolerance_tripped  # True once a pinned tolerance was exceeded
view.retry_of        # source batch_id when minted by client.rerun(batch_ref)
```

Counts are keyed by **logical item key**, never by completion order or
child workflow id. The terminal buckets (`completed`, `failed`, `partial`,
`stopped`) come from the child runs row; `active` is a started nonterminal
child; `queued` a child not yet started but still claimable;
`recovery_exhausted` a parked child (it counts as settled); `unstarted` a
child that finished without ever executing (stop-before-start, or a
tolerance trip that closed admission before it ran).

`client.watch(batch_ref, after=cursor)` follows the whole Batch through one
gap-free per-Batch durable sequence, yielding `BatchUpdate` values with
`bseq:N` cursors. Every manifest or child-outcome change appends one
`batch_updates` row — and where a child fact and a Batch fact commit
together, they land in the same transaction.
Batch update writes are append-only and never backpressure child execution.
Durable facts are `manifest` (bseq 1), `child_settled` (a child settled for
good, with `item_key`, `workflow_id`, and `status` — a terminal status, or
`"recovery_exhausted"` when the recovery brake parked the child),
`tolerance_tripped`, `child_unstarted` (an item that ended unstarted
without the trip fact naming it — a stopped Batch's child that never
executed, or a child a crash returned to pending after the trip — with
`item_key` and `workflow_id`), and `stopped`; live previews fanned in from
child runs arrive with `update.durable is False`, repeat the last durable
cursor, and never advance it:

```python
cursor = None
async for update in client.watch(receipt.batch_ref, after=cursor):
    if update.durable:
        cursor = update.cursor             # "bseq:N" — store this
```

A Batch watch terminates once every child is terminally settled (or the
Batch is durably stopped) and every committed fact has been delivered. The
stream accounts **every manifest item exactly once**, so a detached watcher
never has to read the view to learn an outcome: settled children by their
`child_settled` fact (parked children included), items a tolerance trip
closed admission on by the `tolerance_tripped` payload's `unstarted_items`,
and any other item that ends unstarted — a stopped Batch's child, or one a
crash returned to pending after the trip — by its own `child_unstarted`
fact. `get(batch_ref)` is still the keyed view.
Reconnecting from a stored cursor replays with no gaps and no repeats,
across process restarts, with no graph code. A `BatchRef` unknown to the
Home terminates immediately with no updates. `client.get_sync(batch_ref)`
is the synchronous mirror of `get()`.

## Failure Tolerance and the Trip

A pinned `BatchTolerance` **trips** when failure-equivalent children
**strictly exceed** either threshold. Both are evaluated independently, so
either one exceeded trips the Batch:

```python
tolerance=BatchTolerance(max_failed=2, max_failed_percent=25)  # over 8 items
# 2 failures → tolerated (2 is not more than 2, and 25% of 8 is exactly 2)
# 3 failures → tripped
```

The percentage denominator is always the **total logical manifest item
count pinned at acceptance** — it never shrinks to the items that happen to
have settled, so an early failure can never read as 100%.

**Failure equivalence.** A child counts toward tolerance when its run
`failed` or its submission is recovery-exhausted. Paused, queued, delayed,
admission-limited, and unstarted children never count — and neither do
`partial` or `stopped` runs.

**What a trip does**, all in the same transaction as the child fact that
caused it (so the `child_settled` and `tolerance_tripped` rows land at
consecutive `bseq` values and a reader never sees a Batch that should have
tripped but has not):

- **closes new child admission** — no pending child is claimed again, and a
  child a crash returns to pending is not re-admitted either (that refusal
  appends its own durable `child_unstarted` fact in the same transaction as
  the state flip, so the stream accounts the item too);
- **lets already-claimed children settle** — running work is never killed;
- **marks every remaining item explicitly unstarted** — named by item key,
  never a fabricated failure;
- **leaves the Batch truthfully partial** — mixed outcomes with explicit
  unstarted items, never a failed Batch and never a stopped one.

A trip is a **Batch fact, not a `WorkflowStatus`**: it appears as
`view.tolerance_tripped`, as the durable `tolerance_tripped` update (with
`failed`, `total_items`, the pinned thresholds, and the `unstarted_items`
admission closed), and nowhere in any run's status.

## Stopping a Batch

`client.stop(batch_ref, info=None)` records a **durable Batch stop**: one
transaction appends the `stopped` batch update and writes a durable stop
command for every unsettled child:

- **pending children** finish without ever executing — no runs row is
  invented; they become explicit `unstarted_items`, each recorded by its
  own `child_unstarted` Batch fact in the same transaction as the flip (the
  `stopped` fact carries only the verb and info, so this is how the stream
  names them);
- **executing children** receive the stop on the worker's next scan and
  settle cooperatively as `STOPPED`;
- **settled children** are unaffected.

```python
receipt = await client.stop(receipt.batch_ref, info={"reason": "drop recalled"})
receipt.verb          # "stop"
receipt.duplicate     # True when the Batch was already stopped
```

`BatchCommandReceipt` mirrors `CommandReceipt` (inert, with `batch_ref`,
`verb`, `duplicate`). Stopping a Batch whose children all settled raises
`AlreadyTerminalError`; an unknown `BatchRef` raises `HostError`.
`client.stop_sync(batch_ref, ...)` is the synchronous mirror.

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
A `ref` unknown to the Home (neither a submission nor a runs row)
terminates immediately with no updates — matching `get()`'s honest `None`
— instead of polling forever. `get_sync()` is the synchronous mirror of
`get()`.

`WaitingCondition` is a closed enum — `QUEUED`, `SCHEDULED`, `PAUSED`,
`VERSION_INCOMPATIBLE`, `ADMISSION_LIMITED`, `RECOVERY_EXHAUSTED` — so
waiting work never looks alike and callers branch on typed values. `waiting`
is `None` while a Run executes or is terminal — including while a running
Run is queued behind a [provider permit](#provider-resource-admission),
which is execution, not waiting.

## Stopping a Run

`client.stop(ref, info=None)` records a **durable stop command**: the
command row and its `command` update commit in one transaction before the
call returns, so the stop survives process loss. The worker applies it on
its next scan:

- **executing run** → the runner receives `stop(workflow_id, info=info)`;
  the run settles as `STOPPED` cooperatively (in-flight nodes finish).
- **stop landed before first execution** → the run never executes and no
  runs row is invented; the submission settles as finished.
- **run completed before the stop was observed** → the command is marked
  applied with no effect.

```python
receipt = await client.stop(receipt.run_ref, info={"reason": "user asked"})
receipt.verb          # "stop"
receipt.duplicate     # True when an unapplied stop already existed
```

`CommandReceipt` mirrors `SubmitReceipt`: inert, with `run_ref`, `verb`,
and `duplicate`. A second stop while one is still unapplied dedupes with
`duplicate=True` — **the first stop's `info` wins**. Stopping a run that is
already terminal at write time raises `AlreadyTerminalError`; stopping an
id unknown to the Home raises `HostError`. `client.stop_sync(...)` is the
synchronous mirror.

Like `submit()`, `stop()` accepts an optional opaque `source_ref`
(`client.stop(ref, info=..., source_ref="ops-console")`) recorded on the
command row for audit (ADR 0005 A11) — it is never authentication and never
affects dedup. The worker only delivers a stop to a run the Definition's
runner reports as live (`runner.has_active_run(workflow_id)`), so a stop
never lands on a stale or not-yet-registered execution.

## Listing Runs

`client.list(query)` filters joined run views through a typed `RunQuery` —
submissions joined with their runs rows, plus bare Tier-0 runs (a runs row
with no submission). Results come oldest first, capped at `query.limit`
(default 100):

```python
from datetime import timedelta
from hypergraph import RunQuery, WaitingCondition
from hypergraph.checkpointers.types import WorkflowStatus

stuck = await client.list(RunQuery(waiting=WaitingCondition.RECOVERY_EXHAUSTED))
old = await client.list(RunQuery(older_than=timedelta(hours=1)))
failed = await client.list(RunQuery(definition="refund", status=WorkflowStatus.FAILED))
```

Every field is a typed value — never a free string: `definition` matches
the Definition name, `status` matches the runs row (runs without one never
match), `waiting` is computed exactly like `RunView.waiting`, `batch`
accepts a `BatchRef` or a bare batch id string and restricts results to
that Batch's children, and `older_than` compares creation time. Omitted
fields match everything. `limit` must be a positive `int`.
`client.list_sync(...)` is the synchronous mirror.

## Rerun: Repeat Settled Work

`client.rerun(ref)` repeats terminal work under a new id with retry
lineage — it lives on the client because it needs **no loaded Definition
code**: the new submission carries the source's pinned `DefinitionId` and
inputs verbatim.

```python
rerun_receipt = await client.rerun(receipt.run_ref)
rerun_receipt.workflow_id      # "refund-c-42-retry-1"
```

The worker executes the rerun with `retry_from` lineage: the new runs row
records `retry_of`/`retry_index`, and completed-step checkpoints from the
source are reused. There is deliberately **no input override** — the
signature is `rerun(ref)` and passing `inputs=` is a `TypeError`; changed
inputs use a normal new `submit()`. The source must exist and be terminal,
else `RerunError` — with one exception: a **recovery-exhausted** source is
exactly the rerun case (reviving braked work), so `rerun()` accepts it even
without a terminal runs row. `client.rerun_sync(...)` is the synchronous
mirror.

### Item-scoped Batch rerun

`client.rerun(batch_ref, item_keys=[...])` repeats **named source items**.
It never mutates the source Batch: it mints a **new immutable manifest**
with a new `BatchRef`, containing new child Runs for the selected keys
only, carrying the source's pinned `DefinitionId`, source inputs, and
pinned tolerance:

```python
rerun_receipt = await client.rerun(receipt.batch_ref,
                                   item_keys=["protocol-20", "protocol-21"])
rerun_receipt.batch_ref        # NEW BatchRef — a new manifest, not a mutation
rerun_receipt.workflow_id      # "schneider-drop-42-retry-1"
```

- **Lineage.** The new Batch records `retry_of` against the source
  `batch_id` (readable as `BatchView.retry_of`), and each new child records
  `retry_of` against its **source child's workflow id**. The source Batch
  stays settled and queryable forever.
- **Keys.** Only keys present in the source manifest are accepted;
  anything else is a `RerunError` naming the offending keys. Selected
  children must already be settled — a child still in flight is a
  `RerunError`, so one logical item never has two live Runs. Omit
  `item_keys` to repeat the whole manifest.
- **No overrides.** There is no `inputs` parameter here either, and
  `item_keys` with a `RunRef` is a `TypeError` (a Run has no items).
  Definition-identity changes go through `fork()`.

`client.rerun_sync(batch_ref, item_keys=[...])` is the synchronous mirror.

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

## Host Work Admission

`RunHome.max_active_runs` caps how many Runs a worker executes at once.
The cap is a **Home-scoped fact stored in the Run Home**, not a setting on
one Python object, so it is **tunable at runtime from anywhere that can open
the Home** — including a one-off operator process while the worker keeps
running:

```python
# In the operator's shell — the worker is a different process.
home = RunHome.open("file:./runs.db")
home.max_active_runs = 1     # shed load; the live worker's next claim honors it
home.max_active_runs = None  # unlimited (the default)
home.max_active_runs         # reads what the store holds
```

Passing `max_active_runs` to `open()` **writes it through**; omitting it
**adopts whatever the store already holds**. That is the difference between
declaring the cap and merely connecting:

```python
RunHome.open("file:./runs.db", max_active_runs=4)     # sets the cap to 4
RunHome.open("file:./runs.db", max_active_runs=None)  # sets it to unlimited
RunHome.open("file:./runs.db")                        # adopts the stored cap
```

Over-limit work **waits in claim order** — oldest submission first, ties
broken by insertion order so the ordering is total. It is never rejected and
never cancelled: the submission stays persisted and pending, its view
reports `WaitingCondition.ADMISSION_LIMITED`, and it claims the moment a
slot frees. Lowering the cap below the work already outstanding revokes
nothing; those Runs finish and the next claim simply waits. The cap must be
an `int >= 1` or `None`.

**What holds a slot.** A *claimed* Run — one a worker owns end to end,
whether it is starting, executing, settling, or parked on a provider permit
— holds one slot. Queued, scheduled, paused, version-incompatible, and
recovery-exhausted Runs hold **none**.

```python
limited = await client.list(RunQuery(waiting=WaitingCondition.ADMISSION_LIMITED))
```

Because the cap and the claim count are both stored facts, a
`RunHomeClient` in an inspection process reports exactly the work the worker
is holding back — no separate configuration to keep in sync. An uncapped
Home never reports `ADMISSION_LIMITED` at all.

Overflow strategies are deliberately **absent** in v1: there is no reject,
no cancel-oldest, no cancel-newest, no keyed fairness, and no
expression-language admission key. Overload delays work; it never drops it.

## Provider-Resource Admission

Host work admission is not the same control as an external provider's
concurrency limit, and Hypergraph keeps them apart. A `ProcessLocalLimiter`
is an injected budget over **external capacity** — the name states its
scope: it coordinates only this process, and this tier ships no distributed
limiter.

```python
from hypergraph import ProcessLocalLimiter

quota = ProcessLocalLimiter(max_in_flight=4)
quota.max_in_flight    # 4
quota.in_flight        # permits held right now
```

Three injection scopes, narrowest budget last:

```python
class SummaryClient:                      # component scope — usually the right
    def __init__(self) -> None:           # owner of a provider quota
        self._quota = ProcessLocalLimiter(max_in_flight=4)

    async def summarize(self, text: str) -> str:
        async with self._quota:           # acquired at the exact scarce call
            return await self._http.post(...)

@node(output_name="summary", provider_limit=ProcessLocalLimiter(max_in_flight=2))
async def summarize(doc: str) -> str: ... # node scope

graph = graph.with_provider_limit(ProcessLocalLimiter(max_in_flight=8))
#                                         graph scope (immutable, metadata only)
```

Several graphs and nodes reuse one shared component, so letting the
component own the quota keeps the permit held for the provider call and
nothing else. Graph- and node-scope limits are **work budgets**: the permit
covers the whole node execution, retry backoff included. They compose as
narrower limits around a component quota; they never replace it. Give each
scope its own limiter instance — the pools are not reentrant.

A graph budget is a shared object, so two concurrent Runs of the same graph
draw on the same permits — what a per-call runner budget cannot express.
`graph.with_provider_limit(...)` returns a new Graph and changes no
structure: `structural_hash` and therefore Definition identity are
untouched. Nested graphs are covered: a graph run inside `as_node()`
inherits the enclosing budget and composes its own on top, so a node stays
covered when it moves into a nested graph.

**Waiting for a permit is neither a failure nor a retry attempt.** The wait
happens outside the attempt coordinator, so ordinary throttling reserves no
attempt, consumes no `RetryPolicy` budget, and runs down no node `timeout`.
A claimed Run parked on a permit is *executing*: `RunView.waiting` is
`None`, its status is unchanged, and it still holds its Host slot.

## Delayed Start

`submit()` and `submit_batch()` accept `start_at` (a `datetime` or ISO
string) for one-shot future work — no external timer service:

```python
receipt = await host.submit("refund", {"claim_id": "c-42"},
                            workflow_id="refund-c-42",
                            start_at=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc))
```

The submission persists immediately, so a restart cannot lose the schedule.
It is normalized to a UTC ISO timestamp at submit time (naive inputs read as
UTC; offsets converted), so equivalent spellings of the same instant dedupe
identically — and it is part of the **start fingerprint**, so the same id
with a different `start_at` is a `WorkflowIdConflictError`.

**Store time controls eligibility.** Until the Run Home's clock reaches
`start_at`, the submission is never claimed and its view reports
`WaitingCondition.SCHEDULED`. A **past** `start_at` is immediately eligible
(it reports `QUEUED`, not `SCHEDULED`). Scheduled work holds no active-Run
slot, so a delayed backlog never blocks work that is due now.

`client.stop(ref)` on future work **prevents execution**: when the time
arrives the pre-run gate finishes the submission without ever running the
graph and without inventing a runs row — the same stop-before-first-execution
path a queued run takes.

## Crash Recovery and the Recovery Brake

When a worker dies mid-run (SIGKILL, power loss), the next worker's startup
scan re-adopts the orphaned submission and **resumes** the run: committed
steps are skipped from their checkpoints, only unfinished work re-executes.
A run that settled before the crash — including `STOPPED` — is finished and
never resumed.

Unbounded resume would loop a poison run forever, so every submission
carries a recovery brake (`recovery_cap` at submit time, default `3`). The
brake counts **progressless re-adoptions**:

- every re-adoption of a crashed (claimed-but-unfinished) submission at
  worker startup increments `recovery_attempts` — a run killed mid-flight
  *with* committed steps therefore shows `recovery_attempts=1` after the
  restart scan;
- **new committed progress** — a saved `StepRecord`, a durable pause, or a
  terminal transition — resets the counter to 0 at commit time (a status
  flip back to `active` at claim/re-adoption does **not**; neither does the
  resume itself, since journal-skipped steps are not re-saved);
- when the incremented count reaches the cap, the submission is parked as
  recovery-exhausted: workers stop claiming it, a durable
  `recovery_exhausted` update is recorded, and the view reports
  `WaitingCondition.RECOVERY_EXHAUSTED`. Parking is a settled,
  failure-equivalent **child outcome**, so a parked Batch child also gets a
  `child_settled` Batch fact with `status: "recovery_exhausted"` in that
  same transaction — the stream reports the parked child exactly as
  `BatchView.outcomes` does.

`client.rerun(ref)` revives braked work under a fresh workflow id.

## Errors

| Error | Raised when |
|---|---|
| `WorkerLockError` | a second worker starts on the same Run Home |
| `AlreadyTerminalError` | a terminal `workflow_id` is reused for submit, submit_batch, or stop (including a fully settled Batch) |
| `WorkflowIdConflictError` | a nonterminal `workflow_id` is reused with a different start fingerprint (Run or Batch), or a Batch id collides with existing work |
| `ForkCompatibilityError` | `host.fork()` targets a structurally incompatible Definition |
| `RerunError` | `client.rerun()` names a missing or nonterminal source (recovery-exhausted sources are allowed) |
| `HostError` | base class for host-specific errors; also raised directly for an unknown stop target |
