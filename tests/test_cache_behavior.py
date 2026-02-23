"""Tests for runtime cache behavior validation (GAP-06).

Tests verify that InMemoryCache prevents redundant node executions
across repeated runs with the same inputs.
"""

from hypergraph import Graph, InMemoryCache, node
from hypergraph.nodes.gate import END, route
from hypergraph.runners import RunStatus, SyncRunner

# === Test Fixtures ===


class CallCounter:
    """Helper to track function calls."""

    def __init__(self):
        self.count = 0

    def increment(self):
        self.count += 1
        return self.count


# === Tests ===


class TestCachePropertyConfiguration:
    """Tests for node cache property configuration."""

    def test_cache_defaults_to_false(self):
        """Node cache defaults to False."""

        @node(output_name="result")
        def no_cache(x: int) -> int:
            return x * 2

        assert no_cache.cache is False

    def test_cache_true_sets_property(self):
        """cache=True sets the cache property."""

        @node(output_name="result", cache=True)
        def with_cache(x: int) -> int:
            return x * 2

        assert with_cache.cache is True

    def test_cache_preserved_through_rename(self):
        """Cache flag preserved through with_name and with_inputs."""

        @node(output_name="result", cache=True)
        def cached_node(x: int) -> int:
            return x * 2

        renamed = cached_node.with_name("renamed")
        assert renamed.cache is True

        with_renamed_inputs = cached_node.with_inputs(x="y")
        assert with_renamed_inputs.cache is True

    def test_cache_preserved_through_with_outputs(self):
        """Cache flag preserved through with_outputs."""

        @node(output_name="result", cache=True)
        def cached_node(x: int) -> int:
            return x * 2

        with_renamed_outputs = cached_node.with_outputs(result="output")
        assert with_renamed_outputs.cache is True


class TestCachedNodeExecution:
    """Tests for cached node execution behavior."""

    def test_cached_node_runs_at_least_once(self):
        """Cached node executes at least once to produce initial result."""
        counter = CallCounter()

        @node(output_name="result", cache=True)
        def counting_node(x: int) -> int:
            counter.increment()
            return x * 2

        graph = Graph([counting_node])
        cache = InMemoryCache()
        runner = SyncRunner(cache=cache)

        result = runner.run(graph, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        assert result["result"] == 10
        assert counter.count == 1

    def test_cached_node_skips_second_run(self):
        """Second run with same inputs is served from cache."""
        counter = CallCounter()

        @node(output_name="result", cache=True)
        def counting_node(x: int) -> int:
            counter.increment()
            return x * 2

        graph = Graph([counting_node])
        cache = InMemoryCache()
        runner = SyncRunner(cache=cache)

        r1 = runner.run(graph, {"x": 5})
        r2 = runner.run(graph, {"x": 5})

        assert r1["result"] == 10
        assert r2["result"] == 10
        assert counter.count == 1  # Only executed once

    def test_uncached_node_executes_each_time(self):
        """Node without cache runs each time it's triggered by a gate."""
        counter = CallCounter()

        @node(output_name="count")
        def counting_node(count: int, limit: int = 3) -> int:
            counter.increment()
            if count >= limit:
                return count
            return count + 1

        @route(targets=["counting_node", END])
        def cycle_gate(count: int, limit: int = 3) -> str:
            return END if count >= limit else "counting_node"

        graph = Graph([counting_node, cycle_gate])
        runner = SyncRunner()

        result = runner.run(graph, {"count": 0, "limit": 3})

        assert result.status == RunStatus.COMPLETED
        # Gate-driven cycle: 0->1->2->3 (3 executions, gate stops at 3)
        assert counter.count == 3

    def test_cached_node_in_dag(self):
        """Cached node in a DAG executes correctly and caches across runs."""
        counter = CallCounter()

        @node(output_name="a", cache=True)
        def cached_producer(x: int) -> int:
            counter.increment()
            return x * 2

        @node(output_name="b")
        def consumer(a: int) -> int:
            return a + 1

        graph = Graph([cached_producer, consumer])
        cache = InMemoryCache()
        runner = SyncRunner(cache=cache)

        result = runner.run(graph, {"x": 5})
        assert result.status == RunStatus.COMPLETED
        assert result["a"] == 10
        assert result["b"] == 11

        # Second run: cached_producer served from cache
        result2 = runner.run(graph, {"x": 5})
        assert result2["a"] == 10
        assert result2["b"] == 11
        assert counter.count == 1


class TestCacheWithCycles:
    """Tests for cache behavior in cyclic graphs."""

    def test_cycle_with_cache_flag(self):
        """Cyclic graph with cache flag and gate behaves correctly."""
        counter = CallCounter()

        @node(output_name="count", cache=True)
        def counter_node(count: int, limit: int = 3) -> int:
            counter.increment()
            if count >= limit:
                return count
            return count + 1

        @route(targets=["counter_node", END])
        def cycle_gate(count: int, limit: int = 3) -> str:
            return END if count >= limit else "counter_node"

        graph = Graph([counter_node, cycle_gate])
        runner = SyncRunner(cache=InMemoryCache())

        result = runner.run(graph, {"count": 0, "limit": 3})

        assert result.status == RunStatus.COMPLETED
        assert result["count"] == 3


class TestCacheWithNestedGraph:
    """Tests for cache behavior with nested GraphNode."""

    def test_nested_graph_with_cached_inner_node(self):
        """Inner graph with cached node behaves correctly."""
        counter = CallCounter()

        @node(output_name="doubled", cache=True)
        def cached_double(x: int) -> int:
            counter.increment()
            return x * 2

        inner = Graph([cached_double], name="inner")
        outer = Graph([inner.as_node()])
        runner = SyncRunner(cache=InMemoryCache())

        result = runner.run(outer, {"x": 5})

        assert result.status == RunStatus.COMPLETED
        assert result["doubled"] == 10

    def test_multiple_runs_same_graph(self):
        """Multiple runs on same graph execute nodes correctly."""
        counter = CallCounter()

        @node(output_name="result")
        def counting_node(x: int) -> int:
            counter.increment()
            return x * 2

        graph = Graph([counting_node])
        runner = SyncRunner()

        # Run multiple times with same input
        result1 = runner.run(graph, {"x": 5})
        result2 = runner.run(graph, {"x": 5})
        result3 = runner.run(graph, {"x": 5})

        assert result1["result"] == 10
        assert result2["result"] == 10
        assert result3["result"] == 10
        # Without persistent caching, each run executes independently
        assert counter.count == 3


class TestCacheKeyBehavior:
    """Tests for cache key computation."""

    def test_different_inputs_produce_different_results(self):
        """Different inputs should produce different results (separate cache keys)."""
        counter = CallCounter()

        @node(output_name="result", cache=True)
        def cached_node(x: int) -> int:
            counter.increment()
            return x * 2

        graph = Graph([cached_node])
        runner = SyncRunner(cache=InMemoryCache())

        result1 = runner.run(graph, {"x": 5})
        result2 = runner.run(graph, {"x": 10})

        assert result1["result"] == 10
        assert result2["result"] == 20
        assert counter.count == 2  # Two distinct inputs

    def test_multi_input_node_cache_hit(self):
        """Repeated identical multi-input calls hit cache."""
        counter = CallCounter()

        @node(output_name="result", cache=True)
        def multi_input(a: int, b: int) -> int:
            counter.increment()
            return a + b

        graph = Graph([multi_input])
        runner = SyncRunner(cache=InMemoryCache())

        r1 = runner.run(graph, {"a": 1, "b": 2})
        r2 = runner.run(graph, {"a": 2, "b": 1})
        r3 = runner.run(graph, {"a": 1, "b": 2})  # same as r1

        assert r1["result"] == 3
        assert r2["result"] == 3
        assert r3["result"] == 3
        assert counter.count == 2  # r3 served from cache


class TestCacheWithGenerators:
    """Tests for cache behavior with generator nodes."""

    def test_generator_node_with_cache(self):
        """Generator node with cache flag accumulates correctly."""
        counter = CallCounter()

        @node(output_name="items", cache=True)
        def gen_items(n: int):
            counter.increment()
            yield from range(n)

        graph = Graph([gen_items])
        runner = SyncRunner(cache=InMemoryCache())

        result = runner.run(graph, {"n": 3})

        assert result.status == RunStatus.COMPLETED
        assert result["items"] == [0, 1, 2]

        # Second run hits cache
        runner.run(graph, {"n": 3})
        assert counter.count == 1

    def test_generator_results_are_lists(self):
        """Generator results are accumulated to lists."""

        @node(output_name="items")
        def gen_items(n: int):
            for i in range(n):
                yield i * 2

        graph = Graph([gen_items])
        runner = SyncRunner()

        result = runner.run(graph, {"n": 4})

        assert result["items"] == [0, 2, 4, 6]


class TestCacheWithMapOver:
    """Tests for cache behavior with map_over."""

    def test_map_over_cached_graph(self):
        """map_over with cached inner graph."""
        counter = CallCounter()

        @node(output_name="doubled", cache=True)
        def cached_double(x: int) -> int:
            counter.increment()
            return x * 2

        inner = Graph([cached_double], name="inner")
        outer = Graph([inner.as_node().map_over("x")])
        runner = SyncRunner(cache=InMemoryCache())

        result = runner.run(outer, {"x": [1, 2, 3]})

        assert result.status == RunStatus.COMPLETED
        assert result["doubled"] == [2, 4, 6]
        assert counter.count == 3

    def test_map_over_with_repeated_values(self):
        """map_over with repeated input values uses cache for duplicates."""
        counter = CallCounter()

        @node(output_name="doubled", cache=True)
        def cached_double(x: int) -> int:
            counter.increment()
            return x * 2

        inner = Graph([cached_double], name="inner")
        outer = Graph([inner.as_node().map_over("x")])
        runner = SyncRunner(cache=InMemoryCache())

        # Same value repeated — cache should deduplicate
        result = runner.run(outer, {"x": [5, 5, 5]})

        assert result.status == RunStatus.COMPLETED
        assert result["doubled"] == [10, 10, 10]
        assert counter.count == 1  # Only computed once


class TestCacheWithGates:
    """Tests for cache behavior on route/ifelse gate nodes."""

    def test_cached_route_skips_second_execution(self):
        """Cached route gate function is not called on second run with same inputs."""
        counter = CallCounter()

        @node(output_name="count")
        def producer(count: int) -> int:
            return count

        @route(targets=["producer", END], cache=True)
        def gate(count: int) -> str:
            counter.increment()
            return END if count >= 3 else "producer"

        graph = Graph([producer, gate])
        cache = InMemoryCache()
        runner = SyncRunner(cache=cache)

        r1 = runner.run(graph, {"count": 5})
        assert r1.status == RunStatus.COMPLETED
        assert r1["count"] == 5
        assert counter.count == 1

        # Second run — gate should be served from cache
        r2 = runner.run(graph, {"count": 5})
        assert r2["count"] == 5
        assert counter.count == 1  # Gate not called again

    def test_cached_route_different_inputs_miss(self):
        """Different inputs to cached gate produce cache miss."""
        counter = CallCounter()

        @node(output_name="count")
        def producer(count: int) -> int:
            return count

        @route(targets=["producer", END], cache=True)
        def gate(count: int) -> str:
            counter.increment()
            return END if count >= 3 else "producer"

        graph = Graph([producer, gate])
        runner = SyncRunner(cache=InMemoryCache())

        runner.run(graph, {"count": 5})
        runner.run(graph, {"count": 1})

        assert counter.count == 2  # Different inputs → different cache keys

    def test_cached_ifelse_skips_second_execution(self):
        """Cached ifelse gate function is not called on second run."""
        from hypergraph.nodes.gate import ifelse

        counter = CallCounter()

        @node(output_name="x")
        def producer(val: int) -> int:
            return val

        @ifelse(when_true="big", when_false="small", cache=True)
        def check(x: int) -> bool:
            counter.increment()
            return x > 10

        @node(output_name="result")
        def big(x: int) -> str:
            return "big"

        @node(output_name="result")
        def small(x: int) -> str:
            return "small"

        graph = Graph([producer, check, big, small])
        runner = SyncRunner(cache=InMemoryCache())

        r1 = runner.run(graph, {"val": 20})
        assert r1["result"] == "big"
        assert counter.count == 1

        r2 = runner.run(graph, {"val": 20})
        assert r2["result"] == "big"
        assert counter.count == 1  # Served from cache

    def test_cached_route_in_cycle(self):
        """Cached route in a cycle: gate is cached per-input across runs."""
        gate_counter = CallCounter()
        node_counter = CallCounter()

        @node(output_name="count", cache=True)
        def increment(count: int) -> int:
            node_counter.increment()
            return count + 1

        @route(targets=["increment", END], cache=True)
        def loop_gate(count: int) -> str:
            gate_counter.increment()
            return END if count >= 3 else "increment"

        graph = Graph([increment, loop_gate])
        runner = SyncRunner(cache=InMemoryCache())

        r1 = runner.run(graph, {"count": 0})
        assert r1["count"] == 3
        first_gate_count = gate_counter.count
        first_node_count = node_counter.count

        # Second run with same initial input — everything from cache
        r2 = runner.run(graph, {"count": 0})
        assert r2["count"] == 3
        # Gate and node calls should not increase (all cached)
        assert gate_counter.count == first_gate_count
        assert node_counter.count == first_node_count


class TestCacheWithBoundValues:
    """Tests for cache behavior with bound values."""

    def test_cached_node_with_bound_values(self):
        """Cached node respects bound values."""

        @node(output_name="result", cache=True)
        def with_bound(x: int, multiplier: int = 2) -> int:
            return x * multiplier

        graph = Graph([with_bound]).bind(multiplier=3)
        runner = SyncRunner(cache=InMemoryCache())

        result = runner.run(graph, {"x": 5})

        assert result["result"] == 15

    def test_different_bindings_produce_different_results(self):
        """Different bindings should produce different cache keys."""

        @node(output_name="result", cache=True)
        def with_bound(x: int, multiplier: int = 2) -> int:
            return x * multiplier

        graph1 = Graph([with_bound]).bind(multiplier=2)
        graph2 = Graph([with_bound]).bind(multiplier=3)
        cache = InMemoryCache()
        runner = SyncRunner(cache=cache)

        r1 = runner.run(graph1, {"x": 5})
        r2 = runner.run(graph2, {"x": 5})

        assert r1["result"] == 10
        assert r2["result"] == 15
