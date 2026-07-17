# Checkpointers

A checkpointer persists every step of a run so it can be resumed, forked, retried, or inspected after the process exits. It is opt-in: a graph runs exactly the same with or without one, and existing `run()`/`map()` behavior is unchanged when `checkpointer=None`.

{% hint style="info" %}
This page covers the checkpointer subsystem itself — the `Checkpointer` ABC, `CheckpointPolicy`, and lineage (fork/retry) mechanics. For `run()`/`map()` parameter semantics (`workflow_id`, `fork_from`, `retry_from`, `override_workflow`), see [Runners](runners.md#run). For live background control versus durable recovery, see [Control Work After It Starts](../05-how-to/control-background-execution.md#use-a-checkpointer-for-recovery-not-handle-reconnection). For the `map()` batch-checkpointing walkthrough, see [Batch Processing](../05-how-to/batch-processing.md#checkpointing-with-map).
{% endhint %}

## Current Inspection Is Separate from Persistence

A current-process inspect view does not require a checkpointer. Use
`inspect=True` when Maya needs the values and timeline for work executing now:

```python
from hypergraph import SyncRunner
from hypergraph.checkpointers import SqliteCheckpointer

# Before: a database was sometimes added only to inspect one current run.
runner = SyncRunner(checkpointer=SqliteCheckpointer("./debug.db"))
result = runner.run(graph, values, workflow_id="debug-1")

# After: current inspection is process-local and needs no persistence setup.
result = SyncRunner().run(graph, values, inspect=True)
result.inspect()
```

Configure `SqliteCheckpointer` only when the use case also needs resume, fork,
retry, restart, or history after the process exits. When a later execution
restores completed work, restored nodes expose their status, timing metadata,
and qualified path, but do not reconstruct successful inputs or outputs for
the inspect view. Fresh nodes can capture their current values with
`inspect=True`.

See [Debug Workflows](../05-how-to/debug-workflows.md) for degraded views,
capture limits, and saved-notebook sensitivity.

## Turning It On

Pass a checkpointer to the runner, then pass a `workflow_id` to `run()`:

```python
from hypergraph import AsyncRunner, Graph, node
from hypergraph.checkpointers import SqliteCheckpointer

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3

checkpointer = SqliteCheckpointer("./runs.db")
runner = AsyncRunner(checkpointer=checkpointer)
graph = Graph([double, triple])

result = await runner.run(graph, {"x": 5}, workflow_id="wf-1")
print(result.values)  # {'doubled': 10, 'tripled': 30}

run = checkpointer.get_run("wf-1")
print(run)  # Run: wf-1 | completed | 6ms | 2 steps

for step in checkpointer.steps("wf-1"):
    print(step)  # Step [0] double | completed | ...
```

`SqliteCheckpointer` exposes both async methods (`get_run_async`, `get_steps`, used by `AsyncRunner`) and sync convenience methods (`get_run`, `steps`, `state`, `values`, `runs`, `lineage`) for reading results back after a run. `SyncRunner` also accepts a `checkpointer=` — it uses the sync write path (`create_run_sync`, `save_step_sync`) instead of the async one.

SQLite-backed durability requires an on-disk or shared-memory database:

```python
# On disk — survives process restarts
SqliteCheckpointer("./runs.db")

# Shared in-memory — same process only, useful for tests
SqliteCheckpointer(":memory:")
```

`MemoryCheckpointer` is a simpler async-only, in-process alternative with no SQLite dependency — good for unit tests that don't need durability across restarts.

## Background Handles and Process Recovery

`start_run()` and `start_map()` use the same checkpoint policies and workflow
lineage as blocking execution. A handle is only the current process's live
control channel; it is not persisted and cannot be discovered or reconnected
from another process.

```python
handle = runner.start_run(graph, values, workflow_id="order-100")
result = await handle.result(raise_on_failure=False)

# Durable evidence is queryable separately.
persisted = checkpointer.get_run("order-100")
```

A persisted `ACTIVE` row records the latest lifecycle transition. It does not
prove that a worker is still alive. After process loss, opening the
checkpointer and calling `start_run()` creates a new execution under the
ordinary resume contract; it does not reconnect to the old handle.

For a stopped background map, persistence contains one parent row plus child
rows for real claimed outcomes only. Inputs listed in
`MapResult.unstarted_item_indexes` have no child checkpoint row. The parent
workflow status is `STOPPED` when stop curtailed requested scope.

Background execution adds no durability default. Sync runner writes remain
synchronous; async and exit modes retain their existing guarantees and report
best-effort gaps through `result.checkpoint_ok` and
`result.checkpoint_errors`.

## Nested Recovery

A nested `GraphNode` runs its child graph as its own child workflow
(`"order-100/enrich"` under `"order-100"`). The child commits its terminal
status before the parent writes the StepRecord for the `GraphNode`, so a crash
can land exactly between the two:

```text
child workflow "order-100/enrich": COMPLETED
parent StepRecord for "enrich":    missing
```

Resuming `workflow_id="order-100"` recovers this state by restoring, not
re-running:

```python
# Before (bug): resume re-invoked the terminal child.
runner.run(order_graph, workflow_id="order-100")
# WorkflowAlreadyCompletedError: Workflow 'order-100/enrich' is already completed.

# After: resume restores the child's persisted outputs and commits the
# missing parent step. The child's inner nodes do not execute again.
result = runner.run(order_graph, workflow_id="order-100")
# result.values contains the child's outputs; the parent step is COMPLETED
# with child_run_id "order-100/enrich".
```

The restore is truthful: no fresh child-node execution events are emitted, and
the recovery recurses through deeper nesting (a terminal grandchild under a
crashed middle parent heals the same way, level by level). A terminal `FAILED`
child is never restored as success — resume follows the ordinary failure path,
re-executing the failed child nodes and resurfacing their error under the
run's `error_handling` mode.

Recovery is evidence-gated so it never shadows a legitimate re-execution. A
real persisted `COMPLETED` parent step is proof that the nested graph completed;
folded carrier values are not, because they lack producer provenance.
Windowed/compacted retention with nested crash-window recovery is therefore
explicitly rejected once parent history has been compacted. Use
`retention="full"` or `retention="latest"` for workflows that combine nested
graphs with resume/crash recovery, or fork the workflow. Windowed support is
tracked in #277.

With a delegated runner (`as_node(runner=...)`), the child workflow persists
in the delegated runner's checkpointer. Crash recovery reads the child's
status and outputs from that store, while the parent's own step receipts stay
in the parent runner's checkpointer.

## Checkpointer ABC

Every checkpointer implements the same contract, so runners don't need to know which backend is behind `checkpointer=`:

| Method | Purpose |
|---|---|
| `save_step(record)` | Persist one node's execution atomically. Upserts on `(run_id, superstep, node_name)`. |
| `create_run(run_id, ...)` | Create or reset a run record at run start. |
| `update_run_status(run_id, status, ...)` | Update lifecycle status with duration/counts. |
| `get_state(run_id, superstep=None)` | Fold step values into accumulated state. `None` means latest. |
| `get_steps(run_id, superstep=None, show_internal=False)` | Public step records through a superstep. Set `show_internal=True` to include retention carriers. |
| `get_checkpoint(run_id, superstep=None)` | `Checkpoint` (values + steps) for forking — has a default implementation built from `get_state`/`get_steps`. |
| `list_runs(status=None, graph_name=None, since=None, parent_run_id=<omitted>, limit=100)` | List runs with composable filters. Omit `parent_run_id` for all runs; pass `None` for top-level runs only. |
| `count_runs(status=None, parent_run_id=<omitted>, retry_of=None)` | Count with the same omitted/all versus explicit-`None`/top-level parent filter. |
| `search_async(query, field=None, limit=20)` | FTS search over step values. Returns `[]` if unsupported. |

`graph_name`, `since`, `status`, and `parent_run_id` compose with AND before `limit` is applied. `since` is inclusive; naive datetimes mean UTC and aware datetimes are normalized to UTC. The same rules apply to SQLite's sync `runs()` adapter.

`SqliteCheckpointer` supports runner-managed use across accepted background
executions. For any other shared backend, follow its documented concurrency
contract. A custom checkpointer shared by overlapping handles is responsible
for making each logical operation concurrency-safe.

Steps are the source of truth — state is always computed by folding steps, never stored as a separate mutable blob. Public step views hide internal `__retained_state__` / `RetentionBaseline` carrier rows by default, while state reconstruction folds the raw internal stream. This keeps `latest` and `windowed` retention reconstructible without showing phantom nodes in checkpoints, search, statistics, or lineage views. Use `show_internal=True` only when debugging the retention mechanism itself.

## CheckpointPolicy

`CheckpointPolicy` controls *when* checkpoints are written and *how much history* is kept:

```python
from hypergraph.checkpointers import CheckpointPolicy

CheckpointPolicy()
# CheckpointPolicy(durability='async', retention='full', window=None, ttl=None)
```

| Field | Values | Meaning |
|---|---|---|
| `durability` | `"sync"` \| `"async"` (default) \| `"exit"` | `sync` blocks until each step is written (safest). `async` writes in the background. `exit` only writes at run completion — fastest, no mid-run recovery. |
| `retention` | `"full"` (default) \| `"latest"` \| `"windowed"` | `full` keeps every step (time travel). `latest` keeps only materialized latest state. `windowed` keeps the last `window` supersteps. |
| `window` | `int \| None` | Required when `retention="windowed"`. |
| `ttl` | `timedelta \| None` | Auto-expire completed runs after this duration. |

**Retry/timeout evidence writes through under every durability mode.** For a node with a [RetryPolicy](nodes.md#retrypolicy) or `timeout=`, attempt reservations/outcomes and the series-closing StepRecord are persisted immediately even under `durability="async"` or `"exit"`: the final attempt outcome, its linked StepRecord, and the series closure must commit atomically, and that invariant takes precedence over buffering. Nodes without retry or timeout buffer normally.

**Async durability is best-effort.** With `durability="async"` (the default), step writes happen in background tasks: a failed write does not fail the run — the run still returns `COMPLETED`, and the failure is reported on the result instead. Check `result.checkpoint_ok` (and `result.checkpoint_errors`, a tuple of error strings) to detect gaps in the persisted history:

```python
result = await runner.run(graph, {"x": 5}, workflow_id="wf-1")
if not result.checkpoint_ok:
    log.warning("run completed but steps were not persisted: %s", result.checkpoint_errors)
```

With `durability="sync"`, the same write failure propagates and fails the run. `SyncRunner` always writes steps synchronously (there is no event loop to defer to), so a write failure fails the run regardless of the configured durability.

Invalid combinations raise at construction, not at run time:

```python
CheckpointPolicy(durability="exit", retention="full")
# ValueError: durability="exit" requires retention="latest", got retention="full".
# With exit mode, steps are not persisted mid-run.

CheckpointPolicy(retention="windowed")
# ValueError: retention="windowed" requires window parameter
```

Pass a policy directly, or use the `durability=`/`retention=` shortcut kwargs on `SqliteCheckpointer` (the two are mutually exclusive):

```python
SqliteCheckpointer("./runs.db", durability="sync", retention="latest")

# Equivalent:
SqliteCheckpointer("./runs.db", policy=CheckpointPolicy(durability="sync", retention="latest"))

# Raises — pick one:
SqliteCheckpointer("./runs.db", policy=CheckpointPolicy(), durability="sync")
# ValueError: Cannot pass both 'policy' and 'durability'/'retention'. Use one or the other.
```

## Fork and Retry

Both operations start a **new** `workflow_id` from an existing run's checkpoint. They differ in intent and in the lineage metadata recorded on the new run:

```python
await runner.run(graph, {"x": 5}, workflow_id="job-1")

# Fork: branch history, optionally override inputs
forked = await runner.run(graph, {"x": 100}, fork_from="job-1")
checkpointer.get_run(forked.workflow_id).forked_from  # "job-1"

# Retry: same intent as fork, but records retry lineage
retried = await runner.run(graph, retry_from="job-1")
checkpointer.get_run(retried.workflow_id).retry_of  # "job-1"
```

{% hint style="warning" %}
`runner.run(graph, fork_from="job-1")` derives a source-readable target such as `job-1-fork-a1b2c3`. An explicit `workflow_id=` remains exact. A nested source such as `job-1/0` cannot be implicitly promoted to a top-level fork; top-level callers must provide an explicit slash-free target. `retry_from=` deliberately keeps the generic `run-YYYYMMDD-hex` runner naming behavior.
{% endhint %}

The checkpointer's own `fork_workflow`/`retry_workflow` (sync) and `fork_workflow_async`/`retry_workflow_async` use source-derived names when their target is omitted:

```python
fork_id, fork_checkpoint = checkpointer.fork_workflow("job-1")
fork_id  # "job-1-fork-a1b2c3"

retry_id, retry_checkpoint = checkpointer.retry_workflow("job-1")
retry_id  # "job-1-retry-1" (increments per retry of the same source)
```

## Lineage

`checkpointer.lineage(root_workflow_id)` renders a git-log-style view of a run and all its fork/retry descendants:

```python
checkpointer.lineage("root-1")
```
```
LineageView: root-1 (root=root-1)

● root-1 [completed] (root) | steps=2 cached=0 failed=0 <selected>
└─ root-1-fork-a [completed] (fork) <- root-1 | steps=2 cached=0 failed=0
```

Use this to audit which forks/retries came from which source run, and their status, without re-deriving anything.

## Lineage and Concurrency Errors

| Error | Raised when |
|---|---|
| `WorkflowAlreadyRunningError` | A second run or map starts for a `workflow_id` that already has an active execution. At most one active execution per `workflow_id`; use different IDs for independently controlled work. |
| `WorkflowForkError` | A fork targets an existing workflow, or a nested source is used without an explicit top-level target. |
| `WorkflowStoppedError` | A stopped workflow is rerun without either a non-empty runtime value mapping or `override_workflow=True`. The rejection happens before new run events or persistence writes. |

```python
from hypergraph.exceptions import WorkflowForkError

await runner.run(graph, {"x": 5}, workflow_id="job-1")
checkpoint = checkpointer.checkpoint("job-1")

try:
    await runner.run(graph, {"x": 100}, checkpoint=checkpoint, workflow_id="job-1")
except WorkflowForkError as e:
    print(e)  # Cannot fork into existing workflow 'job-1'. Use a new workflow_id.
```

See also `GraphChangedError`, `WorkflowAlreadyCompletedError`, and `InputOverrideRequiresForkError`, covered in [Batch Processing — Run Lineage](../05-how-to/batch-processing.md#run-lineage-resume-vs-fork).

## Types

`checkpointer.steps(run_id)` and `checkpointer.get_run(run_id)` (used throughout this page) return these dataclasses:

| Type | Fields | Notes |
|---|---|---|
| `StepRecord` | `run_id`, `superstep`, `node_name`, `index`, `status`, `input_versions`, `values`, `duration_ms`, `cached`, `decision`, `error`, `created_at`, `completed_at` | One per node execution. `status` is a `StepStatus`. |
| `StepStatus` | `COMPLETED`, `FAILED`, `PAUSED` | Enum. |
| `Run` | `id`, `status`, `graph_name`, `duration_ms`, `node_count`, `error_count`, `parent_run_id`, `forked_from`, `fork_superstep`, `retry_of`, `retry_index`, `created_at`, `completed_at` | `status` is a `WorkflowStatus`. |
| `WorkflowStatus` | `ACTIVE`, `PAUSED`, `STOPPED`, `PARTIAL`, `COMPLETED`, `FAILED` | Enum. Kept distinct from the runner-level `RunStatus` to avoid a naming collision. |
| `Checkpoint` | `values`, `steps`, `source_run_id`, `source_superstep`, `retry_of`, `retry_index` | What `get_checkpoint()`/`fork_workflow()`/`retry_workflow()` return — folded state plus the steps it was built from. |

```python
step = checkpointer.steps("wf-1")[0]
step.status == StepStatus.COMPLETED  # True

run = checkpointer.get_run("wf-1")
run.status == WorkflowStatus.COMPLETED  # True
```

## Attempt Ledger (Internal)

{% hint style="warning" %}
Internal attempt-ledger persistence. These records back retry and cooperative `@node(timeout=...)`. The shapes below are stable for inspection but the write seam is not a public API.
{% endhint %}

An attempt-managed node stays **one logical graph step**; each callable invocation is a separately durable attempt. The evidence lives next to the steps:

```text
step 4 = call_model                       # one StepRecord, one state application
attempts = #1 failed, #2 timed out, #3 succeeded   # the attempt ledger
```

| Type | Fields | Notes |
|---|---|---|
| `AttemptSeries` | `id`, `run_id`, `node_name`, `policy_fingerprint`, `max_attempts`, `opened_at`, `deadline_at`, `committed_superstep`, `closed_at` | One durable retry budget per logical node execution. The id stays stable across superstep drift; open (`closed_at is None`) series are never pruned. |
| `AttemptRecord` | `series_id`, `attempt_number`, `scheduled_superstep`, `status`, `started_at`, `completed_at`, `error`, `retry_not_before`, `sampled_delay`, `deadline_elapsed`, `cancellation_requested` | One-based `attempt_number`. Backoff is sampled once and persisted as data. Deadline and cancellation are independent facts; there is deliberately no `work_stopped` field. |
| `AttemptStatus` | `STARTED`, `FAILED`, `TIMED_OUT`, `SUCCEEDED`, `CANCELLED`, `OUTCOME_UNKNOWN` | Enum. `STARTED` is a durable reservation that consumes budget; crash-stranded reservations settle as `OUTCOME_UNKNOWN` on resume. |
| `AttemptError` | `type_name`, `message` | Bounded error projection (type name + capped message text). No args, no stack traces, no `repr` of user values — but the capped message is stored verbatim; message-content redaction is #233's diagnostics scope. |

Attempt rows never participate in state folding, step counts, baselines, or staleness — `get_state()` and `get_steps()` are byte-identical with or without retry history. A closed series links its final `StepRecord` via `StepRecord.attempt_series_id` and follows that step's retention fate.

Runnable inspection example:

```python
import asyncio
from hypergraph.checkpointers import AttemptError, AttemptStatus, SqliteCheckpointer, StepRecord, StepStatus


async def main() -> None:
    checkpointer = SqliteCheckpointer("./attempts-demo.db")
    await checkpointer.create_run("wf-1", graph_name="demo")

    # What the retry runtime will do internally: reserve, fail, reserve, close.
    series = await checkpointer.open_attempt_series(
        "wf-1", "call_model", policy_fingerprint="demo-fp", max_attempts=3
    )
    first = await checkpointer.begin_attempt(
        series.id, policy_fingerprint="demo-fp", scheduled_superstep=0
    )
    await checkpointer.record_attempt_outcome(
        series.id, first.attempt_number, AttemptStatus.FAILED,
        error=AttemptError.from_exception(TimeoutError("slow upstream")),
    )
    second = await checkpointer.begin_attempt(
        series.id, policy_fingerprint="demo-fp", scheduled_superstep=0
    )
    await checkpointer.close_attempt_series(
        series.id, second.attempt_number, AttemptStatus.SUCCEEDED,
        step_record=StepRecord(
            run_id="wf-1", superstep=0, node_name="call_model", index=0,
            status=StepStatus.COMPLETED, input_versions={},
            values={"answer": 42}, attempt_series_id=series.id,
        ),
    )

    # Inspection: the step is one logical execution; the ledger holds the truth.
    closed = await checkpointer.get_attempt_series(series.id)
    print(closed)  # AttemptSeries series-... | wf-1/call_model | closed | max_attempts=3
    for record in await checkpointer.get_attempt_records(series.id):
        print(record)
    # Attempt #1 | failed | superstep 0 | error: TimeoutError
    # Attempt #2 | succeeded | superstep 0
    print(await checkpointer.remaining_attempts(series.id))  # 1

    await checkpointer.close()


asyncio.run(main())
```

Crash-recovery queries used on resume:

```python
open_series = await checkpointer.get_open_attempt_series("wf-1", "call_model")
if open_series is not None:
    # Precondition: the caller must know no other invocation of this series can
    # still be live (the runner's resume path owns that assertion). This is the
    # ONLY path that converts STARTED rows — begin_attempt over a STARTED row
    # raises instead, because the ledger cannot tell dead from live.
    records = await checkpointer.resolve_stranded_attempts(open_series.id)
    remaining = await checkpointer.remaining_attempts(open_series.id)
    # A reservation that never settled is now OUTCOME_UNKNOWN and stays consumed:
    # max_attempts=3 with one stranded attempt leaves remaining == 2.
```

## Backend Comparison

| | `SqliteCheckpointer` | `MemoryCheckpointer` |
|---|---|---|
| Durability | On disk (or shared `:memory:`) | In-process only, lost on exit |
| Works with | `AsyncRunner` and `SyncRunner` | `AsyncRunner` only |
| Sync convenience methods (`get_run`, `steps`, `lineage`, ...) | Yes | No — async only |
| Best for | Production durability, multi-process resume, cross-process inspection | Unit tests, short-lived scripts |

## Checkpointing vs the No-Checkpointer Re-Drive Pattern

You do not need a checkpointer to pause and resume a graph. Without one, each `run()` call replays the graph from the start, and you re-supply previously-collected interrupt responses by seeding them back into the input values:

```python
from dataclasses import dataclass
from typing import ClassVar

from hypergraph import Graph, node, AsyncRunner, interrupt

@dataclass(frozen=True)
class FreeText:  # Stand-in for the companion question-vocabulary package.
    answer_type: ClassVar[object] = str
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple[object, ...] = ()

@node(output_name="draft")
def generate(prompt: str) -> str:
    return f"Draft: {prompt}"

@interrupt(answer_name="feedback")
def review(draft: str) -> FreeText:
    return FreeText(prompt="What should change?", evidence=(draft,))

@interrupt(answer_name="final_draft")
def edit(feedback: str) -> FreeText:
    return FreeText(prompt="Provide the final draft", evidence=(feedback,))

@node(output_name="result")
def publish(final_draft: str) -> str:
    return f"Published: {final_draft}"

graph = Graph([generate, review, edit, publish])
runner = AsyncRunner()  # no checkpointer

values = {"prompt": "hello"}
r1 = await runner.run(graph, values)
# r1.pause.node_name == "review", r1.pause.response_key == "feedback"

values[r1.pause.response_key] = "Needs more detail"
r2 = await runner.run(graph, values)
# r2.pause.node_name == "edit", r2.pause.response_key == "final_draft"

values[r2.pause.response_key] = "Detailed draft about hello"
r3 = await runner.run(graph, values)
# r3.status == RunStatus.COMPLETED, r3.values["result"] == "Published: Detailed draft about hello"
```

This works with zero setup and no persistence dependency, but the caller owns replay: it must resend the whole `values` dict every call, and there is no durability across process restarts.

| | No checkpointer (re-drive) | With checkpointer |
|---|---|---|
| Setup | None | `SqliteCheckpointer` + `workflow_id` |
| Resume across process restart | No — state lives only in the caller's `values` dict | Yes — state is read back from disk |
| What you resend each call | The full accumulated `values` dict | Only the new interrupt response |
| Time travel / inspect past steps | Not available | `get_state(run_id, superstep=N)`, `steps(run_id)` |
| Fork / retry a past run | Not available | `fork_from=`, `retry_from=` |
| Best for | Prototyping, stateless request/response apps that already keep their own conversation state | Durable multi-turn workflows, production HITL, long-running batches |

See [Human-in-the-Loop](../03-patterns/07-human-in-the-loop.md) for the interrupt side of this pattern, including nested-graph interrupts and multi-turn chat.

## What's Next

- [Human-in-the-Loop](../03-patterns/07-human-in-the-loop.md) — pause/resume mechanics, cyclic-graph entrypoints
- [Runners](runners.md#run) — `workflow_id`, `fork_from`, `retry_from` parameter reference
- [Control Work After It Starts](../05-how-to/control-background-execution.md) — process-local handles versus durable recovery
- [Batch Processing — Checkpointing with map()](../05-how-to/batch-processing.md#checkpointing-with-map) — parent/child batch runs

## Cleanup

A checkpointer owns a live database connection, and the runner does not manage its lifecycle. Call `await checkpointer.close()` when you are done — a process that skips this can hang on exit waiting for the open connection:

{% code overflow="wrap" %}
```python
checkpointer = SqliteCheckpointer("./runs.db")
try:
    runner = AsyncRunner(checkpointer=checkpointer)
    result = await runner.run(graph, {"x": 1}, workflow_id="run-1")
finally:
    await checkpointer.close()
```
{% endcode %}
