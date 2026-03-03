# Observe Execution

Hypergraph's event system lets you observe graph execution without modifying your workflow logic. Pass event processors to `runner.run()` or `runner.map()` to receive events as they happen.

## Rich Progress Bars

The fastest way to observe execution — hierarchical progress bars powered by Rich.

```bash
pip install 'hypergraph[progress]'
```

```python
from hypergraph import SyncRunner, RichProgressProcessor

runner = SyncRunner()
result = runner.run(graph, inputs, event_processors=[RichProgressProcessor()])
```

Output:

```
📦 my_graph ━━━━━━━━━━━━━━━━━━━━ 100% 3/3
  🌳 inner_rag ━━━━━━━━━━━━━━━━━ 100% 2/2
```

Works with `map()` too — failed items are tracked automatically:

```python
results = runner.map(graph, {"url": urls}, map_over="url",
                     event_processors=[RichProgressProcessor()])
```

```
🗺️ scrape_graph Progress ━━━━━━━ 100% 50/50 (3 failed)
  📦 fetch ━━━━━━━━━━━━━━━━━━━━━ 100% 50/50
  📦 parse ━━━━━━━━━━━━━━━━━━━━━  94% 47/50
```

### Non-TTY Fallback (CI and Piped Logs)

`RichProgressProcessor` auto-detects whether stdout is a TTY:

- **TTY**: live Rich progress bars (default terminal experience)
- **Non-TTY** (CI, redirected output): plain-text progress logs

In non-TTY mode, map runs log milestone progress at **10%**, **25%**, **50%**, **75%**, and **100%**:

```text
[14:20:00] 🗺️ scrape_graph: 25% (25/100)
[14:20:08] 🗺️ scrape_graph: 50% (50/100)
```

You can override auto-detection for testing/debugging:

```python
from hypergraph import RichProgressProcessor

# Force plain-text mode even in a local terminal
processor = RichProgressProcessor(force_mode="non-tty")

# Force Rich live bars (useful in tests that provide a TTY-like stream)
processor = RichProgressProcessor(force_mode="tty")
```

## Custom Event Processors

### Collect All Events

Use `EventProcessor` to receive every event:

```python
from hypergraph import EventProcessor

class ListProcessor(EventProcessor):
    def __init__(self):
        self.events = []

    def on_event(self, event):
        self.events.append(event)

collector = ListProcessor()
runner.run(graph, inputs, event_processors=[collector])

for event in collector.events:
    print(f"{type(event).__name__}: {event.span_id}")
```

### Handle Specific Event Types

Use `TypedEventProcessor` to handle only the events you care about:

```python
from hypergraph import TypedEventProcessor, NodeEndEvent, NodeErrorEvent

class PerformanceMonitor(TypedEventProcessor):
    def __init__(self, threshold_ms: float = 500):
        self.threshold_ms = threshold_ms
        self.slow_nodes = []

    def on_node_end(self, event: NodeEndEvent) -> None:
        if event.duration_ms > self.threshold_ms:
            self.slow_nodes.append((event.node_name, event.duration_ms))
            print(f"⚠️  {event.node_name}: {event.duration_ms:.0f}ms")

    def on_node_error(self, event: NodeErrorEvent) -> None:
        print(f"❌ {event.node_name}: {event.error_type} - {event.error}")

monitor = PerformanceMonitor(threshold_ms=200)
runner.run(graph, inputs, event_processors=[monitor])
print(f"Slow nodes: {monitor.slow_nodes}")
```

### Async Processors

For async runners, use `AsyncEventProcessor`:

```python
from hypergraph import AsyncEventProcessor, AsyncRunner

class AsyncMetricsProcessor(AsyncEventProcessor):
    async def on_event_async(self, event):
        await metrics_client.send(type(event).__name__, event.timestamp)

    async def shutdown_async(self):
        await metrics_client.flush()

runner = AsyncRunner()
result = await runner.run(graph, inputs,
                          event_processors=[AsyncMetricsProcessor()])
```

The async runner calls `on_event_async` when available, falling back to `on_event` for sync processors. You can mix sync and async processors in the same list.

## Multiple Processors

Pass multiple processors to observe different aspects simultaneously:

```python
result = runner.run(
    graph,
    inputs,
    event_processors=[
        RichProgressProcessor(),    # Visual progress
        PerformanceMonitor(),       # Slow node detection
        ListProcessor(),            # Event collection
    ],
)
```

## Real-World Example: Logging Execution History

```python
import json
from pathlib import Path
from hypergraph import TypedEventProcessor, RunStartEvent, RunEndEvent, NodeEndEvent

class ExecutionLogger(TypedEventProcessor):
    """Write a JSON log of each run for debugging or auditing."""

    def __init__(self, log_dir: str = "logs"):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(exist_ok=True)
        self._entries = []

    def on_run_start(self, event: RunStartEvent) -> None:
        self._run_id = event.run_id
        self._entries = []

    def on_node_end(self, event: NodeEndEvent) -> None:
        self._entries.append({
            "node": event.node_name,
            "duration_ms": event.duration_ms,
            "timestamp": event.timestamp,
        })

    def on_run_end(self, event: RunEndEvent) -> None:
        log = {
            "run_id": self._run_id,
            "graph": event.graph_name,
            "status": event.status,
            "duration_ms": event.duration_ms,
            "nodes": self._entries,
        }
        path = self._log_dir / f"{self._run_id}.json"
        path.write_text(json.dumps(log, indent=2))
```

## Real-World Example: Route Tracing

Track which paths your routing nodes take:

```python
from hypergraph import TypedEventProcessor, RouteDecisionEvent

class RouteTracer(TypedEventProcessor):
    def __init__(self):
        self.decisions = []

    def on_route_decision(self, event: RouteDecisionEvent) -> None:
        self.decisions.append({
            "node": event.node_name,
            "decision": event.decision,
        })

tracer = RouteTracer()
runner.run(agent_graph, inputs, event_processors=[tracer])

for d in tracer.decisions:
    print(f"  {d['node']} → {d['decision']}")
```

## Error Handling

Event processors use best-effort delivery. If a processor raises an exception, the error is logged but execution continues uninterrupted. This ensures observability code never breaks your workflow.

## See Also

- [Events API Reference](../06-api-reference/events.md) — Full type definitions and dispatcher internals
- [Runners API Reference](../06-api-reference/runners.md) — `event_processors` parameter on `run()` and `map()`
