"""Composing the injected provider-resource budgets around a node execution.

Both function-node executors (sync and async) call ``provider_permits`` so
graph- and node-scope budgets compose identically on either path. This is
provider-resource admission — external capacity — and is never the durable
host's active-Run cap (``RunHome.max_active_runs``).

Graph scope crosses the nested-graph boundary. A nested graph is executed by
its own ``run()`` call with its own ``ExecutionContext``, so the enclosing
graph's budget is carried across that boundary in a ``ContextVar`` — the same
mechanism the async runner already uses to share one concurrency semaphore
with nested graphs. Without it, moving a node into ``as_node()`` would
silently drop it out of the parent's budget.

Acquisition order is fixed: graph budgets outermost-first (the enclosing
graph before the nested one), then the node budget. One fixed order across
every execution path is what keeps two nodes holding two limiters from
deadlocking each other.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hypergraph.limits import ProcessLocalLimiter

# The graph-scope budgets in force for the graph currently executing,
# outermost first. Nested runs read it at entry; nothing else touches it.
_GRAPH_SCOPE_LIMITS: ContextVar[tuple[ProcessLocalLimiter, ...]] = ContextVar("hypergraph_graph_provider_limits", default=())


def current_graph_limits() -> tuple[ProcessLocalLimiter, ...]:
    """Graph-scope budgets inherited from an enclosing graph, outermost first."""
    return _GRAPH_SCOPE_LIMITS.get()


def compose_graph_limits(graph_limit: ProcessLocalLimiter | None) -> tuple[ProcessLocalLimiter, ...]:
    """Budgets in force for a graph that declares ``graph_limit``.

    Returns the inherited tuple **unchanged and identical** when this graph
    adds nothing, so a caller can test ``result is current_graph_limits()``
    to decide whether it needs to push a new scope at all. A graph that
    re-declares a limiter an enclosing graph already holds contributes
    nothing: these pools are not reentrant, so acquiring twice would
    deadlock.
    """
    inherited = _GRAPH_SCOPE_LIMITS.get()
    if graph_limit is None or any(graph_limit is held for held in inherited):
        return inherited
    return (*inherited, graph_limit)


def push_graph_limits(limits: tuple[ProcessLocalLimiter, ...]) -> Token[tuple[ProcessLocalLimiter, ...]]:
    """Make ``limits`` the budgets nested graph runs inherit."""
    return _GRAPH_SCOPE_LIMITS.set(limits)


def pop_graph_limits(token: Token[tuple[ProcessLocalLimiter, ...]]) -> None:
    """Restore the budgets in force before the matching ``push_graph_limits``."""
    _GRAPH_SCOPE_LIMITS.reset(token)


def provider_permits(
    graph_limits: tuple[ProcessLocalLimiter, ...],
    node_limit: ProcessLocalLimiter | None,
) -> tuple[ProcessLocalLimiter, ...]:
    """Budgets to hold for one node execution, outermost first.

    The same limiter injected at two scopes yields ONE permit: these pools
    are not reentrant, so acquiring twice would deadlock a graph that shares
    its budget with a node.
    """
    permits: list[ProcessLocalLimiter] = []
    for limiter in (*graph_limits, node_limit):
        if limiter is not None and not any(limiter is held for held in permits):
            permits.append(limiter)
    return tuple(permits)
