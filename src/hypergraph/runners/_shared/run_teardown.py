"""The exit ladder a run or a map walks, written once per runner family.

Every exit from a template method — clean, stopped, paused, failed, or abrupt —
owes the same steps in the same order: settle the run row this call created,
shut the dispatcher down, forward this run's checkpoint-save errors, release a
concurrency limiter it installed, drop the stop-signal context token, and hand
the workflow reservation back. Writing that ladder out at each exit is how the
templates drifted: nine shutdown sites per file spelled the same guard three
different ways, and only the async file remembered the two middle steps.

The ladder has two exit policies, and the templates need both:

* :meth:`settle` is the ordinary exit. The first step that raises stops the
  rest; the template records it as the run's terminal error and its ``finally``
  finishes what this call did not reach.
* :meth:`settle_completely` is the last chance — the template's ``finally`` and
  its setup-failure handler. Every step runs even when an earlier one raised.

Each step clears its own state only once it has succeeded, so a step that
raised is retried by the next call and a step that succeeded is not repeated.

``InspectionSettlement`` is the matching single place an inspection session
reaches a terminal snapshot, so a teardown that itself fails can never leave a
notebook shell running forever.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.results import RunStatus
from hypergraph.runners._shared.stop import reset_stop_signal, set_stop_signal

if TYPE_CHECKING:
    from hypergraph.events.dispatcher import EventDispatcher
    from hypergraph.runners._shared._inspect import InspectionSession, MapInspectionSession
    from hypergraph.runners._shared._inspect_transport import NotebookInspectionTransport
    from hypergraph.runners._shared.state import CheckpointErrorSink
    from hypergraph.runners._shared.stop import _WorkflowReservation


class RunTeardown:
    """One sync run's or map's exit ladder."""

    def __init__(
        self,
        reservation: _WorkflowReservation,
        *,
        parent_span_id: str | None,
        shutdown_dispatcher: Callable[[EventDispatcher], None],
        settle_run_row: Callable[[], None],
    ) -> None:
        self._reservation = reservation
        self._parent_span_id = parent_span_id
        self._shutdown_dispatcher = shutdown_dispatcher
        self._settle_run_row = settle_run_row
        self._signal_token: Any | None = None

    def arm(self, workflow_id: str | None) -> None:
        """Bind the reservation to its final identity and publish its signal."""
        self._reservation.bind(workflow_id)
        self._signal_token = set_stop_signal(self._reservation.signal)

    def settle(
        self,
        dispatcher: EventDispatcher | None,
        *,
        settle_run_row: bool = False,
    ) -> EventDispatcher | None:
        """Walk the ladder; the first step that raises stops the rest.

        Returns the dispatcher that still owes a shutdown — ``None`` once it
        has had one, and the caller's own dispatcher for a nested run, whose
        dispatcher belongs to its parent.
        """
        if settle_run_row:
            self._settle_run_row()
        dispatcher = self._shut_down(dispatcher)
        self._release_stop_signal()
        self._reservation.release()
        return dispatcher

    def settle_completely(
        self,
        dispatcher: EventDispatcher | None,
        *,
        settle_run_row: bool = False,
    ) -> EventDispatcher | None:
        """Walk the whole ladder: a later step runs even if an earlier raised.

        The exception that propagates is the LAST one raised, carrying the
        earlier ones as its ``__context__`` — Python's ``finally`` re-raise.
        """
        try:
            try:
                if settle_run_row:
                    self._settle_run_row()
            finally:
                dispatcher = self._shut_down(dispatcher)
        finally:
            try:
                self._release_stop_signal()
            finally:
                self._reservation.release()
        return dispatcher

    def _shut_down(self, dispatcher: EventDispatcher | None) -> EventDispatcher | None:
        """Shut a top-level dispatcher down; a nested run's belongs to its parent."""
        if dispatcher is None or self._parent_span_id is not None:
            return dispatcher
        self._shutdown_dispatcher(dispatcher)
        return None

    def _release_stop_signal(self) -> None:
        if self._signal_token is None:
            return
        reset_stop_signal(self._signal_token)
        self._signal_token = None


class AsyncRunTeardown:
    """One async run's or map's exit ladder.

    Same ladder as :class:`RunTeardown`, with the two steps only the async
    templates have sitting between the dispatcher shutdown and the stop signal:
    forwarding this run's checkpoint-save errors to the caller that asked for
    them, and releasing a concurrency limiter this call installed.
    """

    def __init__(
        self,
        reservation: _WorkflowReservation,
        *,
        parent_span_id: str | None,
        shutdown_dispatcher: Callable[[EventDispatcher], Any],
        settle_run_row: Callable[[], Any],
        checkpoint_error_sink: CheckpointErrorSink | None,
        checkpoint_errors: Callable[[], Iterable[str]],
        release_concurrency_limiter: Callable[[Any], None] | None = None,
    ) -> None:
        self._reservation = reservation
        self._parent_span_id = parent_span_id
        self._shutdown_dispatcher = shutdown_dispatcher
        self._settle_run_row = settle_run_row
        self._checkpoint_error_sink = checkpoint_error_sink
        self._checkpoint_errors = checkpoint_errors
        self._release_concurrency_limiter = release_concurrency_limiter
        self._signal_token: Any | None = None
        self._forwarded_checkpoint_errors = False
        self._limiter_token: Any | None = None

    def arm(self, workflow_id: str | None) -> None:
        """Bind the reservation to its final identity and publish its signal."""
        self._reservation.bind(workflow_id)
        self._signal_token = set_stop_signal(self._reservation.signal)

    def adopt_limiter(self, token: Any | None) -> None:
        """Take ownership of a concurrency-limiter token this call installed."""
        self._limiter_token = token

    async def settle(
        self,
        dispatcher: EventDispatcher | None,
        *,
        settle_run_row: bool = False,
    ) -> EventDispatcher | None:
        """Walk the ladder; the first step that raises stops the rest."""
        if settle_run_row:
            await self._settle_run_row()
        dispatcher = await self._shut_down(dispatcher)
        self._forward_checkpoint_errors()
        self._release_limiter()
        self._release_stop_signal()
        self._reservation.release()
        return dispatcher

    async def settle_completely(
        self,
        dispatcher: EventDispatcher | None,
        *,
        settle_run_row: bool = False,
    ) -> EventDispatcher | None:
        """Walk the whole ladder: a later step runs even if an earlier raised.

        The exception that propagates is the LAST one raised, carrying the
        earlier ones as its ``__context__`` — Python's ``finally`` re-raise.
        """
        try:
            try:
                try:
                    if settle_run_row:
                        await self._settle_run_row()
                finally:
                    dispatcher = await self._shut_down(dispatcher)
            finally:
                self._forward_checkpoint_errors()
                self._release_limiter()
        finally:
            try:
                self._release_stop_signal()
            finally:
                self._reservation.release()
        return dispatcher

    async def _shut_down(self, dispatcher: EventDispatcher | None) -> EventDispatcher | None:
        """Shut a top-level dispatcher down; a nested run's belongs to its parent."""
        if dispatcher is None or self._parent_span_id is not None:
            return dispatcher
        await self._shutdown_dispatcher(dispatcher)
        return None

    def _forward_checkpoint_errors(self) -> None:
        """Hand this run's checkpoint-save errors to the caller, exactly once."""
        if self._checkpoint_error_sink is None or self._forwarded_checkpoint_errors:
            return
        self._forwarded_checkpoint_errors = True
        for message in self._checkpoint_errors():
            self._checkpoint_error_sink(message)

    def _release_limiter(self) -> None:
        if self._limiter_token is None or self._release_concurrency_limiter is None:
            return
        self._release_concurrency_limiter(self._limiter_token)
        self._limiter_token = None

    def _release_stop_signal(self) -> None:
        if self._signal_token is None:
            return
        reset_stop_signal(self._signal_token)
        self._signal_token = None


class InspectionSettlement:
    """The one place a run's inspection session reaches a terminal snapshot.

    An owned session publishes its own terminal artifact. When there is none to
    publish — ``inspect=False``, or a caller-supplied notebook shell around a
    run that failed before it claimed a session — the transport is told the run
    will not report, so the shell settles instead of spinning forever.
    """

    def __init__(
        self,
        session: InspectionSession | MapInspectionSession | None,
        *,
        transport: NotebookInspectionTransport | None,
    ) -> None:
        self._session = session
        self._transport = transport
        self._started_at: float | None = None

    def start(self) -> None:
        """Start the duration clock; an abort before this reports 0.0."""
        self._started_at = time.time()

    def publish(self, *, status: str, total_duration_ms: float, **detail: Any) -> Any:
        """Publish the terminal snapshot this run reached on its own terms.

        ``detail`` is the per-session evidence: ``failures`` for a run,
        ``unstarted_item_indexes`` for a batch. The return is the session's own
        artifact type — ``RunInspection`` or ``MapInspection`` — so it stays
        ``Any`` rather than forcing every caller to narrow a union.
        """
        session = self._session
        if session is None:
            return None
        if session.snapshot().terminal:
            return session.snapshot()
        return session.finish(status=status, total_duration_ms=total_duration_ms, **detail)

    def abort(self, error: BaseException, **detail: Any) -> None:
        """Settle a run that never reached a terminal state of its own."""
        session = self._session
        if session is not None and not session.snapshot().terminal:
            session.finish(
                status=RunStatus.FAILED.value,
                total_duration_ms=self._elapsed_ms(),
                error=error,
                **detail,
            )
        elif self._transport is not None:
            self._transport.fail_to_start(error)

    def _elapsed_ms(self) -> float:
        """0.0 before the clock starts: nothing had run yet to take time."""
        if self._started_at is None:
            return 0.0
        return (time.time() - self._started_at) * 1000
