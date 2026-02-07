"""Event dispatcher that fans out events to processors."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from hypergraph.events.processor import AsyncEventProcessor, EventProcessor

if TYPE_CHECKING:
    from hypergraph.events.types import Event

logger = logging.getLogger(__name__)


class EventDispatcher:
    """Manages a list of event processors and dispatches events to them.

    By default, dispatch is best-effort: a failing processor never breaks
    execution. With ``strict=True``, exceptions propagate immediately.
    """

    def __init__(
        self,
        processors: list[EventProcessor] | None = None,
        *,
        strict: bool = False,
    ) -> None:
        self._processors: list[EventProcessor] = list(processors) if processors else []
        self._strict = strict

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
                if self._strict:
                    raise
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
                if self._strict:
                    raise
                logger.warning(
                    "EventProcessor %s failed on %s",
                    processor,
                    type(event).__name__,
                    exc_info=True,
                )

    def shutdown(self) -> None:
        """Shut down all processors. Best-effort unless strict."""
        first_error = None
        for processor in self._processors:
            try:
                processor.shutdown()
            except Exception:
                if self._strict:
                    if first_error is None:
                        first_error = sys.exc_info()
                else:
                    logger.warning(
                        "EventProcessor %s failed during shutdown",
                        processor,
                        exc_info=True,
                    )
        if first_error is not None:
            raise first_error[1].with_traceback(first_error[2])

    async def shutdown_async(self) -> None:
        """Shut down all processors, using async when available. Best-effort unless strict."""
        for processor in self._processors:
            try:
                if isinstance(processor, AsyncEventProcessor):
                    await processor.shutdown_async()
                else:
                    processor.shutdown()
            except Exception:
                if self._strict:
                    raise
                logger.warning(
                    "EventProcessor %s failed during shutdown",
                    processor,
                    exc_info=True,
                )
