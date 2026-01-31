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
1. **Node identity** — `node.definition_hash` (already exists on HyperNode, hashes the function bytecode + output names)
2. **Input values** — sorted tuple of `(param_name, value)` for all resolved inputs

Combined via a deterministic hash. Values are hashed using `pickle` → `hashlib.sha256`. Non-picklable values cause a cache miss (logged, not raised).

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
- `InMemoryCache` — wraps a `dict`
- `DiskCache` — wraps `diskcache.Cache` (optional import)

### Integration Point

Caching is injected in the **superstep execution** (both sync and async), before calling `execute_node`:

```
For each ready node:
  1. Collect inputs (existing)
  2. If node.cache and cache_backend:
     a. Compute cache key from (node.definition_hash, inputs)
     b. Check cache → if hit, use cached outputs, emit CacheHitEvent, skip execution
     c. If miss, execute normally, store result, emit CacheMissEvent
  3. Else: execute normally (existing)
```

This keeps caching orthogonal to the executor hierarchy — no changes to FunctionNodeExecutor, GraphNodeExecutor, etc.

### New Event: `CacheHitEvent`

```python
@dataclass(frozen=True)
class CacheHitEvent(BaseEvent):
    node_name: str = ""
    graph_name: str = ""
    cache_key: str = ""
```

When a cache hit occurs, the superstep emits `NodeStartEvent` → `CacheHitEvent` → `NodeEndEvent` (with duration_ms ≈ 0). This preserves the existing event contract (start/end always paired) while adding cache observability.

No separate `CacheMissEvent` needed — a normal `NodeStartEvent` → `NodeEndEvent` without a `CacheHitEvent` in between implies a miss (or caching not enabled).

### TypedEventProcessor Extension

Add `on_cache_hit(self, event: CacheHitEvent) -> None` to `TypedEventProcessor` and update `_EVENT_METHOD_MAP`.

## Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `src/hypergraph/cache.py` | **Create** | `CacheBackend` protocol, `InMemoryCache`, `DiskCache`, cache key computation |
| `src/hypergraph/events/types.py` | Modify | Add `CacheHitEvent`, update `Event` union |
| `src/hypergraph/events/processor.py` | Modify | Add `on_cache_hit` to `TypedEventProcessor` and `_EVENT_METHOD_MAP` |
| `src/hypergraph/runners/sync/superstep.py` | Modify | Add cache lookup/store around `execute_node` |
| `src/hypergraph/runners/async_/superstep.py` | Modify | Same for async path |
| `src/hypergraph/runners/sync/runner.py` | Modify | Accept `cache` param, pass to superstep |
| `src/hypergraph/runners/async_/runner.py` | Modify | Accept `cache` param, pass to superstep |
| `src/hypergraph/runners/base.py` | Modify | Add `cache` param to BaseRunner |
| `src/hypergraph/__init__.py` | Modify | Export `InMemoryCache`, `DiskCache`, `CacheBackend`, `CacheHitEvent` |
| `tests/test_cache_behavior.py` | Modify | Update tests to use `SyncRunner(cache=InMemoryCache())` and verify cache hits |
| `tests/test_cache_events.py` | **Create** | Test `CacheHitEvent` emission |
| `pyproject.toml` | Modify | Add `[cache]` optional dependency for `diskcache` |

## Edge Cases

- **Generator nodes with `cache=True`**: Cache the fully-materialized list (generators are already collected to lists before output).
- **Non-picklable inputs**: Log warning, skip cache for that call (cache miss).
- **Gate nodes**: Gates should NOT be cacheable (routing decisions depend on execution context). Validate at graph build time that gate nodes don't have `cache=True`.
- **Cyclic graphs**: Cache still works per-invocation — different input values in each iteration produce different cache keys. The cache key includes actual input values, not versions.
- **Nested graphs (GraphNode)**: Caching applies to inner nodes individually, not to the GraphNode as a whole. The inner runner shares the same cache backend.

## Verification

1. All existing tests in `test_cache_behavior.py` pass (update to use `InMemoryCache`).
2. New tests verify cache hit/miss counts using `CallCounter`.
3. New tests verify `CacheHitEvent` emission.
4. `DiskCache` tests check persistence across runner instantiations.
5. Run `uv run pytest` — all tests green.
6. Run `uv run ruff check` — no lint errors.
