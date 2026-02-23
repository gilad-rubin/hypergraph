"""Rich-based hierarchical progress bar for graph execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hypergraph.events.processor import TypedEventProcessor
from hypergraph.events.types import RunStatus

if TYPE_CHECKING:
    from hypergraph.events.types import (
        NodeEndEvent,
        NodeErrorEvent,
        NodeStartEvent,
        RunEndEvent,
        RunStartEvent,
    )


def _require_rich() -> None:
    """Raise a clear error if rich is not installed."""
    try:
        import rich  # noqa: F401
    except ImportError:
        raise ImportError(
            "The 'rich' package is required for RichProgressProcessor. Install it with: pip install 'hypergraph[progress]' or pip install rich"
        ) from None


@dataclass
class _SpanInfo:
    """Tracking state for a single span (run or node)."""

    depth: int = 0
    parent_span_id: str | None = None
    is_map: bool = False
    map_size: int = 0
    rich_task_id: Any = None  # Rich TaskID for map-level bars
    node_count: int = 0  # Number of node-end events seen
    map_parent: str | None = None  # Map span that owns this run
    failures: int = 0  # Failed item count (for map spans)


@dataclass
class _NodeBarInfo:
    """Tracking state for a node progress bar."""

    rich_task_id: Any = None  # Rich TaskID
    total: int = 0


# Key type for node bar lookups: (graph_name, node_name, depth)
_NodeKey = tuple[str, str, int]


class RichProgressProcessor(TypedEventProcessor):
    """Displays hierarchical progress bars using Rich.

    Tracks graph execution events and renders live progress bars with
    proper nesting, icons, and aggregation for map operations.

    Visual conventions:
        - ``📦`` regular nodes (depth 0)
        - ``🌳`` nested graph nodes (depth > 0)
        - ``🗺️`` map-level progress bars
        - Indentation: ``"  " * depth``
    """

    def __init__(self, *, transient: bool = True) -> None:
        """Initialize the progress processor.

        Args:
            transient: If True, remove progress bars after completion.
        """
        _require_rich()
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            transient=transient,
        )

        self._spans: dict[str, _SpanInfo] = {}
        self._node_bars: dict[_NodeKey, _NodeBarInfo] = {}
        self._started = False

    def _ensure_started(self) -> None:
        """Start the Rich progress display if not already started."""
        if not self._started:
            self._progress.start()
            self._started = True

    def _get_span(self, span_id: str) -> _SpanInfo:
        """Get or create span info for a given span ID."""
        if span_id not in self._spans:
            self._spans[span_id] = _SpanInfo()
        return self._spans[span_id]

    def _get_depth(self, parent_span_id: str | None) -> int:
        """Calculate depth from parent span chain."""
        if parent_span_id is None:
            return 0
        parent = self._spans.get(parent_span_id)
        if parent is None:
            return 0
        # If parent is a map span, depth stays the same as parent
        if parent.is_map:
            return parent.depth
        # If parent is a run (non-map), add 1 for nesting
        return parent.depth + 1

    def _icon(self, depth: int, is_map: bool = False) -> str:
        """Return the appropriate icon for the given depth."""
        if is_map:
            return "🗺️ "
        if depth > 0:
            return "🌳"
        return "📦"

    def _make_description(self, name: str, depth: int, is_map: bool = False) -> str:
        """Build an indented, icon-prefixed description."""
        indent = "  " * depth
        icon = self._icon(depth, is_map=is_map)
        return f"{indent}{icon} {name}"

    def _find_map_ancestor(self, span_id: str) -> str | None:
        """Walk up the parent chain to find the nearest map span."""
        info = self._spans.get(span_id)
        if info is None:
            return None
        current = info.parent_span_id
        while current is not None:
            parent_info = self._spans.get(current)
            if parent_info is None:
                break
            if parent_info.is_map:
                return current
            current = parent_info.parent_span_id
        return None

    def _get_node_total(self, span_id: str) -> int:
        """Determine the total for a node bar based on map context."""
        map_span = self._find_map_ancestor(span_id)
        if map_span is not None:
            return self._spans[map_span].map_size or 1
        return 1

    def on_run_start(self, event: RunStartEvent) -> None:
        """Handle run start: track depth and create map bars."""
        self._ensure_started()
        span = event.span_id
        parent = event.parent_span_id

        info = self._get_span(span)
        info.parent_span_id = parent
        info.depth = self._get_depth(parent)
        info.is_map = event.is_map

        if event.is_map and event.map_size is not None:
            info.map_size = event.map_size
            desc = self._make_description(f"{event.graph_name or 'Map'} Progress", info.depth, is_map=True)
            info.rich_task_id = self._progress.add_task(desc, total=event.map_size)

        # If this run is a child of a map, track the relationship
        if parent is not None:
            parent_info = self._spans.get(parent)
            if parent_info is not None and parent_info.is_map:
                info.map_parent = parent

    def on_node_start(self, event: NodeStartEvent) -> None:
        """Handle node start: create or reuse progress bars."""
        self._ensure_started()
        span = event.span_id
        parent = event.parent_span_id

        info = self._get_span(span)
        info.parent_span_id = parent
        # Node depth = run depth (parent is the run span)
        parent_info = self._spans.get(parent) if parent else None
        node_depth = parent_info.depth if parent_info else 0
        info.depth = node_depth

        key: _NodeKey = (event.graph_name, event.node_name, node_depth)
        total = self._get_node_total(span)

        bar = self._node_bars.get(key)
        if bar is None:
            desc = self._make_description(event.node_name, node_depth)
            task_id = self._progress.add_task(desc, total=total)
            self._node_bars[key] = _NodeBarInfo(rich_task_id=task_id, total=total)
        elif bar.total < total:
            # Update total if map size increased (e.g., outer map * inner map)
            bar.total = total
            self._progress.update(bar.rich_task_id, total=total)

    def on_node_end(self, event: NodeEndEvent) -> None:
        """Handle node end: advance progress bars."""
        span = event.span_id
        parent = event.parent_span_id
        span_info = self._spans.get(span)
        node_depth = span_info.depth if span_info else 0

        key: _NodeKey = (event.graph_name, event.node_name, node_depth)
        bar = self._node_bars.get(key)
        if bar is not None:
            self._progress.advance(bar.rich_task_id, 1)

        # Track node completions for map-item runs
        if parent:
            parent_info = self._spans.get(parent)
            if parent_info is not None:
                parent_info.node_count += 1

    def on_node_error(self, event: NodeErrorEvent) -> None:
        """Handle node error: mark bar as failed."""
        span_info = self._spans.get(event.span_id)
        node_depth = span_info.depth if span_info else 0

        key: _NodeKey = (event.graph_name, event.node_name, node_depth)
        bar = self._node_bars.get(key)
        if bar is not None:
            current = self._progress.tasks[bar.rich_task_id].description
            self._progress.update(bar.rich_task_id, description=f"{current} [red]FAILED[/red]")

    def on_run_end(self, event: RunEndEvent) -> None:
        """Handle run end: advance map bars and show completion."""
        span_info = self._spans.get(event.span_id)
        if span_info is None:
            return

        # If this run is a child of a map, advance the map bar
        map_parent = span_info.map_parent
        if map_parent:
            map_info = self._spans.get(map_parent)
            if map_info is not None and map_info.rich_task_id is not None:
                self._progress.advance(map_info.rich_task_id, 1)

                # Track failures and update map bar description
                if event.status == RunStatus.FAILED:
                    map_info.failures += 1
                    base_desc = self._make_description(
                        f"{event.graph_name or 'Map'} Progress",
                        map_info.depth,
                        is_map=True,
                    )
                    self._progress.update(
                        map_info.rich_task_id,
                        description=f"{base_desc} [red]({map_info.failures} failed)[/red]",
                    )

        # If this is a root run (no parent), show completion
        if span_info.parent_span_id is None:
            if event.status == RunStatus.COMPLETED:
                self._progress.console.print(f"[bold green]✓ {event.graph_name or 'Run'} completed![/bold green]")
            else:
                self._progress.console.print(f"[bold red]✗ {event.graph_name or 'Run'} failed: {event.error}[/bold red]")

    def shutdown(self) -> None:
        """Stop the Rich progress display."""
        if self._started:
            self._progress.stop()
            self._started = False
