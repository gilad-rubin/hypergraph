# Observability

**Events are the primitive. Processors consume them.**

hypergraph uses a unified event stream for all observability. The core execution engine emits events; pluggable processors handle logging, tracing, and integration with external tools.

---

## Overview

### Design Principles

1. **Events are data** - Simple dataclasses, stable public API
2. **Single interface** - One `EventProcessor` class to implement
3. **Pull or push** - Use `.iter()` for pull-based, processors for push-based
4. **OTel-compatible** - Span hierarchy maps directly to OpenTelemetry traces
5. **Nested graph support** - Events include hierarchy for full visibility

### Architecture

```
                    ┌─────────────────────────┐
                    │   Core Execution        │
                    │   (Runners)             │
                    └───────────┬─────────────┘
                                │ Events (with span hierarchy)
                    ┌───────────▼─────────────┐
                    │   Event Stream          │
                    └─┬──────────┬──────────┬─┘
                      │          │          │
        ┌─────────────▼──┐  ┌────▼─────┐  ┌▼────────────┐
        │ EventProcessor │  │ .iter()  │  │ OTel Export │
        │ (push-based)   │  │ (pull)   │  │             │
        └────────────────┘  └──────────┘  └─────────────┘
```

### Which Runners Emit Events?

Events are a feature of the **core runners** (SyncRunner, AsyncRunner). External integrations have their own observability:

| Runner | Emits Events? | Notes |
|--------|:-------------:|-------|
| **SyncRunner** | ✅ | Core runner |
| **AsyncRunner** | ✅ | Core runner |
| **DBOSAsyncRunner** | ❌ | Use DBOS observability |
| **DaftRunner** | ❌ | Use Daft observability |

External runners (DBOS, Daft) delegate execution to systems that provide their own workflow tracking and tracing.

### Why Checkpointer Is NOT an EventProcessor

Persistence uses a **separate `Checkpointer` interface**, not `EventProcessor`. This is a deliberate design choice:

| Concern | EventProcessor | Checkpointer |
|---------|----------------|--------------|
| **Direction** | Write-only (events push to processor) | Read + Write (load and save) |
| **Data** | All events (including streaming chunks) | All node outputs |
| **Purpose** | Observability (fire-and-forget) | Durability (must succeed) |
| **Timing** | During execution | After node completion |

**The read path doesn't fit the event model.** When resuming a workflow, the runner needs to *query* the checkpointer for existing state. Events are write-only; they flow from runner to consumers. The checkpointer needs bidirectional communication.

**Configuration belongs on Checkpointer.** Streaming recovery modes, serialization options, and storage backends are concerns of the checkpointer, not the event stream.

See [Checkpointer API](checkpointer.md) for the full interface definition.

---

## Event Types

All events share common fields for correlation and hierarchy:

```python
@dataclass
class BaseEvent:
    run_id: str              # Unique per .run() invocation
    span_id: str             # Unique per node execution
    parent_span_id: str | None  # Links to parent (None for root nodes)
    timestamp: float         # Unix timestamp
```

### Terminology: hypergraph Events vs OTel Span Events

hypergraph "events" are **not** OTel "span events". In OpenTelemetry, span events are log annotations *attached to* spans. hypergraph events are structured records that together **define** spans:

- `NodeStartEvent` + `NodeEndEvent` = span boundaries (start/end)
- `span_id` = correlation ID linking all events for one node execution
- `parent_span_id` = nested graph hierarchy (maps to OTel parent span)

Processors reconstruct OTel spans from hypergraph events. The `OpenTelemetryProcessor` example below shows this mapping: it creates an OTel span on `NodeStartEvent` and ends it on `NodeEndEvent`.

### Core Events

| Event | When Emitted | Key Fields |
|-------|--------------|------------|
| `RunStartEvent` | Execution begins | `inputs`, `session_id` |
| `RunEndEvent` | Execution completes | `outputs`, `duration_ms`, `iterations` |
| `NodeStartEvent` | Node begins | `node_name`, `inputs` |
| `NodeEndEvent` | Node completes successfully | `node_name`, `outputs`, `duration_ms`, `cached`, `replayed` |
| `NodeErrorEvent` | Node raises exception | `node_name`, `error`, `error_type` |
| `StreamingChunkEvent` | Generator yields | `node_name`, `chunk`, `chunk_index` |
| `CacheHitEvent` | Cache lookup succeeded | `node_name` (emitted *before* NodeEndEvent) |
| `RouteDecisionEvent` | Gate routes | `gate_name`, `decision` |
| `InterruptEvent` | Paused for input | `workflow_id`, `interrupt_name`, `value`, `response_param` |

### NodeErrorEvent

```python
@dataclass
class NodeErrorEvent:
    run_id: str
    span_id: str
    parent_span_id: str | None
    node_name: str
    error: Exception
    error_type: str          # e.g., "ValueError", "TimeoutError"
    timestamp: float
```

**Note:** After `NodeErrorEvent`, the runner may still emit `RunEndEvent` (with error status) and call `shutdown()` on processors.

### Cache vs Checkpoint: The `cached` and `replayed` Flags

`NodeEndEvent` has two boolean flags to distinguish how outputs were obtained:

| Flag | Meaning | Use Case |
|------|---------|----------|
| `cached=True` | Loaded from cache | Optimization - same inputs seen before |
| `replayed=True` | Loaded from checkpoint | Recovery - resuming after crash/pause |

**Why distinguish?** For observability and debugging:
- `cached=True` → "optimization working, saved compute"
- `replayed=True` → "recovering from step X, workflow resuming"

### CacheHitEvent vs NodeEndEvent.cached

Both relate to caching but serve different purposes:

| Event | When | Purpose |
|-------|------|---------|
| `CacheHitEvent` | Before value returned | Early notification for UI ("loading from cache...") |
| `NodeEndEvent` with `cached=True` | After value returned | Final record with actual outputs |

**Sequence for cache hit:**
1. `NodeStartEvent` (node begins)
2. `CacheHitEvent` (cache lookup succeeded)
3. `NodeEndEvent` with `cached=True` (outputs available)

**Sequence for fresh execution:**
1. `NodeStartEvent` (node begins)
2. `NodeEndEvent` with `cached=False, replayed=False` (computed fresh)

**Sequence for checkpoint replay:**
1. `NodeEndEvent` with `replayed=True` (no NodeStartEvent - already ran)

### Span Hierarchy

Events form a tree via `span_id` → `parent_span_id`:

```
run-123 (root)
├── span-1: preprocess           (parent=None)
├── span-2: rag                  (parent=None, GraphNode)
│   ├── span-3: embed            (parent=span-2)
│   └── span-4: retrieve         (parent=span-2)
└── span-5: postprocess          (parent=None)
```

This maps directly to OpenTelemetry's trace/span model, enabling integration with any OTel-compatible backend.

### Parallel Execution and Spans

When nodes execute in parallel (same level, no dependencies), they share the same `parent_span_id` but have different `span_id`s:

```
run-456 (parallel execution)
├── span-1: fetch_a              (parent=None)  ─┐
├── span-2: fetch_b              (parent=None)  ─┼─ Running concurrently
├── span-3: fetch_c              (parent=None)  ─┘
└── span-4: combine              (parent=None, waits for all)
```

**Implications for processors:**
- `NodeStartEvent`s may arrive in any order
- Multiple spans can be "open" simultaneously
- Use `span_id` (not event order) for correlation
- `TreeProcessor` example handles this correctly

### Event Ordering Guarantees

hypergraph provides specific ordering guarantees that processors can rely on. Understanding these helps build correct integrations.

#### Single Node (Strict Order)

Within a single node execution, events follow strict happens-before ordering:

```
NodeStartEvent → StreamingChunkEvent* → NodeEndEvent
                 (in emission order)
```

**Guarantees:**
- `NodeStartEvent` always precedes any `StreamingChunkEvent` for that span
- `StreamingChunkEvent`s preserve generator yield order (`chunk_index` is monotonic)
- `NodeEndEvent` always follows all chunks (or `NodeErrorEvent` on failure)
- `CacheHitEvent`, when present, occurs between `NodeStartEvent` and `NodeEndEvent`

#### Nested Graphs (Parent-Child)

For `GraphNode` (nested graph) execution:

```
Parent NodeStartEvent
├── Child events (fully contained)
│   └── ...
Parent NodeEndEvent
```

**Guarantees:**
- Child graph's `RunStartEvent` happens-after parent node's `NodeStartEvent`
- Child graph's `RunEndEvent` happens-before parent node's `NodeEndEvent`
- All child events are bracketed by parent's start/end

#### Parallel Execution (No Order Guarantee)

For independent nodes running concurrently:

```
NodeStartEvent(A) ─┐
NodeStartEvent(B) ─┼─ May interleave in ANY order
NodeEndEvent(B)   ─┤
NodeEndEvent(A)   ─┘
```

**No guarantees:**
- `NodeStartEvent` order across parallel nodes is undefined
- Sibling nodes may interleave events freely
- Processors must use `span_id` for correlation, not arrival order

#### Checkpoint Events

```
[all node events] → InterruptEvent → force_flush() called
```

**Guarantees:**
- `InterruptEvent` fires after all in-progress nodes complete
- `force_flush()` is called on all processors before pausing
- On resume, `NodeEndEvent` with `replayed=True` (no `NodeStartEvent`)

#### Formal Happens-Before Summary

| A | B | Relationship |
|---|---|--------------|
| `NodeStartEvent(X)` | `StreamingChunkEvent(X)` | A → B (strict) |
| `StreamingChunkEvent(X, i)` | `StreamingChunkEvent(X, i+1)` | A → B (strict) |
| `StreamingChunkEvent(X)` | `NodeEndEvent(X)` | A → B (strict) |
| `NodeStartEvent(parent)` | `NodeStartEvent(child)` | A → B (strict) |
| `NodeEndEvent(child)` | `NodeEndEvent(parent)` | A → B (strict) |
| `NodeStartEvent(A)` | `NodeStartEvent(B)` | **No guarantee** (parallel) |
| `NodeEndEvent(A)` | `NodeStartEvent(B)` | A → B only if edge A→B exists |

**Key insight:** Use `span_id` and `parent_span_id` for reconstruction, not event arrival order.

---

## EventProcessor Interface

### Base Class

```python
class EventProcessor:
    """
    Base class for processing execution events.

    Implement on_event() to receive all events. Use match/case
    to filter by type. For convenience, extend TypedEventProcessor
    instead for pre-dispatched typed methods.

    Lifecycle:
    - Events are delivered in-order per processor (FIFO)
    - Processor failures do not fail the run by default (best-effort)
    - shutdown() is called once after RunEndEvent (even on errors)
    - force_flush() is called at durability boundaries (interrupt/checkpoint)
    """

    def on_event(self, event: Event) -> None:
        """
        Called for every event during execution.

        Args:
            event: The event that occurred. Use isinstance() or
                   match/case to filter by type.

        Note: This must be fast and non-blocking. For expensive operations
        (network calls, disk I/O), buffer internally and export in the background.
        """
        pass

    def shutdown(self) -> None:
        """
        Called once after RunEndEvent, before .run()/.iter() returns.

        Use this to:
        - Flush any buffered events
        - Close connections
        - Clean up resources

        Note: Called even if execution errors (after NodeErrorEvent).
        """
        pass

    def force_flush(self) -> None:
        """
        Force immediate export of any buffered events.

        Called automatically:
        - Before checkpoint serialization
        - On InterruptEvent (before pausing)
        - When max_iterations is about to be exceeded

        Also callable manually: processor.force_flush()
        """
        pass
```

---

## Processor Semantics (Failure, Concurrency, Backpressure)

This section is **normative**: it defines what users can rely on across runners.

### Goals (Why this exists)

- **Observability must not change execution behavior by accident.** Logging/tracing should not silently cause workflow failures or large slowdowns.
- **Users can opt into strictness when they want it.** Tests and debugging sometimes *should* fail if observability is broken.

These defaults mirror common practice in other systems:
- **LangChain / LangGraph callbacks**: exceptions are logged and ignored by default; optional `raise_error` makes them fatal.
- **Mastra observability exporters**: export is best-effort; failures are caught and do not stop execution (`Promise.allSettled` / per-target try/catch).
- **OpenTelemetry SDKs**: exporters run behind bounded queues; exporter exceptions are caught; telemetry may be dropped under pressure.
- **Temporal interceptors** (contrast): interceptors are in the call path; exceptions can fail activities/workflows unless the interceptor catches them.

### 1) If a processor throws, what happens?

**Default: best-effort (recommended).**
- If `on_event()` / `on_event_async()` raises, the runner **MUST NOT** fail the graph run.
- The runner **SHOULD**:
  - log the exception (including processor name/type and event type),
  - record it for diagnostics (e.g., in `RunResult` or an internal counter),
  - optionally **disable that processor for the remainder of the run** after repeated failures (to avoid infinite error spam).
- The runner **MUST NOT** retry events automatically. Retries (and deduplication) are processor concerns.

**Optional: strict mode (opt-in).**
- A runner MAY offer a configuration where processor exceptions **fail the run** (useful in tests/CI).

**Example: network outage**
- You attach `ConsoleLogProcessor()` and `OpenTelemetryProcessor()`.
- The OTel exporter gets a timeout and raises while exporting spans.
- The graph still completes; the runner logs the processor failure and continues delivering events to the console logger.

### 2) Are processors called sequentially or concurrently?

- **Per-processor ordering:** events delivered to a single processor are **FIFO** (the same order the runner emitted them).
- **Across processors:** runners **MAY** deliver the same event to different processors concurrently.
- **Across spans:** because nodes can run in parallel, events from different spans may interleave arbitrarily; processors must correlate using `run_id` + `span_id`, not arrival order.

### 3) Do async processors backpressure execution?

**Default: no (recommended).**
- Runners SHOULD treat processors as **out-of-band** (fire-and-forget) so slow processors do not slow node execution.
- Ordering is preserved by giving each processor a **single-consumer FIFO queue** (one background task/thread per processor).

**Durability boundaries:** the runner MAY block briefly on:
- `force_flush()` (before checkpoint serialization / on interrupt),
- `shutdown()` (at the end of the run),
so processors can export buffered telemetry. Runners SHOULD apply a timeout and continue if the processor cannot flush in time.

**Optional: inline mode (opt-in).**
- A runner MAY provide an inline delivery mode that calls/awaits processors on the execution path. This can be useful for “stream tokens to UI” processors, but it makes backpressure explicit.

### 4) Backpressure and queue overflow

Runners SHOULD provide bounded buffering for processors. When a processor falls behind:
- **Default policy:** drop events (typically “drop oldest”) and log a warning with counts.
- **Opt-in policies:** block the producer (backpressure) or drop newest, depending on the use case.

**Rule of thumb:**
- Observability exporters (Langfuse/OTel/Datadog) → drop is usually fine.
- User-facing streaming (“show tokens live”) → prefer `.iter()`; if forced to use processors (e.g., DBOS), opt into inline/backpressure explicitly.

### Async Processors

For AsyncRunner, processors can optionally implement async event handling:

```python
class AsyncEventProcessor(EventProcessor):
    """
    Base class for async-aware processors.

    When used with AsyncRunner, on_event_async() is used for non-blocking I/O.
    Runners may schedule it out-of-band (recommended) or await it in inline mode.
    Falls back to sync on_event() if not overridden.
    """

    async def on_event_async(self, event: Event) -> None:
        """
        Async version of on_event(). Override for non-blocking I/O.

        Default implementation calls sync on_event().
        """
        self.on_event(event)

    async def shutdown_async(self) -> None:
        """Async shutdown. Default calls sync shutdown()."""
        self.shutdown()
```

**Usage:**
- `SyncRunner` always calls `on_event()` (sync)
- `AsyncRunner` calls `on_event_async()` if processor is `AsyncEventProcessor` (typically scheduled out-of-band), else `on_event()`

### TypedEventProcessor (Convenience)

For processors that handle specific event types:

```python
class TypedEventProcessor(EventProcessor):
    """
    Convenience base class with typed methods for each event type.
    Override only the methods you need - defaults are no-ops.

    Method naming convention: on_<event_type_without_event_suffix>
    Example: NodeStartEvent → on_node_start()
    """

    def on_run_start(self, event: RunStartEvent) -> None: ...
    def on_run_end(self, event: RunEndEvent) -> None: ...
    def on_node_start(self, event: NodeStartEvent) -> None: ...
    def on_node_end(self, event: NodeEndEvent) -> None: ...
    def on_node_error(self, event: NodeErrorEvent) -> None: ...
    def on_streaming_chunk(self, event: StreamingChunkEvent) -> None: ...
    def on_cache_hit(self, event: CacheHitEvent) -> None: ...
    def on_route_decision(self, event: RouteDecisionEvent) -> None: ...
    def on_interrupt(self, event: InterruptEvent) -> None: ...

    def on_event(self, event: Event) -> None:
        """
        Dispatches to typed methods based on event class name.

        Uses dynamic dispatch: NodeStartEvent → on_node_start()
        This allows custom event types to work without modifying base class.
        """
        # Convert "NodeStartEvent" → "node_start" → "on_node_start"
        event_name = type(event).__name__
        if event_name.endswith("Event"):
            event_name = event_name[:-5]  # Remove "Event" suffix
        # CamelCase to snake_case
        method_name = "on_" + "".join(
            f"_{c.lower()}" if c.isupper() else c
            for c in event_name
        ).lstrip("_")

        handler = getattr(self, method_name, None)
        if handler:
            handler(event)
```

**Extensibility:** Custom events automatically dispatch to matching methods:
```python
# Custom event
@dataclass
class MyCustomEvent:
    run_id: str
    data: Any

# Handler method is auto-discovered
class MyProcessor(TypedEventProcessor):
    def on_my_custom(self, event: MyCustomEvent) -> None:
        print(event.data)
```

---

## Registration

### Per-Runner (Recommended)

```python
from hypergraph import AsyncRunner, SyncRunner

# Processors receive events from all runs on this runner
runner = AsyncRunner(
    event_processors=[
        LangfuseProcessor(api_key="..."),
        ConsoleLogProcessor(),
    ]
)

result = await runner.run(graph, values={...})
```

### Factory Pattern (For Consistent Configuration)

Instead of global registration, use a factory for consistent runner configuration:

```python
# my_app/runners.py
def create_runner(*, cache: Cache | None = None) -> AsyncRunner:
    """Factory that ensures consistent observability setup."""
    return AsyncRunner(
        cache=cache,
        event_processors=[
            LangfuseProcessor(api_key=os.environ["LANGFUSE_KEY"]),
            MetricsProcessor(),
        ],
    )

# Usage
runner = create_runner(cache=DiskCache("./cache"))
```

### Dependency Injection

For frameworks that support DI (FastAPI, etc.):

```python
# Using FastAPI dependency injection
from fastapi import Depends

def get_runner() -> AsyncRunner:
    return AsyncRunner(
        event_processors=[LangfuseProcessor()],
    )

@app.post("/run")
async def run_graph(runner: AsyncRunner = Depends(get_runner)):
    return await runner.run(graph, values={...})
```

### Per-Run Processors

Add processors for a single run without modifying the runner:

```python
runner = AsyncRunner()  # No default processors

# Add processors for this run only
result = await runner.run(
    graph,
    values={...},
    event_processors=[DebugProcessor()],  # This run only
)
```

**Note:** Per-run processors are appended to runner's processors, not replacing them.

---

## Nested Graph Event Flow

### Context Propagation

When a `GraphNode` (nested graph) executes, the execution context is propagated automatically. Child events reference the parent span:

```python
outer = Graph(nodes=[preprocess, inner.as_node(name="rag"), postprocess])

async for event in runner.iter(outer, values={...}):
    print(f"{event.node_name}: parent={event.parent_span_id}")

# Output:
# preprocess: parent=None
# rag: parent=None           <- GraphNode starts
# embed: parent=span-2       <- Inside rag, references rag's span
# retrieve: parent=span-2    <- Inside rag
# rag: parent=None           <- GraphNode ends
# postprocess: parent=None
```

### Processor Inheritance

Nested graphs **inherit** event processors from the parent runner by default:

```python
# All events (outer + inner) go to same processors
runner = AsyncRunner(event_processors=[LangfuseProcessor()])
await runner.run(outer_graph, values={...})
```

### Overriding for Nested Graphs

Use explicit `runner=` on `.as_node()` for different processing:

```python
# Inner graph uses different/additional processors
inner = Graph(nodes=[embed, retrieve], name="rag")

outer = Graph(nodes=[
    preprocess,
    inner.as_node(
        runner=AsyncRunner(event_processors=[DebugProcessor()])
    ),
    postprocess
])
```

**Note:** When overriding, the nested runner's processors replace (not extend) the parent's for that subgraph.

---

## Pull-Based Access with `.iter()`

For direct event consumption without processors:

```python
async for event in runner.iter(graph, values={...}):
    match event:
        case StreamingChunkEvent(chunk=chunk):
            print(chunk, end="", flush=True)

        case NodeEndEvent(node_name=name, duration_ms=ms, cached=cached, replayed=replayed):
            if cached:
                status = "cached"
            elif replayed:
                status = "replayed"
            else:
                status = f"{ms:.1f}ms"
            print(f"\n[{name}: {status}]")

        case InterruptEvent(value=prompt, workflow_id=wf_id):
            response = await get_user_input(prompt)
            # Resume via workflow_id (checkpointer or DBOS.send())
```

**When to use `.iter()` vs processors:**

| Use Case | Approach |
|----------|----------|
| Real-time UI updates | `.iter()` - direct control over rendering |
| Background logging/tracing | Processors - fire-and-forget |
| Streaming to WebSocket | `.iter()` - need async send |
| Integration with Langfuse/Datadog | Processors - standard pattern |

---

## Integration Patterns

### Example: Simple Logging Processor

```python
import logging

class LoggingProcessor(TypedEventProcessor):
    def __init__(self, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger("hypergraph")

    def on_node_start(self, event: NodeStartEvent) -> None:
        self.logger.info(f"Starting {event.node_name}")

    def on_node_end(self, event: NodeEndEvent) -> None:
        if event.cached:
            status = "cached"
        elif event.replayed:
            status = "replayed"
        else:
            status = f"{event.duration_ms:.1f}ms"
        self.logger.info(f"Completed {event.node_name} ({status})")

    def on_route_decision(self, event: RouteDecisionEvent) -> None:
        self.logger.debug(f"Gate {event.gate_name} → {event.decision}")
```

### Example: Top-Level Only (Filtering by Depth)

```python
class TopLevelProcessor(EventProcessor):
    """Only process root-level events, ignore nested graph internals."""

    def on_event(self, event: Event) -> None:
        if event.parent_span_id is not None:
            return  # Skip nested events

        # Process top-level only
        print(f"[TOP] {event}")
```

### Example: Span Tree Reconstruction

```python
from dataclasses import dataclass, field

@dataclass
class SpanNode:
    event: NodeStartEvent
    children: list["SpanNode"] = field(default_factory=list)
    end_event: NodeEndEvent | None = None

class TreeProcessor(EventProcessor):
    """Reconstruct span tree for visualization."""

    def __init__(self):
        self.spans: dict[str, SpanNode] = {}
        self.roots: list[SpanNode] = []

    def on_event(self, event: Event) -> None:
        match event:
            case NodeStartEvent(span_id=sid, parent_span_id=parent):
                node = SpanNode(event=event)
                self.spans[sid] = node

                if parent is None:
                    self.roots.append(node)
                elif parent in self.spans:
                    self.spans[parent].children.append(node)

            case NodeEndEvent(span_id=sid):
                if sid in self.spans:
                    self.spans[sid].end_event = event

    def get_tree(self) -> list[SpanNode]:
        return self.roots
```

### Example: OpenTelemetry Export

```python
from opentelemetry import trace
from opentelemetry.trace import SpanKind

class OpenTelemetryProcessor(EventProcessor):
    """Export events to any OpenTelemetry-compatible backend."""

    def __init__(self):
        self.tracer = trace.get_tracer("hypergraph")
        self.spans: dict[str, trace.Span] = {}

    def on_event(self, event: Event) -> None:
        match event:
            case NodeStartEvent(span_id=sid, parent_span_id=parent, node_name=name):
                # Get parent context if exists
                context = None
                if parent and parent in self.spans:
                    context = trace.set_span_in_context(self.spans[parent])

                span = self.tracer.start_span(
                    name,
                    context=context,
                    kind=SpanKind.INTERNAL,
                )
                span.set_attribute("hypergraph.span_id", sid)
                span.set_attribute("hypergraph.run_id", event.run_id)
                self.spans[sid] = span

            case NodeEndEvent(span_id=sid, duration_ms=ms, cached=cached, replayed=replayed):
                if sid in self.spans:
                    span = self.spans[sid]
                    span.set_attribute("hypergraph.cached", cached)
                    span.set_attribute("hypergraph.replayed", replayed)
                    span.set_attribute("hypergraph.duration_ms", ms)
                    span.end()
                    del self.spans[sid]

    def shutdown(self) -> None:
        # End any unclosed spans
        for span in self.spans.values():
            span.end()
        self.spans.clear()
```

---

## Comparison: Old Callbacks vs New EventProcessor

| Aspect | Old (Callbacks) | New (EventProcessor) |
|--------|-----------------|----------------------|
| Interface | `on_node_start(name, inputs)`, `on_node_end(name, outputs)`, ... (many methods) | Single `on_event(event)` |
| Event data | Scattered across method parameters | Single event dataclass with all context |
| Hierarchy | Not supported | `span_id` + `parent_span_id` |
| Filtering | Override specific methods | Match on event type or `parent_span_id` |
| OTel compatibility | Manual mapping | Direct span model |

---

## Best Practices

### For Integration Authors

1. **Extend `TypedEventProcessor`** for cleaner code when handling multiple event types
2. **Buffer events** and flush periodically for performance
3. **Handle `shutdown()`** to ensure all events are exported
4. **Use `span_id`** for correlation, not `node_name` (names can repeat)

### For Users

1. **Prefer per-runner processors** over global for better isolation
2. **Use `.iter()`** for real-time UI, processors for background observability
3. **Check `parent_span_id`** if you only care about top-level execution

### Performance Considerations

- Processors SHOULD be treated as out-of-band (best-effort) so observability does not slow execution
- For expensive operations (network calls), buffer internally and export in the background
- `force_flush()` exists for durability boundaries; apply timeouts so flush cannot stall the run indefinitely

---

## API Reference

See also:
- [Execution Types](execution-types.md) - Event dataclass definitions
- [Runners API Reference](runners-api-reference.md) - `event_processors` parameter
- [Runners Guide](runners.md) - Conceptual overview
