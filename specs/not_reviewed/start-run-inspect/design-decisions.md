# Design Decisions

The current spec, distilled. Not a transcript log.

## 1. User-facing API

### Background execution (the new primary surface)

```python
from hypergraph.runners import SyncRunner, AsyncRunner

# sync
runner = SyncRunner()
run = runner.start_run(graph, {"x": 5}, inspect=True)
result = run.result()                       # blocks, RunResult

# async
runner = AsyncRunner()
run = runner.start_run(graph, {"x": 5}, inspect=True)
result = await run.result()                 # awaits, RunResult
```

```python
batch = runner.start_map(graph, {"x": [1, 2, 3]}, map_over="x", inspect=True)
results = batch.result()                    # MapResult
```

The blocking `run()` / `map()` stay as boring convenience wrappers. No flag changes their return type. `start_run()` always returns a handle; `start_map()` always returns a batch handle. `run()` returns `RunResult`, `map()` returns `MapResult`.

### Handle surface

Every handle exposes the same shape. Sync and async only differ in whether `result()` / `wait()` are coroutines.

```python
run.status              # "running" | RunStatus enum after done
run.failure             # FailureCase | None  (single-run handles)
run.failures            # list[FailureCase]   (map handles)
run.view()              # RunView (live or terminal)
run.inspect()           # alias for view() — renderer hook
run.stop(info=...)      # cooperative stop (requires stop_callback)
run.result(raise_on_failure=True)   # block/await, returns Result
run.wait()              # low-level: completion only, no payload, no raise
```

`wait()` is intentionally low-level and stays out of the main examples. Default to `result(raise_on_failure=False)` when you want to inspect without raising.

### `inspect=True` is the only display knob

```python
result = runner.run(graph, values, inspect=True)   # blocking — captures snapshots
run = runner.start_run(graph, values, inspect=True)  # background — also auto-shows widget
```

Decision: `inspect=True` is one knob, not two. Earlier drafts had a separate `capture_values=True` for capturing intermediate outputs without showing the widget. That was rejected — if the user asks for inspection, Hypergraph captures whatever the inspect surface needs. No `capture_values` parameter ships.

### `inspect()` reuses one renderer

```python
view = result.inspect()              # RunView
view["classify"].outputs             # raw dict (only with inspect=True)
view["classify"].inputs              # always present for the failed node
view.failures                        # list[FailureCase]
```

Reuse contract:
- `view()` returns the single structured artifact (`RunView`)
- `inspect()` is the single renderer over that artifact
- `inspect=True` means Hypergraph auto-calls that renderer during execution
- `result.inspect()` later renders the same inspector from the saved snapshot

There is one inspect data model and one inspect renderer. Not a separate live-only widget plus a separate post-run failure UI.

### `FailureCase` is always captured

```python
@dataclass(frozen=True)
class FailureCase:
    node_name: str          # fully qualified for nested ("inner.embed")
    error: BaseException
    inputs: dict[str, Any]  # exact resolved inputs to the failed node
    superstep: int
    duration_ms: float
    started_at_ms: float | None = None
    ended_at_ms: float | None = None
    item_index: int | None = None   # populated by map() template
```

No flag is needed. Always available on:
- `RunResult.failure` (continue mode)
- `ExecutionError.failure_case` (raise mode)
- `MapHandle.failures[i]` (item index injected from the batch position)

The user said `node_path` -> `node_name` explicitly. The field still represents a fully qualified nested name when needed.

### Error matrix

| `error_handling` | `inspect` | Outcome on failure |
|---|---|---|
| raise | False | `ExecutionError` with `.failure_case` |
| raise | True | `ExecutionError` with `.failure_case` + `.inspect_data`; widget stays visible in failed state |
| continue | False | `RunResult` with `.failure` populated |
| continue | True | `RunResult` with `.failure` + `._inspect_data`; widget settles to failed |

### Checkpointing stays orthogonal

```python
checkpointer = SqliteCheckpointer("runs.db")
runner = AsyncRunner(checkpointer=checkpointer)

run   = runner.start_run(graph, values, workflow_id="job-123")
retry = runner.start_run(retry_graph, retry_from="job-123")
fork  = runner.start_run(graph, new_values, fork_from="job-123")
```

Hard rule: `workflow_id`, `retry_from`, `fork_from` require a checkpointer. Without one, Hypergraph errors. This was tightened mid-session 4 — earlier drafts allowed `workflow_id` as an in-process control label, which conflated control identity with durable lineage. Per-run stop is now done via the handle (`run.stop(...)`); by-id stop stays as a secondary surface.

## 2. Internal architecture

### `inspect.py` — data model + live state

- `FailureCase`, `NodeSnapshot`, `NodeView`, `RunView` — the shared artifact set
- `InspectCollector` — accumulates `NodeSnapshot`s during execution
- `LiveInspectState` — thread-safe shared state owned by the run; collects running nodes, snapshots, failure; produces a `RunView` on demand via `.view()`
- `build_run_view(result)` — terminal `RunView` from a completed `RunResult` (merges `result.log.steps` with `_inspect_data` snapshots)
- `build_live_run_view(...)` — live `RunView` from `LiveInspectState`
- `InspectWidget` — single-run notebook display via `IPython.display` + `display_id`, refreshes through `LiveInspectState`
- `MapInspectWidget` — batch-level notebook display

### `handles.py` — four handle classes

- `SyncRunHandle` wraps `concurrent.futures.Future[RunResult]`
- `AsyncRunHandle` wraps `asyncio.Task[RunResult]`
- `SyncMapHandle` / `AsyncMapHandle` — same pattern around `MapResult`

Each handle holds:
- the future / task
- `stop_callback: StopSignal.set | None` (set when the run is stoppable)
- `live_state: LiveInspectState | None` (set when `inspect=True`) for run handles
- `inspect_widget: MapInspectWidget | None` for map handles

`view()` delegates to `live_state.view()` while running, then to `result.view()` once done. `stop()` raises if no stop callback was wired.

### `inspect_html.py` — single HTML renderer

Two entry points: `render_inspect_widget(view)` for single runs, `render_map_inspect_widget(...)` for batches. Both produce a self-contained HTML fragment. `RunView._repr_html_` calls `render_inspect_widget(self)` so a `RunView` displays directly in notebooks.

### Wiring into the existing runner machinery

- `ExecutionContext` gains `on_node_snapshot: Callable | None = None` (mirrors `on_inner_log`).
- Sync/async `superstep.py` invoke that callback after successful execution and build `FailureCase` at the except site (where `inputs` is in scope).
- `template_sync.py` / `template_async.py` accept `inspect=True`, instantiate `LiveInspectState`, wire `on_node_snapshot=state.record_snapshot`, attach snapshots to `RunResult._inspect_data` on success or to `ExecutionError.inspect_data` on raise.
- `start_run()` / `start_map()` are thin wrappers: pop `error_handling`, force `"continue"` internally so the handle always has a result to read, pass everything else through, spin up the thread / task.
- `inspect` is a reserved runner option name.

### Sync/async parity points

| Surface | Sync | Async |
|---|---|---|
| `start_run` execution | `threading.Thread` + `concurrent.futures.Future` | `asyncio.Task` on current loop |
| `handle.result()` | blocking `future.result()` | `await task` |
| `handle.wait()` | `future.result()` (drops payload) | `await task` |
| `handle.view()` while running | `live_state.view()` | `live_state.view()` |
| `handle.stop()` | `stop_signal.set(info=...)` | same |

Same `LiveInspectState`, same `RunView`, same renderer. The only divergence is the concurrency primitive.

## 3. Widget UX

### Three-panel composition

References live in `worktrees/9444/hypergraph/tmp/run-viewer/`:

1. **Timeline / waterfall** — primary live surface. From `GanttPanel.tsx`, `WaterfallChart.tsx`, `tmp/waterfall-demo.html`. Shows progress, failure location, nested run rows, map item rows.
2. **Output / failure detail panel** — selected node or item shows outputs, live chunks, failure inputs/error. From `IntermediateResults.tsx`, `OutputViewer.tsx`, `JsonTree.tsx`.
3. **Graph topology panel** — reuses Hypergraph's existing graph viz (`viz/assets/viz.js`, `viz/html/generator.py`, `viz/widget.py`). Optional secondary tab. Do not rebuild topology.

First layout: timeline left/top, detail panel right, topology as a secondary tab.

### Notebook transport

`anywidget` for live: Python owns the `RunView`, the widget model sends small serialized diffs, the frontend rerenders from state. Avoids `display(HTML(...))` churn on every superstep. Plain HTML fallback for non-live environments. `ipywidgets` is already a dep; `anywidget` would be new. Bundle the viewer assets the same way `viz/html/generator.py` does.

### Decisions on what to render

- The widget stays visible after failure, stop, or pause. It does not disappear like a progress bar.
- Settled states are explicit: `completed`, `failed`, `partial`, `stopped`, `paused`.
- Cached nodes show as `cached`, not `completed`.
- Earlier successful nodes still show their outputs even when a later node fails.
- `result.inspect()` after the fact renders the exact same view from the saved snapshot — not a separate "post-mortem" UI.
- Map widgets show batch row + per-item progress + failed items as visible failed rows (do not collapse into a traceback).

### Status enum

```python
class RunHandleStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    PARTIAL = "partial"
```

## 4. Non-negotiables

- `inspect=True` is the only display knob. No second `capture_values` parameter.
- One inspect data model (`RunView` / `FailureCase`), one renderer. `inspect=True`, `result.inspect()`, and `run.inspect()` all render from it.
- `FailureCase` is always captured, regardless of `inspect`.
- Sync and async users see the same surface. Implementation strategy may differ, public shape does not.
- Nested graphs and nested maps are first-class everywhere — fully qualified `node_name`, item drill-down from the same view.
- `workflow_id` requires a checkpointer or errors. No silent in-process pseudo-persistence.
- Reuse over rebuild: graph topology comes from the existing viz; timeline/output ideas port from `tmp/run-viewer`; live notebook transport mirrors `rich_progress.py`.
- "Cleanliness over effort." Solo project, no backwards compat constraint.

## 5. Out of scope (for the first cut)

- TTL enforcement on checkpointers
- Auto-provisioned temp SQLite in notebooks
- Replay affordances on `FailureCase` (`case.repro_code`, `case.reproduce()`)
- Retry/fork buttons inside the widget — checkpoint-aware actions come later
- Full React/Vite app shell from `tmp/run-viewer`; port the ideas, not the shell
