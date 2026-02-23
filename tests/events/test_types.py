"""Tests for event types and processor interfaces."""

from __future__ import annotations

import asyncio

import pytest

from hypergraph.events import (
    AsyncEventProcessor,
    EventDispatcher,
    EventProcessor,
    TypedEventProcessor,
)
from hypergraph.events.types import (
    InterruptEvent,
    NodeEndEvent,
    NodeErrorEvent,
    NodeStartEvent,
    RouteDecisionEvent,
    RunEndEvent,
    RunStartEvent,
    StopRequestedEvent,
)

# ---------------------------------------------------------------------------
# Event immutability
# ---------------------------------------------------------------------------


class TestEventImmutability:
    def test_frozen_prevents_mutation(self):
        event = RunStartEvent(run_id="r1", graph_name="g")
        with pytest.raises(AttributeError):
            event.graph_name = "other"  # type: ignore[misc]

    def test_default_fields(self):
        event = NodeStartEvent(run_id="r1", node_name="n", graph_name="g")
        assert event.run_id == "r1"
        assert event.parent_span_id is None
        assert event.span_id  # auto-generated
        assert event.timestamp > 0

    def test_all_event_types_constructible(self):
        """Every event type can be instantiated with just run_id."""
        for cls in (
            RunStartEvent,
            RunEndEvent,
            NodeStartEvent,
            NodeEndEvent,
            NodeErrorEvent,
            RouteDecisionEvent,
            InterruptEvent,
            StopRequestedEvent,
        ):
            e = cls(run_id="r1")
            assert e.run_id == "r1"


# ---------------------------------------------------------------------------
# TypedEventProcessor dispatch
# ---------------------------------------------------------------------------


class _Recorder(TypedEventProcessor):
    """Records which handler methods were called."""

    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def on_run_start(self, event):
        self.calls.append(("on_run_start", event))

    def on_run_end(self, event):
        self.calls.append(("on_run_end", event))

    def on_node_start(self, event):
        self.calls.append(("on_node_start", event))

    def on_node_end(self, event):
        self.calls.append(("on_node_end", event))

    def on_node_error(self, event):
        self.calls.append(("on_node_error", event))

    def on_route_decision(self, event):
        self.calls.append(("on_route_decision", event))

    def on_interrupt(self, event):
        self.calls.append(("on_interrupt", event))

    def on_stop_requested(self, event):
        self.calls.append(("on_stop_requested", event))


class TestTypedEventProcessor:
    def test_dispatches_to_correct_handler(self):
        rec = _Recorder()
        ev = NodeStartEvent(run_id="r1", node_name="a", graph_name="g")
        rec.on_event(ev)
        assert len(rec.calls) == 1
        assert rec.calls[0] == ("on_node_start", ev)

    def test_all_event_types_dispatch(self):
        rec = _Recorder()
        events = [
            RunStartEvent(run_id="r1"),
            RunEndEvent(run_id="r1"),
            NodeStartEvent(run_id="r1"),
            NodeEndEvent(run_id="r1"),
            NodeErrorEvent(run_id="r1"),
            RouteDecisionEvent(run_id="r1"),
            InterruptEvent(run_id="r1"),
            StopRequestedEvent(run_id="r1"),
        ]
        for ev in events:
            rec.on_event(ev)
        assert len(rec.calls) == 8
        method_names = [c[0] for c in rec.calls]
        assert "on_run_start" in method_names
        assert "on_stop_requested" in method_names

    def test_partial_override_ignores_unhandled(self):
        """A processor that only overrides on_run_start silently ignores other events."""

        class Partial(TypedEventProcessor):
            def __init__(self):
                self.seen = False

            def on_run_start(self, _event):
                self.seen = True

        p = Partial()
        p.on_event(NodeStartEvent(run_id="r1"))
        assert not p.seen
        p.on_event(RunStartEvent(run_id="r1"))
        assert p.seen


# ---------------------------------------------------------------------------
# EventDispatcher
# ---------------------------------------------------------------------------


class _ListProcessor(EventProcessor):
    """Collects all events."""

    def __init__(self):
        self.events: list[object] = []
        self.shutdown_called = False

    def on_event(self, event):
        self.events.append(event)

    def shutdown(self):
        self.shutdown_called = True


class _FailingProcessor(EventProcessor):
    """Always raises on on_event."""

    def on_event(self, event):
        raise RuntimeError("boom")

    def shutdown(self):
        raise RuntimeError("boom shutdown")


class TestEventDispatcher:
    def test_emit_to_multiple_processors(self):
        p1, p2 = _ListProcessor(), _ListProcessor()
        d = EventDispatcher([p1, p2])
        ev = RunStartEvent(run_id="r1", graph_name="g")
        d.emit(ev)
        assert p1.events == [ev]
        assert p2.events == [ev]

    def test_active_property(self):
        assert not EventDispatcher().active
        assert not EventDispatcher([]).active
        assert EventDispatcher([_ListProcessor()]).active

    def test_failing_processor_does_not_block_others(self):
        good = _ListProcessor()
        d = EventDispatcher([_FailingProcessor(), good])
        ev = RunStartEvent(run_id="r1")
        d.emit(ev)
        assert good.events == [ev]

    def test_shutdown_calls_all(self):
        p1, p2 = _ListProcessor(), _ListProcessor()
        d = EventDispatcher([p1, p2])
        d.shutdown()
        assert p1.shutdown_called
        assert p2.shutdown_called

    def test_shutdown_best_effort(self):
        good = _ListProcessor()
        d = EventDispatcher([_FailingProcessor(), good])
        d.shutdown()
        assert good.shutdown_called

    def test_empty_dispatcher_noop(self):
        d = EventDispatcher()
        d.emit(RunStartEvent(run_id="r1"))  # should not raise
        d.shutdown()


# ---------------------------------------------------------------------------
# Async dispatcher
# ---------------------------------------------------------------------------


class _AsyncListProcessor(AsyncEventProcessor):
    def __init__(self):
        self.events: list[object] = []
        self.shutdown_called = False

    async def on_event_async(self, event):
        self.events.append(event)

    async def shutdown_async(self):
        self.shutdown_called = True


class TestAsyncDispatcher:
    def test_emit_async(self):
        sync_p = _ListProcessor()
        async_p = _AsyncListProcessor()
        d = EventDispatcher([sync_p, async_p])
        ev = RunStartEvent(run_id="r1")
        asyncio.run(d.emit_async(ev))
        assert sync_p.events == [ev]
        assert async_p.events == [ev]

    def test_shutdown_async(self):
        sync_p = _ListProcessor()
        async_p = _AsyncListProcessor()
        d = EventDispatcher([sync_p, async_p])
        asyncio.run(d.shutdown_async())
        assert sync_p.shutdown_called
        assert async_p.shutdown_called
