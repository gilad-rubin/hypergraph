"""Hierarchical progress event processor with pluggable output renderers."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Literal

from hypergraph.events._progress_renderers import (
    _make_progress_renderer,
)
from hypergraph.events._progress_tracker import _ProgressTracker
from hypergraph.events.processor import AsyncEventProcessor, TypedEventProcessor

if TYPE_CHECKING:
    from hypergraph.events.types import (
        InnerCacheEvent,
        NodeEndEvent,
        NodeErrorEvent,
        NodeStartEvent,
        RunEndEvent,
        RunStartEvent,
    )


def _is_notebook() -> bool:
    """Detect whether execution is inside a Jupyter/IPython kernel."""
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
        config = getattr(shell, "config", {})
        try:
            if "IPKernelApp" in config:
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


class RichProgressProcessor(TypedEventProcessor, AsyncEventProcessor):
    """Track graph execution once and render it for the active output mode.

    Rich TTY, notebook HTML, and non-TTY logs consume the same renderer-neutral
    state updates. Rich remains optional unless TTY mode is selected.
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
        self._tracker = _ProgressTracker()
        self._renderer = _make_progress_renderer(mode, transient=transient)
        self._started = False

    def _ensure_started(self) -> None:
        if not self._started:
            self._renderer.start()
            self._started = True

    def on_run_start(self, event: RunStartEvent) -> None:
        self._ensure_started()
        self._renderer.emit(self._tracker.on_run_start(event))

    def on_node_start(self, event: NodeStartEvent) -> None:
        self._ensure_started()
        self._renderer.emit(self._tracker.on_node_start(event))

    def on_node_end(self, event: NodeEndEvent) -> None:
        self._renderer.emit(self._tracker.on_node_end(event))

    def on_node_error(self, event: NodeErrorEvent) -> None:
        self._renderer.emit(self._tracker.on_node_error(event))

    def on_inner_cache(self, event: InnerCacheEvent) -> None:
        self._renderer.emit(self._tracker.on_inner_cache(event))

    def on_run_end(self, event: RunEndEvent) -> None:
        self._renderer.emit(self._tracker.on_run_end(event))

    def shutdown(self) -> None:
        if self._started:
            self._renderer.shutdown()
            self._started = False

    async def on_event_async(self, event: object) -> None:
        """Dispatch synchronously, yielding only after structural notebook work."""
        self.on_event(event)
        if self._renderer.take_async_flush():
            import asyncio

            await asyncio.sleep(0)

    async def shutdown_async(self) -> None:
        self.shutdown()
