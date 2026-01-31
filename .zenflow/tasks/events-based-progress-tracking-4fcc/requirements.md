# Events-Based Progress Tracking - Requirements

## Overview

Add an event system to hypergraph and implement a hierarchical Rich progress bar as the first event consumer. Events are emitted during graph execution (run/map) and consumed by pluggable `EventProcessor` instances.

## Goals

1. **Event layer**: Runners emit structured events at key execution points
2. **Progress bar**: A `RichProgressProcessor` renders real-time progress using Rich
3. **Extensible**: The `EventProcessor` interface supports future consumers (OTel, logging, custom)

## Non-Goals (Deferred)

- OpenTelemetry processor
- Pull-based `.iter()` event access
- Cache, streaming, retry, timeout events
- Checkpointer integration
- DBOS/Daft runner event support

---

## Event Types (8)

All events share common fields for correlation:

```
run_id: str                  # Unique per .run() invocation
span_id: str                 # Unique per event scope (run or node)
parent_span_id: str | None   # Links to parent (None for root)
timestamp: float             # time.time()
```

### Included

| Event | Emitted when | Key fields |
|-------|-------------|------------|
| `RunStartEvent` | `run()`/`map()` begins | `graph_name`, `workflow_id` |
| `RunEndEvent` | `run()`/`map()` completes | `status` (COMPLETED/FAILED/PAUSED/STOPPED), `error`, `duration_ms` |
| `NodeStartEvent` | Node begins execution | `node_name`, `inputs` |
| `NodeEndEvent` | Node completes | `node_name`, `outputs`, `duration_ms` |
| `NodeErrorEvent` | Node raises exception | `node_name`, `error`, `error_type` |
| `RouteDecisionEvent` | Gate makes routing decision | `node_name`, `decision` (str or list[str]) |
| `InterruptEvent` | Paused at InterruptNode | `node_name`, `value`, `response_param`, `workflow_id` |
| `StopRequestedEvent` | Stop requested | `workflow_id` |

### Deferred

| Event | Reason |
|-------|--------|
| `CacheHitEvent` | Cache not implemented yet |
| `StreamingChunkEvent` | Streaming not implemented yet |
| `RetryAttemptEvent` | Retry not implemented yet |
| `TimeoutEvent` | Timeout not implemented yet |

---

## EventProcessor Interface

### Push-based (included)

```python
class EventProcessor:
    def on_event(self, event: Event) -> None: ...
    def shutdown(self) -> None: ...

class AsyncEventProcessor(EventProcessor):
    async def on_event_async(self, event: Event) -> None: ...
    async def shutdown_async(self) -> None: ...

class TypedEventProcessor(EventProcessor):
    """Auto-dispatches to on_run_start(), on_node_end(), etc."""
```

### Pull-based (deferred)

`.iter()` returning events for direct consumption - deferred.

---

## Runner Integration

Events are emitted by `SyncRunner` and `AsyncRunner`. Processors are passed as a parameter:

```python
runner = SyncRunner()
runner.run(graph, values, event_processors=[RichProgressProcessor()])
```

- Processors receive events in FIFO order
- Processor failures are best-effort (logged, don't crash the run)
- Nested graphs: events propagate up with correct `parent_span_id`

---

## Rich Progress Bar - Visual Specification

The progress bar is implemented as `RichProgressProcessor(TypedEventProcessor)`.

### 7 Scenarios

#### 1. Single `.run()` - One graph, one execution

One bar per node, `total=1`, marks complete on `NodeEndEvent`.

```
⠋ 📦 load_data    ━━━━━━━━━━━━━━━━━━━━ 1/1
⠋ 📦 preprocess   ━━━━━━━━━━━━━━━━━━━━ 1/1
⠋ 📦 transform    ━━━━━━━━━━╸          0/1   ← in progress
⠋ 📦 output       ╺━━━━━━━━━━━━━━━━━━━ 0/1
```

#### 2. `.map()` - One graph, k items

Top-level map bar + per-node bars, all `total=k`.

```
⠋ 🗺️  Map Progress   ━━━━━━━━━━━━╸     3/5  00:04
⠋   📦 load_data     ━━━━━━━━━━━━━━━╸  4/5  00:01
⠋   📦 preprocess    ━━━━━━━━━━━━╸     3/5  00:02
⠋   📦 transform     ━━━━━━━━━╸        3/5  00:03
```

#### 3. Nested graph - Hierarchy with indentation

Outer nodes at level 0, inner nodes indented 2 spaces with `🌳` prefix.

```
⠋ 📦 node_a          ━━━━━━━━━━━━━━━━ 1/1
⠋   🌳 inner_node_1  ━━━━━━━━━━━━━━━━ 1/1
⠋   🌳 inner_node_2  ━━━━━━━━━╸       0/1
⠋ 📦 node_b          ╺━━━━━━━━━━━━━━━ 0/1
```

#### 4. Outer `.map()` + nested graph

All bars have `total=k` (number of outer map items).

```
⠋ 🗺️  Items Processed      ━━━━━━━━╸  2/3
⠋ 📦 load_item             ━━━━━━━━━╸ 2/3
⠋   🌳 process_step_1      ━━━━━━━━╸  2/3
⠋   🌳 process_step_2      ━━━━━━╸    2/3
⠋ 📦 save_result           ━━━━━━╸    1/3
```

#### 5. Nested graph with inner `.map()`

Outer nodes `total=1`, inner nodes `total=inner_k`, indented deeper.

```
⠋ 📦 prepare              ━━━━━━━━━━━ 1/1
⠋   🗺️  Inner Map Progress ━━━━━━━╸   2/4
⠋     🌳 process           ━━━━━━━╸   2/4
⠋     🌳 validate          ━━━━━━╸    2/4
⠋ 📦 finalize             ╺━━━━━━━━━━ 0/1
```

#### 6. Both outer and inner `.map()`

Inner totals multiply: `outer_k × inner_k`.

```
⠋ 🗺️  Outer Map      ━━━━━━━━━╸  1/2
⠋ 📦 load_batch      ━━━━━━━━━━━ 1/2
⠋   🗺️  Inner Map    ━━━━━━━━╸   4/6
⠋     🌳 transform   ━━━━━━━━╸   4/6
⠋     🌳 enrich      ━━━━━━━╸    4/6
⠋ 📦 save_batch      ━━━━━━╸     1/2
```

#### 7. 3-level nesting

Each level adds 2 spaces of indentation.

```
⠋ 📦 init                ━━━━━━━━━━ 1/1
⠋   🌳 middle_node_a     ━━━━━━━━━━ 1/1
⠋     🌳 inner_node_1    ━━━━━━━━━━ 1/1
⠋     🌳 inner_node_2    ━━━━━━━╸   0/1
⠋   🌳 middle_node_b     ╺━━━━━━━━━ 0/1
⠋ 📦 cleanup             ╺━━━━━━━━━ 0/1
```

### Visual Conventions

- `📦` regular nodes, `🌳` nested graph nodes, `🗺️` map progress
- Indentation: `"  " * nesting_level`
- Columns: SpinnerColumn, TextColumn (description), BarColumn, completed/total
- TimeRemainingColumn added for map operations
- Completion message: `✓ ...completed!` in bold green

### Progress Totals Logic

| Context | `total` |
|---------|---------|
| `.run()` node | `1` |
| `.map()` node | `num_items` |
| `.map()` overall | `num_items` |
| Nested `.map()` node | `outer_items × inner_items` (cumulative) |

---

## Assumptions

1. **Best-effort progress**: Processor errors are logged but don't affect execution
2. **No backpressure**: Events are fire-and-forget to processors
3. **Thread safety**: `RichProgressProcessor` handles concurrent updates (Rich's Progress is thread-safe)
4. **Cyclic graphs**: For graphs with cycles, node bars show cumulative executions (total unknown upfront, use indeterminate or update total dynamically)
5. **`rich` is an optional dependency**: The progress processor lives in an extras package (e.g., `hypergraph[rich]`)
