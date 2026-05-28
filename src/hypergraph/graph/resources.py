"""Graph resource scope support."""

from __future__ import annotations

import inspect
import sys
from contextlib import AsyncExitStack, ExitStack
from typing import TYPE_CHECKING, Any

from hypergraph.stateful import StatefulHandle, is_stateful_handle

if TYPE_CHECKING:
    from hypergraph.graph import Graph


class GraphResourceScope:
    """Materialize lazy stateful handles for a graph scope."""

    def __init__(self, graph: Graph) -> None:
        self._graph = graph
        self._instances: dict[StatefulHandle, Any] = {}
        self._stack: ExitStack | None = None
        self._astack: AsyncExitStack | None = None

    def __enter__(self) -> Graph:
        if self._stack is not None or self._astack is not None:
            raise RuntimeError("GraphResourceScope is already active")
        self._instances = {}
        self._astack = None
        handles = list(_iter_stateful_handles(self._graph))
        _validate_sync_scope(handles)
        self._stack = ExitStack()
        try:
            return self._materialize_graph(async_scope=False)
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        if self._stack is None:
            return None
        try:
            return self._stack.__exit__(exc_type, exc, tb)
        finally:
            self._instances = {}
            self._stack = None

    async def __aenter__(self) -> Graph:
        if self._stack is not None or self._astack is not None:
            raise RuntimeError("GraphResourceScope is already active")
        self._instances = {}
        self._stack = None
        self._astack = AsyncExitStack()
        try:
            return self._materialize_graph(async_scope=True)
        except BaseException:
            await self.__aexit__(*sys.exc_info())
            raise

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        if self._astack is None:
            return None
        try:
            return await self._astack.__aexit__(exc_type, exc, tb)
        finally:
            self._instances = {}
            self._astack = None

    def _materialize_graph(self, graph: Graph | None = None, *, async_scope: bool) -> Graph:
        from hypergraph.nodes.graph_node import GraphNode

        graph = self._graph if graph is None else graph
        new_graph = graph._shallow_copy()
        new_graph._bound = {name: self._materialize_value(value, async_scope=async_scope) for name, value in graph._bound.items()}

        nodes_changed = False
        new_nodes = dict(graph._nodes)
        for name, node in graph._nodes.items():
            if not isinstance(node, GraphNode):
                continue
            inner = self._materialize_graph(node.graph, async_scope=async_scope)
            copied = node._copy()
            copied._graph = inner
            new_nodes[name] = copied
            nodes_changed = True

        if nodes_changed:
            new_graph._nodes = new_nodes
        new_graph.__dict__.pop("inputs", None)
        return new_graph

    def _materialize_value(self, value: Any, *, async_scope: bool) -> Any:
        if not is_stateful_handle(value):
            return value

        if value in self._instances:
            return self._instances[value]

        instance = value.materialize()
        self._instances[value] = instance
        self._register_cleanup(value, instance, async_scope=async_scope)
        return instance

    def _register_cleanup(self, handle: StatefulHandle, instance: Any, *, async_scope: bool) -> None:
        policy = handle.policy
        if not policy.resource:
            return

        if async_scope:
            if policy.aclose is not None:
                assert self._astack is not None
                self._astack.push_async_callback(_call_async_cleanup, instance, policy.aclose)
            elif policy.close is not None:
                assert self._astack is not None
                self._astack.callback(_call_sync_cleanup, instance, policy.close)
            return

        if policy.close is None:
            raise TypeError(f"{handle.cls.__name__} requires async cleanup via aclose(); use async with graph.resources().")
        assert self._stack is not None
        self._stack.callback(_call_sync_cleanup, instance, policy.close)


def _iter_stateful_handles(graph: Graph) -> list[StatefulHandle]:
    from hypergraph.nodes.graph_node import GraphNode

    handles: list[StatefulHandle] = []
    for value in graph._bound.values():
        if is_stateful_handle(value):
            handles.append(value)

    for node in graph._nodes.values():
        if isinstance(node, GraphNode):
            handles.extend(_iter_stateful_handles(node.graph))
    return handles


def _validate_sync_scope(handles: list[StatefulHandle]) -> None:
    for handle in handles:
        if handle.policy.async_only:
            raise TypeError(f"{handle.cls.__name__} requires async cleanup via aclose(); use async with graph.resources().")


def _call_sync_cleanup(instance: Any, method_name: str) -> None:
    result = getattr(instance, method_name)()
    if inspect.isawaitable(result):
        raise TypeError(f"{type(instance).__name__}.{method_name} returned an awaitable during sync cleanup")


async def _call_async_cleanup(instance: Any, method_name: str) -> None:
    result = getattr(instance, method_name)()
    if inspect.isawaitable(result):
        await result
        return
    raise TypeError(f"{type(instance).__name__}.{method_name} returned a non-awaitable value during async cleanup")
