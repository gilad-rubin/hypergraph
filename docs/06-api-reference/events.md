# Events API Reference

The event system lets you observe graph execution without modifying your workflow logic. Events are emitted at key points — run start/end, node start/end, routing decisions — and delivered to processors you provide.

- **Event types** - Immutable dataclasses for each execution milestone
- **EventProcessor** - Sync base class for consuming events
- **AsyncEventProcessor** - Async-aware variant for async runners
- **TypedEventProcessor** - Auto-dispatches to typed handler methods
- **RichProgressProcessor** - Hierarchical Rich progress bars out of the box

## Overview

```python
from hypergraph import SyncRunner, RichProgressProcessor

runner = SyncRunner()
result = runner.run(graph, inputs, event_processors=[RichProgressProcessor()])
```

Events flow through this pipeline:

```text
Runner emits event → EventDispatcher → each EventProcessor.on_event()
```

All dispatch is best-effort: a failing processor never breaks execution.

---

## Event Types

All events inherit from `BaseEvent` and are frozen dataclasses (immutable after creation).

### BaseEvent

```python
@dataclass(frozen=True)
class BaseEvent:
    run_id: str                    # Unique identifier for the run
    span_id: str                   # Unique identifier for this event's scope
    parent_span_id: str | None     # Parent scope, or None for root runs
    timestamp: float               # Unix timestamp
```

Every event carries tracing context: `run_id` groups events from the same execution, `span_id` uniquely identifies the scope, and `parent_span_id` links nested graphs to their parents.

### RunStartEvent

Emitted when a graph run begins.

```python
@dataclass(frozen=True)
class RunStartEvent(BaseEvent):
    graph_name: str              # Name of the graph
    workflow_id: str | None      # Optional workflow tracking ID
    is_map: bool                 # True if this is a map() operation
    map_size: int | None         # Number of items in map, if applicable
```

### RunEndEvent

Emitted when a graph run completes (successfully or not).

```python
@dataclass(frozen=True)
class RunEndEvent(BaseEvent):
    graph_name: str              # Name of the graph
    status: str                  # "completed" or "failed"
    error: str | None            # Error message if failed
    duration_ms: float           # Wall-clock duration in milliseconds
```

### NodeStartEvent

Emitted when a node begins execution.

```python
@dataclass(frozen=True)
class NodeStartEvent(BaseEvent):
    node_name: str               # Name of the node
    graph_name: str              # Graph containing the node
```

### NodeEndEvent

Emitted when a node completes successfully.

```python
@dataclass(frozen=True)
class NodeEndEvent(BaseEvent):
    node_name: str               # Name of the node
    graph_name: str              # Graph containing the node
    duration_ms: float           # Wall-clock duration in milliseconds
    cached: bool                 # True if result was served from cache
```

### NodeErrorEvent

Emitted when a node fails with an exception.

```python
@dataclass(frozen=True)
class NodeErrorEvent(BaseEvent):
    node_name: str               # Name of the node
    graph_name: str              # Graph containing the node
    error: str                   # Error message
    error_type: str              # Fully qualified exception type
```

### RouteDecisionEvent

Emitted when a routing node (`@route` or `@ifelse`) makes a decision.

```python
@dataclass(frozen=True)
class RouteDecisionEvent(BaseEvent):
    node_name: str               # Name of the routing node
    graph_name: str              # Graph containing the node
    decision: str | list[str]    # Chosen target(s)
```

### InterruptEvent

Emitted when execution pauses for human-in-the-loop input.

```python
@dataclass(frozen=True)
class InterruptEvent(BaseEvent):
    node_name: str               # Node that triggered the interrupt
    graph_name: str              # Graph containing the node
    workflow_id: str | None      # Workflow identifier
    value: object                # Interrupt payload
    response_param: str          # Parameter name for the response
```

### StopRequestedEvent

Emitted when a stop is requested on a workflow.

```python
@dataclass(frozen=True)
class StopRequestedEvent(BaseEvent):
    workflow_id: str | None      # Workflow identifier
```

### CacheHitEvent

Emitted when a node result is served from cache instead of being executed.

```python
@dataclass(frozen=True)
class CacheHitEvent(BaseEvent):
    node_name: str               # Name of the cached node
    graph_name: str              # Graph containing the node
    cache_key: str               # The cache key that was hit
```

### Event (Union Type)

```python
Event = (
    RunStartEvent | RunEndEvent | NodeStartEvent | NodeEndEvent
    | NodeErrorEvent | RouteDecisionEvent | InterruptEvent | StopRequestedEvent
    | CacheHitEvent
)
```

---

## Processor Interfaces

### EventProcessor

Base class for synchronous event consumers.

```python
class EventProcessor:
    def on_event(self, event: Event) -> None:
        """Called for every event."""

    def shutdown(self) -> None:
        """Called once when the run completes. Flush buffers here."""
```

**Example — logging processor:**

```python
from hypergraph import EventProcessor

class LoggingProcessor(EventProcessor):
    def on_event(self, event):
        print(f"[{type(event).__name__}] {event.span_id}")
```

### AsyncEventProcessor

Extends `EventProcessor` with async variants. The async runner prefers `on_event_async` and `shutdown_async` when available, falling back to sync methods otherwise.

```python
class AsyncEventProcessor(EventProcessor):
    async def on_event_async(self, event: Event) -> None:
        """Async version of on_event."""

    async def shutdown_async(self) -> None:
        """Async version of shutdown."""
```

### TypedEventProcessor

Auto-dispatches `on_event` to typed handler methods. Override only the events you care about — unhandled types are silently ignored.

```python
class TypedEventProcessor(EventProcessor):
    def on_run_start(self, event: RunStartEvent) -> None: ...
    def on_run_end(self, event: RunEndEvent) -> None: ...
    def on_node_start(self, event: NodeStartEvent) -> None: ...
    def on_node_end(self, event: NodeEndEvent) -> None: ...
    def on_node_error(self, event: NodeErrorEvent) -> None: ...
    def on_route_decision(self, event: RouteDecisionEvent) -> None: ...
    def on_interrupt(self, event: InterruptEvent) -> None: ...
    def on_stop_requested(self, event: StopRequestedEvent) -> None: ...
    def on_cache_hit(self, event: CacheHitEvent) -> None: ...
```

**Example — timing processor:**

```python
from hypergraph import TypedEventProcessor, NodeEndEvent

class SlowNodeDetector(TypedEventProcessor):
    def on_node_end(self, event: NodeEndEvent) -> None:
        if event.duration_ms > 1000:
            print(f"⚠️  Slow node: {event.node_name} ({event.duration_ms:.0f}ms)")
```

---

## RichProgressProcessor

Hierarchical Rich progress bars for graph execution. Requires the `rich` package.

```bash
pip install 'hypergraph[progress]'
# or
pip install rich
```

### Usage

```python
from hypergraph import SyncRunner, RichProgressProcessor

runner = SyncRunner()
result = runner.run(graph, inputs, event_processors=[RichProgressProcessor()])
```

### Constructor

```python
class RichProgressProcessor(TypedEventProcessor):
    def __init__(
        self,
        *,
        transient: bool = True,
        force_mode: Literal["tty", "non-tty", "auto"] = "auto",
    ) -> None: ...
```

**Args:**
- `transient` - If `True` (default), progress bars are removed after completion. Set to `False` to keep them visible.
- `force_mode` - Controls output mode:
  - `"auto"` (default): detect via `stdout.isatty()`
  - `"tty"`: force Rich live bars
  - `"non-tty"`: force plain-text milestone logging (useful for CI/log pipelines)

### Visual Output

**Single run:**

```text
📦 my_graph ━━━━━━━━━━━━━━━━━━━━ 100% 3/3
```

**Nested graph:**

```text
📦 outer_graph ━━━━━━━━━━━━━━━━━ 100% 3/3
  🌳 inner_rag ━━━━━━━━━━━━━━━━━ 100% 2/2
```

**Map operation:**

```text
🗺️ scrape_graph Progress ━━━━━━━ 100% 50/50
  📦 fetch ━━━━━━━━━━━━━━━━━━━━━ 100% 50/50
  📦 parse ━━━━━━━━━━━━━━━━━━━━━ 100% 50/50
```

**Map with failures:**

```text
🗺️ scrape_graph Progress ━━━━━━━ 100% 50/50 (3 failed)
  📦 fetch ━━━━━━━━━━━━━━━━━━━━━ 100% 50/50
  📦 parse ━━━━━━━━━━━━━━━━━━━━━  94% 47/50
```

### Non-TTY Milestones

In non-TTY mode, map progress is logged at fixed milestones (10%, 25%, 50%, 75%, 100%) instead of rendering live bars:

```text
[14:20:00] 🗺️ scrape_graph: 50% (50/100)
```

### Visual Conventions

| Icon | Meaning |
|------|---------|
| 📦 | Regular node (depth 0) |
| 🌳 | Nested graph node (depth > 0) |
| 🗺️ | Map-level progress bar |

Indentation reflects nesting depth. Failed nodes show `[red]FAILED[/red]` in the description.

---

## EventDispatcher

Manages processor lifecycle and event delivery. You typically don't interact with this directly — runners create it internally from `event_processors`.

```python
class EventDispatcher:
    def __init__(self, processors: list[EventProcessor] | None = None) -> None: ...

    @property
    def active(self) -> bool:
        """True if there is at least one registered processor."""

    def emit(self, event: Event) -> None:
        """Send event to every processor synchronously."""

    async def emit_async(self, event: Event) -> None:
        """Send event to every processor, using async when available."""

    def shutdown(self) -> None:
        """Shut down all processors. Best-effort."""

    async def shutdown_async(self) -> None:
        """Shut down all processors, using async when available."""
```

---

## Event Sequence

A typical DAG run emits events in this order:

```text
RunStartEvent(graph_name="rag")
  NodeStartEvent(node_name="embed")
  NodeEndEvent(node_name="embed")
  NodeStartEvent(node_name="retrieve")
  NodeEndEvent(node_name="retrieve")
  NodeStartEvent(node_name="generate")
  NodeEndEvent(node_name="generate")
RunEndEvent(graph_name="rag", status="completed")
```

For cached nodes, the sequence includes a `CacheHitEvent`:

```text
NodeStartEvent(node_name="embed")
CacheHitEvent(node_name="embed", cache_key="abc123...")
NodeEndEvent(node_name="embed", cached=True, duration_ms=0.0)
```

For map operations, each item gets its own `RunStartEvent`/`RunEndEvent` pair nested under the map's span:

```text
RunStartEvent(is_map=True, map_size=3)
  RunStartEvent(graph_name="pipeline")   # item 1
    NodeStartEvent / NodeEndEvent ...
  RunEndEvent(graph_name="pipeline")
  RunStartEvent(graph_name="pipeline")   # item 2
    ...
  RunEndEvent(graph_name="pipeline")
  ...
RunEndEvent(status="completed")
```

For nested graphs, `parent_span_id` links inner events to the outer run:

```text
RunStartEvent(graph_name="outer")
  NodeStartEvent(node_name="validate")
  NodeEndEvent(node_name="validate")
  NodeStartEvent(node_name="rag")        # GraphNode start
    RunStartEvent(graph_name="rag", parent_span_id=<outer_span>)
      NodeStartEvent(node_name="embed")
      NodeEndEvent(node_name="embed")
      ...
    RunEndEvent(graph_name="rag")
  NodeEndEvent(node_name="rag")          # GraphNode end
RunEndEvent(graph_name="outer")
```
