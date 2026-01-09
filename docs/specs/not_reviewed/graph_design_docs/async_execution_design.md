# Async Execution Design

> hypergraph V2 execution model for sync and async nodes.

---

## Runners

| Runner | Node types | Use case |
|--------|-----------|----------|
| `SyncRunner` | `def` only | Simple pipelines, scripts |
| `AsyncRunner` | `def` + `async def` | I/O-bound workloads (LLM, APIs) |

```python
# SyncRunner - sequential execution
runner = SyncRunner()
result = runner.run(graph, inputs={"x": 5})

# AsyncRunner - async support with concurrency
runner = AsyncRunner()
result = await runner.run(graph, inputs={"x": 5})
```

**SyncRunner constraint**: Raises `TypeError` if graph contains `async def` nodes.

---

## Node Execution Rules (AsyncRunner)

### Independent `async def` nodes run concurrently

```python
@node(output_name="a")
async def fetch_a(x: int) -> int:
    return await api_a.call(x)

@node(output_name="b")
async def fetch_b(x: int) -> int:
    return await api_b.call(x)

@node(output_name="c")
def combine(a: int, b: int) -> int:
    return a + b
```

```
Step 1: asyncio.gather(fetch_a(), fetch_b())  # concurrent
Step 2: combine()
```

### `def` nodes run sequentially

Sync nodes run one at a time. No thread pool by default (GIL makes it pointless for CPU-bound work).

### Mixed async + sync in same generation

1. Async nodes gathered first (concurrent)
2. Then sync nodes run (sequential)

```
Generation 0: fetch (async), compute (sync)
  Phase 1: await fetch()
  Phase 2: compute()
```

### Want sync code concurrent? Wrap it.

```python
@node(output_name="data")
async def fetch_data(url: str) -> dict:
    return await asyncio.to_thread(requests.get, url).json()
```

---

## Map Execution

### Concurrency control

```python
results = await runner.map(
    graph,
    inputs={"query": queries},
    map_over="query",
    max_concurrency=10,  # Default: 10 items in flight
)
```

| Setting | Use case |
|---------|----------|
| `max_concurrency=100` | High throughput |
| `max_concurrency=1` | Debugging, strict rate limits |
| `max_concurrency=None` | Unlimited (careful!) |

### Execution order: DFS vs BFS

| Order | Behavior |
|-------|----------|
| **Depth-First (DFS)** | Each item completes full graph before next |
| **Breadth-First (BFS)** | All items at node 1, then all at node 2 |

**Rule**: BFS activates only when BOTH conditions are true:
1. In `.map()` mode
2. Graph contains DualNodes

Otherwise: always DFS.

---

## DualNode

Provides two implementations—`singular` for single items, `batch` for vectorized execution:

```python
node = DualNode(
    output_name="doubled",
    singular=lambda x: x * 2,
    batch=lambda x: pc.multiply(x, 2),  # PyArrow
)
```

- `.run()` → uses `singular`
- `.map()` without DualNodes → uses `singular` (DFS)
- `.map()` with DualNodes → uses `batch` (BFS)

In BFS mode, node types still get their optimal execution: async nodes use `asyncio.gather()`, DualNodes use `batch`, sync nodes run sequentially.

---

## Streaming

```python
async with runner.iter(graph, inputs={"x": 5}) as run:
    async for event in run:
        match event:
            case NodeEndEvent(node_name=name, outputs=outputs):
                print(f"{name} → {outputs}")

print(run.result)
```

---

## Nested Graphs

Nested pipelines are opaque to the parent runner. By default, they inherit the parent's runner. Override only when needed:

```python
# Simple: inner inherits parent's AsyncRunner
inner = Graph(nodes=[node_a, node_b])
outer = Graph(nodes=[inner.as_node(), other_node])
await AsyncRunner().run(outer, inputs={...})

# Override: inner needs batch processing
inner = Graph(nodes=[batch_node1, batch_node2])
outer = Graph(nodes=[inner.as_node(runner=DaftEngine()), async_node])
await AsyncRunner().run(outer, inputs={...})
```

### Runner resolution order

1. Explicit `runner=` on `.as_node()` (override)
2. Parent runner (inheritance)
3. `SyncRunner` (default)

### Cross-runner execution

| Parent Runner | Nested Runner | Execution |
|---------------|---------------|-----------|
| `AsyncRunner` | `DaftEngine` (sync) | `asyncio.to_thread()` |
| `AsyncRunner` | `AsyncRunner` | `await` nested |
| `SyncRunner` | `DaftEngine` | Direct call |
| `SyncRunner` | `AsyncRunner` | `asyncio.run()` |
| `SyncRunner` | None (inherit) + async nodes | **Error** |

### Rules

1. The `runner=` parameter on `.as_node()` is optional—use it as an escape hatch
2. If no runner specified, inherit from parent; if no parent, use `SyncRunner`
3. Inherited runner must be compatible—`SyncRunner` with async nodes in nested → runtime error
4. Sync nested runners run in thread pool when parent is async (avoids blocking event loop)
5. Some runners optimize nested graphs: `DaftRunner` expands the nested graph into its DataFrame plan only if the nested pipeline has no runner or the exact same `DaftRunner` instance. Otherwise, it wraps the entire nested pipeline as a single UDF.

---

## Summary

| Aspect | Rule |
|--------|------|
| `async def` nodes | Concurrent via `asyncio.gather()` |
| `def` nodes | Sequential |
| Mixed generation | Async first, then sync |
| `.map()` concurrency | Default `max_concurrency=10` |
| BFS activation | `.map()` AND DualNodes present |
| Nested runners | Inherit or specify via `.as_node(runner=...)` |
| SyncRunner + async | Error |
