# Observe Execution

Hypergraph's event system lets you observe graph execution without modifying your workflow logic. Pass event processors to blocking `runner.run()` / `runner.map()` or background `runner.start_run()` / `runner.start_map()` calls to receive events as they happen.

Use native inspection when a person needs to investigate one current run. Use
events or OpenTelemetry when software must consume many runs:

```python
# Before: inspect status and logs separately after settlement.
result = runner.run(graph, values, error_handling="continue")
print(result.status, result.log, result.failure)

# After: capture successful values and return one explicit inspect view.
result = runner.run(graph, values, inspect=True, error_handling="continue")
result.inspect()
```

The inspect view stays inside Hypergraph and does not require a checkpointer.
See [Debug Workflows](debug-workflows.md) for capture limits, saved-notebook
sensitivity, degraded views, and map failure drill-down.

## Rich Progress Bars

The fastest way to observe execution — hierarchical progress bars powered by Rich.

```bash
pip install 'hypergraph-ai[progress]'
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

## OpenTelemetry Export

Use OpenTelemetry when you want Hypergraph runs to show up in external
observability backends such as Jaeger, Honeycomb, Datadog, or Logfire.
Hypergraph's native `result.inspect()` view, failure evidence, and checkpoint
tools remain the primary debugging experience; OTel is the export layer.

```bash
pip install 'hypergraph-ai[otel]'
```

```python
from hypergraph import SyncRunner
from hypergraph.events.otel import OpenTelemetryProcessor

runner = SyncRunner()
result = runner.run(
    graph,
    inputs,
    event_processors=[OpenTelemetryProcessor()],
)
```

Hypergraph emits:

- Run spans for graph and `map()` scopes
- Child node spans for node execution
- Span events for supersteps, routing, cache hits, pauses, stops, forks, resumes, and retries
- Explicit attributes such as `workflow_id`, `run_id`, `item_index`, `graph_name`, `node_name`, and batch summary counts

### Attempt Span Events

A retrying or timeout-bearing node stays ONE logical node span. Each callable
invocation projects as a pair of span events on that span —
`hypergraph.attempt.start` and `hypergraph.attempt.end` — carrying
`hypergraph.attempt.*` attributes (`series_id`, one-based `number`,
`max_attempts`, `outcome`, `settlement`, `deadline_scope`,
`deadline_elapsed`, `cancellation_requested`, `duration_ms`, `error_type`,
`retry_scheduled`, `retry_not_before`).

Intermediate failed attempts never mark the node span as error and never
close it; only the terminal escaping failure sets error status. A cache hit
opens no attempts and emits no attempt span events.

### Export Privacy

Exported spans follow the diagnostics privacy boundary: span error status,
`exception.message`, and attempt `error_type` attributes carry the safe
projection — exception type names and stable `hypergraph.diagnostic.code`
values — never raw exception message text (which can embed secrets; see the
OTel `exception.message` sensitivity warning). The exact exception object
stays local on `RunResult.error` and `get_failure_evidence(...)`. See
[Errors — diagnostic code registry](../06-api-reference/errors.md#diagnostic-code-registry).

### Tag Spans and Keep the Global Tracer Untouched

`OpenTelemetryProcessor` accepts two constructor options for embedding
hypergraph telemetry inside a host platform:

```python
from opentelemetry.sdk.trace import TracerProvider
from hypergraph.events.otel import OpenTelemetryProcessor

provider = TracerProvider()  # private provider — configure exporters yourself

processor = OpenTelemetryProcessor(
    extra_attributes={"deployment.environment": "staging"},
    tracer_provider=provider,
)
```

- `extra_attributes` is merged onto **every** span the processor creates —
  run root spans (`graph …`/`map …`) and node spans alike. All spans rather
  than the root only: it is cheap, and lets backends filter on any span.
  Hypergraph's own attributes win on key collisions.
- `tracer_provider` writes spans on the provider you pass instead of the
  global one. The global tracer provider is neither consulted nor modified —
  a host can keep its provider fully private. With the default `None`, the
  tracer is looked up on the global provider exactly as before.

Typical hierarchy:

```text
graph outer
└── node inner
    └── graph inner
        └── node double
```

### Ambient Context: Third-Party Telemetry Nests Under Node Spans

While a run executes, `OpenTelemetryProcessor` makes its spans the **ambient
OTel context** for the code they cover — the run root around the run, each
node span around that node's body. Any OTel-instrumented library called
inside a node (an openinference-instrumented OpenAI client, an agent SDK, a
database driver) parents its spans under the node span automatically, with
zero coupling on either side:

```python
from opentelemetry import trace
from hypergraph import Graph, SyncRunner, node
from hypergraph.events.otel import OpenTelemetryProcessor

tracer = trace.get_tracer("my.instrumentation")

@node(output_name="answer")
def call_llm(prompt: str) -> str:
    # Any instrumented call here — this explicit span stands in for one —
    # picks up the node span as its parent from the ambient context.
    with tracer.start_as_current_span("ChatCompletion"):
        return llm.complete(prompt)

SyncRunner().run(Graph([call_llm], name="rag"), {"prompt": "hi"},
                 event_processors=[OpenTelemetryProcessor()])
```

```text
graph rag
└── node call_llm
    └── ChatCompletion        ← nested under the node, same trace
```

Without ambient activation the `ChatCompletion` span would attach to
whatever context was current before `.run()` — a sibling of the node at
best, a separate trace when no outer span existed. Activation holds for
`SyncRunner`, `AsyncRunner` (concurrent nodes each get their own context —
no cross-node or cross-map-item leakage), and per-item map runs. The
previous ambient context is restored when the run ends, including on
failure. Runs without an `OpenTelemetryProcessor` never touch OTel context
(nothing is imported or attached).

Span *parentage* is unchanged by activation — hypergraph still parents its
own run and node spans explicitly, so exported span structure is identical
to before; activation only changes what
`opentelemetry.context.get_current()` returns inside the bracketed code.

To correlate your own logging with the current node span from inside a node body, call `current_node_span()`:

```python
from hypergraph import Graph, node, SyncRunner, current_node_span

@node(output_name="result")
def do_work(x: int) -> int:
    ref = current_node_span()
    # NodeSpanRef(run_id="...", span_id="...", node_name="do_work", graph_name=...)
    my_logger.info("processing", extra={"span_id": ref.span_id if ref else None})
    return x * 2

runner = SyncRunner()
runner.run(Graph([do_work]), {"x": 5})
```

`current_node_span()` returns `None` outside of node execution (for example, if called at module import time).

Mapped work uses a parent `map` span plus child graph spans per item:

```text
map evaluate_batch
├── graph evaluate_batch   item_index=0
├── graph evaluate_batch   item_index=1
└── graph evaluate_batch   item_index=2
```

Parent `map` spans export aggregate outcome attributes instead of vague blobs:

- `hypergraph.batch.total_items`
- `hypergraph.batch.completed_items`
- `hypergraph.batch.failed_items`
- `hypergraph.batch.paused_items`
- `hypergraph.batch.stopped_items`
- `hypergraph.batch.outcome`

These counts describe real settled child outcomes. If Maya stops a ten-item
background map after four items were claimed, OTel exports counts for those
four real children and `hypergraph.batch.outcome="stopped"`. Read
`MapResult.requested_count == 10` and `unstarted_item_indexes` for the requested
scope; Hypergraph does not add synthetic child spans or a new OTel requested
count.

Rich native debugging data stays inside Hypergraph on purpose:

- Raw inputs and outputs
- Checkpoint snapshots
- Streamed chunks
- Inspect-only UI payloads

`StopRequestedEvent.info` preserves the first accepted stop request. The OTel
stop span event records that the request occurred but intentionally does not
export the arbitrary `info` payload. Repeating `handle.stop(...)` or
`runner.stop(...)` does not rewrite native event metadata. A stopped background
map always emits one parent-level stop event, even if no child was claimed.

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

## Carry Processors on the Graph

Instead of threading `event_processors=` through every call site, a graph can
carry its own default processors. The instrumented graph survives being handed
to any runner:

```python
instrumented = graph.with_processors(OpenTelemetryProcessor())

SyncRunner().run(instrumented, inputs)                  # spans exported
await AsyncRunner().map(instrumented, items, map_over="x")  # spans exported
```

`with_processors(...)` returns a **new** graph (immutable, like `bind()`), and
is accumulative — each call appends to what the graph already carries. The
repr shows what a graph carries:

```python
>>> instrumented
Graph: my_graph | 3 nodes | 2 edges | no cycles · 1 processor
```

Two contracts hold (see the [Events API Reference](../06-api-reference/events.md#overview)):

1. Processors observe only and are failure-isolated — a raising processor is
   logged, never breaking the run.
2. Carried processors **merge** with call-site processors, never replace:
   runners dispatch to `[*graph.default_event_processors, *event_processors]`.

Notes:

- `map()` delivers the top-level map events and every per-item event to
  carried processors; `map_iter()` delegates through `run()` per item, so
  carried processors observe each item (there is no top-level map span).
- Nested `GraphNode` sub-runs forward carried processors like call-site ones,
  so an outer graph's processor observes inner-graph events too.
- `DaftRunner` does not support events — it warns and ignores carried
  processors, exactly as it does for explicit `event_processors`.

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
