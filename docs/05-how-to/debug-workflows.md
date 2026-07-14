# Debug Workflows

Four surfaces answer different questions about live control, current evidence,
and durable history.

| Tool | When to Use | Setup | Scope |
|------|-------------|-------|-------|
| **Background handle** | "Can I stop this live execution?" | Call `start_run()` / `start_map()` | Process-local, live work |
| **Inspect mode** | "Which item and node produced this value or failure?" | Pass `inspect=True`; call `.inspect()` after settlement | Process-local live notebook view and explicit settled display |
| **RunLog** | "What happened in this run?" | Zero — always on | In-process, current run |
| **Checkpointer** | "What happened yesterday?" | Pass to runner | Cross-process, persisted |

A handle intentionally does not duplicate status, failures, or logs. Retrieve
the settled `RunResult` / `MapResult`, then inspect the same result surfaces as
blocking execution:

```python
handle = runner.start_run(graph, values)
handle.stop(info={"reason": "user request"})
result = handle.result(raise_on_failure=False)

print(result.status)
print(result.log)
```

For an async runner, await `handle.result(...)`. See
[Control Work After It Starts](control-background-execution.md) for the live
control contract.

## Inspect One Current Execution

Suppose Maya is reviewing three customer checks and one fails. Before inspect
mode, she can find the failure, but the application has to correlate batch
status, original item indexes, logs, node timing, and values itself:

```python
# Before: assemble the debugging story from separate result surfaces.
batch = runner.map(
    customer_review,
    {
        "customer_id": ["alex-10", "maya-23", "sam-04"],
        "lifetime_value": [2400, 1200, 3100],
    },
    map_over=["customer_id", "lifetime_value"],
    error_handling="continue",
)

print(batch.summary())
for result in batch.failures:
    failure = result.failure
    if failure is not None:
        print(failure.item_index, failure.inputs, failure.error)
    else:
        print(result.error)
```

After, opt into successful-value capture and leave `batch.inspect()` as the
final notebook expression:

```python
# After: one explicit view joins the batch, items, timeline, values, and failure.
batch = runner.map(
    customer_review,
    {
        "customer_id": ["alex-10", "maya-23", "sam-04"],
        "lifetime_value": [2400, 1200, 3100],
    },
    map_over=["customer_id", "lifetime_value"],
    inspect=True,
    error_handling="continue",
)

batch.inspect()
```

`runner.run(...)` returns a `RunResult`; `runner.map(...)` returns a
`MapResult`. Both expose `.inspect()`:

```python
from hypergraph import AsyncRunner, SyncRunner

result = SyncRunner().run(graph, values, inspect=True)
result.inspect()

result = await AsyncRunner().run(graph, values, inspect=True)
result.inspect()
```

Calling `.inspect()` is explicit: it returns one display value and does not
emit a hidden notebook output. In a notebook, the returned value renders when
it is the final expression. In a script, assign or return it like any other
value. Rendering captured objects can still execute their bounded `repr`
fallback, as described under sensitivity below.

### Find a Mapped Failure by Original Index

Original map item indexes are evidence, not compact sequence positions. Search
the real failed children and compare `failure.item_index`:

```python
failed = next(
    result
    for result in batch.failures
    if result.failure is not None and result.failure.item_index == 1
)
failure = failed.failure
assert failure is not None

print(failure.inputs)
# {"customer_id": "maya-23", "lifetime_value": 1200}

batch.inspect()
```

Do not assume `batch[1]` means original item 1 after a stopped sparse map;
sequence positions contain only real claimed outcomes.

### Keep a Graph Input Named `inspect`

`inspect=` is a runner option and accepts only a boolean. Put a graph input
with the same name inside `values`:

```python
result = runner.run(
    graph,
    values={"inspect": "graph-owned"},
    inspect=True,
)

result.inspect()
```

### Use Inspection Without a Checkpointer

`inspect=True` does not require a checkpointer for the current execution. The
captured view belongs to the current Python process and result. Add a
`SqliteCheckpointer` only when you also need resume, fork, retry, restart, or
historical queries:

```python
# Current-process inspection: no database setup.
result = SyncRunner().run(graph, values, inspect=True)
result.inspect()
```

```python
# Persistence is explicit because this workflow must resume after restart.
from hypergraph.checkpointers import SqliteCheckpointer

runner = SyncRunner(checkpointer=SqliteCheckpointer("./runs.db"))
result = runner.run(graph, values, workflow_id="customer-review-42", inspect=True)
result.inspect()
```

On a resumed run, restored nodes show their real status and metadata, but they
do not reconstruct successful inputs or outputs that were never captured in
the current process. Freshly executed nodes can still show newly captured
values.

### Identify a Generated Background Run

When a checkpointer-backed `start_run()` omits `workflow_id`, Hypergraph
generates one. The generated workflow ID appears on the settled result and in
its inspection view; the handle stays control-only:

```python
handle = runner.start_run(graph, values, inspect=True)
result = handle.result(raise_on_failure=False)

print(result.workflow_id)  # "run-..."
result.inspect()
```

With `inspect=True`, the sync and async runners bind inspection to that ID
before restored state or node evidence is published. For an async handle,
await `handle.result(...)`. `start_map()` persistence still requires an
explicit `workflow_id`; omitting it gives the batch current-process inspection
only.

### Understand Degraded Views

`.inspect()` also works when the execution did not use `inspect=True`. It
builds an honest degraded view from always-on status, log, and failure facts.
Successful values say `not captured; rerun with inspect=True`; Hypergraph does
not guess from final outputs, defaults, or checkpoint rows.

Failed nodes can still show their always-on `FailureEvidence`, including the
resolved failure inputs. This is why a degraded failure can be more detailed
than a degraded success.

### Treat Captured Values and Saved Notebooks as Sensitive

Capture owns new top-level input/output mappings, but values inside those
mappings retain the same object identities as the running application. The
result therefore keeps references to those objects until the result is
collected. A large model, open client, token, or customer record can stay alive
longer than expected.

Notebook output contains the serialized captured values. Treat the notebook
as sensitive data before sharing or committing it. Serialization is bounded
per top-level value:

- depth 6
- 100 mapping items
- 200 sequence items
- 200 rows and 20 columns for tables
- 20,000 characters of captured text

The per-container limits sit inside a global per-top-level value work budget.
The 20,000-character ceiling is also aggregate across captured strings and
JSON number text in that value, rather than restarting for every nested leaf.
When either global budget is exhausted, the affected leaf says
`serialization budget exhausted` and its ancestors are marked truncated. A
serialization failure or raised `repr()` exception likewise becomes a bounded
typed placeholder and does not replace the run status.

Hypergraph stores captured node inputs, outputs, and requested map inputs in a
private `CapturedMapping` snapshot adapter. This shallow snapshot owns its
top-level mapping while contained values retain their identities. It supports
`copy.copy`, `copy.deepcopy`, `dataclasses.asdict`, and pickle round trips, so
adding `inspect=True` does not make an otherwise copyable result fail. Captured
mappings are not stored as `MappingProxyType`.

That private snapshot storage is separate from source-value rendering.
Structured inspection deliberately recognizes only exact inert containers and
safe concrete adapters: exact built-in `dict`, exact built-in `list`, exact
built-in `tuple`, ordinary dataclasses, recognized Pydantic models, exact NumPy
`ndarray`, exact pandas `DataFrame`, and a user-supplied `MappingProxyType`
backed by an exact `dict`.

Exact built-in `bytes` and `bytearray` values use a separate bounded scalar
path. Hypergraph reads only a prefix that can fit in the 20,000-character
preview, reports the exact original byte count, and marks the preview truncated
when bytes remain. It never creates the whole binary `repr` before applying the
limit, so temporary inspection memory stays bounded as the source grows.

- **Before:** inspecting a 5.12 MB payload first created a multi-megabyte Python
  `repr`, and the displayed original size counted repr characters rather than
  source bytes.
- **After:** the preview remains at most 20,000 characters, reports
  `5,120,000 bytes`, and is explicitly truncated without a whole-value `repr`.

For the NumPy, pandas, and Pydantic adapters, trust comes from canonical class
provenance rather than mutable public aliases. Reassigning public names such as
`numpy.ndarray`, `pandas.DataFrame`, or `pydantic.BaseModel` therefore cannot
make an unrelated custom class trusted.

Within the documented rank and size limits, exact NumPy arrays with canonical
NumPy 1.x and 2.x `ndarray` provenance stay structured. The package's optional
`examples` dependency range currently permits `numpy>=1.21.0` and
`pandas>=1.3.0`. Those ranges describe installation compatibility; they do not
promise that Hypergraph will traverse every pandas internal layout.

An exact pandas `DataFrame` is structured only when it has a recognized
trusted NumPy-backed internal storage layout—the standard NumPy-backed storage
Hypergraph knows how to inspect. An allowed pandas version with an
unrecognized internal storage layout becomes a bounded
`unsupported DataFrame storage` placeholder without calling DataFrame `repr`.
An ExtensionArray-backed DataFrame—one whose data blocks, row axis, or column
axis use extension storage—gets the narrower
`unsupported extension-backed DataFrame` result, also without invoking
extension hooks. This is an
implementation safety boundary, not an all-version guarantee. DataFrame
`repr` delegates to extension hooks, so both storage placeholders bypass it.

- **Before:** an unfamiliar internal pandas layout could be described as
  extension-backed even when Hypergraph had not proved that.
- **After:** unrecognized storage says `unsupported DataFrame storage`; only
  proven ExtensionArray-backed data or axes receive the narrower placeholder.

For unsupported subclasses and custom protocols—including `bytes` and
`bytearray` subclasses, plus objects that advertise mapping, sequence, model,
array, or DataFrame hooks—Hypergraph uses one whole-value bounded `repr`
fallback for each rendered occurrence and does not call their advertised
traversal hooks. A proxy backed by a custom mapping uses the same whole-value
bounded `repr` fallback instead of traversing that mapping.

`repr` is Python user code. Hypergraph cannot prevent or undo its side effects,
and the same object can reach the fallback during multiple live snapshots. Any
raised `repr` exceptions become bounded typed placeholders and do not replace
the run status or exception evidence.

Sparse row tables use the bounded union of keys across captured rows instead
of treating the first row as the whole schema:

```python
rows = [{}, {"customer_id": "maya-23", "risk": 0.9}]
```

- **Before:** an empty first row could make the inspection look like a
  two-row, zero-column table and hide Maya's values.
- **After:** the view has `customer_id` and `risk` columns; the first row has
  explicit `missing table cell` placeholders and the second row shows the
  captured values.

The displayed table still stops at 20 columns. Its source column count is
exact when every source row was captured and each row could be fully scanned
within the 20-key-per-row safety cap, and its keys are safely comparable
without executing user code. Otherwise the count is a proven lower bound: for
example, `2 × ≥21 table`. The view marks those columns truncated instead of
presenting `21` as an exact count that Hypergraph did not prove.

Truncated values report their original size or proven lower bound when it can
be determined. Counts above JavaScript's safe integer range cross the notebook
boundary as exact decimal text instead of being rounded by the browser.

Set `HYPERGRAPH_DISPLAY=plain` to suppress automatic notebook display while
keeping capture and explicit `.inspect()` available:

```bash
HYPERGRAPH_DISPLAY=plain uv run python my_workflow.py
```

### Read Live, Saved, and Graph Views Correctly

In a supported notebook, `inspect=True` opens one live view and updates its
payload as work advances. The terminal output becomes a saved snapshot. After
the notebook is saved, trusted active output remains locally interactive
without a kernel, Hypergraph server, or network connection; it is labelled as
saved, not live. An untrusted notebook may remove active HTML. In that state,
the terminal record keeps a smaller native summary rather than claiming the
full inspector can run.

The normal notebook path keeps exactly two physical outputs: one immutable
shell and one mutable payload channel. Ordinary updates stay payload-only. At
terminal or stale settlement, the channel becomes a self-contained portable
inspector. A shared notebook hides the portable fallback only after the
original iframe accepts and applies the authenticated update, preserving that
iframe's selected tab and other local UI state. An isolated-output renderer
can instead open that terminal channel by itself. The normal path still has
two physical outputs because
`DisplayHandle.update()` replaces the mutable channel in place.

Notebook trust is host policy, not Hypergraph widget state. Hypergraph never
auto-trusts or signs a notebook, calls a server trust endpoint, or weakens the
iframe sandbox. If a host strips scripts, styles, iframes, and output
identifiers, a terminal or stale channel still leaves one plain semantic
summary. It uses native `<details>` and contains:

- saved/stale delivery plus exact execution status and counts
- `First failure of N`, the original map item, and the qualified node
- bounded captured inputs and exception evidence: exact only for a complete
  safe payload, **Exception preview (bounded repr)** for a representation, or
  **Exception details unavailable** with its reason; an opaque repr is prefixed
  with its exception type once, while a repr already beginning with that type
  is not duplicated; truncated previews include the original character count
- copy-faithful input and exception whitespace using valid `<pre><code>`
  nesting, with copy-inert wrap opportunities so an unbroken 20,000-character
  value fits a 360px page
- a short `RunResult` / `MapResult` evidence snippet that reruns with
  `error_handling="continue"` before reading a result
- the canonical guide path: `docs/05-how-to/debug-workflows.md`

The compact summary shows the first failure and says how many failures exist;
it does not imply that one displayed failure is the whole batch. Its count uses
top-level failure evidence when available and otherwise counts embedded node,
run-boundary, or status-only failures once, without counting the same failure at
two levels. When active HTML is trusted, the portable iframe hides this small
summary only if it retains a non-empty local `srcdoc`; an empty iframe tag is
not treated as available. In a shared document, the complete terminal fallback
still hides only after the original iframe accepts the exact authenticated
update.

A complete safe exception uses **Exact exception** for attributable node
evidence. Infrastructure failures stay at their real boundary as **Exact run
exception** or **Exact batch exception**; Hypergraph does not borrow a nearby
node name or inputs. Repr-backed evidence instead uses **Exception preview
(bounded repr)**, and a truncated preview includes its original character
count. A placeholder uses **Exception details unavailable** and explains why.
The full inspector and the trust-safe native summary follow the same labels.

Recovery code follows the captured runner kind. Sync snippets call
`runner.run(...)` or `runner.map(...)` directly; async snippets use
`await runner.run(...)` or `await runner.map(...)`. If the runner kind was not
captured, the summary says recovery code is unavailable instead of guessing
sync. Each retry assignment is inside `try`/`except` and uses
`error_handling="continue"`. A persistent infrastructure exception therefore
prints its real type and message without reading an unbound result. In the
`else` branch, a transient recovery prints the settled successful result or
batch; a returned failed result prints its real run or item error. Map snippets
read `batch.failures` or the original item position and never read a nonexistent
`MapResult.error`. Node, run-boundary, batch-boundary, and start-failure views
follow this same provenance policy in both the full inspector and native
summary.

For a nested mapped graph, **Show failure** correlates the containing outer item
to the explicit slash-qualified failing leaf. The selected execution, heading,
input, exception, and recovery evidence therefore show
`review_group/review_customer` plus the failing scalar input—not the aggregate
`review_group` container and its list input. An explicit failure with no stable
node-identity match keeps its own name, inputs, and error instead of borrowing a
same-name node's qualified path. Correlation is established from raw Python
evidence before error or input presentation is serialized, then carried by an
opaque internal occurrence identity. Changing `repr()` output is never
identity, and the internal identity contains no object address, input value, or
secret. If the exact slash-qualified leaf is absent, **Show failure** stays at
the run boundary instead of selecting the aggregate container.

Before (misleading async recovery):

```text
Exact exception
PaymentDeclined: <redacted>
result = runner.run(graph, values)
```

After (truthful async recovery):

```text
Exception preview (bounded repr)
PaymentDeclined: <redacted>
```

```python
result = await runner.run(
    graph,
    values,
    inspect=True,
    error_handling="continue",
)
print(result.failure)
```

Here, `runner` is an `AsyncRunner`; `graph` and `values` are the same graph and
inputs used for the failed execution. A sync inspection shows the same call
without `await`.

Before (a transient recovery succeeds):

```python
failure = result.failure
print(failure.inputs)  # AttributeError: failure is None
```

After (the copied snippet remains truthful):

```python
failure = result.failure
if failure is None:
    print(result)
else:
    print(failure.inputs)
    print(failure.error)
```

Before (a boundary retry can raise or print `None`):

```python
result = runner.run(graph, values, inspect=True, error_handling="continue")
print(result.error)
```

After (the assignment and evidence read are both guarded):

```python
try:
    result = runner.run(
        graph,
        values,
        inspect=True,
        error_handling="continue",
    )
except Exception as error:
    print(f"{type(error).__name__}: {error}")
else:
    if result.error is None:
        print(result)
    else:
        print(f"{type(result.error).__name__}: {result.error}")
```

```text
Before (untrusted output): Python says partial / 2 completed / 1 failed,
                           but Jupyter can leave a blank terminal record.

After (still untrusted):   Saved snapshot / partial / 2 / 1 remains visible.
                           Expand Item 1 failure to read score_customer,
                           customer_id=maya-23, and the exact ValueError.

After normal host trust:   The same saved record opens the full offline
                           inspector; no kernel or server is required.
```

Hypergraph also has a best-effort compatibility path for the measured
`jupyter-server-nbmodel==0.1.1a4` executor, which persists ordinary
`display_data` but drops `update_display_data`. When the **kernel environment**
reports that exact package version, ordinary coalesced updates are appended as
payload-only records at the existing four-per-second bound. The notebook
therefore retains hidden payload-only history. Terminal or stale settlement
adds one terminal physical record containing the same portable inspector. It
is hidden only after the original iframe accepts the update in a shared Jupyter
document, but remains visible and interactive when a host isolates each saved
output record. Terminal and error states can still flush immediately.

- **Before on that executor:** Python reaches the terminal result while the
  iframe can remain at `pending`, `0 completed`, `0 failed`.
- **After when detected:** the iframe reaches `partial`, `2 completed`,
  `1 failed`, and the saved output retains the exact failed input.

- **Before in an isolated saved-output host:** the first shell can remain at
  `pending`, `0 completed`, `0 failed` because later payload scripts cannot
  reach sibling output documents.
- **After:** the terminal channel alone opens the saved inspector at `partial`,
  `2 completed`, `1 failed`; **Show failure** reveals `maya-23` and
  `Customer maya-23 requires manual review` without a kernel or sibling DOM.

If that terminal record is untrusted, the full **Show failure** interface is
not available because scripts and iframes are host-blocked. Expand its native
**Item 1 failure** disclosure instead; it preserves the same original item,
qualified node, bounded input, exact error, code evidence, and docs reference.

Detection is exact and kernel-local. A missing or different package version
uses the normal display-handle update path. A separate server environment
cannot be inferred from package metadata in the kernel: if the broken package
is installed only on the server, Hypergraph does not claim to detect that
split-environment case.

For Maya, the before/after is explicit: before the renderer is ready,
`Waiting for live inspection` means the payload channel is not authenticated
or live yet. After a ready-handshake timeout, the label becomes
`Live inspection unavailable`; the latest state remains locally inspectable as
a saved snapshot instead of silently freezing a view that still claims to be
live.

The Inspect **Graph** tab shows executed slash-qualified paths such as
`worker/parser/parse_order`. It answers "what executed?" and preserves nested
execution identity. Use `graph.visualize()` when you need the full configured
topology, including paths that did not execute.

The checked-in reference proves this exact state:

| Original item | Result | Evidence |
|---|---|---|
| 0 | completed | `review_action="approve"` |
| 1 | failed | `score_customer` received `customer_id="maya-23"` and raised `ValueError` |
| 2 | completed | `review_action="approve"` |

For the complete runnable scenario, see
[`examples/inspect_mode.py`](../../examples/inspect_mode.py). GitBook and
GitHub show the [generated HTML reference](../../examples/inspect-mode-reference.html)
as source; download that file and open it locally for the interactive failure
drill-down.

## RunLog — Always-On Run Trace

Every `runner.run()` call returns a `RunResult` with a `.log` attribute.
`runner.map()` returns a `MapResult` with a batch log and per-item
`RunResult.log` values. These always-on traces require zero configuration.

### Quick Start

```python
from hypergraph import Graph, SyncRunner, node

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3

graph = Graph([double, triple], name="graph")
runner = SyncRunner()
result = runner.run(graph, {"x": 5})

# RunLog is always available
print(result.log)
```

Output:

```
RunLog: graph | 4ms | 2 nodes | 0 errors

  Step            Node              Duration          Status
────────────────  ────────────────  ────────────────  ────────────────
     0            double            3ms               completed
     1            triple            0ms               completed
```

### Finding Slow Nodes

```python
# result.log.steps is a tuple[NodeRecord, ...] in execution order
# Sort by duration (slowest first)
for record in sorted(result.log.steps, key=lambda r: r.duration_ms, reverse=True):
    print(f"{record.node_name}: {record.duration_ms:.1f}ms")

# Per-node aggregates (useful for map operations)
for name, stats in result.log.node_stats.items():
    print(f"{name}: avg={stats.avg_ms:.1f}ms, count={stats.count}")
```

`result.log.steps` yields `NodeRecord` — one per node execution, with `node_name`, `superstep`, `duration_ms`, `status`, `decision`, `error`. `result.log.node_stats` maps node name to `NodeStats` — aggregate `avg_ms`/`count` across all executions of that node in the run. `result.log.errors` is the subset of `.steps` where `status == "failed"`.

### Finding Errors

```python
result = runner.run(graph, {"x": 5}, error_handling="continue")

# All errors
for record in result.log.errors:
    print(f"{record.node_name}: {record.error}")

# Summary
print(result.log.summary())
# "2 nodes | 21ms | 0 errors | slowest: double (16ms)"
```

### Routing Decisions

```python
# Which path did each gate take?
for record in result.log.steps:
    if record.decision:
        print(f"{record.node_name} → {record.decision}")
```

### Serialization

```python
# Export for logging, dashboards, etc.
log_dict = result.log.to_dict()
# {"graph_name": "...", "run_id": "...", "total_duration_ms": 42.5,
#  "steps": [...], "node_stats": {...}}
```

### RunLog with map()

`runner.map()` returns a `MapResult` with a batch-level summary and per-item RunLogs:

```python
results = runner.map(graph, {"x": [1, 2, 3]}, map_over="x")

# Batch-level overview
print(results.summary())  # "3 items | 3 completed | avg 4ms/item"

# Per-item RunLogs
for i, r in enumerate(results):
    print(f"Item {i}: {r.log.summary()}")

# Failed items
if results.failed:
    for f in results.failures:
        print(f"Failed: {f.error}")
```

`results.log` also returns a single batch-level `MapLog` (`graph_name`, `total_duration_ms`, `items` — a tuple of the per-item `RunLog`s, plus aggregate `.errors` and `restored_count`), for when you want one object instead of iterating `results` yourself. A checkpoint-skipped item has `RunResult.restored=True` and a visible synthetic `NodeRecord(status="restored")`; terminal and HTML views show it as restored, never as failed or as fake `0ms` work.

When stop curtails a background map, `results.log.items` covers only real
claimed outcomes. Compare `len(results)` with `results.requested_count` and
read `results.unstarted_item_indexes` for inputs that never ran; those inputs
have no synthetic node record, run log, or child run ID.

### runner.map() vs map_over for debugging

The batch pattern you choose affects what debugging data is available:

| | `runner.map()` | `map_over` |
|---|---|---|
| **RunLog granularity** | Per-item RunLogs with full traces | One RunLog (batch = one step) |
| **Error isolation** | Each item independent | One failure affects entire step |
| **Checkpointing** | Parent + per-item child runs | Persisted as one run step |
| **Checkpointer drill-down** | `cp.runs(parent_run_id="<batch-id>")` | `cp.values("<id>")` |

Both patterns now support checkpointing. `runner.map()` creates a hierarchical structure (parent batch run + child runs per item), while `map_over` records the batch as a single step within the parent run.

## Checkpointer — Persistent Run History

The Checkpointer persists every step to a database, enabling cross-process inspection, crash recovery, and time-travel debugging.

A persisted `ACTIVE` status is execution history, not proof that a Python
worker or background handle is still alive. Handles cannot be looked up or
reconnected after process loss. A recovery process opens the checkpointer and
starts a new execution under the existing resume rules.

### Quick Start

```bash
pip install 'hypergraph[checkpoint]'
```

```python
from hypergraph import AsyncRunner, Graph, node
from hypergraph.checkpointers import SqliteCheckpointer

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3

graph = Graph([double, triple])

# Create a checkpointer (SQLite database)
checkpointer = SqliteCheckpointer("./runs.db")
runner = AsyncRunner(checkpointer=checkpointer)

# Run with a workflow_id to enable persistence
result = await runner.run(graph, {"x": 5}, workflow_id="my-run-1")
```

Every step is now persisted. You can inspect it from another process or another day.

Both `SyncRunner` and `AsyncRunner` support checkpointing:

```python
from hypergraph import SyncRunner
from hypergraph.checkpointers import SqliteCheckpointer

cp = SqliteCheckpointer("./runs.db")
runner = SyncRunner(checkpointer=cp)
result = runner.run(graph, {"x": 5}, workflow_id="my-run-1")
```

`SyncRunner` writes steps synchronously via the `SyncCheckpointerProtocol`. `SqliteCheckpointer` implements this out of the box.

### Inspecting Persisted State

```python
# From any process that can access the DB file:
from hypergraph.checkpointers import SqliteCheckpointer, WorkflowStatus

cp = SqliteCheckpointer("./runs.db")

# Sync reads — no await needed, works from any context
cp.runs()                             # List all runs
cp.get_run("my-run-1")                # Run metadata (status, duration, counts)
cp.values("my-run-1")                 # {"doubled": 10, "tripled": 30}
cp.steps("my-run-1")                  # Step records with timing
cp.steps("my-run-1", show_internal=True)  # Include retention carriers
cp.stats("my-run-1")                  # Per-node duration/frequency breakdown
cp.checkpoint("my-run-1")             # Full snapshot (values + steps)

# Filter by status, graph, or time
cp.runs(status=WorkflowStatus.FAILED)
cp.runs(graph_name="my_graph", since=datetime(2024, 1, 1))

# Full-text search across step records
cp.search("generate")                 # Match node names and errors

# Time travel: state at a specific superstep
cp.state("my-run-1", superstep=1)     # {"doubled": 10}

# Git-like fork visualization (lanes + expandable step traces)
cp.lineage("my-run-1")
```

The sync read methods (`runs()`, `get_run()`, `values()`, `steps()`, `search()`, `stats()`, `checkpoint()`) work without async/await, making them ideal for debugging scripts and notebooks. No `initialize()` call needed. Public step/checkpoint/search/statistics views hide `__retained_state__` / `RetentionBaseline` compaction carriers by default; state reconstruction still folds those internal rows. Use `show_internal=True` only to inspect retention internals.

#### Interrupt Steps

For workflows with `@interrupt` nodes, the step log shows the pause/resume cycle. Each interrupt appears twice: first as `paused` (waiting for input), then as `completed` (resolved with the provided value):

```python
steps = cp.steps("my-chat")
for s in steps:
    print(f"  ss={s.superstep}  {s.node_name:20s}  {s.status}")

#   ss=0   add_user_message      completed
#   ss=1   llm_reply             completed
#   ss=2   add_response          completed
#   ss=3   should_continue       completed    (decision: wait_for_user)
#   ss=4   wait_for_user         paused
#   ss=5   wait_for_user         completed    (values: {user_input: "..."})
```

Routing decisions are stored on the step record (`s.decision`), and resolved interrupt values appear in `s.values`. See [Human-in-the-Loop](../03-patterns/07-human-in-the-loop.md#inspecting-checkpoint-history) for the full pattern.

For branching semantics, use explicit checkpoints:
`checkpoint = cp.checkpoint("workflow-id", superstep=...)` and pass it to
`runner.run(..., checkpoint=checkpoint, workflow_id="new-id")`.

### Durability Modes

The `CheckpointPolicy` controls when steps are written to the database:

```python
from hypergraph.checkpointers import CheckpointPolicy, SqliteCheckpointer

# Default: async — steps saved in background tasks (fastest)
cp = SqliteCheckpointer("./runs.db")

# Sync: steps saved immediately after each superstep (safest)
cp = SqliteCheckpointer(
    "./runs.db",
    policy=CheckpointPolicy(durability="sync"),
)

# Exit: steps buffered, flushed once after the run (lowest overhead)
cp = SqliteCheckpointer(
    "./runs.db",
    policy=CheckpointPolicy(durability="exit", retention="latest"),
)
```

| Mode | Behavior | Crash Safety | Performance |
|------|----------|-------------|-------------|
| `async` | Background save tasks | Most steps saved | Good (default) |
| `sync` | Await each save | Full | Slight overhead |
| `exit` | Buffer, flush at end | None during run | Best |

### Without workflow_id

For a fresh `runner.run()` (or `retry_from=`), if a checkpointer is configured and you omit `workflow_id`, hypergraph generates a generic `run-...` ID and persists the run. `fork_from=` instead derives `{source}-fork-{hex}`; an explicit target remains exact.

```python
# Auto-generated workflow_id (when checkpointer exists)
result = await runner.run(graph, {"x": 5})
print(result.workflow_id)  # e.g. run-20260302-a7b3c2

# Explicit workflow_id
result = await runner.run(graph, {"x": 5}, workflow_id="my-run-1")
```

Paused interrupt-driven workflows are now persisted as **`PAUSED`** runs rather than overloading `ACTIVE`. In the notebook explorer, that means:

- `active` means currently executing and non-terminal
- `paused` means waiting for interrupt input, resumable, and also non-terminal
- `completed` and `failed` remain terminal

`runner.map()` still requires an explicit `workflow_id` to persist parent/child batch runs.

### Hierarchical Checkpointing

When you use nested graphs or `runner.map()` with a `workflow_id`, hypergraph automatically creates a parent-child run hierarchy. This lets you drill into specific sub-runs without losing the big picture.

#### Nested Graphs

A `GraphNode` step automatically creates a child run with the ID `{workflow_id}/{node_name}`:

```python
from hypergraph import Graph, SyncRunner, node
from hypergraph.checkpointers import SqliteCheckpointer

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3

inner = Graph([double], name="inner")
outer = Graph([inner.as_node(name="embed"), triple], name="outer")

cp = SqliteCheckpointer("./runs.db")
runner = SyncRunner(checkpointer=cp)
result = runner.run(outer, {"x": 5}, workflow_id="pipeline-001")

# Two runs are created:
#   pipeline-001       (parent — outer graph)
#   pipeline-001/embed (child — inner graph)

# The parent's step record for "embed" includes a child_run_id
steps = cp.steps("pipeline-001")
embed_step = [s for s in steps if s.node_name == "embed"][0]
print(embed_step.child_run_id)  # "pipeline-001/embed"

# Drill into the child run
cp.values("pipeline-001/embed")  # {"doubled": 10}
```

#### Batch Runs with map()

`runner.map()` creates a parent batch run and per-item child runs with IDs `{workflow_id}/{index}`:

```python
results = runner.map(
    graph,
    {"x": [1, 2, 3]},
    map_over="x",
    workflow_id="batch-001",
)
# Creates:
#   batch-001    (parent batch run)
#   batch-001/0  (child — x=1)
#   batch-001/1  (child — x=2)
#   batch-001/2  (child — x=3)
```

#### Querying the Hierarchy

```python
# Top-level runs only
cp.runs(parent_run_id=None)

# Children of a specific run
cp.runs(parent_run_id="batch-001")

# All runs (including children)
cp.runs()
```

> **Note**: `cp.runs()` returns all runs (including children) by default.
> Pass `parent_run_id=None` to see top-level runs only.

## Choosing the Right Tool

| Question | Tool |
|----------|------|
| "Which current item and node produced this value?" | Inspect view (`inspect=True`, then `.inspect()`) |
| "Can I inspect this result if capture was off?" | Degraded inspect view (`result.inspect()`) |
| "What happened in the run I just finished?" | RunLog (`result.log`) |
| "Which node was slowest?" | RunLog (`sorted(result.log.steps, key=...)`) or Checkpointer (`cp.stats(...)`) |
| "What happened in yesterday's run?" | Checkpointer (`cp.runs()`, `cp.steps(...)`) |
| "What values were produced at step 3?" | Checkpointer (`cp.state("<id>", superstep=3)`) |
| "What failed runs exist?" | Checkpointer (`cp.runs(status=WorkflowStatus.FAILED)`) |
| "What paused runs can I resume?" | Checkpointer (`cp.runs(status=WorkflowStatus.PAUSED)`) |
| "Can this process stop the work that is live now?" | Background handle (`handle.stop(...)`) |
| "What did the nested graph produce?" | Checkpointer (`cp.values("<parent-id>/<node-name>")`) |
| "What happened in batch item 5?" | Checkpointer (`cp.steps("<batch-id>/5")`) |
| "Find all steps that hit a specific error" | Checkpointer (`cp.search("error message")`) |
| "I need to inspect from another process" | Checkpointer sync reads |
| "I need JSON for my monitoring system" | RunLog (`to_dict()`) |

## See Also

- [Observe Execution](observe-execution.md) — Event processors (Rich progress, custom metrics)
- [Events API Reference](../06-api-reference/events.md) — Event type definitions
