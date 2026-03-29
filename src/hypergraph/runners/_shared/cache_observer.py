"""Context manager that installs a hypercache observer during node execution.

When hypercache is installed, this bridges CacheTelemetry events from
hypercache.CacheService into hypergraph's InnerCacheEvent stream.

Zero coupling: hypercache has no knowledge of hypergraph. This module
performs a one-way import of hypercache's public observer API at runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from typing import Any


@contextmanager
def node_cache_observer(
    emit_fn: Callable[[Any], None],
    *,
    run_id: str,
    node_name: str,
    graph_name: str,
    node_span_id: str,
):
    """Install a hypercache observer scoped to the current node execution.

    Observer exceptions are caught by hypercache
    and never propagate to node execution. If hypercache is not installed,
    this is a pure no-op.

    Args:
        emit_fn: The dispatcher emit function from ExecutionContext.
        run_id: Current run ID for event correlation.
        node_name: Name of the executing node.
        graph_name: Name of the graph.
        node_span_id: The node's span ID (ctx.parent_span_id in executors),
            used as parent_span_id on InnerCacheEvent for OTEL correlation.
    """
    try:
        from hypercache import observe_cache
    except ImportError:
        yield
        return

    from hypergraph.events.types import InnerCacheEvent

    def on_cache(t) -> None:
        emit_fn(
            InnerCacheEvent(
                run_id=run_id,
                parent_span_id=node_span_id,
                node_name=node_name,
                graph_name=graph_name,
                instance=t.instance,
                operation=t.operation,
                hit=t.hit,
                stale=t.stale,
                refreshing=t.refreshing,
                wrote=t.wrote,
                mode=t.mode,
            )
        )

    with observe_cache(on_cache):
        yield
