"""Stop signal internals — not part of the public API.

StopSignal wraps a threading.Event with optional metadata.  The runner
creates one per active run and stores it in a contextvar so nested graphs
can read it without explicit wiring.

threading.Event is used (not asyncio.Event) because runner.stop() is
documented as callable from any thread, and asyncio.Event is not
thread-safe per Python docs.  Since is_set is polled — never awaited —
threading.Event works correctly for both sync and async runners.
"""

from __future__ import annotations

import threading
from contextvars import ContextVar
from typing import Any

# ---------------------------------------------------------------------------
# StopSignal
# ---------------------------------------------------------------------------


class StopSignal:
    """Cooperative stop primitive.  Created by the runner, never by user code.

    Wraps ``threading.Event`` so that ``runner.stop(workflow_id)`` from any
    thread or coroutine can set the flag and in-flight nodes see it immediately.

    When ``parent`` is set, ``is_set`` also checks the parent signal.
    This enables nested graphs to see the outer stop without explicit wiring.
    """

    def __init__(self, *, parent: StopSignal | None = None) -> None:
        self._event: threading.Event = threading.Event()
        self._info: Any = None
        self._parent: StopSignal | None = parent

    def set(self, info: Any = None) -> None:
        """Set the stop signal with optional metadata."""
        self._info = info
        self._event.set()

    @property
    def is_set(self) -> bool:
        if self._event.is_set():
            return True
        if self._parent is not None and self._parent.is_set:
            # Inherit parent's info on first detection
            if self._info is None:
                self._info = self._parent.info
            return True
        return False

    @property
    def info(self) -> Any:
        if self._info is not None:
            return self._info
        if self._parent is not None:
            return self._parent.info
        return None


# ---------------------------------------------------------------------------
# ContextVar helpers (same pattern as _concurrency_limiter)
# ---------------------------------------------------------------------------

_stop_signal: ContextVar[StopSignal | None] = ContextVar("_stop_signal", default=None)


def get_stop_signal() -> StopSignal | None:
    """Get the current stop signal from contextvar."""
    return _stop_signal.get()


def set_stop_signal(signal: StopSignal) -> Any:
    """Set the stop signal and return a token for reset."""
    return _stop_signal.set(signal)


def reset_stop_signal(token: Any) -> None:
    """Reset the stop signal using a token."""
    _stop_signal.reset(token)
