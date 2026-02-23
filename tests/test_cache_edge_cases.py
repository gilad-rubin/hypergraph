"""Red-team / edge-case tests for the caching subsystem.

Covers: LRU eviction boundaries, non-picklable inputs, exception safety,
async parity, definition_hash stability, empty inputs, validation errors,
and select() interaction.
"""

from __future__ import annotations

import asyncio
import threading

from hypergraph import AsyncRunner, Graph, InMemoryCache, SyncRunner, node
from hypergraph.cache import compute_cache_key
from hypergraph.events import EventProcessor
from hypergraph.events.types import CacheHitEvent, NodeEndEvent
from hypergraph.nodes.gate import END, ifelse, route
from hypergraph.runners import RunStatus


class CallCounter:
    def __init__(self):
        self.count = 0

    def increment(self):
        self.count += 1


class ListProcessor(EventProcessor):
    def __init__(self):
        self.events: list = []

    def on_event(self, event):
        self.events.append(event)

    def of_type(self, cls):
        return [e for e in self.events if isinstance(e, cls)]


# ---------------------------------------------------------------------------
# LRU eviction edge cases
# ---------------------------------------------------------------------------


class TestLRUEviction:
    """Edge cases around InMemoryCache max_size."""

    def test_max_size_zero_never_caches(self):
        """max_size=0 means every entry is immediately evicted."""
        counter = CallCounter()

        @node(output_name="r", cache=True)
        def f(x: int) -> int:
            counter.increment()
            return x

        graph = Graph([f])
        runner = SyncRunner(cache=InMemoryCache(max_size=0))

        runner.run(graph, {"x": 1})
        runner.run(graph, {"x": 1})

        assert counter.count == 2  # never hits cache

    def test_max_size_one_evicts_on_new_key(self):
        """max_size=1: second distinct key evicts the first."""
        counter = CallCounter()

        @node(output_name="r", cache=True)
        def f(x: int) -> int:
            counter.increment()
            return x

        graph = Graph([f])
        runner = SyncRunner(cache=InMemoryCache(max_size=1))

        runner.run(graph, {"x": 1})  # miss, store
        runner.run(graph, {"x": 2})  # miss, evicts x=1
        runner.run(graph, {"x": 1})  # miss again (was evicted)

        assert counter.count == 3

    def test_max_size_one_same_key_hits(self):
        """max_size=1: repeated same key always hits."""
        counter = CallCounter()

        @node(output_name="r", cache=True)
        def f(x: int) -> int:
            counter.increment()
            return x

        graph = Graph([f])
        runner = SyncRunner(cache=InMemoryCache(max_size=1))

        runner.run(graph, {"x": 1})
        runner.run(graph, {"x": 1})
        runner.run(graph, {"x": 1})

        assert counter.count == 1

    def test_lru_eviction_order(self):
        """LRU: accessing key refreshes it; oldest unused key is evicted."""
        cache = InMemoryCache(max_size=2)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.get("a")  # refresh a -> order: b, a
        cache.set("c", 3)  # evicts b (oldest)

        assert cache.get("a") == (True, 1)
        assert cache.get("b") == (False, None)
        assert cache.get("c") == (True, 3)


# ---------------------------------------------------------------------------
# Non-picklable inputs
# ---------------------------------------------------------------------------


class TestNonPicklableInputs:
    """Inputs that can't be pickled should degrade gracefully."""

    def test_lambda_input_skips_cache(self):
        """Lambda inputs are not picklable; node executes every time."""
        counter = CallCounter()

        @node(output_name="r", cache=True)
        def f(func: object) -> str:
            counter.increment()
            return "done"

        graph = Graph([f])
        runner = SyncRunner(cache=InMemoryCache())

        fn = lambda: None  # noqa: E731
        runner.run(graph, {"func": fn})
        runner.run(graph, {"func": fn})

        assert counter.count == 2  # can't cache

    def test_compute_cache_key_returns_empty_for_unpicklable(self):
        """compute_cache_key returns '' when inputs aren't picklable."""
        key = compute_cache_key("hash", {"f": lambda: None})
        assert key == ""


# ---------------------------------------------------------------------------
# Exception safety
# ---------------------------------------------------------------------------


class TestExceptionSafety:
    """Failed executions must not pollute the cache."""

    def test_exception_not_cached(self):
        """A node that raises should not store the error in cache."""
        counter = CallCounter()

        @node(output_name="r", cache=True)
        def flaky(x: int) -> int:
            counter.increment()
            if counter.count == 1:
                raise ValueError("boom")
            return x

        graph = Graph([flaky])
        runner = SyncRunner(cache=InMemoryCache())

        r1 = runner.run(graph, {"x": 1})
        assert r1.status == RunStatus.FAILED

        # Second run should execute (not serve cached error)
        result = runner.run(graph, {"x": 1})
        assert result.status == RunStatus.COMPLETED
        assert result["r"] == 1
        assert counter.count == 2


# ---------------------------------------------------------------------------
# Async parity
# ---------------------------------------------------------------------------


class TestAsyncCacheParity:
    """Async runner must behave identically to sync for caching."""

    def test_async_cache_hit(self):
        """AsyncRunner should hit cache on second run."""
        counter = CallCounter()

        @node(output_name="r", cache=True)
        async def f(x: int) -> int:
            counter.increment()
            return x * 2

        graph = Graph([f])
        cache = InMemoryCache()
        runner = AsyncRunner(cache=cache)

        r1 = asyncio.run(runner.run(graph, {"x": 5}))
        r2 = asyncio.run(runner.run(graph, {"x": 5}))

        assert r1["r"] == 10
        assert r2["r"] == 10
        assert counter.count == 1

    def test_async_cache_events(self):
        """AsyncRunner emits CacheHitEvent on hit."""
        counter = CallCounter()

        @node(output_name="r", cache=True)
        async def f(x: int) -> int:
            counter.increment()
            return x

        graph = Graph([f])
        cache = InMemoryCache()
        runner = AsyncRunner(cache=cache)

        asyncio.run(runner.run(graph, {"x": 1}))

        proc = ListProcessor()
        asyncio.run(runner.run(graph, {"x": 1}, event_processors=[proc]))

        hits = proc.of_type(CacheHitEvent)
        assert len(hits) == 1

        ends = [e for e in proc.of_type(NodeEndEvent) if e.node_name == "f"]
        assert ends[0].cached is True


# ---------------------------------------------------------------------------
# Cache key stability
# ---------------------------------------------------------------------------


class TestCacheKeyStability:
    """Definition hash and key computation invariants."""

    def test_same_function_same_hash(self):
        """Two nodes wrapping the same function share definition_hash."""

        @node(output_name="a", cache=True)
        def f(x: int) -> int:
            return x

        n1 = f
        n2 = f.with_name("f_copy")

        assert n1.definition_hash == n2.definition_hash

    def test_different_functions_different_hash(self):
        """Different function bodies produce different hashes."""

        @node(output_name="a", cache=True)
        def f1(x: int) -> int:
            return x + 1

        @node(output_name="b", cache=True)
        def f2(x: int) -> int:
            return x + 2

        assert f1.definition_hash != f2.definition_hash

    def test_same_inputs_different_definition_different_key(self):
        """Same inputs but different definition_hash -> different cache key."""
        key1 = compute_cache_key("hash_a", {"x": 1})
        key2 = compute_cache_key("hash_b", {"x": 1})
        assert key1 != key2

    def test_empty_inputs_produces_valid_key(self):
        """Node with no inputs still produces a valid cache key."""
        key = compute_cache_key("some_hash", {})
        assert isinstance(key, str)
        assert len(key) == 64  # SHA256 hex

    def test_input_order_irrelevant(self):
        """Dict ordering should not affect cache key."""
        key1 = compute_cache_key("h", {"a": 1, "b": 2})
        key2 = compute_cache_key("h", {"b": 2, "a": 1})
        assert key1 == key2


# ---------------------------------------------------------------------------
# Build-time validation
# ---------------------------------------------------------------------------


class TestCacheValidation:
    """cache=True on disallowed node types must raise at build time."""

    def test_cache_on_route_node_builds(self):
        """cache=True on a @route node should build successfully."""

        @route(targets=["a", END], cache=True)
        def gate(x: int) -> str:
            return END

        @node(output_name="a")
        def a(x: int) -> int:
            return x

        # Gates are cacheable — their routing function return value is cached
        graph = Graph([gate, a])
        assert graph is not None

    def test_cache_on_ifelse_node_builds(self):
        """cache=True on an @ifelse node should build successfully."""

        @ifelse(when_true="t", when_false="f", cache=True)
        def gate(x: int) -> bool:
            return True

        @node(output_name="t")
        def t(x: int) -> int:
            return x

        @node(output_name="f")
        def f(x: int) -> int:
            return x

        graph = Graph([gate, t, f])
        assert graph is not None


# ---------------------------------------------------------------------------
# select() interaction
# ---------------------------------------------------------------------------


class TestCacheWithSelect:
    """Caching should work correctly when graph uses .select()."""

    def test_select_with_cache(self):
        """Selected outputs work with caching."""
        counter = CallCounter()

        @node(output_name="a", cache=True)
        def producer(x: int) -> int:
            counter.increment()
            return x * 2

        @node(output_name="b")
        def consumer(a: int) -> int:
            return a + 1

        graph = Graph([producer, consumer]).select("b")
        runner = SyncRunner(cache=InMemoryCache())

        r1 = runner.run(graph, {"x": 5})
        r2 = runner.run(graph, {"x": 5})

        assert r1["b"] == 11
        assert r2["b"] == 11
        assert counter.count == 1  # producer cached on second run


# ---------------------------------------------------------------------------
# Shared cache across different graphs
# ---------------------------------------------------------------------------


class TestSharedCacheAcrossGraphs:
    """Same cache backend shared between graphs with same nodes."""

    def test_shared_cache_cross_graph_hit(self):
        """Same function in two different graphs shares cache entries."""
        counter = CallCounter()

        @node(output_name="r", cache=True)
        def shared(x: int) -> int:
            counter.increment()
            return x

        g1 = Graph([shared])
        g2 = Graph([shared])
        cache = InMemoryCache()
        runner = SyncRunner(cache=cache)

        runner.run(g1, {"x": 1})
        runner.run(g2, {"x": 1})

        # Same definition_hash + same inputs = cache hit
        assert counter.count == 1


# ---------------------------------------------------------------------------
# Thread safety (InMemoryCache used from multiple threads)
# ---------------------------------------------------------------------------


class TestInMemoryCacheThreadSafety:
    """Basic thread safety smoke test."""

    def test_concurrent_writes_no_crash(self):
        """Concurrent writes should not crash (CPython GIL protects dict)."""
        cache = InMemoryCache(max_size=100)
        errors = []

        def writer(start: int):
            try:
                for i in range(100):
                    cache.set(f"key_{start + i}", i)
                    cache.get(f"key_{start + i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i * 100,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


# ---------------------------------------------------------------------------
# Cache mutation bug: pop() on cached dict must not corrupt cache entries
# ---------------------------------------------------------------------------


class TestCachedRoutingDecisionNotMutated:
    """Verify that cache hits for gate nodes don't corrupt cached entries.

    When a gate node hits cache, the routing decision is popped from the
    cached dict. If the cache returns the same dict reference (InMemoryCache),
    pop() would permanently remove __routing_decision__ from the entry,
    breaking subsequent cache hits.
    """

    def test_route_cache_hit_twice_preserves_routing(self):
        """Second cache hit on a @route node must still restore routing."""
        counter = CallCounter()

        @route(targets=["a", END], cache=True)
        def gate(x: int) -> str:
            counter.increment()
            return "a"

        @node(output_name="a")
        def a(x: int) -> int:
            return x * 2

        graph = Graph([gate, a])
        cache = InMemoryCache()
        runner = SyncRunner(cache=cache)

        r1 = runner.run(graph, {"x": 1})
        assert r1["a"] == 2
        assert counter.count == 1

        # Second run — cache hit, routing must still work
        r2 = runner.run(graph, {"x": 1})
        assert r2["a"] == 2
        assert counter.count == 1  # still cached

        # Third run — cache hit again, routing must still work
        r3 = runner.run(graph, {"x": 1})
        assert r3["a"] == 2
        assert counter.count == 1

    def test_ifelse_cache_hit_twice_preserves_routing(self):
        """Second cache hit on an @ifelse node must still restore routing."""
        counter = CallCounter()

        @ifelse(when_true="t", when_false="f", cache=True)
        def gate(x: int) -> bool:
            counter.increment()
            return True

        @node(output_name="t")
        def t(x: int) -> int:
            return x

        @node(output_name="f")
        def f(x: int) -> int:
            return -x

        graph = Graph([gate, t, f])
        cache = InMemoryCache()
        runner = SyncRunner(cache=cache)

        r1 = runner.run(graph, {"x": 5})
        assert r1["t"] == 5
        assert counter.count == 1

        r2 = runner.run(graph, {"x": 5})
        assert r2["t"] == 5
        assert counter.count == 1

        r3 = runner.run(graph, {"x": 5})
        assert r3["t"] == 5
        assert counter.count == 1

    async def test_route_cache_hit_twice_async(self):
        """Async: second cache hit on a @route node must still restore routing."""
        counter = CallCounter()

        @route(targets=["a", END], cache=True)
        def gate(x: int) -> str:
            counter.increment()
            return "a"

        @node(output_name="a")
        def a(x: int) -> int:
            return x * 2

        graph = Graph([gate, a])
        cache = InMemoryCache()
        runner = AsyncRunner(cache=cache)

        r1 = await runner.run(graph, {"x": 1})
        assert r1["a"] == 2

        r2 = await runner.run(graph, {"x": 1})
        assert r2["a"] == 2

        r3 = await runner.run(graph, {"x": 1})
        assert r3["a"] == 2
        assert counter.count == 1

    def test_routing_decision_key_not_in_result_values(self):
        """__routing_decision__ internal key must not leak into RunResult.values."""

        @route(targets=["a", END], cache=True)
        def gate(x: int) -> str:
            return "a"

        @node(output_name="a")
        def a(x: int) -> int:
            return x * 2

        graph = Graph([gate, a])
        cache = InMemoryCache()
        runner = SyncRunner(cache=cache)

        # First run (cache miss) and second run (cache hit)
        r1 = runner.run(graph, {"x": 1})
        r2 = runner.run(graph, {"x": 1})

        for r in (r1, r2):
            assert "__routing_decision__" not in r.values
