"""Failure logging — a failed node's real message and traceback, on a stdlib logger.

Durable records keep only the privacy-safe projection of a failure; the real
message and traceback travel on the in-memory ``NodeErrorEvent.error_detail``
and nowhere else. ``FailureLogProcessor`` is the opt-in consumer that hands
that detail to :mod:`logging`, so a failed run — durable or not — leaves a
readable clue wherever the application's logging config sends records.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from hypergraph.events.processor import TypedEventProcessor

if TYPE_CHECKING:
    from hypergraph.events.types import NodeErrorEvent


class FailureLogProcessor(TypedEventProcessor):
    """Log every ``NodeErrorEvent``'s real message and traceback to a stdlib logger.

    One record per event, at ``level``, rendered as::

        node '<node>' in graph '<graph>' failed (run_id=<id>, workflow_id=<wf>, item_index=<i>): <Type>: <message>
        Traceback (most recent call last):
          ...

    ``in graph`` is dropped for an unnamed graph, and ``workflow_id`` /
    ``item_index`` when the event has none. The record carries no
    ``exc_info`` (the event holds no exception object; the traceback is
    already formatted text) and no ``extra`` attributes. The message is built
    with lazy ``%``-args, and nothing is formatted when the logger would
    discard the record.

    Args:
        logger: The logger to write to — a ``logging.Logger``, or a name
            resolved with ``logging.getLogger``. Defaults to
            ``"hypergraph.failures"``.
        level: The ``int`` level every record is logged at. Defaults to
            ``logging.ERROR``.

    Raises:
        TypeError: ``logger`` is neither a ``logging.Logger`` nor a ``str``,
            or ``level`` is not an ``int`` (a ``bool`` is refused).

    Note:
        Opt-in: nothing is logged unless you install this processor. It logs
        in-memory detail only — hypergraph persists nothing here, and every
        durable record keeps the safe projection — but the handlers your
        logging config attaches may persist what they receive, and exception
        messages and tracebacks can carry sensitive text verbatim.

        Node failures only: a nested failure logs once per nesting level,
        innermost first (each level emits its own ``NodeErrorEvent``); a
        run-level failure that emits no ``NodeErrorEvent`` is not logged.
        The processor holds no per-run state, so one instance may be shared
        across concurrent runs.
    """

    def __init__(self, logger: logging.Logger | str = "hypergraph.failures", *, level: int = logging.ERROR) -> None:
        if isinstance(logger, str):
            logger = logging.getLogger(logger)
        elif not isinstance(logger, logging.Logger):
            raise TypeError(
                f"FailureLogProcessor logger must be a logging.Logger or a logger name, got {type(logger).__name__}.\n\n"
                "Every failed node's message and traceback is written to this logger.\n\n"
                'How to fix: Pass a logger name such as "app.failures", or a logging.getLogger(...) instance.'
            )
        if isinstance(level, bool) or not isinstance(level, int):
            raise TypeError(
                f"FailureLogProcessor level must be an int logging level, got {type(level).__name__} {level!r}.\n\n"
                "Logger.log accepts only an int level, and a bool would silently log at level 0 or 1.\n\n"
                "How to fix: Pass a stdlib level constant, e.g. level=logging.WARNING."
            )
        self._logger = logger
        self._level = level

    def on_node_error(self, event: NodeErrorEvent) -> None:
        if not self._logger.isEnabledFor(self._level):
            return
        template = "node '%s'"
        args: list[object] = [event.node_name]
        if event.graph_name:
            template += " in graph '%s'"
            args.append(event.graph_name)
        template += " failed (run_id=%s"
        args.append(event.run_id)
        if event.workflow_id is not None:
            template += ", workflow_id=%s"
            args.append(event.workflow_id)
        if event.item_index is not None:
            template += ", item_index=%s"
            args.append(event.item_index)
        detail = event.error_detail
        if detail is None:
            template += "): %s"
            args.append(event.error or event.error_type)
        else:
            if detail.message:
                template += "): %s: %s"
                args.extend((detail.type_name, detail.message))
            else:
                template += "): %s"
                args.append(detail.type_name)
            if detail.traceback:
                template += "\n%s"
                args.append(detail.traceback.rstrip())
        self._logger.log(self._level, template, *args)
