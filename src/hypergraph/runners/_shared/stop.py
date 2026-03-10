"""Stop signal internals — not part of the public API.

StopSignal wraps an event primitive (asyncio.Event or threading.Event)
with optional metadata.  The runner creates one per active run and stores
it in a contextvar so nested graphs can read it without explicit threading.
"""

from __future__ import annotations

import asyncio
import threading
from contextvars import ContextVar
from typing import Any

# ---------------------------------------------------------------------------
# StopSignal
# ---------------------------------------------------------------------------


class StopSignal:
    """Cooperative stop primitive.  Created by the runner, never by user code.

    Wraps ``asyncio.Event`` for async runners and ``threading.Event`` for sync
    runners so that ``runner.stop(workflow_id)`` from any coroutine/thread can
    set the flag and in-flight nodes see it immediately.
    """

    def __init__(self, *, use_threading: bool = False) -> None:
        self._event: asyncio.Event | threading.Event = threading.Event() if use_threading else asyncio.Event()
        self._info: Any = None

    def set(self, info: Any = None) -> None:
        """Set the stop signal with optional metadata."""
        self._info = info
        self._event.set()

    @property
    def is_set(self) -> bool:
        return self._event.is_set()

    @property
    def info(self) -> Any:
        return self._info


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
