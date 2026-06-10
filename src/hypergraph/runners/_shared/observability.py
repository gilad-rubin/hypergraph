"""Current-node-span context for external observability tools.

While a node executes, the runner publishes the node's span identity in a
``ContextVar``. Code that runs *inside* the node — an LLM client, a vector
store, any instrumented component — can call :func:`current_node_span` to
attribute its own telemetry (model calls, token usage, cache lookups) to the
exact node span that triggered it, even when many nodes run concurrently.

Zero coupling, mirroring ``cache_observer``: hypergraph knows nothing about
the consumers; consumers soft-import this accessor and tolerate ``None``
(no graph running, or a runner that does not publish spans).
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class NodeSpanRef:
    """Identity of the node span currently executing in this context."""

    run_id: str
    span_id: str
    node_name: str
    graph_name: str


_current_node_span: ContextVar[NodeSpanRef | None] = ContextVar("hypergraph_current_node_span", default=None)


def current_node_span() -> NodeSpanRef | None:
    """Return the span identity of the node executing in this context, if any."""
    return _current_node_span.get()


def set_current_node_span(ref: NodeSpanRef) -> Token:
    """Publish the executing node's span (runner-internal); returns a reset token."""
    return _current_node_span.set(ref)


def reset_current_node_span(token: Token) -> None:
    """Restore the previous span context (runner-internal)."""
    _current_node_span.reset(token)
