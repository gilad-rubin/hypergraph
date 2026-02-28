"""Phase 0: Prerequisite tests — verify assumptions the RunLogCollector depends on.

These must pass BEFORE writing the collector. They codify invariants
from the existing event system that the collector's correctness relies on.
"""

from hypergraph import (
    EventDispatcher,
    Graph,
    NodeEndEvent,
    RouteDecisionEvent,
    SyncRunner,
    TypedEventProcessor,
    node,
    route,
)


class RecordingProcessor(TypedEventProcessor):
    """Records event types in order for assertion."""

    def __init__(self):
        self.events: list[type] = []

    def on_node_start(self, event):
        self.events.append(type(event))

    def on_node_end(self, event):
        self.events.append(type(event))

    def on_route_decision(self, event):
        self.events.append(type(event))

    def on_node_error(self, event):
        self.events.append(type(event))

    def on_cache_hit(self, event):
        self.events.append(type(event))


def test_route_decision_fires_before_node_end():
    """RouteDecisionEvent fires BEFORE NodeEndEvent for gate nodes.

    The collector buffers decisions and applies them on NodeEndEvent.
    If this ordering changes, the collector breaks.
    """

    @route(targets=["a", "b"])
    def decide(x: int) -> str:
        return "a"

    @node(output_name="result")
    def a(x: int) -> str:
        return "from a"

    @node(output_name="result")
    def b(x: int) -> str:
        return "from b"

    recorder = RecordingProcessor()
    graph = Graph([decide, a, b])
    SyncRunner().run(graph, {"x": 1}, event_processors=[recorder])

    # Find the decision event and the node_end for "decide"
    assert RouteDecisionEvent in recorder.events
    assert NodeEndEvent in recorder.events

    decision_idx = recorder.events.index(RouteDecisionEvent)
    # The NodeEndEvent after the decision should come after it
    end_indices = [i for i, e in enumerate(recorder.events) if e is NodeEndEvent]
    # At least one NodeEndEvent comes after the RouteDecisionEvent
    assert any(i > decision_idx for i in end_indices)


def test_dispatcher_active_with_prepended_processor():
    """Prepending a processor to the dispatcher makes it active,
    even when no user processors exist.

    The collector is always prepended — this ensures events flow.
    """
    processor = RecordingProcessor()
    dispatcher = EventDispatcher([processor])
    assert dispatcher.active is True


def test_dispatcher_inactive_with_no_processors():
    """Baseline: dispatcher is inactive when no processors exist."""
    dispatcher = EventDispatcher(None)
    assert dispatcher.active is False


def test_node_end_event_has_timing_and_cached_fields():
    """NodeEndEvent carries duration_ms and cached — data the collector needs."""

    @node(output_name="y")
    def add_one(x: int) -> int:
        return x + 1

    events_with_details: list = []

    class DetailRecorder(TypedEventProcessor):
        def on_node_end(self, event):
            events_with_details.append(event)

    graph = Graph([add_one])
    SyncRunner().run(graph, {"x": 1}, event_processors=[DetailRecorder()])

    assert len(events_with_details) == 1
    evt = events_with_details[0]
    assert hasattr(evt, "duration_ms")
    assert hasattr(evt, "cached")
    assert isinstance(evt.duration_ms, float)
    assert evt.duration_ms >= 0
    assert evt.cached is False
