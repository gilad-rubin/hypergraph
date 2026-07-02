"""current_node_span(): in-node telemetry can identify the executing node span."""

from __future__ import annotations

import asyncio

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, current_node_span, node
from hypergraph.events import EventProcessor, NodeStartEvent


class _SpanIndex(EventProcessor):
    """Record node span ids exactly as the event stream reports them."""

    def __init__(self) -> None:
        self.by_node: dict[str, list[str]] = {}

    def on_event(self, event) -> None:
        if isinstance(event, NodeStartEvent):
            self.by_node.setdefault(event.node_name, []).append(event.span_id)


def test_no_span_outside_graph_execution():
    assert current_node_span() is None


def test_sync_runner_publishes_node_span():
    seen: list = []

    @node(output_name="out")
    def observe(value: int) -> int:
        seen.append(current_node_span())
        return value

    index = _SpanIndex()
    graph = Graph([observe], name="obs_graph")
    SyncRunner().run(graph, {"value": 1}, event_processors=[index])

    assert current_node_span() is None
    (ref,) = seen
    assert ref is not None
    assert ref.node_name == "observe"
    assert ref.graph_name == "obs_graph"
    assert ref.span_id in index.by_node["observe"]


@pytest.mark.asyncio
async def test_async_concurrent_nodes_see_their_own_span():
    """Concurrently executing nodes each observe their own span, not a sibling's."""
    observed: dict[str, str] = {}

    @node(output_name="a_out")
    async def node_a(value: int) -> int:
        await asyncio.sleep(0.01)
        observed["node_a"] = current_node_span().span_id
        return value

    @node(output_name="b_out")
    async def node_b(value: int) -> int:
        await asyncio.sleep(0.01)
        observed["node_b"] = current_node_span().span_id
        return value

    index = _SpanIndex()
    graph = Graph([node_a, node_b], name="parallel_graph")
    await AsyncRunner().run(graph, {"value": 1}, event_processors=[index])

    assert observed["node_a"] != observed["node_b"]
    assert observed["node_a"] in index.by_node["node_a"]
    assert observed["node_b"] in index.by_node["node_b"]


@pytest.mark.asyncio
async def test_mapped_items_see_distinct_spans():
    """Each .map_over item execution observes a distinct span id."""
    span_ids: list[str] = []

    @node(output_name="doubled")
    async def double(value: int) -> int:
        await asyncio.sleep(0.01)
        span_ids.append(current_node_span().span_id)
        return value * 2

    graph = Graph([double], name="map_graph")
    await AsyncRunner().map(graph, {"value": [1, 2, 3]}, map_over="value")

    assert len(span_ids) == 3
    assert len(set(span_ids)) == 3
