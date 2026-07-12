"""Output renderers for renderer-neutral progress tracker updates."""

from __future__ import annotations

import contextlib
import html as _html
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from hypergraph._repr import (
    ALIGNUI_WIDGET_THEME,
    FONT_SANS_STYLE,
    MUTED_COLOR,
    STATUS_COLORS,
    theme_wrap,
)
from hypergraph.events._progress_tracker import (
    _ProgressMessage,
    _ProgressUpdate,
    _TaskKey,
)
from hypergraph.events.types import RunStatus


class _ProgressRenderer(Protocol):
    """Small lifecycle and update boundary shared by all output modes."""

    def start(self) -> None: ...

    def emit(self, update: _ProgressUpdate) -> None: ...

    def flush(self) -> None: ...

    def shutdown(self) -> None: ...

    def take_async_flush(self) -> bool: ...


def _require_rich() -> None:
    try:
        import rich  # noqa: F401
    except ImportError:
        raise ImportError(
            "The 'rich' package is required for RichProgressProcessor. Install it with: pip install 'hypergraph[progress]' or pip install rich"
        ) from None


class _RichTTYRenderer:
    """Render progress tasks through Rich's terminal Progress display."""

    def __init__(self, *, transient: bool) -> None:
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
        self._task_ids: dict[_TaskKey, Any] = {}

    def start(self) -> None:
        self._progress.start()

    def emit(self, update: _ProgressUpdate) -> None:
        if update.reset:
            # Rich historically kept completed tasks visible across processor
            # reuse; only logical ownership resets for the new root run.
            self._task_ids.clear()
        for key in update.removed:
            task_id = self._task_ids.pop(key, None)
            if task_id is not None:
                self._progress.update(task_id, visible=False)
        for task in update.tasks:
            task_id = self._task_ids.get(task.key)
            if task_id is None:
                self._task_ids[task.key] = self._progress.add_task(
                    task.description,
                    total=task.total,
                    stats=task.stats,
                )
            else:
                self._progress.update(
                    task_id,
                    description=task.description,
                    total=task.total,
                    completed=task.completed,
                    stats=task.stats,
                )
        if update.message is not None and update.message.kind == "run-end":
            self._display_completion(update.message)

    def _display_completion(self, message: _ProgressMessage) -> None:
        if message.status == RunStatus.COMPLETED:
            text = f"[bold green]✓ {message.name} completed![/bold green]"
        elif message.status == RunStatus.PARTIAL:
            text = f"[bold yellow]◐ {message.name} completed with failures[/bold yellow]"
        elif message.status == RunStatus.PAUSED:
            text = f"[bold yellow]‖ {message.name} paused[/bold yellow]"
        elif message.status == RunStatus.STOPPED:
            text = f"[bold yellow]◼ {message.name} stopped[/bold yellow]"
        else:
            text = f"[bold red]✗ {message.name} failed: {message.error}[/bold red]"
        self._progress.console.print(text)

    def flush(self) -> None:
        return

    def shutdown(self) -> None:
        self._progress.stop()

    def take_async_flush(self) -> bool:
        return False


@dataclass(slots=True)
class _NotebookTask:
    description: str
    total: int
    completed: int = 0
    stats: str = ""
    started_at: float = field(default_factory=time.monotonic)


class _NotebookProgress:
    """Notebook-native HTML task display with one stable display handle."""

    def __init__(self, *, transient: bool, state_key: str) -> None:
        self.transient = transient
        self.state_key = state_key
        self._tasks: dict[int, _NotebookTask] = {}
        self._task_order: list[int] = []
        self._next_task_id = 0
        self._started = False
        self._display_handle: Any = None

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
        self._tasks[task_id] = _NotebookTask(
            description=description,
            total=max(0, int(total or 0)),
            stats=stats,
        )
        self._task_order.append(task_id)
        return task_id

    def remove_task(self, task_id: int) -> None:
        self._tasks.pop(task_id, None)
        with contextlib.suppress(ValueError):
            self._task_order.remove(task_id)

    def update(
        self,
        task_id: int,
        *,
        description: str | None = None,
        total: int | None = None,
        completed: int | None = None,
        stats: str | None = None,
    ) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        if description is not None:
            task.description = description
        if total is not None:
            task.total = max(0, int(total))
        if completed is not None:
            task.completed = max(0, int(completed))
        if task.total > 0:
            task.completed = min(task.completed, task.total)
        if stats is not None:
            task.stats = stats

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total = max(0, int(seconds))
        hours = total // 3600
        minutes = (total % 3600) // 60
        seconds = total % 60
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _stats_html(stats: str) -> str:
        text = _html.escape(stats)
        text = re.sub(
            r"(\d+)✓",
            f'<span style="color:{STATUS_COLORS["completed"]}">\\1✓</span>',
            text,
        )
        text = re.sub(
            r"(\d+)✗",
            f'<span style="color:{STATUS_COLORS["failed"]}">\\1✗</span>',
            text,
        )
        text = re.sub(
            r"(\d+)◉",
            f'<span style="color:{STATUS_COLORS["cached"]}">\\1◉</span>',
            text,
        )
        return re.sub(
            r"(~[0-9:.a-zA-Z]+)",
            f'<span style="color:{STATUS_COLORS["partial"]}">\\1</span>',
            text,
        )

    def _render_html(self) -> str:
        rows: list[str] = []
        now = time.monotonic()
        for task_id in self._task_order:
            task = self._tasks[task_id]
            total = task.total if task.total > 0 else 1
            fraction = max(0.0, min(1.0, task.completed / total))
            description = _html.escape(task.description)
            completed = task.completed if task.total > 0 else 0
            rows.append(
                '<div style="display:flex; align-items:center; margin:2px 0">'
                f'<span style="{FONT_SANS_STYLE}; white-space:pre; width:168px; overflow:hidden; text-overflow:ellipsis; margin-right:4px; color:{ALIGNUI_WIDGET_THEME["text_strong"]}">{description}</span>'
                f'<div style="width:300px; height:5px; border-radius:9999px; overflow:hidden; margin-right:10px; background:{ALIGNUI_WIDGET_THEME["border_soft"]}">'
                f'<div style="height:100%; width:{fraction * 100:.2f}%; background:{ALIGNUI_WIDGET_THEME["success_base"]}"></div>'
                "</div>"
                f'<span style="{FONT_SANS_STYLE}; width:64px; text-align:right; margin-right:10px; color:{ALIGNUI_WIDGET_THEME["text_strong"]}; font-variant-numeric:tabular-nums">{completed}/{task.total}</span>'
                f'<span style="{FONT_SANS_STYLE}; width:66px; margin-right:10px; color:{MUTED_COLOR}; font-variant-numeric:tabular-nums">{self._format_elapsed(now - task.started_at)}</span>'
                f'<span style="{FONT_SANS_STYLE}; min-width:140px; color:{MUTED_COLOR}">{self._stats_html(task.stats)}</span>'
                "</div>"
            )
        content = (
            '<div style="display:inline-block; max-width:100%; '
            f'{FONT_SANS_STYLE}; font-size:12px; line-height:1.3; color:{ALIGNUI_WIDGET_THEME["text_strong"]}">' + "".join(rows) + "</div>"
        )
        return theme_wrap(content, state_key=self.state_key)

    def refresh(self) -> None:
        from IPython.display import HTML, display

        html = self._render_html()
        if self._display_handle is None:
            self._display_handle = display(HTML(html), display_id=True)
        else:
            self._display_handle.update(HTML(html))


class _NotebookRenderer:
    """Render tracker updates to a throttled notebook HTML display."""

    _REFRESH_INTERVAL = 0.1

    def __init__(self, *, transient: bool) -> None:
        self._progress = _NotebookProgress(
            transient=transient,
            state_key=f"notebook-progress-{int(time.time() * 1000)}",
        )
        self._task_ids: dict[_TaskKey, int] = {}
        self._last_refresh = 0.0
        self._refresh_dirty = False
        self._needs_async_flush = False

    def start(self) -> None:
        self._progress.start()

    def emit(self, update: _ProgressUpdate) -> None:
        if update.reset:
            self._task_ids.clear()
            self._refresh_dirty = False
            self._progress.reset()

        structural = False
        for key in update.removed:
            task_id = self._task_ids.pop(key, None)
            if task_id is not None:
                self._progress.remove_task(task_id)
                structural = True
        for task in update.tasks:
            task_id = self._task_ids.get(task.key)
            if task_id is None:
                self._task_ids[task.key] = self._progress.add_task(
                    task.description,
                    total=task.total,
                    stats=task.stats,
                )
                structural = True
            else:
                self._progress.update(
                    task_id,
                    description=task.description,
                    total=task.total,
                    completed=task.completed,
                    stats=task.stats,
                )

        if structural:
            self._refresh_structural()
        elif update.tasks:
            self._refresh()

        if update.message is not None and update.message.kind == "run-end":
            self.flush()
            self._display_completion(update.message)

    def _refresh(self) -> None:
        now = time.monotonic()
        if now - self._last_refresh >= self._REFRESH_INTERVAL:
            self._progress.refresh()
            self._last_refresh = now
            self._refresh_dirty = False
        else:
            self._refresh_dirty = True

    def _refresh_structural(self) -> None:
        self._progress.refresh()
        self._last_refresh = time.monotonic()
        self._refresh_dirty = False
        self._needs_async_flush = True

    def _display_completion(self, message: _ProgressMessage) -> None:
        from IPython.display import HTML, display

        if message.status == RunStatus.COMPLETED:
            color = STATUS_COLORS["completed"]
            text = f"✓ {message.name} completed!"
        elif message.status == RunStatus.PARTIAL:
            color = STATUS_COLORS["partial"]
            text = f"◐ {message.name} completed with failures"
        elif message.status == RunStatus.PAUSED:
            color = STATUS_COLORS["paused"]
            text = f"‖ {message.name} paused"
        elif message.status == RunStatus.STOPPED:
            # Preserve the pre-refactor notebook completion foreground. Badge
            # policy intentionally has a distinct canonical stopped color.
            color = STATUS_COLORS["paused"]
            text = f"◼ {message.name} stopped"
        else:
            color = STATUS_COLORS["failed"]
            suffix = f": {message.error}" if message.error else ""
            text = f"✗ {message.name} failed{suffix}"
        html = f'<div style="{FONT_SANS_STYLE}; color:{color}; font-weight:700; padding:4px 0">{text}</div>'
        display(HTML(theme_wrap(html)))

    def flush(self) -> None:
        if self._refresh_dirty:
            self._progress.refresh()
            self._last_refresh = time.monotonic()
            self._refresh_dirty = False

    def shutdown(self) -> None:
        self.flush()
        self._progress.stop()

    def take_async_flush(self) -> bool:
        needed = self._needs_async_flush
        self._needs_async_flush = False
        return needed


_MAP_MILESTONES = frozenset({10, 25, 50, 75, 100})


def _timestamp() -> str:
    return datetime.now().strftime("[%H:%M:%S]")


class _LogRenderer:
    """Render the semantic transcript as exact flushed non-TTY log lines."""

    def __init__(self) -> None:
        self._logged_milestones: dict[str, set[int]] = {}

    def start(self) -> None:
        return

    @staticmethod
    def _print(message: str) -> None:
        print(f"{_timestamp()} {message}", flush=True)

    def emit(self, update: _ProgressUpdate) -> None:
        if update.reset:
            self._logged_milestones.clear()
        message = update.message
        if message is None:
            return
        if message.kind == "node-start":
            self._print(f"▶ {message.name} started")
        elif message.kind == "node-end":
            suffix = " (cached)" if message.cached else ""
            self._print(f"✓ {message.name} completed{suffix}")
        elif message.kind == "node-error":
            self._print(f"✗ {message.name} FAILED")
        elif message.kind == "map-progress":
            self._render_map_progress(message)
        elif message.kind == "run-end":
            self._display_completion(message)

    def _render_map_progress(self, message: _ProgressMessage) -> None:
        if message.scope_id is None or message.total == 0:
            return
        percent = int(message.completed * 100 / message.total)
        logged = self._logged_milestones.setdefault(message.scope_id, set())
        crossed = [milestone for milestone in _MAP_MILESTONES if percent >= milestone and milestone not in logged]
        if not crossed:
            return
        milestone = max(crossed)
        logged.update(item for item in _MAP_MILESTONES if item <= milestone)
        self._print(f"◈ {message.name}: {milestone}% ({message.completed}/{message.total})")

    def _display_completion(self, message: _ProgressMessage) -> None:
        if message.status == RunStatus.COMPLETED:
            text = f"✓ {message.name} completed!"
        elif message.status == RunStatus.PARTIAL:
            text = f"◐ {message.name} completed with failures"
        elif message.status == RunStatus.PAUSED:
            text = f"‖ {message.name} paused"
        elif message.status == RunStatus.STOPPED:
            text = f"◼ {message.name} stopped"
        else:
            suffix = f": {message.error}" if message.error else ""
            text = f"✗ {message.name} failed{suffix}"
        self._print(text)

    def flush(self) -> None:
        return

    def shutdown(self) -> None:
        return

    def take_async_flush(self) -> bool:
        return False


def _make_progress_renderer(
    mode: Literal["tty", "notebook", "non-tty"] | str,
    *,
    transient: bool,
) -> _ProgressRenderer:
    if mode == "tty":
        return _RichTTYRenderer(transient=transient)
    if mode == "notebook":
        return _NotebookRenderer(transient=transient)
    return _LogRenderer()
