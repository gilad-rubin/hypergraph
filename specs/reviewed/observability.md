# Observability

**Events are the primitive. Processors consume them.**

HyperNodes uses a unified event stream for all observability. The core execution engine emits events; pluggable processors handle logging, tracing, and integration with external tools.

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

### Why Checkpointer Is NOT an EventProcessor

Persistence uses a **separate `Checkpointer` interface**, not `EventProcessor`. This is a deliberate design choice:

| Concern | EventProcessor | Checkpointer |
|---------|----------------|--------------|
| **Direction** | Write-only (events push to processor) | Read + Write (load and save) |
| **Data** | All events (including streaming chunks) | Only `persist=True` outputs |
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

### Core Events

| Event | When Emitted | Key Fields |
|-------|--------------|------------|
| `RunStartEvent` | Execution begins | `inputs`, `session_id` |
| `RunEndEvent` | Execution completes | `outputs`, `duration_ms`, `iterations` |
| `NodeStartEvent` | Node begins | `node_name`, `inputs` |
| `NodeEndEvent` | Node completes successfully | `node_name`, `outputs`, `duration_ms`, `cached` |
| `NodeErrorEvent` | Node raises exception | `node_name`, `error`, `error_type` |
| `StreamingChunkEvent` | Generator yields | `node_name`, `chunk`, `chunk_index` |
| `CacheHitEvent` | Cache lookup succeeded | `node_name` (emitted *before* NodeEndEvent) |
| `RouteDecisionEvent` | Gate routes | `gate_name`, `decision` |
| `InterruptEvent` | Paused for input | `interrupt_name`, `value`, `checkpoint` |

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

**Sequence for cache miss:**
1. `NodeStartEvent` (node begins)
2. `NodeEndEvent` with `cached=False` (computed fresh)

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
    - on_event() is called synchronously for each event during execution
    - shutdown() is called once after RunEndEvent, before .run() returns
    - force_flush() is called before checkpoints and on interrupts
    """

    def on_event(self, event: Event) -> None:
        """
        Called for every event during execution.

        Args:
            event: The event that occurred. Use isinstance() or
                   match/case to filter by type.

        Note: This runs in the execution path. For expensive operations
        (network calls, disk I/O), buffer internally and flush async.
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

### Async Processors

For AsyncRunner, processors can optionally implement async event handling:

```python
class AsyncEventProcessor(EventProcessor):
    """
    Base class for async-aware processors.

    When used with AsyncRunner, on_event_async() is awaited.
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
- `AsyncRunner` calls `on_event_async()` if processor is `AsyncEventProcessor`, else `on_event()`

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
from hypernodes import AsyncRunner, SyncRunner

# Processors receive events from all runs on this runner
runner = AsyncRunner(
    event_processors=[
        LangfuseProcessor(api_key="..."),
        ConsoleLogProcessor(),
    ]
)

result = await runner.run(graph, inputs={...})
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
    return await runner.run(graph, inputs={...})
```

### Per-Run Processors

Add processors for a single run without modifying the runner:

```python
runner = AsyncRunner()  # No default processors

# Add processors for this run only
result = await runner.run(
    graph,
    inputs={...},
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

async for event in runner.iter(outer, inputs={...}):
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
await runner.run(outer_graph, inputs={...})
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
async for event in runner.iter(graph, inputs={...}):
    match event:
        case StreamingChunkEvent(chunk=chunk):
            print(chunk, end="", flush=True)

        case NodeEndEvent(node_name=name, duration_ms=ms, cached=cached):
            status = "cached" if cached else f"{ms:.1f}ms"
            print(f"\n[{name}: {status}]")

        case InterruptEvent(value=prompt, checkpoint=cp):
            response = await get_user_input(prompt)
            # Resume handled separately
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
        self.logger = logger or logging.getLogger("hypernodes")

    def on_node_start(self, event: NodeStartEvent) -> None:
        self.logger.info(f"Starting {event.node_name}")

    def on_node_end(self, event: NodeEndEvent) -> None:
        status = "cached" if event.cached else f"{event.duration_ms:.1f}ms"
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
        self.tracer = trace.get_tracer("hypernodes")
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
                span.set_attribute("hypernodes.span_id", sid)
                span.set_attribute("hypernodes.run_id", event.run_id)
                self.spans[sid] = span

            case NodeEndEvent(span_id=sid, duration_ms=ms, cached=cached):
                if sid in self.spans:
                    span = self.spans[sid]
                    span.set_attribute("hypernodes.cached", cached)
                    span.set_attribute("hypernodes.duration_ms", ms)
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

- Processors run synchronously in the execution path
- For expensive operations (network calls), buffer and flush async
- Use `force_flush()` before checkpoints to avoid data loss

---

## API Reference

See also:
- [Execution Types](execution-types.md) - Event dataclass definitions
- [Runners API Reference](runners-api-reference.md) - `event_processors` parameter
- [Runners Guide](runners.md) - Conceptual overview
