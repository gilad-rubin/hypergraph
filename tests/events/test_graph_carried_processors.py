"""Tests for graph-carried default event processors.

Covers ``Graph.with_processors(...)`` / ``Graph.default_event_processors`` and
the runner merge contract: every runner merges
``[*graph.default_event_processors, *event_processors]`` — carried processors
merge with call-site processors, never replace them, in both directions.
"""

from __future__ import annotations

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, node
from hypergraph.events import EventProcessor
from hypergraph.events.types import (
    NodeStartEvent,
    RunEndEvent,
    RunStartEvent,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class Recorder(EventProcessor):
    """Collects events; optionally journals (label, event) into a shared list."""

    def __init__(self, label: str = "", journal: list | None = None) -> None:
        self.label = label
        self.events: list = []
        self.shutdown_calls = 0
        self._journal = journal

    def on_event(self, event) -> None:
        self.events.append(event)
        if self._journal is not None:
            self._journal.append((self.label, event))

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def of_type(self, cls) -> list:
        return [e for e in self.events if isinstance(e, cls)]


class Exploder(EventProcessor):
    """Raises on every event — for the failure-isolation contract."""

    def on_event(self, event) -> None:
        raise RuntimeError("processor boom")


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3


# ---------------------------------------------------------------------------
# Graph API: with_processors / default_event_processors / repr
# ---------------------------------------------------------------------------


class TestWithProcessors:
    def test_default_is_empty_tuple(self):
        graph = Graph([double])
        assert graph.default_event_processors == ()
        assert isinstance(graph.default_event_processors, tuple)

    def test_returns_new_graph_accumulative(self):
        r1, r2, r3 = Recorder("a"), Recorder("b"), Recorder("c")
        graph = Graph([double])

        g2 = graph.with_processors(r1)
        g3 = g2.with_processors(r2, r3)

        assert g2 is not graph
        assert g3 is not g2
        assert g2.default_event_processors == (r1,)
        assert g3.default_event_processors == (r1, r2, r3)

    def test_original_graph_unchanged(self):
        r1 = Recorder("a")
        graph = Graph([double])
        original_tuple = graph.default_event_processors

        g2 = graph.with_processors(r1)

        assert graph.default_event_processors is original_tuple
        assert graph.default_event_processors == ()
        assert g2.default_event_processors == (r1,)

    def test_preserves_bind_and_select(self):
        graph = Graph([double, triple], name="carried").bind(x=2).select("tripled")
        g2 = graph.with_processors(Recorder())

        assert g2.selected == ("tripled",)
        result = SyncRunner().run(g2)
        assert result["tripled"] == 12

    def test_rejects_non_processor(self):
        graph = Graph([double])
        with pytest.raises(TypeError, match="expects EventProcessor instances, got object"):
            graph.with_processors(object())

    def test_add_nodes_preserves_carried_processors(self):
        r1 = Recorder("a")
        graph = Graph([double]).with_processors(r1)

        bigger = graph.add_nodes(triple)

        assert bigger.default_event_processors == (r1,)

    def test_repr_unchanged_when_no_processors(self):
        graph = Graph([double], name="plain")
        assert "processor" not in repr(graph)

    def test_repr_shows_carried_processors(self):
        graph = Graph([double], name="plain")
        g1 = graph.with_processors(Recorder())
        g2 = graph.with_processors(Recorder(), Recorder())

        assert repr(g1) == repr(graph) + " · 1 processor"
        assert repr(g2) == repr(graph) + " · 2 processors"


# ---------------------------------------------------------------------------
# Merge site 1 + 2: sync run() and sync map()
# ---------------------------------------------------------------------------


class TestSyncMergeSites:
    def test_run_carried_and_callsite_both_observe(self):
        journal: list = []
        carried = Recorder("carried", journal)
        callsite = Recorder("callsite", journal)
        graph = Graph([double], name="merged").with_processors(carried)

        result = SyncRunner().run(graph, {"x": 3}, event_processors=[callsite])

        assert result["doubled"] == 6
        for recorder in (carried, callsite):
            assert len(recorder.of_type(RunStartEvent)) == 1
            assert len(recorder.of_type(RunEndEvent)) == 1
        # Merge order: [*graph.default_event_processors, *event_processors]
        run_start_labels = [label for label, e in journal if isinstance(e, RunStartEvent)]
        assert run_start_labels == ["carried", "callsite"]

    def test_map_top_level_span_reaches_carried_processor(self):
        journal: list = []
        carried = Recorder("carried", journal)
        callsite = Recorder("callsite", journal)
        graph = Graph([double], name="mapped").with_processors(carried)

        results = SyncRunner().map(graph, {"x": [1, 2, 3]}, map_over="x", event_processors=[callsite])

        assert results["doubled"] == [2, 4, 6]
        for recorder in (carried, callsite):
            starts = recorder.of_type(RunStartEvent)
            # Exactly one top-level map RunStart plus one per item — each event
            # delivered exactly once (no double delivery through re-merging).
            assert sum(1 for e in starts if e.is_map) == 1
            assert sum(1 for e in starts if not e.is_map) == 3
            assert len(recorder.of_type(NodeStartEvent)) == 3
        map_start_labels = [label for label, e in journal if isinstance(e, RunStartEvent) and e.is_map]
        assert map_start_labels == ["carried", "callsite"]

    def test_run_carried_only(self):
        carried = Recorder("carried")
        graph = Graph([double], name="solo").with_processors(carried)

        result = SyncRunner().run(graph, {"x": 4})

        assert result["doubled"] == 8
        assert len(carried.of_type(RunStartEvent)) == 1
        assert len(carried.of_type(RunEndEvent)) == 1
        assert carried.shutdown_calls == 1

    def test_run_callsite_only_unaffected(self):
        callsite = Recorder("callsite")
        graph = Graph([double], name="bare")

        result = SyncRunner().run(graph, {"x": 4}, event_processors=[callsite])

        assert result["doubled"] == 8
        assert len(callsite.of_type(RunStartEvent)) == 1

    def test_map_iter_picks_up_carried_processors(self):
        carried = Recorder("carried")
        graph = Graph([double], name="streamed").with_processors(carried)

        results = list(SyncRunner().map_iter(graph, {"x": [5, 6]}, map_over="x"))

        assert [r["doubled"] for _, r in results] == [10, 12]
        starts = carried.of_type(RunStartEvent)
        # map_iter delegates through run() per item: one RunStart per item,
        # no top-level map span.
        assert len(starts) == 2
        assert all(not e.is_map for e in starts)

    def test_carried_processor_failure_is_isolated(self):
        survivor = Recorder("survivor")
        graph = Graph([double], name="isolated").with_processors(Exploder(), survivor)

        result = SyncRunner().run(graph, {"x": 5})

        assert result.completed
        assert result["doubled"] == 10
        assert len(survivor.of_type(RunStartEvent)) == 1
        assert len(survivor.of_type(RunEndEvent)) == 1

    def test_nested_graphnode_events_reach_carried_processor(self):
        """Carried processors follow into GraphNode sub-runs like call-site ones."""
        carried = Recorder("carried")
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node(), triple], name="outer").with_processors(carried)

        result = SyncRunner().run(outer, {"x": 2})

        assert result["tripled"] == 12
        starts = carried.of_type(RunStartEvent)
        assert {e.graph_name for e in starts} == {"outer", "inner"}
        inner_start = next(e for e in starts if e.graph_name == "inner")
        assert inner_start.parent_span_id is not None


# ---------------------------------------------------------------------------
# Merge site 3 + 4: async run() and async map()
# ---------------------------------------------------------------------------


class TestAsyncMergeSites:
    async def test_run_carried_and_callsite_both_observe(self):
        journal: list = []
        carried = Recorder("carried", journal)
        callsite = Recorder("callsite", journal)
        graph = Graph([double], name="merged_async").with_processors(carried)

        result = await AsyncRunner().run(graph, {"x": 3}, event_processors=[callsite])

        assert result["doubled"] == 6
        for recorder in (carried, callsite):
            assert len(recorder.of_type(RunStartEvent)) == 1
            assert len(recorder.of_type(RunEndEvent)) == 1
        run_start_labels = [label for label, e in journal if isinstance(e, RunStartEvent)]
        assert run_start_labels == ["carried", "callsite"]

    async def test_map_top_level_span_reaches_carried_processor(self):
        journal: list = []
        carried = Recorder("carried", journal)
        callsite = Recorder("callsite", journal)
        graph = Graph([double], name="mapped_async").with_processors(carried)

        results = await AsyncRunner().map(graph, {"x": [1, 2, 3]}, map_over="x", event_processors=[callsite])

        assert results["doubled"] == [2, 4, 6]
        for recorder in (carried, callsite):
            starts = recorder.of_type(RunStartEvent)
            assert sum(1 for e in starts if e.is_map) == 1
            assert sum(1 for e in starts if not e.is_map) == 3
            assert len(recorder.of_type(NodeStartEvent)) == 3
        map_start_labels = [label for label, e in journal if isinstance(e, RunStartEvent) and e.is_map]
        assert map_start_labels == ["carried", "callsite"]

    async def test_run_carried_only(self):
        carried = Recorder("carried")
        graph = Graph([double], name="solo_async").with_processors(carried)

        result = await AsyncRunner().run(graph, {"x": 4})

        assert result["doubled"] == 8
        assert len(carried.of_type(RunStartEvent)) == 1
        assert len(carried.of_type(RunEndEvent)) == 1
