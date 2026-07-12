"""Renderer-neutral state transitions for progress event streams."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

from hypergraph.events.types import RunStatus

if TYPE_CHECKING:
    from hypergraph.events.types import (
        InnerCacheEvent,
        NodeEndEvent,
        NodeErrorEvent,
        NodeStartEvent,
        RunEndEvent,
        RunStartEvent,
    )


_NodeKey: TypeAlias = tuple[str, str, int]
_TaskKey: TypeAlias = str | _NodeKey


@dataclass(frozen=True, slots=True)
class _TaskView:
    """Complete renderer-facing snapshot of one logical progress task."""

    key: _TaskKey
    description: str
    total: int
    completed: int = 0
    stats: str = ""


@dataclass(frozen=True, slots=True)
class _ProgressMessage:
    """Semantic transcript item interpreted by each output renderer."""

    kind: Literal["node-start", "node-end", "node-error", "map-progress", "run-end"]
    name: str
    scope_id: str | None = None
    cached: bool = False
    completed: int = 0
    total: int = 0
    status: RunStatus | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _ProgressUpdate:
    """One ordered state transition emitted through the renderer protocol."""

    tasks: tuple[_TaskView, ...] = ()
    removed: tuple[_TaskKey, ...] = ()
    reset: bool = False
    message: _ProgressMessage | None = None


@dataclass(slots=True)
class _SpanState:
    depth: int = 0
    parent_span_id: str | None = None
    is_map: bool = False
    map_size: int | None = None
    completed: int = 0
    map_parent: str | None = None
    failures: int = 0
    succeeded: int = 0
    node_bar_key: _NodeKey | None = None
    display_name: str = "Map"
    log_name: str = "Map"


@dataclass(slots=True)
class _NodeState:
    total: int
    name: str
    depth: int
    map_span_id: str | None
    completed: int = 0
    succeeded: int = 0
    cached: int = 0
    failures: int = 0
    total_duration_ms: float = 0.0
    inner_cache_hits: int = 0
    inner_cache_refreshing: int = 0


def _format_duration(ms: float) -> str:
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000:.1f}s"


def _format_stats(
    *,
    succeeded: int = 0,
    failures: int = 0,
    cached: int = 0,
    avg_ms: float | None = None,
    inner_cache_hits: int = 0,
    inner_cache_refreshing: int = 0,
) -> str:
    parts: list[str] = []
    if succeeded:
        parts.append(f"{succeeded}✓")
    if failures:
        parts.append(f"{failures}✗")
    if cached:
        parts.append(f"{cached}◉")
    if avg_ms is not None:
        parts.append(f"~{_format_duration(avg_ms)}")
    if inner_cache_hits:
        parts.append(f"{inner_cache_hits}↩")
    if inner_cache_refreshing:
        parts.append(f"{inner_cache_refreshing}↻")
    return " ".join(parts)


class _ProgressTracker:
    """Own span/node/map decisions without presentation handles or modes."""

    def __init__(self) -> None:
        self.spans: dict[str, _SpanState] = {}
        self.node_bars: dict[_NodeKey, _NodeState] = {}
        self.map_children: dict[str, list[_NodeKey]] = {}

    def _get_span(self, span_id: str) -> _SpanState:
        if span_id not in self.spans:
            self.spans[span_id] = _SpanState()
        return self.spans[span_id]

    def _get_depth(self, parent_span_id: str | None) -> int:
        if parent_span_id is None:
            return 0
        parent = self.spans.get(parent_span_id)
        if parent is None:
            return 0
        return parent.depth if parent.is_map else parent.depth + 1

    def _find_map_ancestor(self, span_id: str) -> str | None:
        info = self.spans.get(span_id)
        if info is None:
            return None
        current = info.parent_span_id
        while current is not None:
            parent = self.spans.get(current)
            if parent is None:
                break
            if parent.is_map:
                return current
            current = parent.parent_span_id
        return None

    def _get_node_total(self, span_id: str) -> int:
        map_span = self._find_map_ancestor(span_id)
        if map_span is None:
            return 1
        return self.spans[map_span].map_size or 1

    @staticmethod
    def _map_description(name: str, depth: int) -> str:
        return f"{'  ' * depth}◈ {name}"

    @staticmethod
    def _child_description(name: str, depth: int, *, is_last: bool) -> str:
        branch = "└─" if is_last else "├─"
        return f"{'  ' * depth}{branch} {name}"

    @staticmethod
    def _node_description(name: str, depth: int) -> str:
        return f"{'  ' * depth}{name}"

    def _node_view(self, key: _NodeKey) -> _TaskView:
        node = self.node_bars[key]
        if node.map_span_id is None:
            description = self._node_description(node.name, node.depth)
        else:
            children = self.map_children.get(node.map_span_id, [])
            description = self._child_description(
                node.name,
                node.depth,
                is_last=bool(children and children[-1] == key),
            )
        non_cached = node.succeeded + node.failures
        avg_ms = node.total_duration_ms / non_cached if non_cached > 1 else None
        return _TaskView(
            key=key,
            description=description,
            total=node.total,
            completed=node.completed,
            stats=_format_stats(
                succeeded=node.succeeded,
                failures=node.failures,
                cached=node.cached,
                avg_ms=avg_ms,
                inner_cache_hits=node.inner_cache_hits,
                inner_cache_refreshing=node.inner_cache_refreshing,
            ),
        )

    def _map_view(self, span_id: str) -> _TaskView:
        info = self.spans[span_id]
        if info.map_size is None:
            raise RuntimeError("Cannot render progress for a map without a known size.")
        return _TaskView(
            key=span_id,
            description=self._map_description(info.display_name, info.depth),
            total=info.map_size,
            completed=info.completed,
            stats=_format_stats(
                succeeded=info.succeeded,
                failures=info.failures,
            ),
        )

    def on_run_start(self, event: RunStartEvent) -> _ProgressUpdate:
        reset = False
        if event.parent_span_id is None and self.spans and event.span_id not in self.spans:
            self.spans.clear()
            self.node_bars.clear()
            self.map_children.clear()
            reset = True

        info = self._get_span(event.span_id)
        info.parent_span_id = event.parent_span_id
        info.depth = self._get_depth(event.parent_span_id)
        info.is_map = event.is_map

        tasks: list[_TaskView] = []
        removed: list[_TaskKey] = []
        if event.is_map and event.map_size is not None:
            info.map_size = event.map_size
            replaced_name: str | None = None
            if event.parent_span_id is not None:
                parent_info = self.spans.get(event.parent_span_id)
                if parent_info is not None and parent_info.node_bar_key is not None:
                    old_key = parent_info.node_bar_key
                    old_bar = self.node_bars.pop(old_key, None)
                    if old_bar is not None:
                        replaced_name = old_bar.name
                        info.depth = old_bar.depth
                        removed.append(old_key)
                        for children in self.map_children.values():
                            if old_key not in children:
                                continue
                            children.remove(old_key)
                            if children:
                                tasks.append(self._node_view(children[-1]))
                    parent_info.node_bar_key = None

            info.display_name = replaced_name or event.graph_name or "Map"
            # Non-TTY historically did not create visual node bars, so map
            # milestones used the graph name even when a visual bar was replaced.
            info.log_name = event.graph_name or "Map"
            tasks.append(self._map_view(event.span_id))

        if event.parent_span_id is not None:
            parent_info = self.spans.get(event.parent_span_id)
            if parent_info is not None and parent_info.is_map:
                info.map_parent = event.parent_span_id

        return _ProgressUpdate(
            tasks=tuple(tasks),
            removed=tuple(removed),
            reset=reset,
        )

    def on_node_start(self, event: NodeStartEvent) -> _ProgressUpdate:
        info = self._get_span(event.span_id)
        info.parent_span_id = event.parent_span_id
        parent_info = self.spans.get(event.parent_span_id) if event.parent_span_id else None
        node_depth = parent_info.depth if parent_info else 0
        if parent_info and parent_info.map_parent:
            node_depth += 1
        info.depth = node_depth

        key: _NodeKey = (event.graph_name, event.node_name, node_depth)
        info.node_bar_key = key
        total = self._get_node_total(event.span_id)
        map_span = self._find_map_ancestor(event.span_id)
        tasks: list[_TaskView] = []

        node = self.node_bars.get(key)
        if node is None:
            node = _NodeState(
                total=total,
                name=event.node_name,
                depth=node_depth,
                map_span_id=map_span,
            )
            self.node_bars[key] = node
            if map_span is not None:
                children = self.map_children.setdefault(map_span, [])
                previous = children[-1] if children else None
                children.append(key)
                if previous is not None:
                    tasks.append(self._node_view(previous))
            tasks.append(self._node_view(key))
        elif node.total < total:
            node.total = total
            tasks.append(self._node_view(key))

        message = None
        if map_span is None:
            message = _ProgressMessage(kind="node-start", name=event.node_name)
        return _ProgressUpdate(tasks=tuple(tasks), message=message)

    def on_node_end(self, event: NodeEndEvent) -> _ProgressUpdate:
        span_info = self.spans.get(event.span_id)
        node_depth = span_info.depth if span_info else 0
        key: _NodeKey = (event.graph_name, event.node_name, node_depth)
        node = self.node_bars.get(key)
        tasks: tuple[_TaskView, ...] = ()
        if node is not None:
            node.completed += 1
            if event.cached:
                node.cached += 1
            else:
                node.succeeded += 1
                node.total_duration_ms += event.duration_ms
            tasks = (self._node_view(key),)

        message = None
        if self._find_map_ancestor(event.span_id) is None:
            message = _ProgressMessage(
                kind="node-end",
                name=event.node_name,
                cached=event.cached,
            )
        return _ProgressUpdate(tasks=tasks, message=message)

    def on_node_error(self, event: NodeErrorEvent) -> _ProgressUpdate:
        span_info = self.spans.get(event.span_id)
        node_depth = span_info.depth if span_info else 0
        key: _NodeKey = (event.graph_name, event.node_name, node_depth)
        node = self.node_bars.get(key)
        tasks: tuple[_TaskView, ...] = ()
        if node is not None:
            node.completed += 1
            node.failures += 1
            tasks = (self._node_view(key),)

        message = None
        if self._find_map_ancestor(event.span_id) is None:
            message = _ProgressMessage(kind="node-error", name=event.node_name)
        return _ProgressUpdate(tasks=tasks, message=message)

    def on_inner_cache(self, event: InnerCacheEvent) -> _ProgressUpdate:
        node: _NodeState | None = None
        key: _NodeKey | None = None
        if event.parent_span_id:
            span_info = self.spans.get(event.parent_span_id)
            if span_info is not None and span_info.node_bar_key is not None:
                key = span_info.node_bar_key
                node = self.node_bars.get(key)

        if node is None:
            for candidate_key, candidate in self.node_bars.items():
                graph_name, node_name, _ = candidate_key
                if node_name == event.node_name and (not event.graph_name or graph_name == event.graph_name):
                    key = candidate_key
                    node = candidate
                    break

        if node is None or key is None:
            return _ProgressUpdate()
        if event.hit:
            node.inner_cache_hits += 1
        if event.refreshing:
            node.inner_cache_refreshing += 1
        return _ProgressUpdate(tasks=(self._node_view(key),))

    def on_run_end(self, event: RunEndEvent) -> _ProgressUpdate:
        span_info = self.spans.get(event.span_id)
        if span_info is None:
            return _ProgressUpdate()

        tasks: tuple[_TaskView, ...] = ()
        message: _ProgressMessage | None = None
        if span_info.map_parent:
            map_info = self.spans.get(span_info.map_parent)
            if map_info is not None and map_info.map_size is not None:
                map_info.completed += 1
                if event.status in (RunStatus.FAILED, RunStatus.PARTIAL):
                    map_info.failures += 1
                elif event.status not in (RunStatus.PAUSED, RunStatus.STOPPED):
                    map_info.succeeded += 1
                tasks = (self._map_view(span_info.map_parent),)
                message = _ProgressMessage(
                    kind="map-progress",
                    name=map_info.log_name,
                    scope_id=span_info.map_parent,
                    completed=map_info.completed,
                    total=map_info.map_size,
                )

        if span_info.parent_span_id is None:
            message = _ProgressMessage(
                kind="run-end",
                name=event.graph_name or "Run",
                status=event.status,
                error=event.error,
            )
        return _ProgressUpdate(tasks=tasks, message=message)
