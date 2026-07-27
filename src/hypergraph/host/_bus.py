"""In-process live-preview fan-out for the durable host.

The bus is explicitly NOT durable: it carries best-effort previews from a
running worker to watchers in the same process. A watcher in another
process simply sees durable facts only — that is correct and honest.
Previews never advance a watch cursor.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import TYPE_CHECKING, Any

from hypergraph.events.processor import EventProcessor

if TYPE_CHECKING:
    from hypergraph.events.types import Event

_PREVIEW_QUEUE_MAX = 1000


def _offer(queue: asyncio.Queue, item: tuple[str, dict[str, Any]]) -> None:
    """Best-effort enqueue on the subscriber's loop; drop when full."""
    # previews are best-effort; a slow watcher never backpressures execution
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(item)


class _PreviewBus:
    """Thread-safe fan-out of live run events to in-process watcher queues."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, set[tuple[asyncio.Queue, asyncio.AbstractEventLoop]]] = {}

    def subscribe(self, run_id: str) -> asyncio.Queue:
        """Register a watcher queue for ``run_id`` on the calling loop."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=_PREVIEW_QUEUE_MAX)
        with self._lock:
            self._subscribers.setdefault(run_id, set()).add((queue, loop))
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            subscribers = self._subscribers.get(run_id)
            if not subscribers:
                return
            for entry in list(subscribers):
                if entry[0] is queue:
                    subscribers.discard(entry)
            if not subscribers:
                del self._subscribers[run_id]

    def publish(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        """Fan out one preview to every subscriber; safe from any thread."""
        with self._lock:
            subscribers = list(self._subscribers.get(run_id, ()))
        for queue, loop in subscribers:
            # subscriber loop may be closed; previews are best-effort
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_offer, queue, (kind, payload))


def _preview_payload(event: Event) -> dict[str, Any]:
    """Project one event to a JSON-safe preview payload (primitives only)."""
    payload: dict[str, Any] = {"event": type(event).__name__}
    for attr in ("node_name", "graph_name", "superstep"):
        value = getattr(event, attr, None)
        if value is not None and isinstance(value, (str, int)):
            payload[attr] = value
    status = getattr(event, "status", None)
    if status is not None:
        payload["status"] = getattr(status, "value", str(status))
    return payload


class _BusEventProcessor(EventProcessor):
    """Event processor that forwards a worker run's events to the preview bus."""

    def __init__(self, bus: _PreviewBus, workflow_id: str) -> None:
        self._bus = bus
        self._workflow_id = workflow_id

    def on_event(self, event: Event) -> None:
        run_id = getattr(event, "workflow_id", None) or self._workflow_id
        self._bus.publish(run_id, type(event).__name__, _preview_payload(event))


# Process-local registry: serve() registers the bus for a Home URI so any
# RunHomeClient in the same process (not just host.client) sees previews.
_buses: dict[str, _PreviewBus] = {}
_buses_lock = threading.Lock()


def _register_bus(home_uri: str, bus: _PreviewBus) -> None:
    with _buses_lock:
        _buses[home_uri] = bus


def _bus_for(home_uri: str) -> _PreviewBus | None:
    with _buses_lock:
        return _buses.get(home_uri)
