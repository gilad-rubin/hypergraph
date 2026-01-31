"""Hypergraph - A hierarchical and modular graph workflow framework."""

from hypergraph.graph import Graph, GraphConfigError, InputSpec
from hypergraph.nodes import (
    END,
    FunctionNode,
    GateNode,
    GraphNode,
    HyperNode,
    IfElseNode,
    RenameError,
    RouteNode,
    ifelse,
    node,
    route,
)
from hypergraph.exceptions import (
    IncompatibleRunnerError,
    InfiniteLoopError,
    MissingInputError,
)
from hypergraph.runners import (
    AsyncRunner,
    BaseRunner,
    RunResult,
    RunStatus,
    SyncRunner,
)
from hypergraph.events import (
    AsyncEventProcessor,
    BaseEvent,
    Event,
    EventDispatcher,
    EventProcessor,
    InterruptEvent,
    NodeEndEvent,
    NodeErrorEvent,
    NodeStartEvent,
    RouteDecisionEvent,
    RunEndEvent,
    RunStartEvent,
    StopRequestedEvent,
    TypedEventProcessor,
)
from hypergraph.events.rich_progress import RichProgressProcessor

__all__ = [
    # Decorators and node types
    "node",
    "ifelse",
    "route",
    "FunctionNode",
    "GraphNode",
    "GateNode",
    "IfElseNode",
    "RouteNode",
    "HyperNode",
    "END",
    # Graph
    "Graph",
    "InputSpec",
    # Runners
    "SyncRunner",
    "AsyncRunner",
    "BaseRunner",
    "RunResult",
    "RunStatus",
    # Errors
    "RenameError",
    "GraphConfigError",
    "MissingInputError",
    "InfiniteLoopError",
    "IncompatibleRunnerError",
    # Events
    "BaseEvent",
    "Event",
    "EventDispatcher",
    "EventProcessor",
    "AsyncEventProcessor",
    "TypedEventProcessor",
    "InterruptEvent",
    "NodeEndEvent",
    "NodeErrorEvent",
    "NodeStartEvent",
    "RouteDecisionEvent",
    "RunEndEvent",
    "RunStartEvent",
    "StopRequestedEvent",
    "RichProgressProcessor",
]
