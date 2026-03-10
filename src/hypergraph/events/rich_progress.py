"""Rich-based hierarchical progress bar for graph execution."""

from __future__ import annotations

import html as _html
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from hypergraph._repr import ALIGNUI_WIDGET_THEME, FONT_MONO_STYLE, FONT_SANS_STYLE, MUTED_COLOR, STATUS_COLORS, theme_wrap
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


def _patch_rich_jupyter() -> None:
    """Add adaptive text color to Rich's Jupyter ``<pre>`` for dark notebooks."""
    import rich.jupyter as rj

    desired = (
        '<pre style="white-space:pre;overflow-x:auto;line-height:normal;'
        f"{FONT_MONO_STYLE};"
        f'background:transparent;color:{ALIGNUI_WIDGET_THEME["text_strong"]}">{{code}}</pre>\n'
    )
    if getattr(rj, "_hypergraph_patched", False) and getattr(rj, "JUPYTER_HTML_FORMAT", None) == desired:
        return
    rj.JUPYTER_HTML_FORMAT = desired
    rj._hypergraph_patched = True  # type: ignore[attr-defined]


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


# Notebook task model for HTML progress rendering.
@dataclass
class _NotebookTask:
    description: str
    total: int
    completed: int = 0
    stats: str = ""
    started_at: float = field(default_factory=time.monotonic)


class _NotebookConsole:
    """Minimal console shim for compatibility with Rich Progress API."""

    def print(self, *_args: Any, **_kwargs: Any) -> None:
        return


class _NotebookProgress:
    """Notebook-native HTML progress renderer with stable display updates."""

    def __init__(self, *, transient: bool = False, state_key: str = "notebook-progress") -> None:
        self.transient = transient
        self.state_key = state_key
        self._tasks: dict[int, _NotebookTask] = {}
        self._task_order: list[int] = []
        self._next_task_id = 0
        self._started = False
        self._display_handle: Any = None
        self.console = _NotebookConsole()

    def start(self) -> None:
        self._started = True
        self.refresh()

    def stop(self) -> None:
        if self.transient and self._display_handle is not None:
            from IPython.display import HTML

            self._display_handle.update(HTML(""))
        self._started = False

    def reset(self) -> None:
        self._tasks.clear()
        self._task_order.clear()
        self._next_task_id = 0
        if self._display_handle is not None:
            self.refresh()

    def add_task(self, description: str, *, total: int, stats: str = "") -> int:
        task_id = self._next_task_id
        self._next_task_id += 1
        self._tasks[task_id] = _NotebookTask(description=description, total=max(0, int(total or 0)), stats=stats)
        self._task_order.append(task_id)
        return task_id

    def update(
        self,
        task_id: int,
        *,
        description: str | None = None,
        total: int | None = None,
        stats: str | None = None,
    ) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        if description is not None:
            task.description = description
        if total is not None:
            task.total = max(0, int(total))
            if task.total and task.completed > task.total:
                task.completed = task.total
        if stats is not None:
            task.stats = stats

    def advance(self, task_id: int, advance: int = 1) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.completed += int(advance)
        if task.total > 0:
            task.completed = min(task.completed, task.total)

    def _format_elapsed(self, seconds: float) -> str:
        total = max(0, int(seconds))
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        return f"{h}:{m:02d}:{s:02d}"

    def _stats_html(self, stats: str) -> str:
        txt = _html.escape(stats)
        txt = re.sub(r"(\d+)✓", f'<span style="color:{STATUS_COLORS["completed"]}">\\1✓</span>', txt)
        txt = re.sub(r"(\d+)✗", f'<span style="color:{STATUS_COLORS["failed"]}">\\1✗</span>', txt)
        txt = re.sub(r"(\d+)◉", f'<span style="color:{STATUS_COLORS["cached"]}">\\1◉</span>', txt)
        txt = re.sub(r"(~[0-9:.a-zA-Z]+)", f'<span style="color:{STATUS_COLORS["partial"]}">\\1</span>', txt)
        return txt

    def _render_html(self) -> str:
        rows: list[str] = []
        now = time.monotonic()
        for task_id in self._task_order:
            task = self._tasks[task_id]
            total = task.total if task.total > 0 else 1
            pct = max(0.0, min(1.0, task.completed / total))
            pct_css = f"{pct * 100:.2f}%"
            desc = _html.escape(task.description)
            completed = task.completed if task.total > 0 else 0
            elapsed = self._format_elapsed(now - task.started_at)
            stats_html = self._stats_html(task.stats)
            rows.append(
                '<div style="display:flex; align-items:center; margin:2px 0">'
                f'<span style="{FONT_SANS_STYLE}; white-space:pre; width:{_NB_DESC_WIDTH_PX}px; overflow:hidden; text-overflow:ellipsis; margin-right:4px; color:{ALIGNUI_WIDGET_THEME["text_strong"]}">{desc}</span>'
                f'<div style="width:{_NB_BAR_WIDTH_PX}px; height:{_NB_BAR_HEIGHT_PX}px; border-radius:9999px; overflow:hidden; margin-right:10px; background:{ALIGNUI_WIDGET_THEME["border_soft"]}">'
                f'<div style="height:100%; width:{pct_css}; background:{ALIGNUI_WIDGET_THEME["success_base"]}"></div>'
                "</div>"
                f'<span style="{FONT_SANS_STYLE}; width:{_NB_COUNT_WIDTH_PX}px; text-align:right; margin-right:10px; color:{ALIGNUI_WIDGET_THEME["text_strong"]}; font-variant-numeric:tabular-nums">{completed}/{task.total}</span>'
                f'<span style="{FONT_SANS_STYLE}; width:{_NB_ELAPSED_WIDTH_PX}px; margin-right:10px; color:{MUTED_COLOR}; font-variant-numeric:tabular-nums">{elapsed}</span>'
                f'<span style="{FONT_SANS_STYLE}; min-width:{_NB_STATS_MIN_WIDTH_PX}px; color:{MUTED_COLOR}">{stats_html}</span>'
                "</div>"
            )
        content = (
            '<div style="display:inline-block; max-width:100%; '
            f'{FONT_SANS_STYLE}; font-size:{_NB_FONT_SIZE_PX}px; line-height:{_NB_LINE_HEIGHT}; color:{ALIGNUI_WIDGET_THEME["text_strong"]}">'
            + "".join(rows)
            + "</div>"
        )
        return theme_wrap(content, state_key=self.state_key)

    def refresh(self) -> None:
        from IPython.display import HTML, display

        html = self._render_html()
        if self._display_handle is None:
            self._display_handle = display(HTML(html), display_id=True)
        else:
            self._display_handle.update(HTML(html))


# Key type for node bar lookups: (graph_name, node_name, depth)
_NodeKey = tuple[str, str, int]


def _is_notebook() -> bool:
    """Detect if running inside a Jupyter/IPython notebook kernel."""
    try:
        from IPython import get_ipython

        shell = get_ipython()
        if shell is None:
            return False
        module = type(shell).__module__.lower()
        name = type(shell).__name__.lower()
        if "zmq" in module or "zmq" in name:
            return True
        if name == "zmqinteractiveshell":
            return True
        if getattr(shell, "kernel", None) is not None:
            return True
        cfg = getattr(shell, "config", {})
        try:
            if "IPKernelApp" in cfg:
                return True
        except Exception:
            pass
        return False
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
        parts.append(f"{failures}✗")
    if cached:
        parts.append(f"{cached}◉")
    if avg_ms is not None:
        parts.append(f"~{_format_duration(avg_ms)}")
    return " ".join(parts)


# Milestones for map progress (percentages to log)
_MAP_MILESTONES = frozenset({10, 25, 50, 75, 100})

# Notebook HTML progress sizing (slightly compact).
_NB_FONT_SIZE_PX = 12
_NB_LINE_HEIGHT = 1.3
_NB_DESC_WIDTH_PX = 168
_NB_BAR_WIDTH_PX = 300
_NB_BAR_HEIGHT_PX = 5
_NB_COUNT_WIDTH_PX = 64
_NB_ELAPSED_WIDTH_PX = 66
_NB_STATS_MIN_WIDTH_PX = 140


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
        transient: bool = False,
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
        self._last_refresh: float = 0.0  # monotonic timestamp of last notebook refresh
        self._refresh_dirty = False  # pending refresh not yet flushed
        self._manual_notebook_refresh = False
        self._uses_rich_progress = False

        # Tree structure: ordered child node keys per map span
        self._map_children: dict[str, list[_NodeKey]] = {}

        # Non-TTY state
        self._nontty_map_states: dict[str, _NonTTYMapState] = {}  # span_id -> state

        if mode == "tty":
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
                auto_refresh=True,
            )
            self._uses_rich_progress = True
        elif mode == "notebook":
            self._progress = _NotebookProgress(
                transient=transient,
                state_key=f"notebook-progress-{int(time.time() * 1000)}",
            )
            # Notebook rendering is refreshed manually in a throttled way.
            self._manual_notebook_refresh = True
        else:
            self._progress = None  # type: ignore[assignment]

    def _print(self, msg: str) -> None:
        """Print a plain-text message (non-TTY mode)."""
        print(f"{_timestamp()} {msg}", flush=True)

    # Minimum interval between notebook refreshes (seconds).
    # Jupyter widget updates cost ~3-5ms each; at 100ms intervals we get
    # smooth visuals without burning hundreds of ms on 300+ redraws.
    _NOTEBOOK_REFRESH_INTERVAL = 0.1

    def _refresh(self) -> None:
        """Throttled refresh for notebook mode.

        In TTY mode Rich auto-refreshes at ~10 Hz, so explicit refreshes
        are unnecessary.  In notebook mode we refresh at most every 100 ms
        to avoid flooding the IPython display protocol.
        """
        if not self._notebook or self._progress is None or not self._manual_notebook_refresh:
            return
        now = time.monotonic()
        if now - self._last_refresh >= self._NOTEBOOK_REFRESH_INTERVAL:
            self._progress.refresh()
            self._last_refresh = now
            self._refresh_dirty = False
        else:
            self._refresh_dirty = True

    def _flush_refresh(self) -> None:
        """Force a final refresh if any updates were throttled."""
        if self._refresh_dirty and self._progress is not None and self._manual_notebook_refresh:
            self._progress.refresh()
            self._last_refresh = time.monotonic()
            self._refresh_dirty = False

    def _ensure_started(self) -> None:
        """Start the Rich progress display if not already started."""
        if not self._started:
            if self._notebook and self._uses_rich_progress:
                _patch_rich_jupyter()
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

        # New top-level run on a reused processor: clear old run state first.
        if parent is None and self._spans and span not in self._spans:
            self._spans.clear()
            self._node_bars.clear()
            self._map_children.clear()
            self._nontty_map_states.clear()
            self._refresh_dirty = False
            if self._notebook and hasattr(self._progress, "reset"):
                self._progress.reset()

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
                    elif event.status in (RunStatus.PAUSED, RunStatus.STOPPED):
                        pass
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
            self._flush_refresh()
            if self._notebook:
                self._display_themed_completion(event)
            elif self._tty_mode:
                if event.status == RunStatus.COMPLETED:
                    self._progress.console.print(f"[bold green]✓ {event.graph_name or 'Run'} completed![/bold green]")
                elif event.status == RunStatus.PAUSED:
                    self._progress.console.print(f"[bold yellow]‖ {event.graph_name or 'Run'} paused[/bold yellow]")
                elif event.status == RunStatus.STOPPED:
                    self._progress.console.print(f"[bold yellow]◼ {event.graph_name or 'Run'} stopped[/bold yellow]")
                else:
                    self._progress.console.print(f"[bold red]✗ {event.graph_name or 'Run'} failed: {event.error}[/bold red]")
            else:
                name = event.graph_name or "Run"
                if event.status == RunStatus.COMPLETED:
                    self._print(f"✓ {name} completed!")
                elif event.status == RunStatus.PAUSED:
                    self._print(f"‖ {name} paused")
                elif event.status == RunStatus.STOPPED:
                    self._print(f"◼ {name} stopped")
                else:
                    error_msg = f": {event.error}" if event.error else ""
                    self._print(f"✗ {name} failed{error_msg}")

    def _display_themed_completion(self, event: RunEndEvent) -> None:
        """Display themed completion message in notebooks.

        Uses CSS light-dark() so the message adapts to the notebook's
        dark/light theme automatically.
        """
        from IPython.display import HTML, display

        name = event.graph_name or "Run"
        if event.status == RunStatus.COMPLETED:
            color = STATUS_COLORS["completed"]
            msg = f"✓ {name} completed!"
        elif event.status == RunStatus.PAUSED:
            color = STATUS_COLORS["paused"]
            msg = f"‖ {name} paused"
        elif event.status == RunStatus.STOPPED:
            color = STATUS_COLORS["paused"]
            msg = f"◼ {name} stopped"
        else:
            color = STATUS_COLORS["failed"]
            error_text = f": {event.error}" if event.error else ""
            msg = f"✗ {name} failed{error_text}"
        html = f'<div style="{FONT_SANS_STYLE}; color:{color}; font-weight:700; padding:4px 0">{msg}</div>'
        display(HTML(theme_wrap(html)))

    def shutdown(self) -> None:
        """Stop the Rich progress display."""
        if self._started:
            self._flush_refresh()
            if self._tty_mode:
                self._progress.stop()
            self._started = False
