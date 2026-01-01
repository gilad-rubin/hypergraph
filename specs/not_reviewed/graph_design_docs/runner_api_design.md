# Runner API Design

> **Status**: Design Decision  
> **Date**: December 2025  
> **Decision**: Use Runner pattern to separate graph definition from execution configuration

---

## Bottom Line

**Graph defines structure. Runner handles execution.**

```python
from hypernodes import Graph, node
from hypernodes import Runner, AsyncRunner, DiskCache

# Define graph (pure structure, no execution config)
@node(output_name="answer")
def generate(query: str) -> str:
    return llm.invoke(query)

graph = Graph(nodes=[generate])

# Run with configuration
runner = Runner(cache=DiskCache("./cache"))
result = runner.run(graph, inputs={"query": "hello"})

# Async execution
async_runner = AsyncRunner(cache=DiskCache("./cache"))
result = await async_runner.run(graph, inputs={"query": "hello"})
```

**Key principle:** Users must create a Runner to execute graphs. No `graph.run()` shortcut (for now).

---

## API Reference

### Graph (Pure Definition)

```python
class Graph:
    """Defines the structure of a computation graph. No execution logic."""
    
    def __init__(self, nodes: list[HyperNode]): ...
    
    def visualize(self, ...): ...
    
    # No run() method - use a Runner
```

### Runner (Sync Execution)

```python
class Runner:
    """Executes graphs synchronously with optional caching and callbacks."""
    
    def __init__(
        self,
        cache: Cache | None = None,
        callbacks: list[Callback] | None = None,
    ): ...
    
    def run(
        self,
        graph: Graph,
        inputs: dict[str, Any],
        output_name: str | list[str] | None = None,
    ) -> dict[str, Any]: ...
    
    def map(
        self,
        graph: Graph,
        inputs: dict[str, Any],
        map_over: str | list[str],
        map_mode: Literal["zip", "product"] = "zip",
        output_name: str | list[str] | None = None,
    ) -> list[dict[str, Any]]: ...
```

### AsyncRunner (Async Execution)

```python
class AsyncRunner:
    """Executes graphs asynchronously. Required for streaming and full interrupt control."""
    
    def __init__(
        self,
        cache: Cache | None = None,
        callbacks: list[Callback] | None = None,
    ): ...
    
    async def run(
        self,
        graph: Graph,
        inputs: dict[str, Any],
        handlers: dict[str, Callable] | None = None,  # Interrupt handlers
        output_name: str | list[str] | None = None,
    ) -> dict[str, Any]: ...
    
    async def map(
        self,
        graph: Graph,
        inputs: dict[str, Any],
        map_over: str | list[str],
        map_mode: Literal["zip", "product"] = "zip",
        output_name: str | list[str] | None = None,
    ) -> list[dict[str, Any]]: ...
    
    def iter(
        self,
        graph: Graph,
        inputs: dict[str, Any],
        **kwargs,
    ) -> GraphRun:
        """Returns async context manager for event streaming."""
        ...
```

### Specialized Runners

```python
from hypernodes.runners import DaftRunner, DaskRunner

# Distributed execution
runner = DaftRunner(cache=DiskCache("./cache"))
results = runner.map(graph, inputs={"query": queries}, map_over="query")
```

---

## Usage Examples

### Basic Execution

```python
from hypernodes import Graph, node, Runner

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

graph = Graph(nodes=[double])
runner = Runner()

result = runner.run(graph, inputs={"x": 5})
print(result["doubled"])  # 10
```

### With Caching

```python
from hypernodes import Runner, DiskCache

runner = Runner(cache=DiskCache("./cache"))
result = runner.run(graph, inputs={"x": 5})  # Computed
result = runner.run(graph, inputs={"x": 5})  # Cached
```

### With Callbacks

```python
from hypernodes import Runner
from hypernodes.callbacks import ProgressCallback

runner = Runner(callbacks=[ProgressCallback()])
result = runner.run(graph, inputs={"x": 5})
```

### Async Execution (Jupyter / Production)

```python
from hypernodes import AsyncRunner

runner = AsyncRunner(cache=DiskCache("./cache"))

# In Jupyter or async function
result = await runner.run(graph, inputs={"query": "hello"})
```

### Streaming Events

```python
from hypernodes import AsyncRunner

runner = AsyncRunner()

async with runner.iter(graph, inputs={"query": "hello"}) as run:
    async for event in run:
        if isinstance(event, StreamingChunkEvent):
            print(event.chunk, end="")
        elif isinstance(event, InterruptEvent):
            run.respond({"choice": "approve"})

print(run.result["answer"])
```

### Batch Processing

```python
from hypernodes import Runner

runner = Runner(cache=DiskCache("./cache"))

results = runner.map(
    graph,
    inputs={"query": ["Q1", "Q2", "Q3"]},
    map_over="query",
)
# Returns: [{"answer": "A1"}, {"answer": "A2"}, {"answer": "A3"}]
```

### Reusing Runner Across Graphs

```python
# Same runner config for multiple graphs
runner = Runner(cache=DiskCache("./cache"), callbacks=[ProgressCallback()])

result1 = runner.run(rag_graph, inputs={"query": "What is RAG?"})
result2 = runner.run(summary_graph, inputs={"text": long_document})
result3 = runner.run(eval_graph, inputs={"prediction": result1["answer"]})
```

---

## Considerations

### Why Not `graph.run()`?

We considered adding a convenience method:

```python
# Rejected for now
graph = Graph(nodes=[...])
result = graph.run(inputs={...})  # Uses internal default runner
```

**Reasons to avoid this:**

| Concern | Problem |
|---------|---------|
| Hidden default | "Where is my cache configured?" debugging |
| Two patterns | Users learn `graph.run()`, then need to unlearn for `runner.run()` |
| Muddied concepts | Graph becomes both definition AND executor |
| Magic behavior | What engine is `graph.run()` using? Sync? Async? |

**We may add this later** as sugar once the runner pattern is established and understood.

### Why Not Cache on Graph?

```python
# Rejected
graph = Graph(nodes=[...], cache=DiskCache("./cache"))
```

**Reasons:**

1. **Cache is execution-specific** — Different runners might cache differently
2. **Same graph, different caches** — You might want prod cache vs test cache
3. **Separation of concerns** — Graph is structure, Runner is behavior

### Why "Runner" Instead of "Engine"?

| Term | Connotation |
|------|-------------|
| Engine | Internal machinery, implementation detail |
| Runner | "Thing that runs stuff" — user-facing, action-oriented |

"Runner" is more intuitive for the user mental model: *"I have a graph, I need a runner to run it."*

### Sync vs Async: Why Separate Classes?

We considered:

```python
# Option A: Dual methods (rejected)
runner.run(graph, ...)        # Sync
await runner.run_async(graph, ...)  # Async

# Option B: Separate classes (chosen)
Runner().run(graph, ...)           # Sync
await AsyncRunner().run(graph, ...)  # Async
```

**Why separate classes:**

1. **Clear intent** — You know at construction time whether you're in sync or async mode
2. **Type safety** — `Runner.run()` returns `dict`, `AsyncRunner.run()` returns `Awaitable[dict]`
3. **No confusion** — No "when do I use which method?" question
4. **Consistency** — Matches the engine pattern already in use (DaftRunner, DaskRunner)

### What About Jupyter?

Jupyter has a running event loop, so:

| Runner | In Jupyter |
|--------|------------|
| `Runner` | ⚠️ May need `nest_asyncio` for async nodes |
| `AsyncRunner` | ✅ Native `await` works |

**Recommendation:** Use `AsyncRunner` in Jupyter notebooks.

```python
# Jupyter cell
runner = AsyncRunner(cache=DiskCache("./cache"))
result = await runner.run(graph, inputs={"query": "hello"})
```

---

## Feature Matrix

| Feature | Runner | AsyncRunner | DaftRunner |
|---------|--------|-------------|------------|
| Sync API | ✅ | ❌ | ✅ |
| Async API | ❌ | ✅ | ❌ |
| `.run()` | ✅ | ✅ | ✅ |
| `.map()` | ✅ | ✅ | ✅ |
| `.iter()` (events) | ❌ | ✅ | ❌ |
| Streaming via callbacks | ✅ | ✅ | ✅ |
| Streaming via events | ❌ | ✅ | ❌ |
| Interrupt handlers | ✅ (sync) | ✅ (async) | ❌ |
| Manual interrupt control | ❌ | ✅ (via `.iter()`) | ❌ |
| Concurrent async nodes | ⚠️ Serialized | ✅ Parallel | ✅ Parallel |
| Distributed execution | ❌ | ❌ | ✅ |
| Works in scripts | ✅ | ⚠️ Needs wrapper | ✅ |
| Works in Jupyter | ⚠️ | ✅ | ✅ |

---

## Migration from V1

V1 pattern:
```python
# V1 - cache on engine, engine on pipeline
engine = SeqEngine(cache=DiskCache("./cache"))
pipeline = Pipeline(nodes=[...], engine=engine)
result = pipeline.run(inputs={...})
```

V2 pattern:
```python
# V2 - runner is separate, graph is pure
graph = Graph(nodes=[...])
runner = Runner(cache=DiskCache("./cache"))
result = runner.run(graph, inputs={...})
```

**Key changes:**

| V1 | V2 |
|----|-----|
| `Pipeline` | `Graph` |
| `SeqEngine` | `Runner` |
| `engine=` on Pipeline | Runner is separate object |
| `pipeline.run()` | `runner.run(graph, ...)` |

---

## Future Considerations

### Possible Sugar (Not Now)

If user feedback shows the runner pattern is too verbose for simple cases, we could add:

```python
# Possible future sugar
result = graph.run(inputs={...})  # Uses Runner() internally
result = graph.run(inputs={...}, cache=DiskCache("./cache"))  # Creates Runner with cache
```

**Decision:** Wait for user feedback before adding this. Start with explicit runner pattern.

### Global Runner Configuration

For notebooks or scripts with many graphs:

```python
# Possible future pattern
import hypernodes as hn

hn.set_default_runner(Runner(cache=DiskCache("./cache")))

# All graphs use default runner
result = hn.run(graph, inputs={...})
```

**Decision:** Not implementing now. Explicit is better than implicit.

---

## Summary

| Principle | Decision |
|-----------|----------|
| Graph responsibility | Structure only, no execution |
| Execution configuration | Lives on Runner |
| Sync vs Async | Separate classes (Runner, AsyncRunner) |
| Default runner | None — user must create one |
| `graph.run()` sugar | Not now, maybe later |
| Naming | "Runner" (user-facing) not "Engine" (internal) |
