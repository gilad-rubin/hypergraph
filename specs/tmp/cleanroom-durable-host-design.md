## 1. Name the atoms

**Before:** `strategy.run()` combines submission and execution; process memory owns uniqueness, stopping, and observation.

**After:** callers submit durable commands to a small `WorkflowHost`; workers claim runnable workflows and invoke the existing strategy.

The design adds three durable record types and one construction-time object.

### A. Workflow control record

One per `workflow_id`.

Contents:

- `workflow_id`
- pinned `(graph_name, code_version)`
- lifecycle state: `ready`, `running`, `paused`, `stop_requested`, or an engine terminal status
- current pause identifier, if paused
- pending command sequence
- result or failure summary
- lease holder, lease deadline, and monotonically increasing fencing epoch
- state revision and timestamps

It comes into existence atomically with the first accepted `Start`. Commands, journal history, events, and workers refer to it by `workflow_id`.

The lease is not another record. Claiming a workflow updates these fields and returns:

```python
ExecutionGrant(
    workflow_id="refund-c-42",
    worker_id="vm-b:1842",
    epoch=7,
    expires_at=...,
)
```

Every control, journal, and event write must include this grant. The shared store accepts the write only if, in the same transaction:

```text
holder == grant.worker_id
epoch == grant.epoch
lease_until > authoritative_store_time
```

A takeover increments `epoch`. A partitioned worker holding epoch 6 cannot commit after epoch 7 exists, even if it wakes up and resumes an old database call.

There is an unavoidable qualification to N3: no distributed program can prove that an old machine stopped executing Python while also recovering from its presumed death. The enforceable guarantee is that only one worker has current authority and stale workers cannot commit authoritative state. Arbitrary external effects need N7’s idempotency identity.

### B. Command receipt

One per caller-supplied `request_id`.

Contents:

- request ID and command fingerprint
- workflow ID
- kind: `start`, `continue`, or `stop`
- durable input or answer
- expected pause ID, where relevant
- per-workflow command sequence
- accepted, rejected, or applied outcome

It comes into existence before acknowledging a request, in the same transaction as its control-state change. The caller’s receipt and the workflow’s pending-work pointer refer to it.

A unique constraint gives these rules:

- Reusing a request ID with identical contents returns the original receipt.
- Reusing it with different contents raises `RequestConflict`.
- A workflow can be started only once.
- A pause ID can be answered only once, even if two clients use different request IDs.
- Stop intent is monotonic and cannot be undone by a later continuation.

This is exactly-once command acceptance, not exactly-once node execution.

### C. Durable event entry

An append-only entry with:

- workflow ID and monotonic cursor
- event kind and typed payload
- pinned code version
- state revision
- journal-entry reference, when caused by engine progress
- authoritative timestamp

It comes into existence with each control or journal transition. `watch()` refers to entries by cursor, allowing replay followed by live tailing from any process.

Node events are appended by the fenced journal adapter in the same transaction as the corresponding journal mutation. Processor objects still receive live copies, but they are not the durable source.

This does not create another execution-history model: the given journal remains authoritative. The shared journal implementation adds fencing, durable event projection, and a stable logical-node-occurrence ID to its existing reservations.

### Construction-time object: workflow definition

This is not durable:

```python
WorkflowDefinition(
    name="refund",
    version="build:8b31c7f",
    graph=refund_graph,
    strategy=lambda journal: AsyncStrategy(journal=journal),
)
```

It binds an immutable code version to a graph and its per-graph strategy. A workflow control record pins its key. Workers claim work only when they have that exact definition registered.

## 2. Show the code

The graph-building syntax was not supplied, so `refund_graph_r17`, `refund_graph_r18`, and `paper_graph_r4` below are ordinary graph objects built from the given plain-function engine.

```python
from pathlib import Path
import os

from workflow_engine import AsyncStrategy, SyncStrategy
from durable_host import (
    CommandConflict,
    Continue,
    Coordination,
    Start,
    Stop,
    WorkflowDefinition,
    WorkflowHost,
    current_execution,
)


# ---------- Application code: unchanged between local and shared use ----------

async def issue_refund(claim, payments):
    # Stable for this logical node occurrence across physical re-executions.
    # The attempt number and worker identity are deliberately absent.
    payment_key = current_execution().effect_id("payment-provider-refund")

    return await payments.refund(
        claim_id=claim.id,
        amount=claim.amount,
        idempotency_key=payment_key,
    )


refund_graph_r17 = make_refund_graph(issue_refund=issue_refund)
refund_graph_r18 = make_new_refund_graph(issue_refund=issue_refund)
paper_graph_r4 = make_paper_triage_graph()


REFUND_R17 = WorkflowDefinition(
    name="refund",
    version="build:8b31c7f",       # immutable artifact identity
    graph=refund_graph_r17,
    strategy=lambda journal: AsyncStrategy(journal=journal),
    current=True,
)

REFUND_R18 = WorkflowDefinition(
    name="refund",
    version="build:f029a61",
    graph=refund_graph_r18,
    strategy=lambda journal: AsyncStrategy(journal=journal),
    current=True,
)

PAPERS_R4 = WorkflowDefinition(
    name="paper-triage",
    version="build:67ad219",
    graph=paper_graph_r4,
    strategy=lambda journal: SyncStrategy(journal=journal),
    current=True,
)


def make_host(coordination, definitions):
    return WorkflowHost(
        coordination=coordination,
        definitions=definitions,
    )


# ---------- Ari's deployment: many processes, one shared authority ----------

shared = Coordination.shared(
    address=os.environ["WORKFLOW_COORDINATION_ADDRESS"]
)

# Web, worker, operator, and observer processes construct the same interface.
webhook = make_host(shared, [REFUND_R17, PAPERS_R4])
vm_a = make_host(shared, [REFUND_R17, PAPERS_R4])
operator = make_host(shared, [REFUND_R17, PAPERS_R4])
observer = make_host(shared, [REFUND_R17, PAPERS_R4])


# 1. A retrying storefront starts the refund twice.

start = Start(
    request_id="storefront:event-9917",
    workflow_id="refund-c-42",
    graph="refund",                 # resolves to R17 now and pins it
    input={"claim_id": "c-42"},
)

first_receipt = await webhook.submit(start)
retry_receipt = await webhook.submit(start)

assert retry_receipt == first_receipt
assert first_receipt.command_sequence == 1


# 2. A worker claims it and invokes the supplied strategy.
# validate -> assess risk -> pause for Dana

result = await vm_a.work_once()
assert result.workflow_id == "refund-c-42"
assert result.status == "paused"

# work_once internally delegates execution; it does not execute graph nodes:
#
# journal = coordination.fenced_journal(control, execution_grant)
# strategy = definition.strategy(journal)
# result = await strategy.run(
#     definition.graph,
#     command.input,
#     workflow_id=control.workflow_id,
#     processors=caller_processors,
# )


# 3. A process that did not start the run reads its durable history.

dana_pause_id = None

async for event in observer.watch(
    workflow_id="refund-c-42",
    after=0,
    follow=False,
):
    print(event.cursor, event.kind, event.payload)

    if event.kind == "run.paused":
        dana_pause_id = event.payload["pause_id"]

assert dana_pause_id is not None

# The workflow now consists only of durable records. It owns no task,
# thread, worker, timer, or lease while Dana is away.


# ---------- Days pass; a new release is deployed ----------

# R18 becomes the default for new refunds. R17 remains registered because
# refund-c-42 is pinned to it.
deployed_web = make_host(shared, [REFUND_R17, REFUND_R18, PAPERS_R4])
vm_b = make_host(shared, [REFUND_R17, REFUND_R18, PAPERS_R4])


# 4. Dana's dashboard submits her answer twice.

answer = Continue(
    request_id="dashboard:approval-204",
    workflow_id="refund-c-42",
    pause_id=dana_pause_id,
    answer={"approved": True, "approved_by": "dana"},
)

answer_receipt = await deployed_web.submit(answer)
answer_retry = await deployed_web.submit(answer)

assert answer_retry == answer_receipt


# 5. Suppose VM A accepts the work, the payment provider completes the refund,
# and VM A loses power before journaling node completion.
#
# After VM A's lease expires, VM B claims the workflow with a higher fencing
# epoch. The host re-invokes the given strategy with the same workflow ID.

result = await vm_b.work_once()

# The strategy resumes from journal state. It may execute issue_refund again.
# current_execution().effect_id("payment-provider-refund") returns the same
# identity, so an idempotent payment provider returns the original operation.
assert result.workflow_id == "refund-c-42"
assert result.status == "completed"


# ---------- A run stopped from another process ----------

nightly = Start(
    request_id="scheduler:papers:2026-07-21",
    workflow_id="papers-2026-07-21",
    graph="paper-triage",
    input={"corpus": "main", "document_count": 30_000},
)

await deployed_web.submit(nightly)

# Assume VM B is now executing it. Ari sends this from an operator process.
stop_receipt = await operator.submit(
    Stop(
        request_id="ari:stop:papers-2026-07-21",
        workflow_id="papers-2026-07-21",
        reason="maintenance window",
    )
)

assert stop_receipt.accepted

# VM B's control watcher observes durable stop intent and calls the supplied:
#
# strategy.stop("papers-2026-07-21")
#
# No later node begins. A node already running may finish because stopping is
# cooperative. The final durable status becomes stopped unless completion
# committed before the stop command.


# ---------- Tom's notebook and reboot recovery ----------

# Same PAPER_R4 graph, functions, and strategy binding; only construction-time
# coordination changes. This needs one Python process and one local file.
lab = make_host(
    Coordination.local(Path("literature-workflows.state")),
    [PAPERS_R4],
)

await lab.submit(
    Start(
        request_id="tom:notebook:triage-check",
        workflow_id="triage-check-17",
        graph="paper-triage",
        input={"documents": ["paper-17.pdf"]},
    )
)

await lab.work_once()


# The nightly process uses the same host.
await lab.submit(
    Start(
        request_id="scheduler:papers:2026-07-22",
        workflow_id="papers-2026-07-22",
        graph="paper-triage",
        input={"corpus": "main", "document_count": 30_000},
    )
)

# If the machine reboots while lab.serve() is running, the operating system
# restarts this entry point. No start request is submitted again.
after_reboot = make_host(
    Coordination.local(Path("literature-workflows.state")),
    [PAPERS_R4],
)

await after_reboot.serve()
```

The durable watcher can later replay output such as:

```text
1 command.accepted   start
2 run.started        build:8b31c7f
3 node.completed     validate_claim
4 node.completed     assess_risk
5 run.paused         pause_id=pause:approve-refund:1
6 command.accepted   continue
7 run.resumed        worker=vm-a epoch=12
8 lease.expired      worker=vm-a epoch=12
9 run.resumed        worker=vm-b epoch=13
10 node.completed    issue_refund
11 node.completed    notify_customer
12 run.completed
```

The important race rules are:

- Commands receive a total per-workflow order through one transactional control record.
- If stop commits first, a concurrent continuation is rejected.
- If continuation commits first, a following stop changes the run to `stop_requested`; queued work is cancelled, while already-running code receives cooperative stop.
- If completion commits before stop, completion wins and the stop receipt says `already_terminal`.
- A stale worker’s journal write fails with `ExecutionGrantRevoked`; it cannot be accepted as a late event or node result.
- `effect_id()` does not promise once-only execution. For the refund guarantee, the payment provider—or an application-owned payment ledger—must atomically honor that identity. Without such cooperation, exactly-once money movement is impossible.

If a deployment omits `REFUND_R17`, the old workflow remains parked as `blocked_version`; R18 cannot claim it. Restoring a worker with R17 resumes it without altering its history.

## 3. Draw the boundary

The clean-slate constraint keeps the host narrow. I would not build:

- **Another graph engine.** Graph construction, node traversal, sync versus async execution, pause, resume, fork, result statuses, and node-attempt bookkeeping remain with the supplied engine and strategies.
- **A database or distributed lock system.** The existing relational database owns transactions, unique constraints, row serialization, authoritative time, and conditional writes. The host supplies schemas and adapters.
- **A separate queue or message broker.** Command rows plus claimable workflow controls already form a durable work queue. Notifications may reduce latency, but workers must always recover by scanning durable state.
- **A deterministic replay runtime.** Recovery remains the given state-based journal resume.
- **Exactly-once side-effect execution.** The host supplies a stable effect identity. The payment provider or application’s payment ledger owns atomic effect deduplication.
- **A deployment or artifact store.** Existing packaging and deployment tools retain immutable builds. The host pins versions and refuses to run an incompatible one.
- **A process supervisor.** The operating system starts `serve()` after reboot and restarts crashed worker processes.
- **HTTP endpoints, authentication, webhooks, or Dana’s inbox.** A web framework, identity system, and product application own those. They call `submit()` and `watch()`.
- **A monitoring warehouse or UI.** Durable events provide the source; existing logging and monitoring tools can consume them.
- **An external compute scheduler.** A future execution strategy may own translation to another compute engine, as the supplied strategy seam already anticipates.

The main temptations were to put graph semantics into the coordinator, treat a lease as proof that the old process died, add a broker beside the relational database, and call command deduplication “exactly-once workflow execution.” All four would weaken the guarantees or duplicate an existing category.

## 4. One sentence

The missing software is a small durable workflow host: a version-aware command coordinator and fenced worker loop that gives the existing strategies and journal local or shared authority, recovery, stopping, and observation.