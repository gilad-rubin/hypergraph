# Caching

Skip redundant computation by caching node results. Same inputs produce the same outputs — hypergraph can remember that.

## When to Use

- Expensive computations you call repeatedly with the same inputs (embeddings, LLM calls)
- Development iteration where you re-run a graph but only change downstream nodes
- Batch processing where many items share common intermediate results

## Basic Pattern

Mark a node with `cache=True` and pass a cache backend to the runner:

```python
from hypergraph import Graph, node, SyncRunner, InMemoryCache

@node(output_name="embedding", cache=True)
def embed(text: str) -> list[float]:
    # Expensive API call — only runs once per unique input
    return model.embed(text)

@node(output_name="answer")
def generate(embedding: list[float], query: str) -> str:
    return llm.generate(embedding, query)

graph = Graph(nodes=[embed, generate])

runner = SyncRunner(cache=InMemoryCache())

# First call — embed executes normally
result = runner.run(graph, {"text": "hello", "query": "What is this?"})

# Second call with same text — embed served from cache
result = runner.run(graph, {"text": "hello", "query": "Different question"})
```

Two things are required:
1. **Node opt-in**: `@node(..., cache=True)` on the nodes you want cached
2. **Runner backend**: `SyncRunner(cache=InMemoryCache())` or `AsyncRunner(cache=...)`

## Cache Backends

### InMemoryCache

Fast, ephemeral. Lives for the duration of the process.

```python
from hypergraph import InMemoryCache

# Unlimited size
cache = InMemoryCache()

# LRU eviction after 1000 entries
cache = InMemoryCache(max_size=1000)
```

### DiskCache

Persistent across runs. Requires the optional cache dependencies:

```bash
pip install 'hypergraph[cache]'
```

This installs:

- `diskcache` for Hypergraph's `DiskCache` backend
- `hypercache` for optional `InnerCacheEvent` telemetry when your node body uses Hypercache internally

```python
from hypergraph import DiskCache

# Persists to ~/.cache/hypergraph (default)
cache = DiskCache()

# Custom directory
cache = DiskCache(cache_dir="/tmp/my-project-cache")

runner = SyncRunner(cache=cache)

# Results survive process restarts
result = runner.run(graph, {"text": "hello", "query": "Q1"})
# ... restart process ...
# embed is still cached from the previous run
```

#### Integrity Verification

`DiskCache` stores serialized bytes plus an HMAC-SHA256 signature:

- On write: value is serialized, signed, and stored with its signature
- On read: signature is verified **before** deserialization

This prevents deserializing tampered cache payloads. If an entry is corrupted, missing a signature, has invalid metadata, or fails deserialization, Hypergraph evicts it and treats it as a cache miss.

### Custom Backend

Implement the `CacheBackend` protocol for Redis, databases, or anything else:

```python
from hypergraph import CacheBackend

class RedisCache(CacheBackend):
    def get(self, key: str) -> tuple[bool, object]:
        value = redis.get(key)
        if value is None:
            return False, None
        return True, pickle.loads(value)

    def set(self, key: str, value: object) -> None:
        redis.set(key, pickle.dumps(value))
```

## How Cache Keys Work

Cache keys are computed from:
1. **Node identity** — a hash of the function's source code (`definition_hash`)
2. **Input values** — a deterministic hash of all inputs passed to the node

If you change the function body, the cache automatically invalidates. If inputs aren't picklable, the node falls back to uncached execution (with a warning).

## Observing Cache Hits

Cache events integrate with the [event system](../05-how-to/observe-execution.md):

```python
from hypergraph import TypedEventProcessor, CacheHitEvent, NodeEndEvent

class CacheMonitor(TypedEventProcessor):
    def __init__(self):
        self.hits = 0
        self.misses = 0

    def on_cache_hit(self, event: CacheHitEvent) -> None:
        self.hits += 1
        print(f"Cache hit: {event.node_name}")

    def on_node_end(self, event: NodeEndEvent) -> None:
        if not event.cached:
            self.misses += 1

monitor = CacheMonitor()
result = runner.run(graph, inputs, event_processors=[monitor])
print(f"Hits: {monitor.hits}, Misses: {monitor.misses}")
```

The event sequence for a cache hit is:

```text
NodeStartEvent(node_name="embed")
CacheHitEvent(node_name="embed", cache_key="abc123...")
NodeEndEvent(node_name="embed", cached=True, duration_ms=0.0)
```

If your node body uses `hypercache` internally, Hypergraph also emits `InnerCacheEvent` entries for those nested cache decisions. Installing `hypergraph[cache]` gives you both Hypergraph's disk backend and the Hypercache observer bridge:

```python
from hypergraph import InnerCacheEvent, TypedEventProcessor

class InnerCacheMonitor(TypedEventProcessor):
    def on_inner_cache(self, event: InnerCacheEvent) -> None:
        status = "hit" if event.hit else "miss"
        print(f"{event.node_name}: {status} via {event.instance}.{event.operation}")
```

## Caching Route and IfElse Nodes

Gate nodes (`@route`, `@ifelse`) are cacheable. The routing function's return value is cached, and the runner restores the routing decision on cache hit:

```python
@route(targets=["fast_path", "full_rag", END], cache=True)
def classify_query(query: str) -> str:
    """Expensive classification — cache the decision."""
    category = llm.classify(query)
    if category == "faq":
        return "fast_path"
    elif category == "complex":
        return "full_rag"
    return END
```

On cache hit, the runner replays the cached routing decision without calling the function again. Downstream routing still works correctly — the cached decision is restored into the graph state.

## Caching in Batch Runs

Caching works per item inside mapped subgraphs. Each iteration of a `map_over` (or `runner.map()`) runs the inner graph with that item's inputs, so a `cache=True` node inside it gets a cache key derived from the per-item input values:

```python
from hypergraph import Graph, node, SyncRunner, InMemoryCache

@node(output_name="embedding", cache=True)
def embed(text: str) -> list[float]:
    return model.embed(text)  # expensive — cached per text

@node(output_name="answer")
def generate(embedding: list[float], temperature: float) -> str:
    return llm.generate(embedding, temperature=temperature)

inner = Graph([embed, generate], name="pipeline")
outer = Graph([inner.as_node().map_over("text")])

runner = SyncRunner(cache=InMemoryCache())

# First batch: embed runs once per unique text
runner.run(outer, {"text": ["a", "b", "c"], "temperature": 0.0})

# Change a downstream parameter and re-run the batch:
# all three embed calls hit the cache, only generate re-executes
runner.run(outer, {"text": ["a", "b", "c"], "temperature": 1.0})

# Add one new item: only "d" is embedded, "a"/"b"/"c" come from cache
runner.run(outer, {"text": ["a", "b", "c", "d"], "temperature": 1.0})
```

The cache backend lives on the runner and is shared by all nested per-item runs, so hits carry across batches, across runs, and (with `DiskCache`) across processes.

This is what makes eval loops cheap: re-running a batch after changing one parameter recomputes only the nodes downstream of that parameter — every cached upstream result is reused per item. A 500-item eval where you only tweaked the generation prompt pays for 500 generations, not 500 embeddings plus 500 retrievals plus 500 generations.

Broadcast (non-mapped) inputs participate in the cache key like any other input: if a cached node consumes a broadcast value and you change it, every item recomputes that node — which is exactly right, since its inputs changed.

## Restrictions

These node types reject `cache=True` at build time:

- **GraphNode** — nested graphs have their own execution flow; cache individual nodes inside them instead

### InterruptNode

InterruptNode supports `cache=True` (defaults to `False`). When cached, a previously auto-resolved response is replayed without re-running the handler.

## Real-World Example: Cached RAG Pipeline

```python
from hypergraph import Graph, node, SyncRunner, InMemoryCache

@node(output_name="embedding", cache=True)
def embed(text: str) -> list[float]:
    """Embedding API call — $0.0001 per call."""
    return openai.embeddings.create(input=text, model="text-embedding-3-small")

@node(output_name="docs", cache=True)
def retrieve(embedding: list[float], top_k: int = 5) -> list[str]:
    """Vector DB search — 50ms per query."""
    return pinecone_index.query(embedding, top_k=top_k)

@node(output_name="answer")
def generate(docs: list[str], query: str) -> str:
    """LLM generation — not cached (we want fresh answers)."""
    return llm.chat(docs=docs, query=query)

graph = Graph(nodes=[embed, retrieve, generate])
runner = SyncRunner(cache=InMemoryCache(max_size=500))

# During development: re-run with different prompts
# embed and retrieve are cached — only generate re-executes
for query in ["What is RAG?", "How does retrieval work?", "What is RAG?"]:
    result = runner.run(graph, {"text": "RAG tutorial", "query": query})
    # Third query hits cache for both embed AND retrieve
```

## What's Next?

- [Observe Execution](../05-how-to/observe-execution.md) — Monitor cache hits with event processors
- [Events API Reference](../06-api-reference/events.md) — `CacheHitEvent` and `NodeEndEvent.cached` details
- [Runners API Reference](../06-api-reference/runners.md) — `cache` parameter on runners
