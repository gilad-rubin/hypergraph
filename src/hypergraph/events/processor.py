"""Event processor base classes."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hypergraph.events.types import (
        Event,
        InterruptEvent,
        NodeEndEvent,
        NodeErrorEvent,
        NodeStartEvent,
        RouteDecisionEvent,
        RunEndEvent,
        RunStartEvent,
        StopRequestedEvent,
    )


# Mapping from event class name to handler method name.
_EVENT_METHOD_MAP: dict[str, str] = {
    "RunStartEvent": "on_run_start",
    "RunEndEvent": "on_run_end",
    "NodeStartEvent": "on_node_start",
    "NodeEndEvent": "on_node_end",
    "NodeErrorEvent": "on_node_error",
    "RouteDecisionEvent": "on_route_decision",
    "InterruptEvent": "on_interrupt",
    "StopRequestedEvent": "on_stop_requested",
}


class EventProcessor:
    """Base class for synchronous event consumers.

    Subclass and override ``on_event`` to receive all events,
    or use ``TypedEventProcessor`` for per-type dispatch.
    """

    def on_event(self, event: Event) -> None:
        """Called for every event. Override in subclasses."""

    def shutdown(self) -> None:
        """Called once when the run is complete. Override to flush buffers."""


class AsyncEventProcessor(EventProcessor):
    """Extends EventProcessor with async variants.

    The async runner will prefer ``on_event_async`` and ``shutdown_async``
    when available, falling back to the sync methods otherwise.
    """

    async def on_event_async(self, event: Event) -> None:
        """Async version of on_event. Override in subclasses."""

    async def shutdown_async(self) -> None:
        """Async version of shutdown. Override to flush buffers."""


class TypedEventProcessor(EventProcessor):
    """Dispatches ``on_event`` to typed handler methods automatically.

    Override any of the ``on_*`` methods below to handle specific event types.
    Unhandled event types are silently ignored.
    """

    def on_event(self, event: Event) -> None:
        method_name = _EVENT_METHOD_MAP.get(type(event).__name__)
        if method_name is not None:
            method = getattr(self, method_name, None)
            if method is not None:
                method(event)

    def on_run_start(self, event: RunStartEvent) -> None: ...
    def on_run_end(self, event: RunEndEvent) -> None: ...
    def on_node_start(self, event: NodeStartEvent) -> None: ...
    def on_node_end(self, event: NodeEndEvent) -> None: ...
    def on_node_error(self, event: NodeErrorEvent) -> None: ...
    def on_route_decision(self, event: RouteDecisionEvent) -> None: ...
    def on_interrupt(self, event: InterruptEvent) -> None: ...
    def on_stop_requested(self, event: StopRequestedEvent) -> None: ...
