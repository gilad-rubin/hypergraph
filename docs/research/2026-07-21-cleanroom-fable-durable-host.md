# Clean-room design — Fable 5 (fresh subagent, Stage A brief only, 2026-07-21)

# Design: the layer between the engine, its strategies, and its journal

The engine already knows how to run, pause, resume, fork, journal, and stop a workflow **inside one process**. Every need in the brief is some version of the same gap: *the intent to run ("start", "here's the answer", "stop") lives in HTTP handlers and process memory, while the ability to run lives in the engine.* The missing layer is the durable home for intent and authority. It is small: three records and one host object. Everything else is either the engine (given) or an existing software category.

---

## 1. Name the atoms

Three durable records, one deliberately *non*-durable name.

### Atom 1 — **Command**
The durable form of "someone wants something to happen to a workflow." Intake writes it; nothing executes it in the HTTP request.

- **Contents:** `command_id` (caller-chosen idempotency key — primary key), `workflow_id`, `kind` (`start | resume | stop`), payload (`graph` name + `inputs` for start; answer `values` for resume; nothing for stop), a store-assigned per-workflow sequence number `seq`, `state` (`pending | applied | rejected(reason)`), timestamps.
- **Comes to exist:** the instant intake accepts a request. The insert *is* the deduplication: a unique constraint on `command_id` means the second webhook delivery doesn't insert — it reads the existing row and returns the same receipt (**N1**). A `start` whose `workflow_id` already exists is applied as a no-op receipt pointing at the existing run, so N1 holds even when the retry invents a fresh `command_id`.
- **Referred to by:** the HTTP response (a receipt), the run head (`last_applied_seq`), and the journal entry each resumption acted on (audit trail: "this run continued because of command X").

Stop is a command like any other. That's what makes **N8** definable: all commands for one workflow serialize through `seq` in one store, so "stop racing continue" reduces to commit order — a pure function of the log, never of two threads' luck.

### Atom 2 — **Lease**
Write authority over one workflow. Not a liveness detector — an authority revoker.

- **Contents:** `workflow_id` (primary key), `holder` (worker name, for humans), `epoch` (a monotonic counter that never repeats and survives holder changes — the fencing token), `expires_at`.
- **Comes to exist:** when a host claims a runnable workflow. Claiming is one transaction: `epoch := epoch + 1`, set holder and expiry. Heartbeats extend `expires_at`; expiry makes the workflow claimable, it does **not** prove the old holder is dead.
- **Referred to by:** **every journal write.** Each write is stamped with the writer's epoch, and the store rejects any write whose epoch is below the current one — inside the same transaction that would have appended it. A partitioned worker that wakes up mid-write doesn't get "discouraged"; its write is *impossible*, because its authority was already reissued to someone else (**N3, N4**). Remote death stays unprovable; we never try to prove it.
- **Crucially absent while parked:** a workflow paused on Dana holds no lease, no process, no memory — it is rows at rest (**N5**).

### Atom 3 — **Run head**
The queryable one-row summary of a workflow. The journal is the history; the head is the current truth the scheduler and the revision guard read without replaying anything.

- **Contents:** `workflow_id` (primary key), `graph` name, `revision` (the code-compatibility tag recorded when the run started), `phase` (`runnable | running | parked | stopped | done | failed | stranded`), `last_applied_seq`, pointer to its journal.
- **Comes to exist:** when the first `start` command is applied.
- **Referred to by:** the recovery sweep ("what is runnable or has an expired lease?" — **N6**), subsequent commands (a `resume` flips `parked → runnable`; a `resume` arriving after `stopped` is rejected as `superseded`), the revision guard (**N10**), and watchers.

### Deliberate non-atoms

- **Effect key (N7):** a deterministic *name*, not a record — `"{workflow_id}/{node_path}#{invocation_ordinal}"`, derived from the attempt reservation the journal already performs before a node runs. It is injected into node code and is stable across crash, re-adoption on another worker, and retry of the *same logical invocation*. It is handed to the payment provider as an idempotency key. The durable dedupe ledger for the charge lives at the payment provider — where it must live, because only the party performing the side effect can deduplicate it. We advertise at-least-once plus a stable identity; never exactly-once.
- **Events:** no new record. The journal already records history as the run progresses; `watch()` is replay-of-journal plus live tail. For low latency, the host passes a relay processor at `run()` time (a given primitive) that forwards typed events onto the Home's ephemeral wakeup channel. Nothing new is persisted.

That's it. Three tables next to the journal, in the same transactional store — which is what makes the fencing check and the append atomic.

---

## 2. Show the code

Two constructs wrap the atoms: `Home` (where the atoms and the journal live — one location string, chosen at construction time) and `Host` (the loop that turns applied commands into `strategy.run()` calls). No user-facing name mentions a storage technology (**N11**).

```python
# ────────────────────────────────────────────────────────────────────
# app.py — IDENTICAL on Tom's laptop and Ari's two VMs. Only the
# location string differs, and it comes from the environment.
# ────────────────────────────────────────────────────────────────────
import os
from engine import AsyncStrategy, SyncStrategy      # given primitives
from runhost import Home, Host, Command, watch      # the missing layer

HOME = Home.open(os.environ.get("RUN_HOME", "file:./runs.db"))
# Tom's laptop / notebook:  RUN_HOME unset  → one file, zero infrastructure
# Ari's VMs:                RUN_HOME points at the team's existing
#                           relational database server (a URL in config)
# Same code paths in both: commands dedupe the same way, leases fence the
# same way. Local mode is a faithful miniature, not a separate mode.

host = Host(HOME, worker=os.environ.get("WORKER_NAME", "local"))

# ── N2: several graphs, each with its own strategy, wired once ──────
host.serve(
    name="refund",
    graph=refund_graph,          # validate → assess → maybe_ask_dana → issue → notify
    strategy=lambda journal: AsyncStrategy(journal=journal),
    revision="refund/7",         # N10: bumped only when run-history shape changes
)
host.serve(
    name="triage",
    graph=triage_graph,          # fetch → parse → extract → summarize → index
    strategy=lambda journal: SyncStrategy(journal=journal),
    revision="triage/3",
)
```

**The money node (N7)** — engine-level at-least-once, application-level idempotency via the stable identity:

```python
async def issue_refund(claim: Claim, ctx: NodeContext) -> Receipt:
    # If vm-1 crashes after the HTTP call but before the journal records
    # completion, vm-2 re-executes this node with the SAME effect_key,
    # and the payment provider deduplicates. That is the whole contract.
    return await payments.charge(
        amount=claim.amount,
        idempotency_key=ctx.effect_key,   # "refund-c-42/issue_refund#1"
    )
```

**Intake from a retrying webhook (N1)** — the HTTP handler writes intent; it never runs the graph:

```python
@app.post("/refunds/{claim_id}")
async def refund_requested(claim_id: str, request: Request):
    receipt = await host.submit(Command.start(
        command_id=request.headers["Idempotency-Key"],  # or f"start:{claim_id}"
        graph="refund",
        workflow_id=f"refund-{claim_id}",
        inputs={"claim_id": claim_id},
    ))
    return {"workflow_id": receipt.workflow_id, "duplicate": not receipt.new}
    # Delivery #2 of the same webhook: unique constraint on command_id
    # short-circuits — same receipt, zero new work.
```

**The worker loop (N3, N4, N5, N6)** — one systemd unit per VM; this *is* the deployment story:

```python
# worker entrypoint, supervised by systemd on each VM
await host.run_forever()
```

What that loop does with the given primitives, sketched:

```python
# inside Host — the essential mechanics, not user-facing
async def _adopt_and_run(self, head):
    lease = await self.home.claim(head.workflow_id)         # epoch := epoch + 1
    served = self.graphs[head.graph]

    if head.revision not in served.accepted_revisions:      # N10
        await self.home.mark(head.workflow_id, "stranded", lease)
        return                                              # surfaced, never guessed at

    journal = self.home.journal_for(head.workflow_id, fenced_by=lease.epoch)
    #  ^ every append this journal makes carries lease.epoch; the store
    #    rejects it, transactionally, if a newer epoch exists.       (N4)

    strategy = served.strategy(journal)
    cmd = await self.home.next_applied_command(head.workflow_id)
    result = await strategy.run(                            # given primitive:
        served.graph,                                       # resumes from journal,
        cmd.values_or_inputs,                               # skips finished nodes
        workflow_id=head.workflow_id,
        processors=[self.home.event_relay(head.workflow_id)],   # N9 live tail
    )

    match result.status:
        case "paused":    await self.home.park(head, lease)      # N5: release lease,
                                                                 # run costs nothing
        case "completed": await self.home.finish(head, lease)
        case "stopped":   await self.home.mark(head, "stopped", lease)
        case _:           await self.home.mark(head, result.status, lease)
```

The sweep in `run_forever()` looks for exactly three things: run heads that are `runnable` (fresh starts, and parked runs a `resume` command just woke), heads that are `running` with an **expired lease** (a worker crashed or was partitioned — claim with a higher epoch and re-invoke `run()`; the journal makes resumption safe, the epoch makes the old worker's late writes impossible), and pending `stop` commands for runs it holds (call the given `strategy.stop()`). Deploys are just crashes with better manners: systemd stops the worker mid-run, the new code starts, the sweep re-adopts — **nobody re-submits anything (N6)**.

**Dana answers, six days and two deploys later (N5):**

```python
@app.post("/refunds/{claim_id}/answer")
async def dana_answered(claim_id: str, body: Answer):
    await host.submit(Command.resume(
        command_id=body.idempotency_key,          # her dashboard retries too — N1 again
        workflow_id=f"refund-{claim_id}",
        values={"dana_approval": body.approved},
    ))
    # Applying this flips the run head parked → runnable and pings the Home's
    # wakeup channel. Whichever worker claims the lease re-invokes run() with
    # her answer and the same workflow_id — the given continuation primitive.
```

**Stop from a process that isn't running it (N8):**

```python
# ops shell on vm-2; the run is currently mid-node on vm-1
await host.submit(Command.stop(
    command_id="stop-refund-c-42-2026-07-21T14:02",
    workflow_id="refund-c-42",
))
```

The defined race: stop and resume land on the same per-workflow command sequence. **If the stop commits first**, the run head becomes `stopping`; vm-1's host sees it (wakeup channel, or at the next fenced journal write) and calls the given cooperative `strategy.stop()`; the resume, applied afterward, is rejected with `superseded("stopped")` — Dana's dashboard gets a truthful answer, not silence. **If the resume commits first**, the run continues and the stop takes effect at the next node boundary. Either way the outcome is read off the command log, not off the scheduler's mood. A stop on a *parked* run needs no compute at all: the head flips to `stopped`.

**Watching a run you didn't start — including one that migrated workers after a crash (N9):**

```python
# monitor.py — any process attached to the same Home; it started nothing
async for event in watch(HOME, "refund-c-42", from_start=True):
    render(event)    # full journal history replayed as typed events,
                     # then the live tail via the event relay
```

**Tom's nightly run (N5, N6, N11) — same layer, zero infrastructure:**

```python
# nightly.py on the lab box, launched by cron
host = Host(Home.open("file:/data/triage.db"), worker="labbox")
host.serve(name="triage", graph=triage_graph,
           strategy=lambda j: SyncStrategy(journal=j), revision="triage/3")

for doc in corpus:                                   # ~30,000 documents
    await host.submit(Command.start(
        command_id=f"triage:{doc.id}:2026-07-20",
        graph="triage",
        workflow_id=f"triage-{doc.id}-2026-07-20",
        inputs={"doc_id": doc.id},
    ))
await host.drain()
# 03:00 patch reboot kills the box mid-corpus. Cron reruns the same script:
# every command dedupes, every finished document's head reads `done` and is
# skipped, the half-done one resumes from its journal. Tom notices nothing,
# which is the requirement.
```

(In the notebook, Tom can still call `AsyncStrategy(...).run(...)` directly while sketching a graph — the layer is additive, not a toll booth.)

**Deploy with incompatible code (N10):**

```python
# Deploy 8 reshapes refund history. Declare what the new code can adopt:
host.serve(name="refund", graph=refund_graph_v8,
           strategy=lambda j: AsyncStrategy(journal=j),
           revision="refund/8",
           accepts=("refund/7",))     # omit ⇒ old parked runs go `stranded`,
                                      # loudly, instead of being corrupted

# Operator migration path for a stranded run uses a GIVEN primitive:
await host.submit(Command.start(
    command_id="migrate-refund-c-42",
    graph="refund", workflow_id="refund-c-42-v8",
    fork_from="refund-c-42",          # new workflow seeded from old history
))
```

---

## 3. Draw the boundary

Things I deliberately did **not** build, because an existing category already owns them — with the temptations named:

- **Durable shared storage, transactions, unique constraints, ordering, wakeups → the relational database Ari already operates.** The Home's shared flavor is a schema plus a handful of transactional statements. The biggest temptation in the whole design was to build a networked *journal service* with its own protocol, clients, and availability story. Refused: the journal was designed pluggable; a backend for the existing store is an implementation of a given extension point, not a new system. The single database is also, for free, the serialization point that makes commands totally ordered and fencing atomic.
- **A message broker.** N1's dedupe and N5's wakeups whisper "queue," and this is where I was most tempted. But two VMs and thirty thousand documents need a table with a unique constraint and a notify/poll channel, not a second stateful cluster with its own delivery semantics to reconcile against the journal's.
- **A coordination service (locks/consensus à la ZooKeeper/etcd).** Fencing tokens ride the same transactional store as the journal, which is precisely why a stale writer can be rejected *in the write itself*. A separate coordination tier would reintroduce the gap between "who holds the lock" and "who can write" that N4 forbids.
- **HTTP intake, auth, routing → the web framework and load balancer.** The layer exposes `submit()` and `watch()` as library calls; it grows no ports of its own.
- **Keeping processes alive and rolling deploys → systemd and Ari's existing routine.** The host owns re-adoption of *runs*, never restart of *processes*.
- **Timers and schedules → cron.** The nightly trigger is cron's job; lease expiry is an internal detail, not a user-facing scheduler.
- **Deterministic replay and workflow-versioning machinery.** The engine is explicitly state-based; I did not smuggle in a replay contract to "solve" N10. A revision tag, a refusal, and the given `fork_from` are the whole story — old runs are never silently reinterpreted by new code.
- **Exactly-once side effects.** Refused as a claim anywhere in the API. The payment provider's idempotency-key support — a mature, existing category — owns final deduplication; we contribute only the stable effect key.
- **Dashboards/UI.** `watch()` emits the given typed events; any existing tool renders them.

---

## 4. One sentence

The missing software is a thin **host runtime** — a durable command inbox, an epoch-fenced single-writer lease enforced inside every journal write, and a run head carrying phase and code-revision — that turns the engine's in-process `run()` into something startable, continuable, stoppable, watchable, and crash-adoptable from anywhere, while storage, transport, supervision, scheduling, and final side-effect deduplication remain the property of software that already exists.
