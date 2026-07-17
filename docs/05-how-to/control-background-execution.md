# Control Work After It Starts

Use a background handle when an application must regain control while a graph
is still running—for example, so an API endpoint can accept a user's **Stop**
request while an order-enrichment workflow is waiting on external services.

## Choose Blocking or Background Execution

Blocking execution is still the simplest choice when the caller only needs the
final answer:

```python
# Before: this call returns only after the graph settles.
result = runner.run(order_graph, {"order_id": "order-100"})
show_receipt(result["receipt"])
```

Start the same work in the background when the caller needs a live control
point:

```python
# After: submission returns a process-local handle immediately.
handle = runner.start_run(order_graph, {"order_id": "order-100"})
show_stop_button()

result = handle.result()
show_receipt(result["receipt"])
```

| Need | Use |
|---|---|
| Return only after one run settles | `run()` |
| Return only after a batch settles | `map()` |
| Keep serving the caller while one run is live | `start_run()` |
| Keep serving the caller while a batch is live | `start_map()` |
| Consume mapped results incrementally | `map_iter()` |

`SyncRunner.start_*()` and `AsyncRunner.start_*()` are ordinary methods. They
return `SyncHandle` and `AsyncHandle` respectively. In particular, do not
`await runner.start_run(...)`; only an async handle's `result()` is awaited.

Daft translates a complete operation into a columnar query plan, so
`DaftRunner` does not provide background handles. Use Daft's own execution
controls when a Daft plan needs job-level orchestration.

## Start and Retrieve Synchronous Work

```python
from threading import Event

from hypergraph import Graph, SyncRunner, node

entered = Event()
release = Event()

@node(output_name="receipt")
def charge_order(order_id: str) -> str:
    entered.set()
    release.wait()  # stands in for a controlled external dependency
    return f"charged {order_id}"

runner = SyncRunner()
handle = runner.start_run(Graph([charge_order]), {"order_id": "order-100"})

entered.wait()
assert not handle.done
release.set()

result = handle.result()  # blocks this caller until the work settles
assert result["receipt"] == "charged order-100"
assert handle.done
```

`done` is a snapshot, not a promise that a new handle is initially unfinished.
A tiny graph may settle before the caller first reads it.

## Start and Retrieve Asynchronous Work

```python
import asyncio

from hypergraph import AsyncRunner, Graph, node

entered = asyncio.Event()
release = asyncio.Event()

@node(output_name="receipt")
async def charge_order(order_id: str) -> str:
    entered.set()
    await release.wait()
    return f"charged {order_id}"

runner = AsyncRunner()
handle = runner.start_run(Graph([charge_order]), {"order_id": "order-200"})

await entered.wait()
assert not handle.done
release.set()

result = await handle.result()
assert result["receipt"] == "charged order-200"
```

Call async start methods from a running event loop. Submission errors, such as
calling one with no running loop, fail directly instead of fabricating an empty
result.

## Keep Live Control Separate from Settled Truth

A handle deliberately has only three public members:

```python
handle.done
handle.stop(info={"requested_by": "Maya"})
handle.result(raise_on_failure=True)
```

Status, failures, logs, checkpoint gaps, and stopped-map scope live on the
settled `RunResult` or `MapResult`. The handle is not a second result model and
does not expose cancellation, callbacks, retry, inspection, or persistence
methods.

## Inspect Background Work After It Settles

Inspection is configured when work starts, but the handle remains control-only.
Before, an application retrieves the result and correlates its log and failures:

```python
# Before: the handle controls work; debugging uses separate result surfaces.
handle = runner.start_map(
    customer_review,
    {
        "customer_id": ["alex-10", "maya-23", "sam-04"],
        "lifetime_value": [2400, 1200, 3100],
    },
    map_over=["customer_id", "lifetime_value"],
)
batch = handle.result(raise_on_failure=False)
print(batch.log, batch.failures)
```

After, pass `inspect=True` through `start_map()`, retrieve the same settled
`MapResult`, and inspect that result:

```python
# After: capture while live, then open the settled batch view explicitly.
handle = runner.start_map(
    customer_review,
    {
        "customer_id": ["alex-10", "maya-23", "sam-04"],
        "lifetime_value": [2400, 1200, 3100],
    },
    map_over=["customer_id", "lifetime_value"],
    inspect=True,
)
batch = handle.result(raise_on_failure=False)
batch.inspect()
```

Async start methods are still ordinary methods: do not `await runner.start_run(...)`.
Await only retrieval:

```python
handle = AsyncRunner().start_run(graph, values, inspect=True)
result = await handle.result(raise_on_failure=False)
result.inspect()
```

In a notebook, the live view settles into a saved snapshot. After saving, the
snapshot remains locally interactive without a kernel or Hypergraph server.
It contains captured values, so treat the notebook as sensitive. See
[Debug Workflows](debug-workflows.md) for limits and degraded behavior.

## Inspect Failures Without Losing the Result

Background execution captures node-body failures so retrieval can choose
between exception-first and inspection-first code:

```python
handle = runner.start_map(
    score_graph,
    {"order_id": ["order-101", "order-bad", "order-102"]},
    map_over="order_id",
)

# Default: wait for settlement, then raise the first real failure in input order.
handle.result()
```

```python
# Diagnostic path: return the same settled MapResult with its failures intact.
batch = handle.result(raise_on_failure=False)
for failed in batch.failures:
    print(failed.error, failed.failure)
```

For `AsyncHandle`, await both forms. A background map keeps claiming siblings
after one item fails; retrieval raises only after the batch settles. An error
that prevents Hypergraph from constructing any `RunResult` or `MapResult`
still propagates under both retrieval settings. Repeated retrieval returns the
same settled result and preserves the original failure evidence.

Blocking calls remain unchanged: `run()` and `map()` still accept
`error_handling="raise" | "continue"`. Background start methods do not accept
`error_handling`; retrieval owns that choice instead.

## Request a Cooperative Stop

The handle can stop work even when the caller did not choose a `workflow_id`:

```python
handle = runner.start_run(report_graph, {"report_id": "quarterly"})

# Later, when Maya clicks Stop:
handle.stop(info={"requested_by": "Maya", "reason": "wrong date range"})
result = handle.result(raise_on_failure=False)
assert result.stopped
```

Stop is cooperative. Hypergraph carries one signal through nested graphs and
checks it at execution boundaries; a long-running node may also check
`NodeContext.stop_requested`. It does not kill a thread or cancel the
framework-owned task.

The first accepted stop owns its `info`. Repeated calls and calls after
settlement return `None`, do not raise, and do not rewrite the result:

```python
handle.stop(info={"reason": "user request"})
handle.stop(info={"reason": "timeout"})  # ignored; first info remains authoritative
```

When an ID is useful elsewhere in the application, the existing runner-level
form controls the same execution:

```python
handle = runner.start_run(graph, values, workflow_id="order-100")
runner.stop("order-100", info={"reason": "user request"})
```

## Cancelling an Async Waiter Does Not Cancel the Work

An HTTP request may disappear while the underlying workflow should finish.
Cancelling one task that awaits `result()` affects only that waiter:

```python
waiter = asyncio.create_task(handle.result())
await asyncio.sleep(0)  # let the waiter reach the shielded result call
waiter.cancel()

try:
    await waiter
except asyncio.CancelledError:
    pass

assert not handle.done  # if the graph is still blocked
result = await handle.result()  # a later waiter can retrieve the real outcome
```

Call `handle.stop()` when the application actually intends to stop execution.

## Understand a Stopped Map's Real Scope

Suppose Maya starts ten document checks and stops after four have been claimed.
The result describes four real executions, not ten invented rows:

```python
batch = handle.result(raise_on_failure=False)

len(batch)                       # 4 real RunResult objects
batch.requested_count            # 10 requested inputs
batch.unstarted_item_indexes     # for example, (4, 5, 6, 7, 8, 9)
batch.stopped                    # True
```

Before background stop support, callers often had to treat requested input
count as if it were execution history. After stopping, these surfaces stay
separate:

```text
Before: 10 requested inputs -> assumed 10 executions
After:  10 requested inputs -> 4 real outcomes + 6 unstarted indexes
```

`results`, iteration, indexing, logs, child run IDs, child events, and child
checkpoint rows contain only claimed work, in original input order. A stopped
batch may also have real failures: `batch.stopped` and `batch.any_failed` can
both be true (`batch.failed` stays false — it mirrors the aggregate status),
while `batch.failures` retains those attempted-item failures. Default
retrieval still raises the first real failed item; non-raising retrieval gives
the settled stopped batch.

Parent `RunEndEvent` and OpenTelemetry batch counts also count real settled
children only. The parent event status, `batch_outcome`, OTel outcome, and
checkpointed parent workflow status report `STOPPED` when the requested scope
was curtailed. Read `requested_count` and `unstarted_item_indexes` from
`MapResult` when a dashboard needs requested scope.

## Reserve Workflow IDs Deliberately

A runner permits only one active execution—run or map—per `workflow_id`:

```python
first = runner.start_run(graph, values, workflow_id="order-100")
second = runner.start_map(graph, batch, map_over="item", workflow_id="order-100")
# WorkflowAlreadyRunningError is raised by start_map(); no second handle exists.
```

The first execution remains live. Different IDs are independently accepted and
controllable, but the API does not promise that synchronous executions start in
parallel or submission order.

`InMemoryCache` and `SqliteCheckpointer` support runner-managed overlap. If an
application shares another backend across overlapping handles, follow that
backend's documented concurrency contract; a custom cache or checkpointer is
responsible for making its compound operations concurrency-safe.

## Use a Checkpointer for Recovery, Not Handle Reconnection

A handle is a Python-process object. Do not store it as a durable job ID, and
do not interpret a persisted `ACTIVE` row as proof that a worker is alive.

```python
from hypergraph import SyncRunner
from hypergraph.checkpointers import SqliteCheckpointer

checkpointer = SqliteCheckpointer("./orders.db")
runner = SyncRunner(checkpointer=checkpointer)
handle = runner.start_run(order_graph, values, workflow_id="order-100")
```

If that process exits, another process opens the database and starts a **new**
execution using the ordinary resume contract:

```python
checkpointer = SqliteCheckpointer("./orders.db")
runner = SyncRunner(checkpointer=checkpointer)
new_handle = runner.start_run(order_graph, workflow_id="order-100")
result = new_handle.result(raise_on_failure=False)
```

The new handle cannot discover or stop the old process. Existing checkpoint
durability rules are unchanged: synchronous writes fail loudly; asynchronous
durability remains best-effort and reports gaps through
`result.checkpoint_ok` and `result.checkpoint_errors`; exit durability saves at
the existing completion boundary.

`start_run()` accepts `checkpoint=` and `workflow_id=`, but intentionally omits
the lineage-changing `override_workflow`, `fork_from`, and `retry_from`
shortcuts. Perform a fork or retry through the checkpointer first, then start
the resulting checkpoint and workflow ID. Passing one of those names directly
raises `TypeError` instead of tunneling into `run()`; if it is genuinely a graph
input, put it inside `values={...}`. The same immediate boundary applies to any
blocking runner option absent from the chosen `start_*()` signature.

## Complete Examples

- [`examples/background_handles_sync.py`](../../examples/background_handles_sync.py)
  demonstrates immediate sync return, stable retrieval, and failure inspection.
- [`examples/background_handles_async.py`](../../examples/background_handles_async.py)
  demonstrates ordinary async submission, waiter-cancellation isolation, and
  non-raising batch retrieval.

See [Runners](../06-api-reference/runners.md) for exact signatures,
[Checkpointers](../06-api-reference/checkpointers.md) for recovery semantics,
and [Observe Execution](observe-execution.md) for event and OTel fields.
