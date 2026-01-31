"""Event system for observing graph execution."""

from hypergraph.events.dispatcher import EventDispatcher
from hypergraph.events.processor import (
    AsyncEventProcessor,
    EventProcessor,
    TypedEventProcessor,
)
from hypergraph.events.types import (
    BaseEvent,
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

__all__ = [
    # Event types
    "BaseEvent",
    "Event",
    "InterruptEvent",
    "NodeEndEvent",
    "NodeErrorEvent",
    "NodeStartEvent",
    "RouteDecisionEvent",
    "RunEndEvent",
    "RunStartEvent",
    "StopRequestedEvent",
    # Processor interfaces
    "AsyncEventProcessor",
    "EventProcessor",
    "TypedEventProcessor",
    # Dispatcher
    "EventDispatcher",
]
