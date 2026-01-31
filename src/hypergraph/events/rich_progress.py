"""Rich-based hierarchical progress bar for graph execution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.events.processor import TypedEventProcessor

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
            "The 'rich' package is required for RichProgressProcessor. "
            "Install it with: pip install 'hypergraph[progress]' or pip install rich"
        ) from None


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

        # span_id -> depth (nesting level, 0 = root)
        self._depth: dict[str, int] = {}
        # span_id -> parent_span_id
        self._parents: dict[str, str | None] = {}
        # span_id -> is_map flag
        self._is_map: dict[str, bool] = {}
        # span_id -> map_size
        self._map_size: dict[str, int] = {}
        # span_id -> Rich TaskID (for map-level bars)
        self._map_tasks: dict[str, Any] = {}
        # (graph_name, node_name, depth) -> Rich TaskID (for node bars)
        self._node_tasks: dict[tuple[str, str, int], Any] = {}
        # span_id -> number of node-end events seen (for advancing map bar)
        self._run_node_count: dict[str, int] = {}
        # span_id -> total nodes expected per run item
        self._run_node_total: dict[str, int] = {}
        # Track which map span owns each run
        self._run_to_map: dict[str, str] = {}
        # Track total for node bars under a map context
        # (graph_name, node_name, depth) -> total
        self._node_totals: dict[tuple[str, str, int], int] = {}

        self._started = False

    def _ensure_started(self) -> None:
        """Start the Rich progress display if not already started."""
        if not self._started:
            self._progress.start()
            self._started = True

    def _get_depth(self, parent_span_id: str | None) -> int:
        """Calculate depth from parent span chain."""
        if parent_span_id is None:
            return 0
        parent_depth = self._depth.get(parent_span_id, 0)
        # If parent is a map span, depth stays the same as parent
        if self._is_map.get(parent_span_id, False):
            return parent_depth
        # If parent is a run (non-map), add 1 for nesting
        if parent_span_id in self._parents:
            return parent_depth + 1
        return 0

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
        current = self._parents.get(span_id)
        while current is not None:
            if self._is_map.get(current, False):
                return current
            current = self._parents.get(current)
        return None

    def _get_node_total(self, span_id: str) -> int:
        """Determine the total for a node bar based on map context."""
        map_span = self._find_map_ancestor(span_id)
        if map_span is not None:
            return self._map_size.get(map_span, 1)
        return 1

    def on_run_start(self, event: RunStartEvent) -> None:
        """Handle run start: track depth and create map bars."""
        self._ensure_started()
        span = event.span_id
        parent = event.parent_span_id

        self._parents[span] = parent
        depth = self._get_depth(parent)
        self._depth[span] = depth
        self._is_map[span] = event.is_map
        self._run_node_count[span] = 0

        if event.is_map and event.map_size is not None:
            self._map_size[span] = event.map_size
            desc = self._make_description(
                f"{event.graph_name or 'Map'} Progress", depth, is_map=True
            )
            task_id = self._progress.add_task(desc, total=event.map_size)
            self._map_tasks[span] = task_id

        # If this run is a child of a map, track the relationship
        if parent is not None and self._is_map.get(parent, False):
            self._run_to_map[span] = parent

    def on_node_start(self, event: NodeStartEvent) -> None:
        """Handle node start: create or reuse progress bars."""
        self._ensure_started()
        span = event.span_id
        parent = event.parent_span_id

        self._parents[span] = parent
        # Node depth = run depth (parent is the run span)
        run_depth = self._depth.get(parent, 0) if parent else 0
        node_depth = run_depth
        self._depth[span] = node_depth

        key = (event.graph_name, event.node_name, node_depth)
        total = self._get_node_total(span)

        if key not in self._node_tasks:
            desc = self._make_description(event.node_name, node_depth)
            self._node_tasks[key] = self._progress.add_task(desc, total=total)
            self._node_totals[key] = total
        elif self._node_totals.get(key, 0) < total:
            # Update total if map size increased (e.g., outer map * inner map)
            self._node_totals[key] = total
            self._progress.update(self._node_tasks[key], total=total)

    def on_node_end(self, event: NodeEndEvent) -> None:
        """Handle node end: advance progress bars."""
        span = event.span_id
        parent = event.parent_span_id
        node_depth = self._depth.get(span, 0)

        key = (event.graph_name, event.node_name, node_depth)
        if key in self._node_tasks:
            self._progress.advance(self._node_tasks[key], 1)

        # Track node completions for map-item runs
        if parent and parent in self._run_node_count:
            self._run_node_count[parent] += 1

    def on_node_error(self, event: NodeErrorEvent) -> None:
        """Handle node error: mark bar as failed."""
        span = event.span_id
        node_depth = self._depth.get(span, 0)

        key = (event.graph_name, event.node_name, node_depth)
        if key in self._node_tasks:
            task_id = self._node_tasks[key]
            current = self._progress.tasks[task_id].description
            self._progress.update(task_id, description=f"{current} [red]FAILED[/red]")

    def on_run_end(self, event: RunEndEvent) -> None:
        """Handle run end: advance map bars and show completion."""
        span = event.span_id

        # If this run is a child of a map, advance the map bar
        map_parent = self._run_to_map.get(span)
        if map_parent and map_parent in self._map_tasks:
            self._progress.advance(self._map_tasks[map_parent], 1)

        # If this is a root run (no parent), show completion
        if self._parents.get(span) is None:
            if event.status == "completed":
                self._progress.console.print(
                    f"[bold green]✓ {event.graph_name or 'Run'} completed![/bold green]"
                )
            else:
                self._progress.console.print(
                    f"[bold red]✗ {event.graph_name or 'Run'} failed: {event.error}[/bold red]"
                )

    def shutdown(self) -> None:
        """Stop the Rich progress display."""
        if self._started:
            self._progress.stop()
            self._started = False
