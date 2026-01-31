"""Tests for cache-related event emission."""

from __future__ import annotations

from hypergraph import Graph, InMemoryCache, node, SyncRunner
from hypergraph.events import EventProcessor
from hypergraph.events.types import CacheHitEvent, NodeEndEvent, NodeStartEvent


class ListProcessor(EventProcessor):
    """Collects all events for assertion."""

    def __init__(self):
        self.events: list = []

    def on_event(self, event):
        self.events.append(event)

    def of_type(self, cls):
        return [e for e in self.events if isinstance(e, cls)]


class TestCacheHitEventEmission:
    """Tests that CacheHitEvent is emitted on cache hits."""

    def test_no_cache_hit_on_first_run(self):
        """First run should not emit CacheHitEvent."""

        @node(output_name="result", cache=True)
        def add_one(x: int) -> int:
            return x + 1

        graph = Graph([add_one])
        proc = ListProcessor()
        runner = SyncRunner(cache=InMemoryCache())

        runner.run(graph, {"x": 1}, event_processors=[proc])

        assert len(proc.of_type(CacheHitEvent)) == 0

    def test_cache_hit_on_second_run(self):
        """Second run with same inputs should emit CacheHitEvent."""

        @node(output_name="result", cache=True)
        def add_one(x: int) -> int:
            return x + 1

        graph = Graph([add_one])
        cache = InMemoryCache()
        runner = SyncRunner(cache=cache)

        runner.run(graph, {"x": 1})

        proc = ListProcessor()
        runner.run(graph, {"x": 1}, event_processors=[proc])

        hits = proc.of_type(CacheHitEvent)
        assert len(hits) == 1
        assert hits[0].node_name == "add_one"
        assert hits[0].cache_key != ""

    def test_no_cache_hit_for_uncached_node(self):
        """Nodes without cache=True never emit CacheHitEvent."""

        @node(output_name="result")
        def add_one(x: int) -> int:
            return x + 1

        graph = Graph([add_one])
        cache = InMemoryCache()
        runner = SyncRunner(cache=cache)

        runner.run(graph, {"x": 1})

        proc = ListProcessor()
        runner.run(graph, {"x": 1}, event_processors=[proc])

        assert len(proc.of_type(CacheHitEvent)) == 0


class TestNodeEndEventCachedField:
    """Tests for NodeEndEvent.cached field."""

    def test_cached_false_on_first_run(self):
        """First execution should have cached=False."""

        @node(output_name="result", cache=True)
        def add_one(x: int) -> int:
            return x + 1

        graph = Graph([add_one])
        proc = ListProcessor()
        runner = SyncRunner(cache=InMemoryCache())

        runner.run(graph, {"x": 1}, event_processors=[proc])

        node_ends = [e for e in proc.of_type(NodeEndEvent) if e.node_name == "add_one"]
        assert len(node_ends) == 1
        assert node_ends[0].cached is False

    def test_cached_true_on_cache_hit(self):
        """Cache hit should produce NodeEndEvent with cached=True."""

        @node(output_name="result", cache=True)
        def add_one(x: int) -> int:
            return x + 1

        graph = Graph([add_one])
        cache = InMemoryCache()
        runner = SyncRunner(cache=cache)

        runner.run(graph, {"x": 1})

        proc = ListProcessor()
        runner.run(graph, {"x": 1}, event_processors=[proc])

        node_ends = [e for e in proc.of_type(NodeEndEvent) if e.node_name == "add_one"]
        assert len(node_ends) == 1
        assert node_ends[0].cached is True

    def test_cache_hit_emits_node_start_before_end(self):
        """Cache hit should still emit NodeStartEvent before NodeEndEvent."""

        @node(output_name="result", cache=True)
        def add_one(x: int) -> int:
            return x + 1

        graph = Graph([add_one])
        cache = InMemoryCache()
        runner = SyncRunner(cache=cache)

        runner.run(graph, {"x": 1})

        proc = ListProcessor()
        runner.run(graph, {"x": 1}, event_processors=[proc])

        # Event order: NodeStartEvent -> CacheHitEvent -> NodeEndEvent
        node_events = [
            e
            for e in proc.events
            if isinstance(e, (NodeStartEvent, CacheHitEvent, NodeEndEvent))
            and getattr(e, "node_name", "") == "add_one"
        ]
        assert len(node_events) == 3
        assert isinstance(node_events[0], NodeStartEvent)
        assert isinstance(node_events[1], CacheHitEvent)
        assert isinstance(node_events[2], NodeEndEvent)
