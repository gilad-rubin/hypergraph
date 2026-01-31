# Caching – Technical Specification

**Difficulty**: Medium

## Context

Hypergraph nodes already carry a `cache: bool` property (default `False`), set via `@node(output_name="x", cache=True)`. The flag is preserved through `with_name`, `with_inputs`, `with_outputs`. However, no runtime caching logic exists — `cache=True` is a no-op today. Tests in `tests/test_cache_behavior.py` document the expected behavior.

## Goal

Add **result caching** for nodes marked `cache=True`. When a cached node is about to execute with inputs it has seen before (within the same runner lifetime or across runs via a persistent backend), skip execution and return the stored result.

Support two backends:
1. **In-memory** (default) — dict-based, lives for the runner's lifetime
2. **Disk-based** — via `diskcache` (optional dependency), persists across runs

Emit proper events so observability tools can distinguish cache hits from real executions.

## Design

### User-facing API

```python
from hypergraph import SyncRunner, AsyncRunner
from hypergraph.cache import InMemoryCache, DiskCache

# In-memory (default when cache param is a CacheBackend)
runner = SyncRunner(cache=InMemoryCache())

# Disk-based (requires `pip install hypergraph[cache]`)
runner = SyncRunner(cache=DiskCache("/tmp/hg-cache"))

# No caching (default, current behavior)
runner = SyncRunner()
```

The runner holds the cache backend. Nodes opt in with `cache=True`. The cache is checked/populated at the superstep level, wrapping the existing execute_node call.

### Cache Key

For a node with `cache=True`, the cache key is derived from:
1. **Node identity** — `node.definition_hash` (`FunctionNode` overrides the base to hash the **function source code** via `inspect.getsource()` + output names; the base `HyperNode` hashes `class_name:name:inputs:outputs`)
2. **Resolved input values** — sorted tuple of `(param_name, value)` for **all resolved inputs**, including bound values. The input collection already resolves bound values (step 2 in `_resolve_input`), so the collected `inputs` dict naturally includes them.

Combined via `pickle.dumps(sorted_items)` → `hashlib.sha256`. Non-picklable values cause a cache miss (warning logged, not raised).

**Limitations**:
- `inspect.getsource()` may fail in frozen/compiled deployments — in that case `definition_hash` falls back to the base structural hash, which is still correct but less precise (renaming a function without changing its behavior would invalidate the cache).
- `pickle` output is deterministic for built-in types in CPython 3.7+ but not guaranteed across Python versions. Cache should not be shared across Python versions.

### Cache Backend Protocol

```python
from typing import Any, Protocol

class CacheBackend(Protocol):
    def get(self, key: str) -> tuple[bool, Any]:
        """Return (hit, value). hit=False means cache miss."""
        ...

    def set(self, key: str, value: Any) -> None:
        """Store a value."""
        ...
```

Two implementations:
- `InMemoryCache` — wraps a `dict`. Optional `max_size: int | None` parameter (default `None` = unlimited). When `max_size` is set, uses LRU eviction via `OrderedDict`.
- `DiskCache` — wraps `diskcache.Cache` (optional import). `diskcache` handles its own size limits and is process-safe.

### Concurrency Safety

The async superstep runs ready nodes concurrently via `asyncio.gather()`. Two concurrent nodes with `cache=True` could race on the same key (both miss, both execute, both write). This is acceptable:
- **`InMemoryCache`**: `dict` writes are atomic in CPython. Worst case: duplicate execution, last write wins. Correct but not optimal.
- **`DiskCache`**: `diskcache.Cache` is thread-safe and process-safe by design.
- No locking needed in v1. If thundering herd becomes a problem (e.g., expensive nodes in `map()`), a future version could add `asyncio.Lock` per cache key.

### Nested Graph Cache Propagation

`SyncGraphNodeExecutor` and `AsyncGraphNodeExecutor` hold a reference to the parent runner (`self.runner`). When they call `self.runner.run()` for a nested graph, the runner already carries the cache backend. So **cache propagation is automatic** — no executor changes needed. Inner nodes with `cache=True` will use the same backend.

### Integration Point

Caching is injected in the **superstep execution** (both sync and async), before calling `execute_node`:

```
For each ready node:
  1. Collect inputs (existing)
  2. If node.cache and cache_backend:
     a. Compute cache key from (node.definition_hash, inputs)
     b. Check cache → if hit, use cached outputs, emit CacheHitEvent, skip execution
     c. If miss, execute normally, store result in cache
  3. Else: execute normally (existing)
```

This keeps caching orthogonal to the executor hierarchy — no changes to individual executors.

### Events

#### `CacheHitEvent`

```python
@dataclass(frozen=True)
class CacheHitEvent(BaseEvent):
    node_name: str = ""
    graph_name: str = ""
    cache_key: str = ""
```

When a cache hit occurs, the superstep emits `NodeStartEvent` → `CacheHitEvent` → `NodeEndEvent` (with duration_ms ≈ 0). This preserves the existing event contract (start/end always paired) while adding cache observability.

#### Why no `CacheMissEvent`

A separate `CacheMissEvent` would be emitted on every non-cached execution, adding noise. Instead, add a `cached: bool = False` field to `NodeEndEvent`. This is cheaper and allows filtering cache hits in any processor without a new event type. When `cached=True`, the node was served from cache. When `cached=False` (default), it was executed normally — whether caching was enabled or not is irrelevant to downstream consumers.

**Decision**: Add `cached: bool = False` to `NodeEndEvent` instead of a separate `CacheMissEvent`. Still emit `CacheHitEvent` for detailed observability (includes cache key).

### TypedEventProcessor Extension

Add `on_cache_hit(self, event: CacheHitEvent) -> None` to `TypedEventProcessor` and update `_EVENT_METHOD_MAP`.

## Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `src/hypergraph/cache.py` | **Create** | `CacheBackend` protocol, `InMemoryCache`, `DiskCache`, cache key computation |
| `src/hypergraph/events/types.py` | Modify | Add `CacheHitEvent`, add `cached` field to `NodeEndEvent`, update `Event` union |
| `src/hypergraph/events/processor.py` | Modify | Add `on_cache_hit` to `TypedEventProcessor` and `_EVENT_METHOD_MAP` |
| `src/hypergraph/runners/sync/superstep.py` | Modify | Add cache lookup/store around `execute_node` |
| `src/hypergraph/runners/async_/superstep.py` | Modify | Same for async path |
| `src/hypergraph/runners/sync/runner.py` | Modify | Accept `cache` param, pass to superstep |
| `src/hypergraph/runners/async_/runner.py` | Modify | Accept `cache` param, pass to superstep |
| `src/hypergraph/__init__.py` | Modify | Export `InMemoryCache`, `DiskCache`, `CacheBackend`, `CacheHitEvent` |
| `tests/test_cache_behavior.py` | Modify | Update tests to use `SyncRunner(cache=InMemoryCache())` and verify cache hits |
| `tests/test_cache_events.py` | **Create** | Test `CacheHitEvent` emission and `NodeEndEvent.cached` field |
| `tests/capabilities/matrix.py` | Modify | Add `Caching` dimension (`NONE`, `ENABLED`) |
| `pyproject.toml` | Modify | Add `[cache]` optional dependency for `diskcache` |

**Not modified**: `BaseRunner` — cache is a concrete runner concern, not an abstract interface requirement. Each runner stores it as `self._cache`.

## Edge Cases

- **Generator nodes with `cache=True`**: Cache the fully-materialized list (generators are already collected to lists before output).
- **Non-picklable inputs**: Log warning, skip cache for that call (cache miss).
- **Gate nodes**: Gates should NOT be cacheable (routing decisions depend on execution context). Validate at graph build time that `cache=True` is not set on `GateNode` subclasses.
- **InterruptNode**: Should NOT be cacheable (pauses execution for human input). Validate at graph build time alongside gates.
- **GraphNode with `cache=True`**: Disallow at build time. Caching applies to inner nodes individually. A user wanting to cache an entire subgraph result should cache the individual nodes inside it.
- **Cyclic graphs**: Cache works per-invocation — different input values in each iteration produce different cache keys. The cache key includes actual input values, not versions.
- **Nested graphs**: Cache propagation is automatic since executors delegate to `self.runner.run()` and the runner holds the cache backend.
- **`map()` with caching**: Each `map()` iteration calls `runner.run()` independently. If multiple iterations share identical inputs for a cached node, the cache provides deduplication. Map iterations run sequentially in `SyncRunner` and concurrently in `AsyncRunner` (see Concurrency Safety above).
- **Bound values**: Already covered — `collect_inputs_for_node` resolves bound values into the `inputs` dict, so they're included in the cache key automatically.

## Verification

1. All existing tests in `test_cache_behavior.py` pass (update to use `InMemoryCache`).
2. New tests verify cache hit/miss counts using `CallCounter`.
3. New tests verify `CacheHitEvent` emission and `NodeEndEvent.cached` field.
4. `DiskCache` tests check persistence across runner instantiations.
5. Build-time validation tests for `cache=True` on gates, InterruptNode, and GraphNode.
6. Capability matrix tests cover caching dimension.
7. Run `uv run pytest` — all tests green.
8. Run `uv run ruff check` — no lint errors.
