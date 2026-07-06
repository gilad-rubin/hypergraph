# Checkpointers

A checkpointer persists every step of a run so it can be resumed, forked, retried, or inspected after the process exits. It is opt-in: a graph runs exactly the same with or without one, and existing `run()`/`map()` behavior is unchanged when `checkpointer=None`.

{% hint style="info" %}
This page covers the checkpointer subsystem itself — the `Checkpointer` ABC, `CheckpointPolicy`, and lineage (fork/retry) mechanics. For `run()`/`map()` parameter semantics (`workflow_id`, `fork_from`, `retry_from`, `override_workflow`), see [Runners](runners.md#run). For the `map()` batch-checkpointing walkthrough, see [Batch Processing](../05-how-to/batch-processing.md#checkpointing-with-map).
{% endhint %}

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

Both durability requires an on-disk or shared-memory SQLite database:

```python
# On disk — survives process restarts
SqliteCheckpointer("./runs.db")

# Shared in-memory — same process only, useful for tests
SqliteCheckpointer(":memory:")
```

`MemoryCheckpointer` is a simpler async-only, in-process alternative with no SQLite dependency — good for unit tests that don't need durability across restarts.

## Checkpointer ABC

Every checkpointer implements the same contract, so runners don't need to know which backend is behind `checkpointer=`:

| Method | Purpose |
|---|---|
| `save_step(record)` | Persist one node's execution atomically. Upserts on `(run_id, superstep, node_name)`. |
| `create_run(run_id, ...)` | Create or reset a run record at run start. |
| `update_run_status(run_id, status, ...)` | Update lifecycle status with duration/counts. |
| `get_state(run_id, superstep=None)` | Fold step values into accumulated state. `None` means latest. |
| `get_steps(run_id, superstep=None)` | Raw step records through a superstep. |
| `get_checkpoint(run_id, superstep=None)` | `Checkpoint` (values + steps) for forking — has a default implementation built from `get_state`/`get_steps`. |
| `list_runs(status=None, parent_run_id=None, limit=100)` | List runs, optionally filtered. |
| `count_runs(status=None, parent_run_id=None, retry_of=None)` | Count without materializing full run objects. |
| `search_async(query, field=None, limit=20)` | FTS search over step values. Returns `[]` if unsupported. |

Steps are the source of truth — state is always computed by folding steps, never stored as a separate mutable blob. This is what makes time-travel (`get_state(run_id, superstep=3)`) and forking from an arbitrary point possible.

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
`fork_workflow_async`/`retry_workflow_async` (called internally by `run(fork_from=...)`) fall back to a `{source}-fork-{hex}` / `{source}-retry-{n}` name **only when `workflow_id` is `None`**. In practice, `run()` always auto-generates a `workflow_id` (`run-YYYYMMDD-hex`) before the fork/retry branch runs, so calling `runner.run(graph, fork_from="job-1")` without an explicit `workflow_id=` produces an auto-generated id, not a `job-1-fork-...` name. Pass `workflow_id=` explicitly if you want a specific, readable name for the new run.
{% endhint %}

Call the checkpointer's own `fork_workflow`/`retry_workflow` (sync) or `fork_workflow_async`/`retry_workflow_async` directly to get the `{source}-fork-{hex}` naming convention:

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
| `WorkflowAlreadyRunningError` | A second `run()` starts for a `workflow_id` that already has an active run. At most one active `run()` per `workflow_id` — use different `workflow_id`s for concurrent runs. |
| `WorkflowForkError` | An explicit `checkpoint=` is passed to fork into a `workflow_id` that already exists. Use a new `workflow_id` for the fork. |

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

## Backend Comparison

| | `SqliteCheckpointer` | `MemoryCheckpointer` |
|---|---|---|
| Durability | On disk (or shared `:memory:`) | In-process only, lost on exit |
| Works with | `AsyncRunner` and `SyncRunner` | `AsyncRunner` only |
| Sync convenience methods (`get_run`, `steps`, `lineage`, ...) | Yes | No — async only |
| Best for | Production durability, multi-process resume, CLI inspection | Unit tests, short-lived scripts |

## Checkpointing vs the No-Checkpointer Re-Drive Pattern

You do not need a checkpointer to pause and resume a graph. Without one, each `run()` call replays the graph from the start, and you re-supply previously-collected interrupt responses by seeding them back into the input values:

```python
from hypergraph import Graph, node, AsyncRunner, interrupt

@node(output_name="draft")
def generate(prompt: str) -> str:
    return f"Draft: {prompt}"

@interrupt(output_name="feedback")
def review(draft: str) -> str | None:
    return None

@interrupt(output_name="final_draft")
def edit(feedback: str) -> str | None:
    return None

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
- [Batch Processing — Checkpointing with map()](../05-how-to/batch-processing.md#checkpointing-with-map) — parent/child batch runs
