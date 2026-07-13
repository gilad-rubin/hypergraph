"""Stop signal internals — not part of the public API.

StopSignal wraps a threading.Event with optional metadata.  The runner
creates one per active execution and stores it in a contextvar so nested graphs
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

from hypergraph.exceptions import WorkflowAlreadyRunningError

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
        self._set_lock = threading.Lock()

    def set(self, info: Any = None) -> None:
        """Accept the first stop request and preserve its optional metadata."""
        with self._set_lock:
            if self._event.is_set():
                return
            self._info = info
            self._event.set()

    @property
    def is_set(self) -> bool:
        if self._event.is_set():
            return True
        return self._parent is not None and self._parent.is_set

    @property
    def info(self) -> Any:
        if self._event.is_set():
            return self._info
        if self._parent is not None:
            return self._parent.info
        return None


class _WorkflowReservation:
    """One identity-safe claim on a runner's live workflow registry."""

    def __init__(
        self,
        _registry: _ActiveWorkflows,
        _workflow_id: str | None,
        _signal: StopSignal,
    ) -> None:
        self._registry = _registry
        self._workflow_id = _workflow_id
        self.signal = _signal
        self._released = False

    def bind(self, workflow_id: str | None) -> None:
        """Atomically bind or rebind this reservation to its final identity."""
        self._registry._bind(self, workflow_id)

    def release(self) -> None:
        """Release this reservation without disturbing a later owner."""
        self._registry._release(self)


class _ActiveWorkflows:
    """Thread-safe ownership of live workflow IDs and their stop signals."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reservations: dict[str, _WorkflowReservation] = {}

    def reserve(self, workflow_id: str | None) -> _WorkflowReservation:
        """Claim an explicit ID now, or create an unbound claim for later."""
        reservation = _WorkflowReservation(
            self,
            workflow_id,
            StopSignal(parent=get_stop_signal()),
        )
        with self._lock:
            if workflow_id is not None:
                if workflow_id in self._reservations:
                    raise WorkflowAlreadyRunningError(workflow_id)
                self._reservations[workflow_id] = reservation
        return reservation

    def stop(self, workflow_id: str, *, info: Any = None) -> None:
        """Request stop through the signal owned by the current reservation."""
        with self._lock:
            reservation = self._reservations.get(workflow_id)
        if reservation is not None:
            reservation.signal.set(info=info)

    def _bind(
        self,
        reservation: _WorkflowReservation,
        workflow_id: str | None,
    ) -> None:
        with self._lock:
            if reservation._released:
                raise RuntimeError("Cannot bind a released workflow reservation")

            current_id = reservation._workflow_id
            if current_id == workflow_id and (workflow_id is None or self._reservations.get(workflow_id) is reservation):
                return

            if workflow_id is not None:
                owner = self._reservations.get(workflow_id)
                if owner is not None and owner is not reservation:
                    raise WorkflowAlreadyRunningError(workflow_id)

            if current_id is not None and self._reservations.get(current_id) is reservation:
                del self._reservations[current_id]
            if workflow_id is not None:
                self._reservations[workflow_id] = reservation
            reservation._workflow_id = workflow_id

    def _release(self, reservation: _WorkflowReservation) -> None:
        with self._lock:
            if reservation._released:
                return
            workflow_id = reservation._workflow_id
            if workflow_id is not None and self._reservations.get(workflow_id) is reservation:
                del self._reservations[workflow_id]
            reservation._released = True


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
