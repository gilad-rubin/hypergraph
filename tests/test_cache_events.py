"""Tests for cache-related event emission."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace

from hypergraph import Graph, InMemoryCache, SyncRunner, node
from hypergraph.events import EventProcessor
from hypergraph.events.types import (
    CacheHitEvent,
    InnerCacheEvent,
    NodeEndEvent,
    NodeStartEvent,
)
from hypergraph.runners._shared.cache_observer import node_cache_observer


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
            e for e in proc.events if isinstance(e, (NodeStartEvent, CacheHitEvent, NodeEndEvent)) and getattr(e, "node_name", "") == "add_one"
        ]
        assert len(node_events) == 3
        assert isinstance(node_events[0], NodeStartEvent)
        assert isinstance(node_events[1], CacheHitEvent)
        assert isinstance(node_events[2], NodeEndEvent)


class TestInnerCacheObserverBridge:
    """Tests for Hypergraph's bridge into Hypercache telemetry."""

    def test_uses_public_hypercache_observer_api(self, monkeypatch):
        fake_hypercache = ModuleType("hypercache")
        observed_callbacks = []

        @contextmanager
        def observe_cache(callback):
            observed_callbacks.append(callback)
            callback(
                SimpleNamespace(
                    instance="Service",
                    operation="embed",
                    hit=True,
                    stale=False,
                    refreshing=False,
                    wrote=False,
                    mode="normal",
                )
            )
            yield

        fake_hypercache.observe_cache = observe_cache
        monkeypatch.setitem(sys.modules, "hypercache", fake_hypercache)

        emitted_events: list[InnerCacheEvent] = []
        with node_cache_observer(
            emitted_events.append,
            run_id="run-1",
            node_name="embed_node",
            graph_name="demo_graph",
            node_span_id="span-1",
        ):
            pass

        assert len(observed_callbacks) == 1
        assert len(emitted_events) == 1
        event = emitted_events[0]
        assert event.node_name == "embed_node"
        assert event.graph_name == "demo_graph"
        assert event.parent_span_id == "span-1"
        assert event.instance == "Service"
        assert event.operation == "embed"
        assert event.hit is True
