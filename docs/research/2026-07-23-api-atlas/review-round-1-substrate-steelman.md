# Verdict

Do not put a general event, message, or stream substrate under Hypergraph.

The owner’s instinct is half right: Hypergraph and Panda need shared execution-control primitives. But the useful shared layer is a small run-control kernel—durable commands, keyed ownership, restart reconciliation, batches, and scoped admission—not an event bus that replaces the checkpointer.

Recommended shape:

```text
                    optional
             durable output stream
              (tokens, logs, fanout)
                       ▲
                       │
Host ── commands / claims / batches / admission / reconciliation
                       │
Runner ── Hypergraph scheduling, nodes, pause, resume, results
                       │
Run Home ── StepRecords, attempts, lineage, coordination rows
```

S2 could later back durable output streams or high-volume observation. Rivet provides a useful actor model. Neither should become Hypergraph’s foundation now.

The committed design survives the steelman, but it needs three changes before acceptance:

1. Add durable batch semantics.
2. Add explicitly scoped admission to Tier 1.
3. Stop claiming PRD 0010 blocks all Host work; it blocks `answer()` and scheduled answers, not crash recovery, submission, stopping, batches, or watching.

## 1. The strongest possible substrate design

The serious version is not “put messages in a queue.” It is an actor runtime built over a durable ordered log.

Its primitives would be:

```python
class ExecutionSubstrate(Protocol):
    async def append(
        self,
        key: str,
        records: Sequence[Record],
        *,
        expected_position: int | None = None,
        fence: str | None = None,
    ) -> Position: ...

    def follow(
        self,
        key: str,
        *,
        after: Position,
    ) -> AsyncIterator[Record]: ...

    async def claim(
        self,
        key: str,
        *,
        owner: str,
        ttl: timedelta,
    ) -> Lease: ...

    async def schedule(
        self,
        key: str,
        command: Command,
        *,
        due_at: datetime,
    ) -> None: ...

    async def acquire(
        self,
        resource: str,
        *,
        cost: int = 1,
    ) -> Permit: ...
```

One stream or actor would exist per `workflow_id`:

```text
Start
Claimed(epoch=4)
AttemptStarted(node="extract")
StepCommitted(node="extract", values=...)
Paused(pause_id=...)
AnswerAccepted(pause_id=...)
Claimed(epoch=5)
StepCommitted(...)
Completed
```

A snapshot would materialize current graph state at a known stream position. Recovery would load the snapshot, fold later records, claim the actor, and continue. `watch()` would follow from a cursor. A batch actor would own a manifest of item actor IDs. Admission would decide when actors or node calls may proceed.

That design genuinely buys:

- Durable cursor-based observation.
- Natural replay of token and log streams.
- Multi-language command producers and observers.
- Potential multi-language node workers if Hypergraph introduced a typed remote-node wire protocol.
- Actor-per-workflow serialization.
- Durable mailboxes and timers.
- Shared infrastructure for Panda, contentbase, and other execution hosts.
- Clean burst absorption and live fanout.

S2 makes this more plausible than a generic Kafka argument. It provides atomic append batches, sequence-number conditions, fencing tokens, replay from a sequence number, and live following. It also documents snapshot-and-follow as the way to bound replay cost. [S2 concurrency control](https://s2.dev/docs/concepts/concurrency-control), [S2 snapshots](https://s2.dev/docs/concepts/snapshots), [S2 reading](https://s2.dev/docs/sdk/reading).

Rivet supplies the actor-shaped upper half: durable per-actor queues, persistent timers, hibernation, state, and realtime events. [Rivet queues](https://rivet.dev/docs/actors/queues/), [Rivet scheduling](https://rivet.dev/docs/actors/schedule/).

That is the steelman. It is a sound architecture—for a team choosing to build or adopt an actor platform.

## 2. Why it is still the wrong foundation here

S2 is a stream store, not a complete work runtime.

Its documented core operations are append, read, and check-tail. Even its ownership guidance says the application may model a lease by appending heartbeats. Fencing is cooperative: an append that omits the token is still allowed. It does not supply runnable-key scans, consumer acknowledgements, timed activation, attempt policy, batch aggregation, or workflow-version routing. [S2 concurrency control](https://s2.dev/docs/concepts/concurrency-control).

To make S2 the Hypergraph journal, Hypergraph would have to build:

- Lease acquisition and expiry.
- Heartbeat processing.
- Runnable-work indexing.
- Timers.
- Consumer positions and redelivery rules.
- State reducers and snapshots.
- Batch projections.
- Query and inspection indexes.
- Cross-stream race rules.
- Schema evolution.
- Large/arbitrary Python-value storage.
- Transaction rules between commands and execution history.

The self-hosted option does not remove that work. `s2-lite` is explicitly a single-node server backed by SlateDB/object storage. That is useful for a firewalled host and output streaming, but it is not fleet supervision. [s2-lite documentation](https://s2.dev/docs/s2-lite), [s2-lite source and architecture](https://github.com/s2-streamstore/s2).

A command-only S2 plane is worse. Hypergraph would then have two authorities:

```text
S2: Answer accepted
SQLite/Postgres: pause settlement not committed
```

or:

```text
Postgres: run completed
S2: concurrent Stop accepted
```

Without one transaction, stop-versus-completion and answer-versus-pause races lose the clear commit-order rule already required by [ADR 0005](../../adr/0005-durable-hosts-accept-commands-runners-retain-execution.md). An outbox repairs this, but the outbox row becomes the real durable command log again.

Rivet comes closer to the full runtime, but it also confirms the cost:

- Actions run in parallel unless the application routes mutations through durable queues.
- Durable workflows use replay.
- The actor server surface is TypeScript/JavaScript, with Rust still newer; Python is an experimental client rather than a Python actor runtime.
- Putting a Python Hypergraph runner behind a Rivet actor creates a remote two-system commit boundary around the existing checkpointer.

See [Rivet action concurrency](https://rivet.dev/docs/actors/actions/), [Rivet workflows](https://rivet.dev/docs/actors/workflows/), and the [Rivet repository’s language/runtime matrix](https://github.com/rivet-dev/rivet).

Most importantly, a substrate does not delete Hypergraph’s distinctive code:

- The superstep scheduler stays.
- Typed graph validation stays.
- State restoration and staleness stay.
- StepRecords and attempt records stay—or must be rebuilt as reducers.
- Fork and lineage stay.
- Tier 0 handles and stop signals stay.
- Python node execution stays.

It replaces a modest Host implementation with a general actor and stream runtime.

## 3. Decision-by-decision verdict

| Draft decision | Verdict | Why |
|---|---|---|
| Host owns durable intent; runner owns execution | **Keep** | Panda’s orphaned work is direct evidence for a Host reconciler, not for replacing the runner. This is exactly [ADR 0005’s boundary](../../adr/0005-durable-hosts-accept-commands-runners-retain-execution.md). |
| Run Home combines checkpointer and coordination | **Keep** | StepRecords already hold state and inspection truth; the ABC says state folds from steps ([base.py](../../../src/hypergraph/checkpointers/base.py)). Same-store commands preserve atomic races. |
| Lease with epoch, fenced inside every mutation | **Keep unchanged** | S2’s own fencing feature validates the atom, but S2 still leaves lease policy to the app. [ADR 0006](../../adr/0006-shared-execution-authority-uses-a-lease-with-an-epoch.md) remains the stronger complete contract. |
| State-based recovery, not scheduler replay | **Keep firmly** | Reissuing a run loads the checkpoint ([template_async.py](../../../src/hypergraph/runners/_shared/template_async.py)); readiness skips non-stale completed nodes ([readiness.py](../../../src/hypergraph/runners/_shared/readiness.py)). A stream would need snapshots to regain this same property. |
| Version pinning plus explicit fork | **Keep** | Neither queues nor actors decide whether new Python code may resume old graph state. |
| Effect identity, never exactly-once execution | **Keep** | Conditional stream appends cannot deduplicate a payment, model call, or object-store mutation. |
| “No message broker” | **Amend, not reverse** | Correct wording: “No broker or stream is required for correctness or owns workflow truth. A notification, fanout, or output-stream adapter may be added.” Claim scans must remain the recovery path. |
| D2: no durable event record | **Keep for lifecycle events; clarify durable outputs** | StepRecords plus commands are enough for lifecycle replay. Durable token/output streaming is a separate need, not a reason to event-source execution. D2 already names streaming chunks as its escape condition ([consolidated design](../2026-07-21-consolidated-durable-host-design.md)). |
| Fleet-wide flow control deferred | **Reopen partly** | Panda has now supplied the missing evidence. Tier 1 needs host/process-scoped admission. Fleet-wide adaptive admission can remain deferred. |
| PRD 0010 gates the whole Host | **Reverse** | Durable pause truth gates `answer()` and scheduled answers. Panda proves that submit, batch, stop, watch, and reboot recovery have independent production value. |
| No process supervisor | **Keep, but fix the language** | systemd/Kubernetes restarts processes. The Host must still reconcile and re-dispatch unfinished runs. Call that the Host’s reconciler, not a process supervisor. |

S2 also changes how D2 should be explained. Today, `ctx.stream()` is explicitly a live side channel that does not affect the result ([node_context.py](../../../src/hypergraph/runners/_shared/node_context.py)), and the iterator drops chunks under pressure ([handles.py](../../../src/hypergraph/runners/_shared/handles.py)). Core canon says `EventProcessor` delivery is best effort ([CORE-BELIEFS.md](../../../dev/CORE-BELIEFS.md)).

Therefore a resumable token stream must not masquerade as an ordinary event processor. It needs a distinct `OutputLog` contract with durable append failure semantics. S2 is a strong candidate for that contract; its agent guide explicitly supports reconnecting token readers from the last sequence number. [S2 agent streams](https://s2.dev/docs/use-cases/agents).

## 4. The unified mental model

The smallest honest model has four public concepts:

1. **Definition** — graph, runner, and pinned version.
2. **Run** — one graph invocation and its state.
3. **Batch** — a named group of item Runs.
4. **Command** — submit, answer, or stop.

Results and `watch()` are two ways of observing Runs:

- Attached execution returns `RunResult` or `MapResult`.
- Detached durable execution returns a receipt, then `watch()` reads durable facts.

Admission and reconciliation are Host policy, not more user verbs.

| Existing surface | Unified meaning |
|---|---|
| `runner.run()` | Execute one attached Run and await its result. |
| `runner.start_run()` | Execute one attached Run; return a process-local live-control handle. |
| `runner.map()` | Execute one attached Batch and await its `MapResult`. |
| `runner.start_map()` | Execute one attached Batch; return a process-local handle. |
| `runner.iter()` | Attached live observation; chunks remain preview data. |
| `host.submit()` | Durably create one Run or Batch. |
| `host.answer()` | Append and atomically apply one pause-scoped command. |
| `host.stop()` | Commit durable stop intent. |
| `host.watch()` | Detached durable observation plus optional live preview. |
| Checkpointer / Run Home | Stores Run facts and coordination. Not a user action. |
| Reconciler | Finds accepted unfinished Runs and invokes the runner. Internal. |
| `hyperlimit` | Resource admission policy. Not workflow truth. |

The public API does not need new `submit_map`, `start_batch`, `enqueue`, `dispatch`, or `resume_job` verbs:

```python
# Tier 0 remains unchanged.
result = await runner.run(document_graph, document=document)

batch = await runner.map(
    document_graph,
    {"document": documents},
    map_over="document",
    max_concurrency=8,
)

handle = runner.start_run(document_graph, document=document)
```

Durable cardinality becomes an option on the same `submit` verb:

```python
ingest = document_graph.with_runner(AsyncRunner())

host = serve(
    ingest,
    home=RunHome.open("file:./runs.db"),
)

# One durable Run.
run = await host.submit(
    "document_graph",
    {"document": document},
    workflow_id="document:749",
)

# One durable Batch containing item Runs.
batch = await host.submit(
    "document_graph",
    {"document": documents},
    map_over="document",
    workflow_id="reingest:2026-07-23",
)

async for update in host.watch(batch.workflow_id):
    ...
```

I would keep `host.run()` excluded. It cannot truthfully recreate local-only `RunResult` evidence from a detached worker, as [ADR 0005 records](../../adr/0005-durable-hosts-accept-commands-runners-retain-execution.md). `submit()` plus `watch()` is not verb sprawl; it expresses a real attached-versus-detached boundary.

## 5. Durable map and batch semantics

Use a first-class Batch as a manifest and control group whose items are independent Runs.

Do not model ordinary document ingestion as one giant driving graph. Use a driving graph only when the items have real graph semantics—ordering, reduction, shared decisions, or cross-item dependencies.

The current implementation already contains most of the right local shape:

- `runner.map()` creates a parent batch Run.
- Children use IDs such as `workflow_id/0`.
- Completed children resume by a stable input signature.
- Completed item state is restored instead of re-executed.

See [template_async.py](../../../src/hypergraph/runners/_shared/template_async.py) and [map_resume.py](../../../src/hypergraph/runners/_shared/map_resume.py).

The durable definition should tighten it:

```text
Batch
  workflow_id: reingest:2026-07-23
  requested_count: 749
  item manifest:
    - item identity
    - input/signature
    - child workflow_id
  stop intent
  derived summary

Child Run × 749
  independently checkpointed
  independently claimable
  independently recoverable
```

Tier 1 can execute those children through the existing `runner.map()` machinery on one worker.

Tier 2 should let workers claim child Runs independently. The Batch never needs one worker to own all 749 items. Its terminal projection derives from the manifest and child Runs; it must not become a second execution journal.

### Admission has two distinct scopes

Panda revealed two problems that must not be collapsed into one “concurrency” option:

- **Work admission:** how many Runs or batch items the Host starts. The Host owns this.
- **Resource admission:** how many calls may reach a specific provider or deployment. The provider component owns this, with `hyperlimit` as one implementation.

Current `max_concurrency` is scoped to one top-level run/map context and inherited by nested graphs ([runner.py](../../../src/hypergraph/runners/async_/runner.py)). Ten independent top-level batches therefore get ten independent budgets—the exact Panda failure mode.

For Tier 1:

```python
provider_limit = AdaptiveLimiter(initial=3, floor=2, cap=12)

# One instance constructed at the app/Host root and shared by every batch.
provider = DocumentProvider(limiter=provider_limit)

host = serve(
    document_graph.with_runner(AsyncRunner()),
    home=RunHome.open("file:./runs.db"),
    max_active_runs=12,  # Host work admission
)
```

Do not force `hyperlimit` into Hypergraph’s node protocol yet. One production use proves the scope problem, but not a stable generic admission API. Construct one limiter at the Host composition root and inject it into the provider. Extract a shared `AdmissionController` only after a second distinct integration proves the protocol.

For Tier 2, document one of these explicitly:

- Per-worker adaptive limits, allowed to multiply.
- Statically partitioned deployment quota.
- A real distributed admission backend.

Do not call a process-local AIMD limiter “global” in a multi-worker deployment.

## 6. Panda’s 749-document run end to end

1. Panda calls one `host.submit(..., map_over="document")` with a stable batch `workflow_id`.
2. The Run Home atomically stores the batch manifest, 749 child identities, and accepted command.
3. The Host admits only a bounded number of child Runs.
4. Every provider call shares the same process-scoped `AdaptiveLimiter`.
5. The machine crashes.
6. systemd restarts the process. The Host reconciler scans unfinished children and reissues the same workflow IDs.
7. Completed StepRecords restore state and staleness rules skip finished work. Started-but-unsettled managed attempts become unknown, never invented failures.
8. Safe idempotent work resumes. Unsafe or unknown effects surface as needs-review.
9. An operator may start an explicit fork through the existing `submit(..., fork_from=...)` family after review.
10. Batch status derives from its real child outcomes; `watch()` does not depend on a fragile polling feeder.

Panda can then delete:

- Request-scoped `BackgroundTasks` for long work.
- The long-running feeder and its retry/poll loop.
- Local in-flight capacity bookkeeping.
- Duplicate-resume protection.
- Startup orphan classification from ticket 0025.
- Any second execution-status ledger.
- Per-request batch concurrency gates.

Panda ticket 0028 (`docs/issues/0028-adopt-hypergraph-durable-host.md` in the sibling Panda repository) already names most of this deletion boundary.

Panda keeps:

- Domain-facing ingestion state and operator wording.
- Provider configuration.
- `hyperlimit` or an equivalent provider gate.
- Business rules deciding which effects are safe to repeat.

Hypergraph does not get to delete its runner, scheduler, checkpointer, handles, or attempts. The Host reuses them. That is less dramatic than a universal substrate, but it deletes the right code from every adopting product.

## 7. Tier 0 should share the engine, not the command loop

Keep Tier 0 as a separate zero-ceremony control path.

Share:

- Graph validation.
- Scheduler and supersteps.
- Node execution.
- Checkpoint restoration.
- Attempt coordination.
- Results and events.
- Map planning and child identity.

Do not share:

- Durable command intake.
- Receipts.
- Worker claims.
- Lease renewal.
- Reconciliation scans.
- Detached observation.

A universal command loop would make notebooks emulate a deployment system and weaken ADR 0004’s clean distinction between live handles and durable identity. Today `start_run()` already delegates to `run()` while retaining only a local task and stop signal ([async runner](../../../src/hypergraph/runners/async_/runner.py)); that is the right amount of internal sharing.

## 8. Shortest path

I would change the current order.

1. **Owner accepts ADRs 0005–0008 with amendments.**
   Keep their core. Add the broker/output distinction, Batch, admission scope, and the narrower PRD 0010 dependency.

2. **Resolve ADR 0007’s exact version identity.**
   My lean remains an explicit deployment version plus structural hash, with explicit compatibility declarations.

3. **Revise PRD 0011 to ship the Panda wedge first.**
   Local SQLite Host with `submit`, `stop`, `watch`, restart reconciliation, durable Batch, and host-scoped admission. `answer()` may initially raise a clear unsupported error until PRD 0010 lands.

4. **Run the Panda kill proof.**
   Kill mid-document, mid-batch, and while the limiter is saturated; restart without resubmission; prove no duplicate active child and truthful unknown outcomes.

5. **Land PRD 0010 and ADR 0008’s scheduled answers.**
   These add human and timed continuation without blocking the first durable production use.

6. **Land pending-node writes and wider attempt coverage before broad safety claims.**
   The attempt ledger currently covers only functions with retry or timeout configured ([attempts.py](../../../src/hypergraph/runners/_shared/attempts.py)). That is insufficient for a general “safe automatic recovery” promise. Node-boundary persistence and stable effect identity gate Tier 2 and unsafe Panda effects.

7. **Build Postgres leases only after Tier 1 works in Panda.**

8. **Prototype S2 only for an actual durable-output use case.**
   Example: reconnectable LLM token output or high-fanout run logs. Do not put it in the control path during Host v1.

### Decisions only the owner should make

- **Should Panda’s durable batch/restart need move ahead of durable pause slots?**

  Lean: yes. PRD 0010 gates only `answer()` and timers.

- **Is a Batch a manifest of independent child Runs?**

  Lean: yes. It preserves map semantics and enables future fleet distribution.

- **Is resumable token output a near-term product requirement?**

  If yes, spec a separate `OutputLog`; if no, keep S2 out entirely.

- **What admission guarantee will Tier 2 claim?**

  Lean: defer fleet-wide adaptive limits until there is a real multi-worker deployment.

### Explicitly do not build

- A universal `Message` or `Event` base class.
- An event-sourced replacement for the Checkpointer.
- Consumer groups, redelivery, or timers over S2.
- A Rivet bridge around Python execution.
- Polyglot node RPC.
- One command loop for notebooks and durable workers.
- A durable lifecycle-event journal beside StepRecords.
- Fleet-wide AIMD in Tier 1.
- Execution machinery in Panda or sp.

The owner’s “mess” comes from several axes being mixed—attached versus detached, one versus many, control versus truth, and work limits versus provider limits. Naming those axes and adding durable Batch plus scoped admission resolves the mess. A shared event substrate would hide the distinctions, then force Hypergraph to rebuild them inside the substrate.
