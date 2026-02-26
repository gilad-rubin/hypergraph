"""Rich-based hierarchical progress bar for graph execution."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

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


def _is_tty() -> bool:
    """Check if stdout is a TTY."""
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _timestamp() -> str:
    """Return current time as [HH:MM:SS]."""
    return datetime.now().strftime("[%H:%M:%S]")


# Milestones for map progress (percentages to log)
_MAP_MILESTONES = frozenset({10, 25, 50, 75, 100})


@dataclass
class _NonTTYMapState:
    """Track map progress for milestone logging."""

    total: int = 0
    completed: int = 0
    name: str = ""
    logged_milestones: set[int] = field(default_factory=set)


class RichProgressProcessor(TypedEventProcessor):
    """Displays hierarchical progress bars using Rich.

    Tracks graph execution events and renders live progress bars with
    proper nesting, icons, and aggregation for map operations.

    In non-TTY environments (CI, piped output), falls back to simple
    text milestone logging instead of Rich live progress bars.

    Visual conventions (TTY mode):
        - ``📦`` regular nodes (depth 0)
        - ``🌳`` nested graph nodes (depth > 0)
        - ``🗺️`` map-level progress bars
        - Indentation: ``"  " * depth``
    """

    def __init__(
        self,
        *,
        transient: bool = True,
        force_mode: Literal["tty", "non-tty", "auto"] = "auto",
    ) -> None:
        """Initialize the progress processor.

        Args:
            transient: If True, remove progress bars after completion.
            force_mode: Force TTY or non-TTY mode. "auto" detects via isatty().
        """
        if force_mode == "auto":
            self._tty_mode = _is_tty()
        else:
            self._tty_mode = force_mode == "tty"

        self._spans: dict[str, _SpanInfo] = {}
        self._node_bars: dict[_NodeKey, _NodeBarInfo] = {}
        self._started = False

        # Non-TTY state
        self._nontty_map_states: dict[str, _NonTTYMapState] = {}  # span_id -> state

        if self._tty_mode:
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
        else:
            self._progress = None  # type: ignore[assignment]

    def _print(self, msg: str) -> None:
        """Print a plain-text message (non-TTY mode)."""
        print(f"{_timestamp()} {msg}", flush=True)

    def _ensure_started(self) -> None:
        """Start the Rich progress display if not already started."""
        if not self._started:
            if self._tty_mode:
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
            if self._tty_mode:
                desc = self._make_description(f"{event.graph_name or 'Map'} Progress", info.depth, is_map=True)
                info.rich_task_id = self._progress.add_task(desc, total=event.map_size)
            else:
                name = event.graph_name or "Map"
                self._nontty_map_states[span] = _NonTTYMapState(total=event.map_size, name=name)

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

        if self._tty_mode:
            bar = self._node_bars.get(key)
            if bar is None:
                desc = self._make_description(event.node_name, node_depth)
                task_id = self._progress.add_task(desc, total=total)
                self._node_bars[key] = _NodeBarInfo(rich_task_id=task_id, total=total)
            elif bar.total < total:
                bar.total = total
                self._progress.update(bar.rich_task_id, total=total)
        else:
            # Non-TTY: log node start only for non-map runs (map items are tracked via milestones)
            if not self._find_map_ancestor(span):
                self._print(f"▶ {event.node_name} started")

    def on_node_end(self, event: NodeEndEvent) -> None:
        """Handle node end: advance progress bars."""
        span = event.span_id
        parent = event.parent_span_id
        span_info = self._spans.get(span)
        node_depth = span_info.depth if span_info else 0

        if self._tty_mode:
            key: _NodeKey = (event.graph_name, event.node_name, node_depth)
            bar = self._node_bars.get(key)
            if bar is not None:
                self._progress.advance(bar.rich_task_id, 1)
        else:
            # Non-TTY: log node completion for non-map runs
            if not self._find_map_ancestor(span):
                self._print(f"✓ {event.node_name} completed")

        # Track node completions for map-item runs
        if parent:
            parent_info = self._spans.get(parent)
            if parent_info is not None:
                parent_info.node_count += 1

    def on_node_error(self, event: NodeErrorEvent) -> None:
        """Handle node error: mark bar as failed."""
        if self._tty_mode:
            span_info = self._spans.get(event.span_id)
            node_depth = span_info.depth if span_info else 0

            key: _NodeKey = (event.graph_name, event.node_name, node_depth)
            bar = self._node_bars.get(key)
            if bar is not None:
                current = self._progress.tasks[bar.rich_task_id].description
                self._progress.update(bar.rich_task_id, description=f"{current} [red]FAILED[/red]")
        else:
            if not self._find_map_ancestor(event.span_id):
                self._print(f"✗ {event.node_name} FAILED")

    def _nontty_check_map_milestone(self, map_span_id: str) -> None:
        """Log map progress at milestone percentages."""
        state = self._nontty_map_states.get(map_span_id)
        if state is None or state.total == 0:
            return
        pct = int(state.completed * 100 / state.total)
        # Find the highest milestone we've crossed but haven't logged
        milestone_to_log = 0
        for m in _MAP_MILESTONES:
            if pct >= m and m not in state.logged_milestones:
                milestone_to_log = max(milestone_to_log, m)
        if milestone_to_log > 0:
            # Mark all milestones up to this one as logged
            for m in _MAP_MILESTONES:
                if m <= milestone_to_log:
                    state.logged_milestones.add(m)
            self._print(f"🗺️ {state.name}: {milestone_to_log}% ({state.completed}/{state.total})")

    def on_run_end(self, event: RunEndEvent) -> None:
        """Handle run end: advance map bars and show completion."""
        span_info = self._spans.get(event.span_id)
        if span_info is None:
            return

        # If this run is a child of a map, advance the map bar
        map_parent = span_info.map_parent
        if map_parent:
            map_info = self._spans.get(map_parent)
            if map_info is not None:
                if self._tty_mode and map_info.rich_task_id is not None:
                    self._progress.advance(map_info.rich_task_id, 1)

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
                elif not self._tty_mode:
                    # Non-TTY: update map state and check milestones
                    nontty_state = self._nontty_map_states.get(map_parent)
                    if nontty_state is not None:
                        nontty_state.completed += 1
                        self._nontty_check_map_milestone(map_parent)

        # If this is a root run (no parent), show completion
        if span_info.parent_span_id is None:
            if self._tty_mode:
                if event.status == RunStatus.COMPLETED:
                    self._progress.console.print(f"[bold green]✓ {event.graph_name or 'Run'} completed![/bold green]")
                else:
                    self._progress.console.print(f"[bold red]✗ {event.graph_name or 'Run'} failed: {event.error}[/bold red]")
            else:
                name = event.graph_name or "Run"
                if event.status == RunStatus.COMPLETED:
                    self._print(f"✓ {name} completed!")
                else:
                    error_msg = f": {event.error}" if event.error else ""
                    self._print(f"✗ {name} failed{error_msg}")

    def shutdown(self) -> None:
        """Stop the Rich progress display."""
        if self._started:
            if self._tty_mode:
                self._progress.stop()
            self._started = False
