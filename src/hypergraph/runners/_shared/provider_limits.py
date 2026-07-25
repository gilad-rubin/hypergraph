"""Composing the injected provider-resource budgets around a node execution.

Both function-node executors (sync and async) call ``provider_permits`` so
graph- and node-scope budgets compose identically on either path. This is
provider-resource admission — external capacity — and is never the durable
host's active-Run cap (``RunHome.max_active_runs``) nor the per-call
``max_concurrency`` work budget.

Acquisition order is fixed: graph budget first (the broader one), then the
node budget. One fixed order across every execution path is what keeps two
nodes holding two limiters from deadlocking each other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hypergraph.limits import ProcessLocalLimiter


def provider_permits(
    graph_limit: ProcessLocalLimiter | None,
    node_limit: ProcessLocalLimiter | None,
) -> tuple[ProcessLocalLimiter, ...]:
    """Budgets to hold for one node execution, outermost first.

    The same limiter injected at both scopes yields ONE permit: these pools
    are not reentrant, so acquiring twice would deadlock a graph that shares
    its budget with a node.
    """
    permits: list[ProcessLocalLimiter] = []
    for limiter in (graph_limit, node_limit):
        if limiter is not None and not any(limiter is held for held in permits):
            permits.append(limiter)
    return tuple(permits)
