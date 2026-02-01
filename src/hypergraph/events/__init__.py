"""Event system for observing graph execution."""

from hypergraph.events.dispatcher import EventDispatcher
from hypergraph.events.processor import (
    AsyncEventProcessor,
    EventProcessor,
    TypedEventProcessor,
)
from hypergraph.events.types import (
    BaseEvent,
    CacheHitEvent,
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
    "CacheHitEvent",
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
