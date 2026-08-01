# Durable Host

A durable host lets one process ask for work durably and leave: submission is
persisted before execution, a product-owned worker executes it, and any other
process can inspect and follow the run through a small client. It is additive
— direct `runner.run()` / `start_run()` (Tier 0) are unchanged, and the host
is never required.

{% hint style="info" %}
This page covers the local Tier 1 host: `HostRuntime`, `serve()`, `RunHome`,
`Host`, and `RunHomeClient`. For direct execution semantics see [Runners](runners.md);
for process-local background handles see
[Control Work After It Starts](../05-how-to/control-background-execution.md).
`client.answer()` settles a durable pause **and re-admits the run** in one
transaction — see [Answering a Pause](#answering-a-pause).
{% endhint %}

## Owning a Host Process

Use `HostRuntime` when one application process should own the Run Home, serve
Definitions as they become known, and keep one worker alive:

```python
from hypergraph import HostRuntime

runtime = HostRuntime("./data/runs.db", deployment_version="2026.07.3")

# The Home opens here, not when HostRuntime is constructed. An unbound graph
# gets AsyncRunner; an explicit graph.with_runner(...) binding is preserved.
host = await runtime.serving(refund_graph)
receipt = await host.submit(refund_graph, {"claim_id": "c-42"})

# Additive and idempotent: the existing worker and active Runs stay in place.
await runtime.serving(triage_graph)

client = runtime.client
await runtime.close()  # stop claiming, drain active Runs, close the Home
```

`serving()` returns the same `Host` on every call. Re-serving the same
Definition identity is a no-op; a new name is added to the live worker. A
served name cannot be replaced with a structurally different Definition
without closing and creating a new runtime. Worker startup runs the ordinary
restart scan, so unfinished durable work is re-adopted without resubmission.

`client` is the runtime's `RunHomeClient` and is also lazy: accessing it before
`serving()` opens the Home for detached reads without starting a worker. If the
worker task exits with an exception, the next `serving()`, `client`, or
`close()` call raises `RuntimeError` with that exception as its cause rather
than silently starting a replacement. A later call may start the worker again.

`close()` is idempotent. It stops new claims, uses the Host's bounded drain,
and closes the Home; queued submissions remain persisted for the next process.
The same runtime may be used again after closing, which lazily reopens it.

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
receipt = await host.submit(refund, {"claim_id": "c-42"},
                            workflow_id="refund-c-42")
receipt.run_ref        # RunRef — inert, JSON-serializable address
receipt.duplicate      # True when an identical nonterminal submission existed
```

Submission is **graph-first**: you pass the `Graph` object you served, not a
Definition-name string. The Host resolves it by the graph's own pinned
identity — its `name` **and** its `structural_hash` — so the code you hold a
reference to is the code that runs. A graph this host does not serve, or one
whose structure has drifted from the served Definition, raises
`UnservedGraphError` at the call site instead of being accepted and parked;
a bare string raises `TypeError`. The same rule covers `submit_batch()` and
`fork(..., into=graph)`.

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

Host submissions, Batches, and **host-less runs share one `workflow_id`
namespace**. A Run Home is also an ordinary checkpointer, so a run may exist
with no submission behind it — executed directly against the store (Tier 0).
The host holds no pinned identity or fingerprint for such a run, so it can
never adopt or dedupe into it: reusing that id raises `AlreadyTerminalError`
when the host-less run is terminal and `WorkflowIdConflictError` while it is
still running. The same check covers a Batch's `workflow_id` and every
generated child id (`<batch workflow_id>:<item key>`).

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
    ingest,                                    # the served Graph object
    {"work_item_id": ["protocol-17", "protocol-18"], "reviewer": "ops"},
    map_over="work_item_id",                   # which inputs expand per item
    identity="work_item_id",                   # which expanded input names the item
    workflow_id="schneider-drop-42",
    tolerance=BatchTolerance(max_failed=2, max_failed_percent=25),  # optional
)
receipt.batch_ref          # BatchRef — inert, JSON-serializable address
receipt.duplicate          # True when an identical nonterminal Batch existed
```

Callers that already hold per-item records can submit them without
transposing into parallel collections. Persisted inputs remain plain mappings:

```python
receipt = await host.submit_batch(
    ingest,
    [
        {"case_label": "station-a", "pdf_uri": "papers/17.pdf", "page_count": 8},
        {"case_label": "station-b", "pdf_uri": "papers/18.pdf", "page_count": 5},
    ],
    identity="case_label",
    admission_cost="page_count",
    workflow_id="schneider-drop-43",
)
```

Every item field except the identity must be a graph boundary input, every
required boundary input must be present, and the resulting mapping must be
JSON-serializable. A manifest-only identity such as `case_label` above is
removed from each child's pinned inputs, so the graph never sees it. These
checks and identity validation all happen before acceptance.

`admission_cost` optionally names a positive-integer item field. The Host
validates and freezes that cost on each child submission; reruns preserve
the stored accounting. When omitted, every child costs one unit.

`submit_batch` speaks the same **input-expansion vocabulary as runner map**
— `values` plus `map_over` (one name or a sequence) and `map_mode`
(`"zip"`, the default, or `"product"`) — and then **freezes** the expansion
into the immutable manifest. Names not in `map_over` are broadcast to every
item, exactly as in `runner.map(...)`. Two knobs runner map has are
deliberately absent: durable concurrency comes from
[Host work admission](#host-work-admission), not a per-call
`max_concurrency`, and durable failure policy comes from `tolerance`, not
`error_handling`.

`identity` names one **expanded** field whose value is the logical item key —
the identity every count, outcome, and durable fact is reported under. It
must be a name in `map_over` for runner-shaped values, but it does not need
to be a graph input. A manifest-only identity is retained as the manifest
key and omitted from child Run inputs. Each identity value must be a
JSON-safe scalar (a non-empty `str`, or an `int`), unique across the
manifest; missing, empty, non-scalar (including `bool`, `float`, and `None`),
or duplicate keys raise `ItemKeyError` before anything is written. Expanding
to zero items is a `ValueError` — an empty Batch is not a Batch.

**One transaction** persists all of it — the manifest row (Definition
identity, item keys with pinned inputs, the tolerance declaration, the
start intent), one child submission per item key (child workflow id
`<workflow_id>:<item_key>`), and the `manifest` fact at per-Batch durable
sequence `bseq=1`. A kill anywhere inside acceptance leaves the Batch fully
absent, never half-accepted. Expansion order is the manifest order used for
keyed outcomes.

There is deliberately no `host.map()`. Two new-work verbs cover durable
submission — `submit` for one Run, `submit_batch` for many — and a `map`
verb would promise an immediate `MapResult` while a durable Batch returns a
receipt for work that has not started yet.

Children are **ordinary Runs**: they flow through the same
claim/execute/stop/recovery machinery as submitted runs — each gets a
`RunView`, a run-level `watch`, durable stop, and crash recovery — with
their Batch membership recorded. `host.submit_batch_sync(...)` is the
synchronous mirror.

Dedup mirrors `submit`, over a start fingerprint covering the pinned
Definition identity, the normalized manifest, the pinned tolerance,
`start_at`. Resubmitting the same `workflow_id`:

- **fingerprint-identical and nonterminal** → the existing receipt with
  `duplicate=True`, naming the same `BatchRef`;
- **fingerprint mismatch** (different identity, items, tolerance, or
  `start_at`) → `WorkflowIdConflictError`;
- **fully settled Batch** → `AlreadyTerminalError`, whether or not the
  fingerprint matches.

Run and Batch workflow ids share one namespace: reusing an id owned by a
plain run submission (or a run id owned by a Batch) is a conflict too — and
so is one already owned by a host-less run, for the Batch id and for every
generated child id alike.

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
                     # {"completed": 1, "failed": 0, "active": 0, "paused": 1,
                     #  "queued": 1, "unstarted": 0, "abandoned": 0, ...}
                     # — all keys always present
view.items           # item key -> BatchItemView (per-item truth, below)
view.outcomes        # item key -> terminal status ("completed" / "failed" /
                     # ...) for settled children, None while in flight
view.unstarted_items # item keys whose child never executed — never
                     # invented results
view.abandoned_items # item keys whose child HAD started when a tolerance
                     # trip closed admission — reconcile before rerunning
view.settled         # True when no child is active, paused, or queued
view.tolerance_tripped  # True once a pinned tolerance was exceeded
view.retry_of        # source batch_id when minted by client.rerun(batch_ref)
```

Counts are keyed by **logical item key**, never by completion order or
child workflow id. The terminal buckets (`completed`, `failed`, `partial`,
`stopped`) come from the child runs row; `active` is a child claimed and
executing; `paused` a child parked on a durable pause slot waiting for a
human; `queued` a child not yet started but still claimable;
`recovery_exhausted` a parked child (it counts as settled); `unstarted` a
child that finished without ever executing (stop-before-start, or a
tolerance trip that closed admission before it ran); `abandoned` a child
that HAD started when a tolerance trip closed admission.

`unstarted` and `abandoned` are separate for the same reason `active` and
`paused` are. An unstarted item ran nothing, so it is safe to rerun from
scratch. An abandoned item committed steps and may already have landed side
effects, so it is the one an operator has to reconcile first. Calling both
"unstarted" would say nothing happened when something did.

`active` and `paused` are separate buckets because they answer different
operational questions. `active` means *this item is consuming a worker right
now*; `paused` means *this item is consuming nothing and is waiting on a
person*. A paused child holds no active-Run admission slot, so a Batch of
100 items where 99 are parked on questions still lets the hundredth run
under `max_active_runs=1`.

`view.items` is the per-item view — one `BatchItemView` per manifest key, so
a caller learns an item's whole situation without cross-referencing three
maps:

```python
item = view.items["protocol-17"]
item.item_key        # "protocol-17" — the logical key, not the child run id
item.run_ref         # RunRef for the child — inert, addresses client.get/answer/stop
item.workflow_id     # "<batch workflow_id>:protocol-17"
item.status          # WorkflowStatus | None — the child runs row, None before it starts
item.waiting         # WaitingCondition | None — PAUSED, QUEUED, ADMISSION_LIMITED, …
item.outcome         # terminal status once settled ("completed" / "recovery_exhausted" / …)
item.started         # False until the child has a runs row at all
```

`item.run_ref` is the address you answer or stop an individual child
through: `await client.answer(item.run_ref, pause_id=…, value=…)`.

Printing one reads as a single line — the item, where it is, and its child
run — so triaging a Batch in a REPL or notebook needs no destructuring:

```python
>>> view.items["protocol-17"]
BatchItem: protocol-17 | waiting: paused | drop-2026-07:protocol-17
```

The middle field answers "where is this item?" in priority order: a settled
`outcome` if it has one, else the `waiting` condition (the actionable
thing), else a bare running `status`, else `unstarted` — the same word
`view.unstarted_items` uses. In a notebook it renders as a panel with the
child's address; `set_display_mode("plain")` falls back to the text form.

`client.watch(batch_ref, after=cursor)` follows the whole Batch through one
gap-free per-Batch durable sequence, yielding `BatchUpdate` values with
`bseq:N` cursors. Every manifest or child-outcome change appends one
`batch_updates` row — and where a child fact and a Batch fact commit
together, they land in the same transaction.
Batch update writes are append-only and never backpressure child execution.
Durable facts are `manifest` (bseq 1), `child_settled` (a child settled for
good, with `item_key`, `workflow_id`, and `status` — a terminal status, or
`"recovery_exhausted"` when the recovery brake parked the child),
`child_paused` (a child parked on a human, with `item_key`, `workflow_id`,
`run_ref`, and the `pause_id` of the occurrence), `child_runnable` (that
child's answer settled and it is claimable again, naming the `pause_id` that
was answered), `tolerance_tripped` (carrying both `unstarted_items` and
`abandoned_items`), `child_abandoned` (a started child a trip closed
admission on, with `item_key` and `workflow_id`), `child_unstarted` (an item that ended
unstarted without the trip fact naming it — a stopped Batch's child that
never executed, or a child a crash returned to pending after the trip —
with `item_key` and `workflow_id`), and `stopped`; live previews fanned in
from child runs arrive with `update.durable is False`, repeat the last
durable cursor, and never advance it:

```python
cursor = None
async for update in client.watch(receipt.batch_ref, after=cursor):
    if update.durable:
        cursor = update.cursor             # "bseq:N" — store this
```

`child_paused` and `child_runnable` are **lifecycle** facts, not
end-of-stream facts: a child can pause, be answered, resume, and pause again
at a second interrupt, appending a new pair each time. Only `child_settled`,
`child_unstarted`, `child_abandoned`, and the trip's `unstarted_items` /
`abandoned_items` account an item for good.

A Batch watch terminates once **every manifest child is accounted** —
settled, unstarted, abandoned, or recovery-exhausted — and every committed
fact has been delivered. Stopping a Batch does **not** end its stream:
`stopped` is a durable control fact appended *before* the child stop
commands it writes, and each of those commits its own `child_unstarted`
fact afterwards. The stream accounts **every manifest item exactly once**,
so a detached watcher never has to read the view to learn an outcome:
settled children by their `child_settled` fact (parked children included),
items a tolerance trip closed admission on by the `tolerance_tripped`
payload's `unstarted_items` and `abandoned_items`, and any item that
reaches closed admission afterwards — a stopped Batch's child, or one a
crash returned to pending after the trip — by its own `child_unstarted`
fact if it never started, or `child_abandoned` if it had. `get(batch_ref)`
is still the keyed view.
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

### Serving truthful UI read models

Products that need HTTP or UI rows should build on `RunHomeReadModel`
instead of translating `RunView` independently:

```python
from hypergraph import RunHomeReadModel, RunQuery

read = RunHomeReadModel(client)
rows = await read.list_runs(RunQuery(definition="ingest"))

# A framework-agnostic route is now only serialization plus a domain join.
return [
    {**row.to_dict(), "title": titles[row.inputs["document_id"]]}
    for row in rows
]
```

`client` is any `RunHomeClient`; a process that owns its Host passes
[`HostRuntime.client`](#owning-a-host-process), which opens the Home for
detached reads without starting a worker.

Each `RunReadModel` carries a closed coarse `status` (`queued`, `running`,
`paused`, or an exact terminal `WorkflowStatus` value) and the precise Run
Home `condition` that produced it. For example, scheduled and
version-incompatible work both renders as queued while retaining
`"scheduled"` or `"version_incompatible"` as its condition. Terminal values
come from `TERMINAL_STATUS_VALUES`; they are not copied into another enum.

Rows also carry pinned inputs, acceptance/start/settlement timestamps, the
latest durable fact timestamp, and an open `PauseReadModel` when the Run is
parked on a person. The pause exposes the graph-authored question unchanged
(`ask`), its exact durable JSON `answer_schema`, and options when the answer
is option-shaped, so a new gate needs no read-surface change.

`get_batch(batch_ref)` returns a `BatchReadModel`: the existing Batch census
plus one `BatchItemReadModel.word` per manifest item. Those words come
directly from `item_condition()`, and counts come directly from `BatchView`;
the read layer does not maintain a second bucket ladder. Every model has a
`to_dict()` of JSON-safe primitives, and every async method has a `_sync`
mirror. Hypergraph intentionally does not mount an HTTP router: the core has
no web-framework dependency, and products remain responsible for
authorization and for deciding which pinned inputs or pause evidence may
cross their HTTP boundary.

## Reading Results

`client.result(ref)` answers what durable work **produced**. The worker keeps
no `RunResult`, so outputs are reconstructed from the same checkpointer rows
that back resume — no graph code, no second store.

```python
outcome = await client.result(receipt.run_ref)   # RunOutcome | None
outcome.status                                   # WorkflowStatus | None
outcome.outputs                                  # dict | None
outcome.failure                                  # RunFailure | None
outcome.to_dict()                                # JSON-safe primitives
```

Four situations a caller must tell apart, and how each reads:

| situation | `result()` | `started` | `settled` | `outputs` |
| --- | --- | --- | --- | --- |
| never submitted here | `None` | — | — | — |
| submitted, still in flight | outcome | any | `False` | `None` |
| stopped/closed before starting | outcome | `False` | `True` | `None` |
| ran and produced nothing | outcome | `True` | `True` | `{}` |

So `outputs is None` never means "produced nothing" — that is `{}`.
`settled` uses the same settled-child rule as `BatchView` and the rerun
gate, so a Run is never settled for one reader and in flight for another; a
paused Run is **not** settled, because an outstanding answer can still
change it.

Two honest caveats on `outputs`:

- They are the run's **folded step outputs**, not a projection narrowed to
  the graph's declared outputs. Narrowing needs the `Graph` object, and this
  client is graph-free by contract, so it reports every value the run's
  steps produced rather than guessing.
- They are JSON-safe because a Run Home's checkpointer defaults to
  `JsonSerializer`. A Home explicitly configured with `PickleSerializer`
  returns whatever it stored; `to_dict()` falls back to `repr` for anything
  that will not serialize.

`RunFailure` carries the **privacy-safe** projection the step persisted —
an exception type name, a stable `HG_*` code, and static wording — never
raw exception message text, which Hypergraph does not persist (see
[the privacy boundary](errors.md#the-privacy-boundary)). The real message,
type, and traceback go to the
[OpenTelemetry export](../05-how-to/observe-execution.md#exception-detail-and-redacting-it)
instead, so a failure is debugged there and merely identified here.

A `BatchRef` reads every child at once:

```python
outcome = await client.result(receipt.batch_ref)   # BatchOutcome | None
outcome.items                                      # item_key -> RunOutcome | None
```

`items` is keyed by logical item key in **manifest order** — completion
order never changes result identity. An item whose child never executed maps
to `None`: Hypergraph does not fabricate results for work that did not run,
and that stays distinct from a child that ran and produced nothing
(`outputs == {}`). The whole Batch reads in a bounded number of statements
regardless of child count, so 100+ children stay a handful of queries.

`result_sync()` is the synchronous mirror.

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

- **run parked on a durable pause** → the run is not terminal, so the stop
  is accepted and waits, landing the next time that run executes.

`CommandReceipt` mirrors `SubmitReceipt`: inert, with `run_ref`, `verb`,
and `duplicate`. A second stop while one is still unapplied dedupes with
`duplicate=True` — **the first stop's `info` wins**. Stopping a run that is
already terminal at write time raises `AlreadyTerminalError`; stopping an
id unknown to the Home raises `HostError`. `client.stop_sync(...)` is the
synchronous mirror.

Like `submit()`, `stop()` accepts an optional opaque `source_ref`
(`client.stop(ref, info=..., source_ref="ops-console")`) recorded on the
command row for audit (ADR 0005 A11) and carried on the durable `command`
update so `watch()` can join a product's authenticated action to Host
history. Every surface that accepts work records it the same way —
`submit()`, `submit_batch()`, `stop()`, `schedule_answer()`, `rerun()`, and
`fork()` — so provenance survives a repeat or a migration instead of ending
at the original submission. It is **never**
authentication and **never** part of dedup: two commands differing only in
`source_ref` are the same command. The worker only delivers a stop to a run the Definition's
runner reports as live (`runner.has_active_run(workflow_id)`), so a stop
never lands on a stale or not-yet-registered execution.

## Answering a Pause

When a run pauses at an `InterruptNode`, the occurrence is persisted as a
**durable pause slot** — question, answer port, answer schema, and a unique
`pause_id` — committed with the `PAUSED` transition and never before the
paused step, so a committed `PAUSED` run always has its slot. See
[Durable Pause Slots](checkpointers.md#durable-pause-slots) for the record
itself.

Read the occurrence, then answer the one you observed:

```python
slot = await client.get_run_slot(ref)      # None when the run has no pause
slot.pause_id                              # 'refund-c-42:8:approval'
slot.question["prompt"]                    # 'Approve this refund?'
slot.answer_schema                         # {'type': 'boolean'}

settled = await client.answer(ref, pause_id=slot.pause_id, value=True)
settled.answer                             # True
settled.settled_at                         # set — the occurrence is closed
```

`answer()` returns the settled `PauseSlot` rather than a `CommandReceipt`:
unlike `stop()` — a command a worker applies later — an answer is applied
immediately, and the schema check, the compare-and-set on `pause_id`, the
resume-input write, and the durable `answer` fact all commit in one
transaction. `client.answer_sync(...)` and `client.get_run_slot_sync(...)`
are the synchronous mirrors. `answer()` takes a `RunRef` only; a Batch is
answered through its child runs.

Every answer names the occurrence it observed, so the three refusals stay
distinguishable:

| Error | Raised when |
|---|---|
| `AnswerRejectedError` | no `pause_id`, an unknown one, a run with no durable pause, a run that is not paused, or a value failing `answer_schema` |
| `PauseAlreadySettledError` | this occurrence was already answered — the first caller's value wins |
| `StalePauseError` | a later pause occurrence is current (carries `.current_pause_id`) |

All three subclass `PauseSettlementError`, which is a `HostError` — so
`except HostError` around client calls catches them like any other
durable-host refusal.

A rejected value is refused **before any write**, so the occurrence stays
open and a corrected value can still answer it. Answer-versus-stop races
resolve by commit order: an answer that commits first stands (a later stop is
still accepted and settles the run), while a stop whose terminal transition
commits first makes the answer raise `AnswerRejectedError` and leaves the
slot open.

**A parked run is in flight, not settled.** When a hosted run pauses, the
worker releases its active-Run slot but the submission is recorded as
`paused`, never `finished`: the run is nonterminal and a human decision is
outstanding. So `watch(run_ref)` and `watch(batch_ref)` keep following it, a
Batch containing a paused child reports `settled=False` with that child
counted `paused`, and `client.stop()` on a parked run is **accepted** rather
than refused as terminal. A `paused` submission is not claimable — parking
is not re-admission.

### Answering re-admits the run

Settlement does not just write resume input; it **makes the run runnable
again**, in the same transaction:

```python
settled = await client.answer(item.run_ref, pause_id=slot.pause_id,
                              value=DuplicateDecision.REPLACE)
# one commit: schema check → CAS on pause_id → answer + resume input →
#             run `answer` fact → Batch `child_runnable` fact →
#             submission paused → pending
```

There is no window in which an accepted answer exists but the run is still
parked, and none in which a run is re-admitted without its answer durably
stored. Process death between the two is therefore not a state the store can
hold: the answer either committed with the re-admission or neither happened,
and an idle worker picks the run up on its next scan.

The worker resumes the **same** checkpointed workflow id with **only** the
settled `{response_key: answer}` — never the pinned start inputs again, which
strict checkpoint resume would refuse as an input override
(`InputOverrideRequiresForkError`). Execution continues from the checkpoint,
so the answer can route the rest of the graph anywhere the graph allows: a
different downstream branch, a loop back to a second interrupt (which mints a
new `pause_id` and parks again), or straight to a terminal status.

Two races resolve **only by commit order**, never by wall clock:

- **answer vs stop** — an answer that commits first stands and the run
  resumes; a stop whose terminal transition commits first makes the answer
  raise `AnswerRejectedError` and leaves the slot open. Stopping a paused
  child is a *stop*, not a duplicate-resolution decision: the run settles
  `STOPPED` and the domain question stays unanswered.
- **answer vs answer** — the first commit wins with
  `PauseAlreadySettledError` for the loser, and exactly one
  `child_runnable` fact is appended, so a doubly-answered item can never
  continue twice.

{% hint style="info" %}
Tier 0 resume — `runner.run(graph, {slot.response_key: slot.answer}, workflow_id=...)`
— is unchanged, and remains how you resume a run you never submitted to a host.
{% endhint %}

## Scheduling an Answer

"If nobody approves this within 72 hours, treat it as declined" is a
**scheduled answer**: one typed value armed against one observed pause
occurrence, applied when store time reaches `due_at`.

```python
from datetime import datetime, timedelta, timezone

slot = await client.get_run_slot(ref)
receipt = await client.schedule_answer(
    ref,
    pause_id=slot.pause_id,
    value=False,                                            # checked against slot.answer_schema NOW
    due_at=datetime.now(timezone.utc) + timedelta(hours=72),
    source_ref="review-console:req-91",
)
receipt.verb          # "schedule_answer"
receipt.duplicate     # True when this occurrence already had an unapplied timer
```

It is admitted through the **same refusal cascade** `answer()` uses, so an
unarmable timer is refused now rather than discovered dead in three days:
no `pause_id`, an unknown one, a superseded one, an already-answered one, a
run that is not paused, or a value failing `answer_schema` all raise before
anything is written. One timer per occurrence — a second `schedule_answer`
for the same pause returns `duplicate=True` and the first one's value wins.
`client.schedule_answer_sync(...)` is the synchronous mirror.

`due_at` is required (an answer with no due time is `answer()`) and is
normalized to a UTC ISO timestamp exactly like `start_at`, because the
worker decides both with **one due-row scan**: each pass takes one `now`
**from the store's own clock** and applies it to submissions whose
`start_at` has arrived and to scheduled answers whose `due_at` has arrived.
One clock, so a worker whose process clock drifts never claims early or
fires late, and two workers on one Home agree on which rows are due. There
is no second timer service, and there is no recurrence — cron, repeating
schedules, and a generic scheduled-command surface are deliberately out of
scope, as are non-interrupting reminders, human-task assignment, and
escalation, which stay product concerns.

**A human answer voids its timer.** Firing goes through the same
compare-and-set on the occurrence that `answer()` does, so a human answer
and a timer racing the same pause resolve by **commit order** — no
preference rule:

| At fire time | Recorded outcome | Result |
|---|---|---|
| the occurrence is still open | `settled` | the scheduled value settles it |
| a human (or another writer) answered first | `already_settled` | the timer is voided; the earlier answer stands |
| a later pause occurrence is current | `superseded` | the timer is voided; it can never fire into a different question |
| the run is no longer paused | `rejected` | the timer is voided; the occurrence stays open |

A voided timer is **recorded, never deleted**: the command row keeps its
pause id, due time, value, and `source_ref`, and gains the outcome that
firing produced. The audit trail keeps the timer that lost, along with why.

The outcome is always the truth about **that** timer, because the
settlement attempt and the outcome it earns commit in **one transaction**,
and a worker claims the timer inside that transaction before settling. A
worker that crashes mid-fire leaves the pause open and the timer unapplied
— it fires cleanly next pass — and a second worker scanning the same due
row records nothing rather than relabelling the winner's row.

Firing is itself a durable Run fact, so a detached `watch(run_ref)` learns
the fate without any store query:

```python
async for update in client.watch(ref):
    if update.kind == "command" and update.payload["verb"] == "schedule_answer":
        update.payload["pause_id"]    # the occurrence it was armed against
        update.payload["due_at"]      # when it became applicable
        update.payload["source_ref"]  # who armed it
        update.payload["outcome"]     # None while armed; the outcome once fired
```

Arming and firing emit the same payload shape — `outcome` is `None` on the
arming fact and one of the four values above on the fact that settles or
voids it — so the stream alone accounts for every timer.

## Listing Runs

`client.list(query)` filters joined run views through a typed `RunQuery` —
submissions joined with their runs rows, plus bare Tier-0 runs (a runs row
with no submission). Results come newest first, capped at `query.limit`
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

`RunView.created_at` and `RunView.completed_at` expose the Run ledger's own
timestamps (`None` until the corresponding runs-row event exists). Read the
graph-boundary values pinned when a Run started without reaching into the
Run Home:

```python
inputs = await client.inputs(run_view.run_ref)
```

`client.inputs_sync(...)` is the synchronous mirror. It returns `{}` for an
unknown Run or a legacy runs row that predates durable input persistence.

## Rerun: Repeat Settled Work

`client.rerun(ref)` repeats terminal work under a new id with retry
lineage — it lives on the client because it needs **no loaded Definition
code**: the new submission carries the source's pinned `DefinitionId` and
inputs verbatim.

```python
rerun_receipt = await client.rerun(receipt.run_ref)
rerun_receipt.workflow_id      # "refund-c-42-retry-1"
```

The id is `<source>-retry-N`, where N is the next ordinal among the reruns
of that source **already accepted**. The ordinal is allocated inside the
acceptance transaction and stored on the submission, so two reruns requested
before either executes get two ids (`-retry-1`, `-retry-2`) instead of
colliding, and concurrent callers each get their own.

The worker executes the rerun with `retry_from` lineage: the new runs row
records `retry_of` and the **same** `retry_index` the id was minted with —
whatever order the reruns execute in — and completed-step checkpoints from
the source are reused. There is deliberately **no input override** — the
signature is `rerun(ref)` and passing `inputs=` is a `TypeError`; changed
inputs use a normal new `submit()`. The source must exist and be terminal,
else `RerunError` — with one exception: a **recovery-exhausted** source is
exactly the rerun case (reviving braked work), so `rerun()` accepts it even
without a terminal runs row. `rerun()` also takes the same optional opaque
`source_ref` `submit()` and `stop()` take, recorded on the new submission —
retry lineage says what was repeated, `source_ref` says who asked.
`client.rerun_sync(...)` is the synchronous mirror.

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
  stays settled and queryable forever. The Batch's `-retry-N` ordinal is
  allocated inside the acceptance transaction, exactly like a Run rerun's,
  so concurrent rerun callers never collide on one manifest.
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
fork_receipt = await host.fork(run_ref, into=refund,
                               reason="2026.07.3 schema migration, approved by ops")
fork_receipt.workflow_id       # "refund-c-42-fork-a1b2c3"
```

Compatibility is checked **at fork time**: the target Definition's
`structural_hash` must equal the source submission's pinned hash (history
seeding requires restorable checkpoints), else `ForkCompatibilityError`
naming both identities. `reason` must be a non-empty string, and `into` is a
served `Graph` object like every other new-work verb — an unserved one is
refused at the call site with `UnservedGraphError`. `fork()` takes the same
optional opaque `source_ref` the other accepting surfaces take, recorded on
the new submission — `reason` says why the migration happened, `source_ref`
says which authenticated action asked for it. `host.fork_sync(...)` is
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

For differently sized Runs, `max_admission_units` adds a durable weighted
budget while `max_active_runs` remains an independent safety cap:

```python
home = RunHome.open(
    "file:./runs.db",
    max_active_runs=128,
    max_admission_units=64,
)
```

The oldest eligible submissions reserve their persisted costs. Normal work
fits within the budget, except that at least two non-oversized Runs may be
admitted to keep the worker useful. An item whose cost exceeds the budget is
never rejected: it waits until it can run alone. Pausing releases the
reservation; answering returns the Run to ordinary FIFO admission. Because
usage is derived from durable `claimed` submissions, another process and a
restarted worker reconstruct the same reservations without a process-local
ledger. Set `max_admission_units=None` for unlimited weighted admission.

For a memory ceiling, configure this single page-denominated budget as the
minimum of the utilization-derived page target and the page count that fits
the available-memory estimate. Input JSON or source-file bytes are not used
as a proxy for resident parsed-page memory.

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
receipt = await host.submit(refund, {"claim_id": "c-42"},
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
| `UnservedGraphError` | `submit`, `submit_batch`, or `fork(into=…)` names a `Graph` this host does not serve, or one whose `structural_hash` drifted from the served Definition |
| `ItemKeyError` | `submit_batch` `identity` names a field outside `map_over`, or an item's key is missing, empty, non-scalar, or duplicated |
| `AlreadyTerminalError` | a terminal `workflow_id` is reused for submit, submit_batch, or stop (including a fully settled Batch) |
| `WorkflowIdConflictError` | a nonterminal `workflow_id` is reused with a different start fingerprint (Run or Batch), or a Batch id collides with existing work |
| `ForkCompatibilityError` | `host.fork()` targets a structurally incompatible Definition |
| `RerunError` | `client.rerun()` names a missing or nonterminal source (recovery-exhausted sources are allowed) |
| `HostError` | base class for host-specific errors; also raised directly for an unknown stop target |
| `AnswerRejectedError` | `client.answer()` named no/unknown occurrence, a run that is not paused, or a value failing `answer_schema` |
| `PauseAlreadySettledError` | `client.answer()` re-answered a settled occurrence |
| `StalePauseError` | `client.answer()` named an occurrence a later pause superseded |

The three pause refusals share the base class `PauseSettlementError` and live
with the checkpointer that raises them
(`hypergraph.checkpointers`); they are re-exported from `hypergraph`.
`PauseSettlementError` is itself a `HostError` (and still a `RuntimeError`),
so the single `except HostError` in the table above covers every row.
