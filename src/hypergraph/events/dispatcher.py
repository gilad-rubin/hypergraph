"""Event dispatcher that fans out events to processors."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from hypergraph.events.processor import AsyncEventProcessor, EventProcessor

if TYPE_CHECKING:
    from hypergraph.events.types import Event

logger = logging.getLogger(__name__)


class EventDispatcher:
    """Manages a list of event processors and dispatches events to them.

    All dispatch is best-effort: a failing processor never breaks execution.
    """

    def __init__(self, processors: list[EventProcessor] | None = None) -> None:
        self._processors: list[EventProcessor] = list(processors) if processors else []

    @property
    def active(self) -> bool:
        """True if there is at least one registered processor."""
        return len(self._processors) > 0

    def emit(self, event: Event) -> None:
        """Send *event* to every processor synchronously."""
        for processor in self._processors:
            try:
                processor.on_event(event)
            except Exception:
                logger.warning(
                    "EventProcessor %s failed on %s",
                    processor,
                    type(event).__name__,
                    exc_info=True,
                )

    async def emit_async(self, event: Event) -> None:
        """Send *event* to every processor, using async when available."""
        for processor in self._processors:
            try:
                if isinstance(processor, AsyncEventProcessor):
                    await processor.on_event_async(event)
                else:
                    processor.on_event(event)
            except Exception:
                logger.warning(
                    "EventProcessor %s failed on %s",
                    processor,
                    type(event).__name__,
                    exc_info=True,
                )

    def shutdown(self) -> None:
        """Shut down all processors. Best-effort."""
        for processor in self._processors:
            try:
                processor.shutdown()
            except Exception:
                logger.warning(
                    "EventProcessor %s failed during shutdown",
                    processor,
                    exc_info=True,
                )

    async def shutdown_async(self) -> None:
        """Shut down all processors, using async when available. Best-effort."""
        for processor in self._processors:
            try:
                if isinstance(processor, AsyncEventProcessor):
                    await processor.shutdown_async()
                else:
                    processor.shutdown()
            except Exception:
                logger.warning(
                    "EventProcessor %s failed during shutdown",
                    processor,
                    exc_info=True,
                )
