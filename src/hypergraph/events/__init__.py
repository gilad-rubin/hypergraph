"""Event system for observing graph execution."""

from hypergraph.events.console import ConsoleProcessor, LiveConsole, render_console
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
    InnerCacheEvent,
    InterruptEvent,
    NodeAttemptEndEvent,
    NodeAttemptStartEvent,
    NodeEndEvent,
    NodeErrorEvent,
    NodeStartEvent,
    RouteDecisionEvent,
    RunEndEvent,
    RunStartEvent,
    RunStatus,
    StopRequestedEvent,
    StreamingChunkEvent,
    SuperstepStartEvent,
)

__all__ = [
    # Event types
    "BaseEvent",
    "CacheHitEvent",
    "InnerCacheEvent",
    "Event",
    "InterruptEvent",
    "NodeAttemptEndEvent",
    "NodeAttemptStartEvent",
    "NodeEndEvent",
    "NodeErrorEvent",
    "NodeStartEvent",
    "RouteDecisionEvent",
    "RunEndEvent",
    "RunStartEvent",
    "RunStatus",
    "StopRequestedEvent",
    "StreamingChunkEvent",
    "SuperstepStartEvent",
    # Processor interfaces
    "AsyncEventProcessor",
    "EventProcessor",
    "TypedEventProcessor",
    # The console
    "ConsoleProcessor",
    "LiveConsole",
    "render_console",
    # Dispatcher
    "EventDispatcher",
]
