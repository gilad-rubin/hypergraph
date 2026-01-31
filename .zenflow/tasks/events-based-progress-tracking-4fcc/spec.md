# Technical Specification: Events-Based Progress Tracking

## Technical Context

- **Language**: Python 3.10+
- **New dependency**: `rich` (optional extra: `hypergraph[rich]`)
- **Existing patterns**: Runners use executor dispatch, superstep-based execution, `GraphState` for runtime state
- **Key files touched**: `runners/base.py`, `runners/sync/runner.py`, `runners/async_/runner.py`, superstep modules, new `events/` package

---

## Implementation Approach

### Module Structure

```
src/hypergraph/events/
├── __init__.py          # Public exports
├── types.py             # Event dataclasses + Event union type
├── processor.py         # EventProcessor, AsyncEventProcessor, TypedEventProcessor
├── dispatcher.py        # EventDispatcher (internal, manages processor list + emission)
└── rich_progress.py     # RichProgressProcessor
```

### Why a separate `events/` package?
- Keeps event types and processors decoupled from runner internals
- `rich_progress.py` imports `rich` lazily — the module only fails if you actually instantiate `RichProgressProcessor` without `rich` installed
- Clean public API: `from hypergraph.events import EventProcessor, RichProgressProcessor`

---

## Data Model: Event Types

All events are frozen dataclasses inheriting common fields:

```python
@dataclass(frozen=True)
class BaseEvent:
    run_id: str                   # Unique per .run()/.map() invocation
    span_id: str                  # Unique per event scope
    parent_span_id: str | None    # Links to parent (None for root run)
    timestamp: float              # time.time()

@dataclass(frozen=True)
class RunStartEvent(BaseEvent):
    graph_name: str
    workflow_id: str | None
    is_map: bool = False
    map_size: int | None = None   # Number of items if is_map

@dataclass(frozen=True)
class RunEndEvent(BaseEvent):
    graph_name: str
    status: str                   # "completed" | "failed"
    error: str | None = None
    duration_ms: float = 0.0

@dataclass(frozen=True)
class NodeStartEvent(BaseEvent):
    node_name: str
    graph_name: str

@dataclass(frozen=True)
class NodeEndEvent(BaseEvent):
    node_name: str
    graph_name: str
    duration_ms: float = 0.0

@dataclass(frozen=True)
class NodeErrorEvent(BaseEvent):
    node_name: str
    graph_name: str
    error: str
    error_type: str

@dataclass(frozen=True)
class RouteDecisionEvent(BaseEvent):
    node_name: str
    graph_name: str
    decision: str | list[str]

@dataclass(frozen=True)
class InterruptEvent(BaseEvent):
    node_name: str
    graph_name: str
    workflow_id: str | None
    value: object
    response_param: str

@dataclass(frozen=True)
class StopRequestedEvent(BaseEvent):
    workflow_id: str | None

# Union type for dispatch
Event = (RunStartEvent | RunEndEvent | NodeStartEvent | NodeEndEvent |
         NodeErrorEvent | RouteDecisionEvent | InterruptEvent | StopRequestedEvent)
```

**Design decisions**:
- `frozen=True` — events are immutable data
- No `inputs`/`outputs` fields on NodeStart/NodeEnd for now (privacy, serialization cost). Can add later behind a flag.
- `graph_name` on node events enables the progress bar to distinguish nesting levels without tracking span trees

---

## Processor Interface

```python
class EventProcessor:
    """Base class for event consumers. All methods are no-ops by default."""
    def on_event(self, event: Event) -> None: ...
    def shutdown(self) -> None: ...

class AsyncEventProcessor(EventProcessor):
    """Extends EventProcessor with async variants."""
    async def on_event_async(self, event: Event) -> None: ...
    async def shutdown_async(self) -> None: ...

class TypedEventProcessor(EventProcessor):
    """Auto-dispatches on_event() to typed methods like on_node_start()."""
    def on_event(self, event: Event) -> None:
        method_name = _event_to_method_name(event)  # e.g. NodeStartEvent -> on_node_start
        method = getattr(self, method_name, None)
        if method:
            method(event)

    # Override these in subclasses:
    def on_run_start(self, event: RunStartEvent) -> None: ...
    def on_run_end(self, event: RunEndEvent) -> None: ...
    def on_node_start(self, event: NodeStartEvent) -> None: ...
    def on_node_end(self, event: NodeEndEvent) -> None: ...
    def on_node_error(self, event: NodeErrorEvent) -> None: ...
    def on_route_decision(self, event: RouteDecisionEvent) -> None: ...
    def on_interrupt(self, event: InterruptEvent) -> None: ...
    def on_stop_requested(self, event: StopRequestedEvent) -> None: ...
```

---

## EventDispatcher (Internal)

The dispatcher is an internal class used by runners to emit events to processors:

```python
class EventDispatcher:
    def __init__(self, processors: list[EventProcessor]):
        self._processors = processors

    def emit(self, event: Event) -> None:
        for processor in self._processors:
            try:
                processor.on_event(event)
            except Exception:
                logger.warning("EventProcessor %s failed on %s", processor, event, exc_info=True)

    async def emit_async(self, event: Event) -> None:
        for processor in self._processors:
            try:
                if isinstance(processor, AsyncEventProcessor):
                    await processor.on_event_async(event)
                else:
                    processor.on_event(event)
            except Exception:
                logger.warning(...)

    def shutdown(self) -> None:
        for processor in self._processors:
            try:
                processor.shutdown()
            except Exception:
                logger.warning(...)

    @property
    def active(self) -> bool:
        return len(self._processors) > 0
```

Key: `emit()` is best-effort — exceptions are caught and logged, never propagated.

---

## Runner Integration

### API Change

```python
# Before
runner.run(graph, values)

# After
runner.run(graph, values, event_processors=[RichProgressProcessor()])
```

`event_processors` parameter added to `BaseRunner.run()` and `BaseRunner.map()`.

### Where Events Are Emitted

| Location | Event |
|----------|-------|
| `SyncRunner.run()` / `AsyncRunner.run()` start | `RunStartEvent` |
| `SyncRunner.run()` / `AsyncRunner.run()` end | `RunEndEvent` |
| `run_superstep_sync()` before executor call | `NodeStartEvent` |
| `run_superstep_sync()` after executor returns | `NodeEndEvent` |
| `run_superstep_sync()` on exception | `NodeErrorEvent` |
| `SyncRouteNodeExecutor` / `SyncIfElseNodeExecutor` after decision | `RouteDecisionEvent` |

### Nested Graph Event Propagation

The key design challenge. When a `GraphNode` executor calls `runner.run()` for the inner graph, that inner `run()` must:
1. Receive the same `event_processors` list
2. Use a `parent_span_id` pointing to the outer node's span

**Approach**: Pass `event_processors` and `parent_span_id` through the runner's internal `_execute_graph()` method (not public API). The `GraphNodeExecutor` already holds a reference to the runner, so it calls `runner.run()` which naturally threads through.

Concretely:
- `run()` creates a root `span_id` and `run_id`, stores them + `event_processors` on a `_RunContext` (simple dataclass or passed as kwargs to internal methods)
- `_execute_graph()` receives the context
- `run_superstep_sync()` receives the dispatcher + current `parent_span_id`
- When `GraphNodeExecutor` calls `runner.run()` for the nested graph, it passes the same `event_processors` — the inner `run()` creates its own `RunStartEvent` with `parent_span_id` set to the outer node's `span_id`

**Span hierarchy example** (outer graph with nested graph):
```
RunStart(span=A, parent=None)           # outer run
  NodeStart(span=B, parent=A)           # outer node "embed"
  NodeEnd(span=B, parent=A)
  NodeStart(span=C, parent=A)           # outer node "rag" (GraphNode)
    RunStart(span=D, parent=C)          # inner run
      NodeStart(span=E, parent=D)       # inner node "retrieve"
      NodeEnd(span=E, parent=D)
      NodeStart(span=F, parent=D)       # inner node "generate"
      NodeEnd(span=F, parent=D)
    RunEnd(span=D, parent=C)
  NodeEnd(span=C, parent=A)
RunEnd(span=A, parent=None)
```

### Map Integration

For `runner.map()`:
1. Emit `RunStartEvent` with `is_map=True`, `map_size=len(items)`
2. Each individual `run()` within `map()` shares the same `event_processors` and uses the map's `span_id` as `parent_span_id`
3. Emit `RunEndEvent` when all map items complete

---

## RichProgressProcessor

```python
class RichProgressProcessor(TypedEventProcessor):
    """Renders hierarchical progress bars using Rich."""
```

### Internal State

```python
# Maps span_id -> Rich task_id for progress bars
_tasks: dict[str, TaskID]

# Maps span_id -> nesting depth (0 = root)
_depth: dict[str, int]

# Maps span_id -> parent_span_id (for depth calculation)
_parents: dict[str, str | None]

# Maps (graph_name, node_name) at a depth -> task_id (for map aggregation)
_node_tasks: dict[tuple[str, str, int], TaskID]

# Current map context
_map_total: dict[str, int]  # span_id -> total items for map runs
```

### Event Handling Logic

**on_run_start**:
- If `is_map`: Create a map-level progress bar with 🗺️ icon, `total=map_size`
- Record depth based on `parent_span_id`

**on_node_start**:
- Calculate depth from parent chain
- Create or find progress bar for this node
- For map runs: bars aggregate across items (same node shows `completed/total`)
- Icon: 📦 for root-level, 🌳 for nested graph nodes
- Indentation: `"  " * depth`

**on_node_end**:
- Advance the progress bar by 1
- For map runs: also advance the parent map bar when all nodes in an item complete

**on_run_end**:
- If this is a map-item run, advance map-level bar
- If root run, show completion message: `✓ ...completed!`

**on_node_error**:
- Mark bar as failed (red)

### Cyclic Graph Handling

For graphs with cycles, nodes execute multiple times. The progress bar handles this by:
- Using indeterminate total initially (spinner only)
- Updating total dynamically as cycles progress
- Or: showing cumulative count without a fixed total (e.g., `3 iterations`)

### Thread Safety

Rich's `Progress` object is thread-safe. The processor's internal dicts are only mutated from event callbacks, which are called sequentially per the dispatcher's FIFO guarantee.

---

## Source Code Structure Changes

### New Files
```
src/hypergraph/events/__init__.py
src/hypergraph/events/types.py
src/hypergraph/events/processor.py
src/hypergraph/events/dispatcher.py
src/hypergraph/events/rich_progress.py
```

### Modified Files
```
src/hypergraph/runners/base.py              # Add event_processors param
src/hypergraph/runners/sync/runner.py       # Emit events, pass dispatcher
src/hypergraph/runners/sync/superstep.py    # Emit node events
src/hypergraph/runners/async_/runner.py     # Same as sync
src/hypergraph/runners/async_/superstep.py  # Same as sync
src/hypergraph/runners/sync/executors/graph_node.py   # Pass event_processors to nested run
src/hypergraph/runners/async_/executors/graph_node.py  # Same
src/hypergraph/runners/sync/executors/route_node.py    # Emit RouteDecisionEvent
src/hypergraph/runners/sync/executors/ifelse_node.py   # Emit RouteDecisionEvent
src/hypergraph/runners/async_/executors/route_node.py  # Same
src/hypergraph/runners/async_/executors/ifelse_node.py # Same
src/hypergraph/__init__.py                  # Export event types + processors
pyproject.toml                              # Add rich optional dependency
```

### No Changes Needed
- `graph/` — Graph construction is unaffected
- `nodes/` — Node definitions are unaffected
- `runners/_shared/types.py` — RunResult unchanged (events are a separate channel)

---

## Delivery Phases

### Phase 1: Event Types + Processor Interface
- `events/types.py` — All 8 event dataclasses
- `events/processor.py` — EventProcessor, AsyncEventProcessor, TypedEventProcessor
- `events/dispatcher.py` — EventDispatcher
- `events/__init__.py` — Exports
- **Tests**: Unit tests for TypedEventProcessor dispatch, EventDispatcher best-effort semantics

### Phase 2: SyncRunner Event Emission
- Modify `SyncRunner.run()` and `map()` to accept `event_processors`
- Add emission calls in runner + superstep + gate executors
- Pass event context through nested graph execution
- **Tests**: Integration tests that collect events via a simple `ListProcessor` and assert correct event sequences for: simple DAG, cyclic graph, nested graph, map, nested map

### Phase 3: AsyncRunner Event Emission
- Mirror Phase 2 changes for async runner
- Handle async processor dispatch in `EventDispatcher.emit_async()`
- **Tests**: Same scenarios as Phase 2 but async

### Phase 4: RichProgressProcessor
- Implement `RichProgressProcessor(TypedEventProcessor)`
- Handle all 7 visual scenarios from requirements
- **Tests**: Unit tests with mocked Rich Progress, visual integration tests (manual or snapshot)

### Phase 5: Public API + Packaging
- Update `__init__.py` exports
- Add `rich` as optional dependency in `pyproject.toml`
- Update AGENTS.md and README.md

---

## Verification Approach

- **Unit tests**: Event dataclass construction, TypedEventProcessor dispatch, EventDispatcher error handling
- **Integration tests**: `ListProcessor` (collects events into a list) used to assert event sequences for each scenario
- **Capability matrix**: Add `event_processors` as a new capability dimension if it interacts with other features
- **Lint**: `uv run ruff check src/hypergraph/events/`
- **Type check**: `uv run mypy src/hypergraph/events/` (if mypy is configured)
- **Manual verification**: Run Rich progress bar against example graphs to verify visual output
