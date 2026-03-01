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
    succeeded: int = 0  # Succeeded item count (for map spans)


@dataclass
class _NodeBarInfo:
    """Tracking state for a node progress bar."""

    rich_task_id: Any = None  # Rich TaskID
    total: int = 0
    succeeded: int = 0
    cached: int = 0
    failures: int = 0
    total_duration_ms: float = 0.0  # Sum of non-cached durations (for avg)
    name: str = ""
    depth: int = 0
    map_span_id: str | None = None  # Parent map span (for tree chars)


# Key type for node bar lookups: (graph_name, node_name, depth)
_NodeKey = tuple[str, str, int]


def _is_notebook() -> bool:
    """Detect if running inside a Jupyter/IPython notebook kernel."""
    try:
        from IPython import get_ipython

        shell = get_ipython()
        return shell is not None and "zmq" in type(shell).__module__
    except (ImportError, NameError):
        return False


def _detect_mode() -> Literal["tty", "notebook", "non-tty"]:
    """Detect output mode: TTY terminal, Jupyter notebook, or plain text."""
    if _is_notebook():
        return "notebook"
    if hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
        return "tty"
    return "non-tty"


def _timestamp() -> str:
    """Return current time as [HH:MM:SS]."""
    return datetime.now().strftime("[%H:%M:%S]")


def _format_duration(ms: float) -> str:
    """Format milliseconds as a human-readable duration."""
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000:.1f}s"


def _format_stats(
    *,
    succeeded: int = 0,
    failures: int = 0,
    cached: int = 0,
    avg_ms: float | None = None,
) -> str:
    """Build a compact stats string like '95✓ 5✗ 10◉ ~45ms'. Only non-zero counts shown."""
    parts: list[str] = []
    if succeeded:
        parts.append(f"{succeeded}✓")
    if failures:
        parts.append(f"[red]{failures}✗[/red]")
    if cached:
        parts.append(f"{cached}◉")
    if avg_ms is not None:
        parts.append(f"~{_format_duration(avg_ms)}")
    return " ".join(parts)


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
    proper nesting, tree structure, and per-node outcome stats.

    In non-TTY environments (CI, piped output), falls back to simple
    text milestone logging instead of Rich live progress bars.

    Visual conventions (TTY mode)::

        ◈ llm-pipeline  ━━━━━━━━━━  100/100  0:00:07  95✓ 5✗
        ├─ classify     ━━━━━━━━━━  100/100  0:00:06  100✓
        └─ generate     ━━━━━━━━━━  100/100  0:00:06  95✓ 5✗
    """

    def __init__(
        self,
        *,
        transient: bool = True,
        force_mode: Literal["tty", "notebook", "non-tty", "auto"] = "auto",
    ) -> None:
        """Initialize the progress processor.

        Args:
            transient: If True, remove progress bars after completion.
            force_mode: Force output mode. "auto" detects environment.
        """
        mode = _detect_mode() if force_mode == "auto" else force_mode
        self._tty_mode = mode in ("tty", "notebook")
        self._notebook = mode == "notebook"

        self._spans: dict[str, _SpanInfo] = {}
        self._node_bars: dict[_NodeKey, _NodeBarInfo] = {}
        self._started = False

        # Tree structure: ordered child node keys per map span
        self._map_children: dict[str, list[_NodeKey]] = {}

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
                TextColumn("{task.fields[stats]}"),
                transient=transient,
            )
        else:
            self._progress = None  # type: ignore[assignment]

    def _print(self, msg: str) -> None:
        """Print a plain-text message (non-TTY mode)."""
        print(f"{_timestamp()} {msg}", flush=True)

    def _refresh(self) -> None:
        """Explicit refresh for notebook mode (auto-refresh is disabled in Jupyter)."""
        if self._notebook and self._progress is not None:
            self._progress.refresh()

    def _ensure_started(self) -> None:
        """Start the Rich progress display if not already started."""
        if not self._started:
            if self._tty_mode:
                self._progress.start()
            self._started = True

    # -- Description builders --------------------------------------------------

    def _make_map_description(self, name: str, depth: int) -> str:
        """Build description for a map bar: '◈ name'."""
        indent = "  " * depth
        return f"{indent}◈ {name}"

    def _make_child_description(self, name: str, depth: int, *, is_last: bool) -> str:
        """Build description for a node under a map: '├─ name' or '└─ name'."""
        indent = "  " * depth
        branch = "└─" if is_last else "├─"
        return f"{indent}{branch} {name}"

    def _make_node_description(self, name: str, depth: int) -> str:
        """Build description for a regular node (no map parent)."""
        indent = "  " * depth
        return f"{indent}{name}"

    # -- Span helpers ----------------------------------------------------------

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

    def _update_node_stats(self, bar: _NodeBarInfo) -> None:
        """Recompute and push the stats field for a node bar."""
        # Show average duration only for multi-item bars (maps), excluding cached
        non_cached = bar.succeeded + bar.failures
        avg_ms = bar.total_duration_ms / non_cached if non_cached > 1 else None
        stats = _format_stats(succeeded=bar.succeeded, failures=bar.failures, cached=bar.cached, avg_ms=avg_ms)
        self._progress.update(bar.rich_task_id, stats=stats)

    def _update_map_stats(self, map_info: _SpanInfo) -> None:
        """Recompute and push the stats field for a map bar."""
        stats = _format_stats(succeeded=map_info.succeeded, failures=map_info.failures)
        self._progress.update(map_info.rich_task_id, stats=stats)

    # -- Tree management -------------------------------------------------------

    def _register_map_child(self, map_span_id: str, key: _NodeKey, bar: _NodeBarInfo) -> str:
        """Register a node bar as a child of a map span. Returns the description."""
        children = self._map_children.setdefault(map_span_id, [])

        # Update previous last child from └─ to ├─
        if children:
            prev_key = children[-1]
            prev_bar = self._node_bars[prev_key]
            prev_desc = self._make_child_description(prev_bar.name, prev_bar.depth, is_last=False)
            self._progress.update(prev_bar.rich_task_id, description=prev_desc)

        children.append(key)
        return self._make_child_description(bar.name, bar.depth, is_last=True)

    # -- Event handlers --------------------------------------------------------

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
                desc = self._make_map_description(event.graph_name or "Map", info.depth)
                info.rich_task_id = self._progress.add_task(desc, total=event.map_size, stats="")
                self._refresh()
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
        # Node depth = run depth, +1 if inside a map (indent under map bar)
        parent_info = self._spans.get(parent) if parent else None
        node_depth = parent_info.depth if parent_info else 0
        if parent_info and parent_info.map_parent:
            node_depth += 1
        info.depth = node_depth

        key: _NodeKey = (event.graph_name, event.node_name, node_depth)
        total = self._get_node_total(span)
        map_span = self._find_map_ancestor(span)

        if self._tty_mode:
            bar = self._node_bars.get(key)
            if bar is None:
                bar = _NodeBarInfo(
                    total=total,
                    name=event.node_name,
                    depth=node_depth,
                    map_span_id=map_span,
                )

                # Tree chars for map children, plain otherwise
                desc = self._register_map_child(map_span, key, bar) if map_span else self._make_node_description(event.node_name, node_depth)

                bar.rich_task_id = self._progress.add_task(desc, total=total, stats="")
                self._node_bars[key] = bar
                self._refresh()
            elif bar.total < total:
                bar.total = total
                self._progress.update(bar.rich_task_id, total=total)
                self._refresh()
        else:
            # Non-TTY: log node start only for non-map runs
            if not map_span:
                self._print(f"▶ {event.node_name} started")

    def on_node_end(self, event: NodeEndEvent) -> None:
        """Handle node end: advance progress bars and update stats."""
        span = event.span_id
        parent = event.parent_span_id
        span_info = self._spans.get(span)
        node_depth = span_info.depth if span_info else 0

        if self._tty_mode:
            key: _NodeKey = (event.graph_name, event.node_name, node_depth)
            bar = self._node_bars.get(key)
            if bar is not None:
                if event.cached:
                    bar.cached += 1
                else:
                    bar.succeeded += 1
                    bar.total_duration_ms += event.duration_ms
                self._progress.advance(bar.rich_task_id, 1)
                self._update_node_stats(bar)
                self._refresh()
        else:
            if not self._find_map_ancestor(span):
                suffix = " (cached)" if event.cached else ""
                self._print(f"✓ {event.node_name} completed{suffix}")

        # Track node completions for map-item runs
        if parent:
            parent_info = self._spans.get(parent)
            if parent_info is not None:
                parent_info.node_count += 1

    def on_node_error(self, event: NodeErrorEvent) -> None:
        """Handle node error: advance bar and update failure count."""
        if self._tty_mode:
            span_info = self._spans.get(event.span_id)
            node_depth = span_info.depth if span_info else 0

            key: _NodeKey = (event.graph_name, event.node_name, node_depth)
            bar = self._node_bars.get(key)
            if bar is not None:
                bar.failures += 1
                self._progress.advance(bar.rich_task_id, 1)
                self._update_node_stats(bar)
                self._refresh()
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
            self._print(f"◈ {state.name}: {milestone_to_log}% ({state.completed}/{state.total})")

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
                    else:
                        map_info.succeeded += 1
                    self._update_map_stats(map_info)
                    self._refresh()
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
