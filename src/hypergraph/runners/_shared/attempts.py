"""Shared retry/timeout attempt coordinator for FunctionNode execution.

The runner execution layer owns retry orchestration: the coordinator sits
inside the sync/async FunctionNode executors — below the superstep's cache
lookup, above state application — so a cache hit consumes zero attempts and
intermediate attempts never fold state, bump versions, emit node events, or
schedule downstream work.

Semantics live in small pure helpers shared by both drivers; the sync/async
drivers differ only in how they sleep and how they call the ledger. All
decisions come from the locked #187 contract:

- Eligibility is an isinstance check of the exact underlying ``Exception``
  against the node's ``retry_on`` allowlist. ``BaseException`` control flow
  (pause, stop, cancellation, KeyboardInterrupt) passes through untouched
  and never settles a reservation as FAILED.
- Backoff is drawn once per failed attempt — nominal
  ``min(max_delay, initial_delay * multiplier ** (n - 1))``, full jitter
  uniform in ``[0, nominal]`` — and persisted with the failed attempt as
  ``sampled_delay`` + absolute ``retry_not_before``. A resume honors the
  persisted wake time; it never redraws or restarts a wait.
- ``RetryAfterError`` is a non-authorizing carrier: its server delay is
  honored exactly (no jitter, no ``max_delay`` cap) but stays bounded by the
  budget and the series window; when no retry may start the exact underlying
  exception is re-raised, never the carrier.
- Budget durability follows the checkpointer: a run with persistence gets the
  durable attempt ledger (write-through reservations); otherwise the budget
  is process-local and no ledger call is made.
- The FINAL attempt (success or terminal failure) intentionally stays
  ``STARTED``: :func:`maybe_close_attempt_series_sync` /
  :func:`maybe_close_attempt_series_async` settle it atomically with the
  linked StepRecord at the runner's step-save site, per the atomic-close
  invariant of the ledger. Series-closing records always persist write-through
  (a deferred close could collide with a cyclic re-execution opening the next
  series).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from hypergraph.checkpointers.base import _new_attempt_series_id
from hypergraph.checkpointers.types import (
    AttemptError,
    AttemptLedgerError,
    AttemptRecord,
    AttemptSeries,
    AttemptStatus,
    StepStatus,
)
from hypergraph.diagnostics import (
    AttemptDiagnosticHint,
    attach_attempt_diagnostic,
    merge_deadline_evidence,
    qualified_type_name,
)
from hypergraph.exceptions import (
    AttemptOutcomeUnknownError,
    AttemptTimeoutError,
    RetryWindowExpiredError,
)
from hypergraph.nodes.retry import RetryAfterError, RetryPolicy

if TYPE_CHECKING:
    from hypergraph.checkpointers.base import Checkpointer
    from hypergraph.checkpointers.types import StepRecord
    from hypergraph.graph import Graph


def _utcnow() -> datetime:
    """Coordinator clock (module-level so tests can pin time deterministically)."""
    return datetime.now(timezone.utc)


def _sleep_sync(seconds: float) -> None:
    """Sync backoff wait (module-level seam for deterministic tests)."""
    time.sleep(seconds)


async def _sleep_async(seconds: float) -> None:
    """Async backoff wait (module-level seam for deterministic tests)."""
    await asyncio.sleep(seconds)


_TIMEOUT_ONLY_POLICY = RetryPolicy(
    max_attempts=1,
    retry_on=(AttemptTimeoutError,),
)


# === Pure decision helpers (single source of truth for both drivers) ===


def nominal_delay(policy: RetryPolicy, failed_attempt_number: int) -> float:
    """Nominal backoff cap after failed one-based attempt ``n``.

    Valid extreme policies (huge multipliers, deep attempt numbers) overflow
    float exponentiation; the nominal delay is then simply the cap.
    """
    try:
        grown = policy.initial_delay * policy.backoff_multiplier ** (failed_attempt_number - 1)
    except OverflowError:
        return policy.max_delay
    return min(policy.max_delay, grown)


@dataclass(frozen=True)
class BackoffDecision:
    """One drawn backoff: what is slept now and persisted with the failure.

    ``nominal_delay`` is None for a server-supplied ``RetryAfterError`` delay,
    which is honored exactly (no jitter, no cap). ``sampled_delay`` is also
    the effective wait; ``retry_not_before`` is the absolute wake time.
    """

    nominal_delay: float | None
    sampled_delay: float
    retry_not_before: datetime

    @property
    def effective_delay(self) -> float:
        """The wait actually enforced (the ticket's third delay fact).

        Always equals ``sampled_delay``: the persisted shape stores the
        irreducible state (sampled draw + absolute wake time) and nominal is
        recomputable from the fingerprinted policy.
        """
        return self.sampled_delay


def draw_backoff(
    policy: RetryPolicy,
    failed_attempt_number: int,
    *,
    now: datetime,
    retry_after: float | None = None,
) -> BackoffDecision:
    """Draw the wait before the next attempt. Sampled exactly once."""
    if retry_after is not None:
        sampled = float(retry_after)
        nominal: float | None = None
    else:
        nominal = nominal_delay(policy, failed_attempt_number)
        sampled = random.uniform(0.0, nominal) if policy.jitter == "full" else nominal
    return BackoffDecision(
        nominal_delay=nominal,
        sampled_delay=sampled,
        retry_not_before=now + timedelta(seconds=sampled),
    )


@dataclass(frozen=True)
class _FailureDisposition:
    """An attempt failure, unwrapped: the exact underlying exception object."""

    underlying: Exception
    retry_after: float | None
    eligible: bool


def classify_failure(error: Exception, policy: RetryPolicy) -> _FailureDisposition:
    """Unwrap a RetryAfterError carrier and check the retry_on allowlist."""
    if isinstance(error, RetryAfterError):
        underlying: Exception = error.error
        retry_after: float | None = error.retry_after
    else:
        underlying, retry_after = error, None
    return _FailureDisposition(
        underlying=underlying,
        retry_after=retry_after,
        eligible=isinstance(underlying, policy.retry_on),
    )


@dataclass(frozen=True)
class RetryStep:
    """A granted retry: the unwrapped failure and the backoff to persist/sleep."""

    underlying: Exception
    decision: BackoffDecision


def _terminal_hint(
    code: str,
    policy: RetryPolicy,
    attempt_number: int,
    limit: Literal["max_attempts", "retry_window"] | None = None,
) -> AttemptDiagnosticHint:
    return AttemptDiagnosticHint(
        code=code,
        attempt_count=attempt_number,
        max_attempts=policy.max_attempts,
        limit=limit,
    )


def plan_after_failure(
    error: Exception,
    policy: RetryPolicy,
    attempt_number: int,
    deadline_at: datetime | None,
    *,
    now: datetime,
) -> RetryStep:
    """Decide what follows a failed attempt.

    Returns the backoff to persist and sleep when another attempt may start.
    Raises the exact underlying exception (never a wrapper, never the
    RetryAfterError carrier) when the failure is ineligible, the budget is
    exhausted, or the wait cannot end before the series deadline — the
    deadline case deliberately skips the pointless sleep. Terminal raises
    carry an attached :class:`AttemptDiagnosticHint`; a framework exception's
    own stable code always outranks the hint at derivation time.
    """
    disposition = classify_failure(error, policy)
    if not disposition.eligible:
        attach_attempt_diagnostic(
            disposition.underlying,
            _terminal_hint("HG_NODE_FAILED", policy, attempt_number),
        )
        raise disposition.underlying
    if attempt_number >= policy.max_attempts:
        attach_attempt_diagnostic(
            disposition.underlying,
            _terminal_hint("HG_RETRY_EXHAUSTED", policy, attempt_number, limit="max_attempts"),
        )
        raise disposition.underlying
    decision = draw_backoff(policy, attempt_number, now=now, retry_after=disposition.retry_after)
    if deadline_at is not None and decision.retry_not_before >= deadline_at:
        attach_attempt_diagnostic(
            disposition.underlying,
            _terminal_hint("HG_RETRY_EXHAUSTED", policy, attempt_number, limit="retry_window"),
        )
        raise disposition.underlying
    return RetryStep(underlying=disposition.underlying, decision=decision)


# === Attempt events (locked #233 shape) ===


@dataclass(frozen=True)
class AttemptEventSink:
    """Builds and emits attempt events with run/node identity pre-bound.

    ``emit`` is the dispatcher's synchronous emit (the same seam streaming
    and inner-cache events use from inside executors), so sync and async
    drivers share one sink. Events parent onto the single logical node span.
    """

    emit: Callable[[Any], None]
    run_id: str
    node_span_id: str | None
    workflow_id: str | None
    item_index: int | None
    node_name: str
    graph_name: str
    superstep: int | None

    def attempt_start(
        self,
        *,
        series_id: str,
        attempt_number: int,
        max_attempts: int,
        timeout_seconds: float | None,
        attempt_deadline_at: datetime | None,
        series_deadline_at: datetime | None,
    ) -> None:
        from hypergraph.events.types import NodeAttemptStartEvent

        self.emit(
            NodeAttemptStartEvent(
                run_id=self.run_id,
                parent_span_id=self.node_span_id,
                workflow_id=self.workflow_id,
                item_index=self.item_index,
                node_name=self.node_name,
                graph_name=self.graph_name,
                superstep=self.superstep,
                attempt_series_id=series_id,
                attempt_number=attempt_number,
                max_attempts=max_attempts,
                timeout_seconds=timeout_seconds,
                attempt_deadline_at=attempt_deadline_at,
                series_deadline_at=series_deadline_at,
            )
        )

    def attempt_end(
        self,
        *,
        series_id: str,
        attempt_number: int,
        outcome: Literal["succeeded", "failed", "timed_out", "cancelled"],
        settlement: Literal["returned", "raised", "cancelled"],
        duration_ms: float,
        deadline_scope: Literal["attempt", "series"] | None = None,
        deadline_elapsed: bool = False,
        cancellation_requested: bool = False,
        error: BaseException | None = None,
        retry_scheduled: bool = False,
        retry_not_before: datetime | None = None,
    ) -> None:
        from hypergraph.events.types import NodeAttemptEndEvent

        self.emit(
            NodeAttemptEndEvent(
                run_id=self.run_id,
                parent_span_id=self.node_span_id,
                workflow_id=self.workflow_id,
                item_index=self.item_index,
                node_name=self.node_name,
                graph_name=self.graph_name,
                superstep=self.superstep,
                attempt_series_id=series_id,
                attempt_number=attempt_number,
                outcome=outcome,
                settlement=settlement,
                deadline_scope=deadline_scope,
                deadline_elapsed=deadline_elapsed,
                cancellation_requested=cancellation_requested,
                duration_ms=duration_ms,
                error_type=qualified_type_name(type(error)) if error is not None else None,
                retry_scheduled=retry_scheduled,
                retry_not_before=retry_not_before,
            )
        )


def _failure_outcome(
    error: Exception,
) -> tuple[Literal["failed", "timed_out"], Literal["raised", "cancelled"]]:
    """(outcome, settlement) for a failed attempt, per the locked vocabulary."""
    if isinstance(error, (AttemptTimeoutError, RetryWindowExpiredError)):
        return "timed_out", "cancelled"
    return "failed", "raised"


@dataclass
class _DeadlineWitness:
    """Independently witnessed deadline facts for one in-flight invocation."""

    deadline_elapsed: bool = False
    cancellation_requested: bool = False
    scope: Literal["attempt", "retry_window"] | None = None

    @property
    def event_scope(self) -> Literal["attempt", "series"] | None:
        if not self.deadline_elapsed or self.scope is None:
            return None
        return "attempt" if self.scope == "attempt" else "series"


def _series_deadline(policy: RetryPolicy, now: datetime) -> datetime | None:
    """The immutable absolute deadline fixed when a series opens."""
    if policy.retry_window is None:
        return None
    return now + timedelta(seconds=policy.retry_window)


@dataclass(frozen=True)
class _ActiveDeadline:
    """The next cooperative deadline for one in-flight invocation."""

    seconds: float
    scope: Literal["attempt", "retry_window"]
    configured_seconds: float


def _active_deadline(
    timeout: float | None,
    policy: RetryPolicy,
    deadline_at: datetime | None,
    *,
    now: datetime,
) -> _ActiveDeadline | None:
    """Choose the earlier of the per-attempt and retry-window deadlines."""
    window_remaining = None if deadline_at is None else max(0.0, (deadline_at - now).total_seconds())
    if window_remaining is not None and (timeout is None or window_remaining <= timeout):
        assert policy.retry_window is not None
        return _ActiveDeadline(
            seconds=window_remaining,
            scope="retry_window",
            configured_seconds=policy.retry_window,
        )
    if timeout is not None:
        return _ActiveDeadline(
            seconds=timeout,
            scope="attempt",
            configured_seconds=timeout,
        )
    return None


async def _invoke_with_deadline(
    invoke: Callable[[], Awaitable[Any]],
    *,
    node_name: str,
    deadline: _ActiveDeadline | None,
    record_deadline: Callable[[], Awaitable[None]] | None,
    witness: _DeadlineWitness | None = None,
) -> Any:
    """Invoke once, requesting cancellation at a cooperative deadline.

    ``asyncio.wait`` supplies the Python-3.10-compatible wait-for shape while
    leaving cancellation and settlement explicit: after the deadline wins we
    call ``Task.cancel()`` and await the task before deciding which terminal
    condition was actually witnessed. ``witness`` records the two independent
    facts (deadline elapsed, cancellation requested) for attempt events.
    """
    if deadline is None:
        return await invoke()

    task = asyncio.create_task(invoke())
    try:
        done, _ = await asyncio.wait((task,), timeout=deadline.seconds)
    except BaseException:
        # External cancellation/control flow remains the terminal cause, but
        # do not leak the child invocation. This mirrors direct-await cleanup.
        if not task.done():
            task.cancel()
        with suppress(BaseException):
            await task
        raise

    if task in done:
        return await task

    # Deadline elapsed. Cancellation is only a request; the task may settle
    # cancelled, raise during cleanup, or suppress cancellation and return.
    task.cancel()
    if witness is not None:
        witness.deadline_elapsed = True
        witness.cancellation_requested = True
        witness.scope = deadline.scope

    async def persist_deadline_evidence() -> None:
        if record_deadline is not None:
            await record_deadline()

    try:
        result = await task
    except asyncio.CancelledError:
        await persist_deadline_evidence()
        if deadline.scope == "retry_window":
            raise RetryWindowExpiredError(node_name, deadline.configured_seconds) from None
        raise AttemptTimeoutError(node_name, deadline.configured_seconds) from None
    except BaseException:
        # A cleanup exception is the exact terminal cause. Record that the
        # deadline/cancellation happened, then preserve the object and trace.
        await persist_deadline_evidence()
        raise
    else:
        # Suppressed cancellation produced a real witnessed value. Keep it.
        await persist_deadline_evidence()
        return result


@dataclass(frozen=True)
class _SeriesPlan:
    """A ledger series ready to continue: consumed budget and pending wake."""

    series_id: str
    node_name: str
    deadline_at: datetime | None
    consumed: int
    pending_wake_at: datetime | None
    last_evidence: AttemptError | None
    last_status: AttemptStatus | None


def _plan_resumed_series(series: AttemptSeries, records: list[AttemptRecord]) -> _SeriesPlan:
    """Continue an open series from its durable evidence (stranded rows already settled)."""
    last = records[-1] if records else None
    return _SeriesPlan(
        series_id=series.id,
        node_name=series.node_name,
        deadline_at=series.deadline_at,
        consumed=len(records),
        pending_wake_at=last.retry_not_before if last is not None else None,
        last_evidence=last.error if last is not None else None,
        last_status=last.status if last is not None else None,
    )


def _evidence_suffix(evidence: AttemptError | None) -> str:
    if evidence is None:
        return ""
    if not evidence.message:
        return f" Last recorded failure: {evidence.type_name}"
    return f" Last recorded failure: {evidence.type_name}: {evidence.message}"


def _pre_loop_wait(plan: _SeriesPlan, policy: RetryPolicy, now: datetime) -> float | None:
    """Seconds to wait before the next resumed reservation, honoring evidence.

    Raises from durable evidence — without sleeping — when no retry may start:
    the last consumed attempt has an unknown outcome, the persisted budget is
    exhausted, or the persisted wake time lies at or beyond the immutable
    series deadline. In a fresh process there is no live exception object to
    re-raise, so a typed evidence error carries the facts instead.
    """
    if plan.last_status is AttemptStatus.OUTCOME_UNKNOWN:
        # There is no witnessed user exception to preserve: the reservation
        # was durable but its process died before the outcome. Never silently
        # re-run — external side effects may have completed.
        raise AttemptOutcomeUnknownError(plan.node_name, plan.series_id, plan.consumed)
    if plan.consumed >= policy.max_attempts:
        exhausted = AttemptLedgerError(
            f"Attempt budget exhausted for series {plan.series_id!r}: "
            f"{plan.consumed} of max_attempts={policy.max_attempts} consumed across resume."
            f"{_evidence_suffix(plan.last_evidence)}\n\n"
            "How to fix:\n"
            "  Fork or start a new workflow to grant a fresh retry budget."
        )
        attach_attempt_diagnostic(
            exhausted,
            _terminal_hint("HG_RETRY_EXHAUSTED", policy, plan.consumed, limit="max_attempts"),
        )
        raise exhausted
    if plan.pending_wake_at is None:
        return None
    if plan.deadline_at is not None and plan.pending_wake_at >= plan.deadline_at:
        expired = AttemptLedgerError(
            f"Persisted retry_not_before {plan.pending_wake_at.isoformat()} lies at or beyond "
            f"deadline_at {plan.deadline_at.isoformat()} for series {plan.series_id!r}; "
            f"no further attempt may start.{_evidence_suffix(plan.last_evidence)}\n\n"
            "How to fix:\n"
            "  Fork or start a new workflow to grant a fresh retry window."
        )
        attach_attempt_diagnostic(
            expired,
            _terminal_hint("HG_RETRY_EXHAUSTED", policy, plan.consumed, limit="retry_window"),
        )
        raise expired
    remaining = (plan.pending_wake_at - now).total_seconds()
    return remaining if remaining > 0 else None


# === Ledger surfaces ===


@runtime_checkable
class SyncAttemptLedger(Protocol):
    """Sync flavor of the #229 attempt-ledger seam (structural).

    SqliteCheckpointer satisfies this via its ``*_sync`` methods. Defined here
    because the checkpointer protocol surface belongs to #229 and is not
    extended by this module.
    """

    def open_attempt_series_sync(
        self,
        run_id: str,
        node_name: str,
        *,
        policy_fingerprint: str,
        max_attempts: int,
        deadline_at: datetime | None = None,
    ) -> AttemptSeries: ...

    def get_open_attempt_series_sync(self, run_id: str, node_name: str) -> AttemptSeries | None: ...

    def get_attempt_records_sync(self, series_id: str) -> list[AttemptRecord]: ...

    def begin_attempt_sync(
        self,
        series_id: str,
        *,
        policy_fingerprint: str,
        scheduled_superstep: int,
    ) -> AttemptRecord: ...

    def record_attempt_outcome_sync(
        self,
        series_id: str,
        attempt_number: int,
        status: AttemptStatus,
        *,
        error: AttemptError | None = None,
        retry_not_before: datetime | None = None,
        sampled_delay: float | None = None,
    ) -> AttemptRecord: ...

    def close_attempt_series_sync(
        self,
        series_id: str,
        attempt_number: int,
        status: AttemptStatus,
        *,
        step_record: StepRecord,
        error: AttemptError | None = None,
    ) -> None: ...

    def resolve_stranded_attempts_sync(self, series_id: str) -> list[AttemptRecord]: ...


def _require_sync_ledger(checkpointer: Any) -> SyncAttemptLedger:
    if isinstance(checkpointer, SyncAttemptLedger):
        return checkpointer
    raise NotImplementedError(
        f"{type(checkpointer).__name__} does not support the durable attempt ledger for SyncRunner.\n\n"
        "How to fix:\n"
        "  Use a checkpointer with the sync attempt-series operations (SqliteCheckpointer),\n"
        "  or implement the *_sync attempt-ledger methods on your backend."
    )


def _tag_persistence_failure(error: Exception) -> None:
    """Mark a raw ledger persistence failure with its stable diagnostic code.

    ``AttemptLedgerError`` invariants (budget, deadline, fingerprint) keep
    their own evidence; only genuine persistence failures — which propagate
    as their original exception — get ``HG_ATTEMPT_PERSISTENCE_FAILED``.
    """
    if not isinstance(error, AttemptLedgerError):
        attach_attempt_diagnostic(error, AttemptDiagnosticHint(code="HG_ATTEMPT_PERSISTENCE_FAILED"))


def _open_or_resume_sync(
    ledger: SyncAttemptLedger,
    run_id: str,
    node_name: str,
    policy: RetryPolicy,
    now: datetime,
) -> _SeriesPlan:
    try:
        series = ledger.get_open_attempt_series_sync(run_id, node_name)
        if series is None:
            series = ledger.open_attempt_series_sync(
                run_id,
                node_name,
                policy_fingerprint=policy.fingerprint,
                max_attempts=policy.max_attempts,
                deadline_at=_series_deadline(policy, now),
            )
            return _SeriesPlan(series.id, node_name, series.deadline_at, 0, None, None, None)
        # Resume: under the workflow reservation no other process can still be
        # running these attempts, so stranded STARTED rows are settled explicitly
        # BEFORE any new reservation.
        records = ledger.resolve_stranded_attempts_sync(series.id)
    except Exception as error:
        _tag_persistence_failure(error)
        raise
    return _plan_resumed_series(series, records)


async def _open_or_resume_async(
    ledger: Checkpointer,
    run_id: str,
    node_name: str,
    policy: RetryPolicy,
    now: datetime,
) -> _SeriesPlan:
    try:
        series = await ledger.get_open_attempt_series(run_id, node_name)
        if series is None:
            series = await ledger.open_attempt_series(
                run_id,
                node_name,
                policy_fingerprint=policy.fingerprint,
                max_attempts=policy.max_attempts,
                deadline_at=_series_deadline(policy, now),
            )
            return _SeriesPlan(series.id, node_name, series.deadline_at, 0, None, None, None)
        # Resume: settle stranded rows explicitly before any new reservation.
        records = await ledger.resolve_stranded_attempts(series.id)
    except Exception as error:
        _tag_persistence_failure(error)
        raise
    return _plan_resumed_series(series, records)


# === Attempt-loop drivers (keep these two structurally parallel) ===


def run_attempts_sync(
    invoke: Callable[[], Any],
    *,
    node_name: str,
    policy: RetryPolicy,
    checkpointer: Any | None,
    run_id: str | None,
    scheduled_superstep: int,
    events: AttemptEventSink | None = None,
) -> Any:
    """Drive one logical node execution as a series of attempts (sync)."""
    now = _utcnow()
    if checkpointer is not None and run_id:
        ledger = _require_sync_ledger(checkpointer)
        plan = _open_or_resume_sync(ledger, run_id, node_name, policy, now)
        wait = _pre_loop_wait(plan, policy, now)
        if wait is not None:
            _sleep_sync(wait)
        series_id, deadline_at, attempt_number = plan.series_id, plan.deadline_at, plan.consumed
    else:
        ledger, series_id = None, _new_attempt_series_id()
        deadline_at, attempt_number = _series_deadline(policy, now), 0

    while True:
        if ledger is not None:
            try:
                record = ledger.begin_attempt_sync(
                    series_id,
                    policy_fingerprint=policy.fingerprint,
                    scheduled_superstep=scheduled_superstep,
                )
            except AttemptLedgerError as backstop:
                # The atomic durable backstop refused the reservation
                # (budget/deadline re-verification lost a race).
                attach_attempt_diagnostic(
                    backstop,
                    _terminal_hint("HG_RETRY_EXHAUSTED", policy, attempt_number),
                )
                raise
            except Exception as persistence_error:
                _tag_persistence_failure(persistence_error)
                raise
            attempt_number = record.attempt_number
        else:
            attempt_number += 1
        if events is not None:
            events.attempt_start(
                series_id=series_id,
                attempt_number=attempt_number,
                max_attempts=policy.max_attempts,
                timeout_seconds=None,
                attempt_deadline_at=None,
                series_deadline_at=deadline_at,
            )
        attempt_started = time.perf_counter()
        try:
            result = invoke()
        except Exception as error:
            duration_ms = (time.perf_counter() - attempt_started) * 1000
            outcome, settlement = _failure_outcome(error)
            # Terminal paths raise the exact underlying exception from here.
            # The reservation intentionally stays STARTED: the step-save site
            # settles it atomically with the linked StepRecord.
            try:
                step = plan_after_failure(error, policy, attempt_number, deadline_at, now=_utcnow())
            except Exception as terminal:
                if events is not None:
                    events.attempt_end(
                        series_id=series_id,
                        attempt_number=attempt_number,
                        outcome=outcome,
                        settlement=settlement,
                        duration_ms=duration_ms,
                        error=terminal,
                        retry_scheduled=False,
                    )
                raise
            if ledger is not None:
                try:
                    ledger.record_attempt_outcome_sync(
                        series_id,
                        attempt_number,
                        AttemptStatus.FAILED,
                        error=AttemptError.from_exception(step.underlying),
                        retry_not_before=step.decision.retry_not_before,
                        sampled_delay=step.decision.sampled_delay,
                    )
                except Exception as persistence_error:
                    _tag_persistence_failure(persistence_error)
                    raise
            if events is not None:
                events.attempt_end(
                    series_id=series_id,
                    attempt_number=attempt_number,
                    outcome=outcome,
                    settlement=settlement,
                    duration_ms=duration_ms,
                    error=step.underlying,
                    retry_scheduled=True,
                    retry_not_before=step.decision.retry_not_before,
                )
            _sleep_sync(step.decision.sampled_delay)
        else:
            if events is not None:
                events.attempt_end(
                    series_id=series_id,
                    attempt_number=attempt_number,
                    outcome="succeeded",
                    settlement="returned",
                    duration_ms=(time.perf_counter() - attempt_started) * 1000,
                )
            return result
        # BaseException control flow is deliberately not caught: it passes
        # through untouched (no fabricated end event) and the reservation
        # stays for resume semantics.
        if deadline_at is not None and _utcnow() >= deadline_at:
            # Post-sleep freshness: the wait itself may have outlived the
            # window. In-process the exact last underlying exception is
            # re-raised; the ledger's begin_attempt re-verifies atomically as
            # the durable backstop.
            attach_attempt_diagnostic(
                step.underlying,
                _terminal_hint("HG_RETRY_EXHAUSTED", policy, attempt_number, limit="retry_window"),
            )
            raise step.underlying


async def run_attempts_async(
    invoke: Callable[[], Awaitable[Any]],
    *,
    node_name: str,
    policy: RetryPolicy | None,
    timeout: float | None = None,
    checkpointer: Any | None,
    run_id: str | None,
    scheduled_superstep: int,
    attempt_scope: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
    events: AttemptEventSink | None = None,
) -> Any:
    """Drive one logical node execution as a series of attempts (async).

    ``attempt_scope`` scopes ONE in-flight invocation (the concurrency
    permit, per #218): it is entered before and exited after each attempt,
    so backoff sleeps never hold a permit.
    """
    policy = policy or _TIMEOUT_ONLY_POLICY
    now = _utcnow()
    if checkpointer is not None and run_id:
        ledger: Checkpointer | None = checkpointer
        plan = await _open_or_resume_async(ledger, run_id, node_name, policy, now)
        wait = _pre_loop_wait(plan, policy, now)
        if wait is not None:
            await _sleep_async(wait)
        series_id, deadline_at, attempt_number = plan.series_id, plan.deadline_at, plan.consumed
    else:
        ledger, series_id = None, _new_attempt_series_id()
        deadline_at, attempt_number = _series_deadline(policy, now), 0

    while True:
        if ledger is not None:
            try:
                record = await ledger.begin_attempt(
                    series_id,
                    policy_fingerprint=policy.fingerprint,
                    scheduled_superstep=scheduled_superstep,
                )
            except AttemptLedgerError as backstop:
                # The atomic durable backstop refused the reservation
                # (budget/deadline re-verification lost a race).
                attach_attempt_diagnostic(
                    backstop,
                    _terminal_hint("HG_RETRY_EXHAUSTED", policy, attempt_number),
                )
                raise
            except Exception as persistence_error:
                _tag_persistence_failure(persistence_error)
                raise
            attempt_number = record.attempt_number
        else:
            attempt_number += 1

        attempt_now = _utcnow()
        deadline = _active_deadline(
            timeout,
            policy,
            deadline_at,
            now=attempt_now,
        )
        witness = _DeadlineWitness()
        if events is not None:
            events.attempt_start(
                series_id=series_id,
                attempt_number=attempt_number,
                max_attempts=policy.max_attempts,
                timeout_seconds=timeout,
                attempt_deadline_at=(attempt_now + timedelta(seconds=timeout) if timeout is not None else None),
                series_deadline_at=deadline_at,
            )

        async def record_deadline(
            current_attempt_number: int = attempt_number,
        ) -> None:
            if ledger is not None:
                assert series_id is not None
                await ledger.record_attempt_deadline(series_id, current_attempt_number)

        async def invoke_attempt(
            current_deadline: _ActiveDeadline | None = deadline,
            current_witness: _DeadlineWitness = witness,
        ) -> Any:
            return await _invoke_with_deadline(
                invoke,
                node_name=node_name,
                deadline=current_deadline,
                record_deadline=record_deadline if ledger is not None else None,
                witness=current_witness,
            )

        attempt_started = time.perf_counter()
        try:
            if attempt_scope is None:
                result = await invoke_attempt()
            else:
                async with attempt_scope():
                    result = await invoke_attempt()
        except Exception as error:
            duration_ms = (time.perf_counter() - attempt_started) * 1000
            outcome, settlement = _failure_outcome(error)
            # Terminal paths raise the exact underlying exception from here.
            # The reservation intentionally stays STARTED: the step-save site
            # settles it atomically with the linked StepRecord.
            try:
                step = plan_after_failure(error, policy, attempt_number, deadline_at, now=_utcnow())
            except Exception as terminal:
                if witness.deadline_elapsed:
                    # Cancellation-cleanup precedence: the terminal cause keeps
                    # its own type/code while the deadline flags survive.
                    merge_deadline_evidence(
                        terminal,
                        deadline_elapsed=witness.deadline_elapsed,
                        cancellation_requested=witness.cancellation_requested,
                    )
                if events is not None:
                    events.attempt_end(
                        series_id=series_id,
                        attempt_number=attempt_number,
                        outcome=outcome,
                        settlement=settlement,
                        duration_ms=duration_ms,
                        deadline_scope=witness.event_scope,
                        deadline_elapsed=witness.deadline_elapsed,
                        cancellation_requested=witness.cancellation_requested,
                        error=terminal,
                        retry_scheduled=False,
                    )
                raise
            if ledger is not None:
                status = AttemptStatus.TIMED_OUT if isinstance(error, (AttemptTimeoutError, RetryWindowExpiredError)) else AttemptStatus.FAILED
                try:
                    await ledger.record_attempt_outcome(
                        series_id,
                        attempt_number,
                        status,
                        error=AttemptError.from_exception(step.underlying),
                        retry_not_before=step.decision.retry_not_before,
                        sampled_delay=step.decision.sampled_delay,
                    )
                except Exception as persistence_error:
                    _tag_persistence_failure(persistence_error)
                    raise
            if events is not None:
                events.attempt_end(
                    series_id=series_id,
                    attempt_number=attempt_number,
                    outcome=outcome,
                    settlement=settlement,
                    duration_ms=duration_ms,
                    deadline_scope=witness.event_scope,
                    deadline_elapsed=witness.deadline_elapsed,
                    cancellation_requested=witness.cancellation_requested,
                    error=step.underlying,
                    retry_scheduled=True,
                    retry_not_before=step.decision.retry_not_before,
                )
            await _sleep_async(step.decision.sampled_delay)
        else:
            if events is not None:
                # A suppressed cancellation that returned a real value is a
                # success recorded WITH its deadline evidence, never discarded.
                events.attempt_end(
                    series_id=series_id,
                    attempt_number=attempt_number,
                    outcome="succeeded",
                    settlement="returned",
                    duration_ms=(time.perf_counter() - attempt_started) * 1000,
                    deadline_scope=witness.event_scope,
                    deadline_elapsed=witness.deadline_elapsed,
                    cancellation_requested=witness.cancellation_requested,
                )
            return result
        # BaseException control flow is deliberately not caught: it passes
        # through untouched (no fabricated end event) and the reservation
        # stays for resume semantics.
        if deadline_at is not None and _utcnow() >= deadline_at:
            # Post-sleep freshness: the wait itself may have outlived the
            # window. In-process the exact last underlying exception is
            # re-raised; the ledger's begin_attempt re-verifies atomically as
            # the durable backstop.
            attach_attempt_diagnostic(
                step.underlying,
                _terminal_hint("HG_RETRY_EXHAUSTED", policy, attempt_number, limit="retry_window"),
            )
            raise step.underlying


# === Atomic close at the step-save boundary ===

_CLOSE_STATUS: dict[StepStatus, AttemptStatus] = {
    StepStatus.COMPLETED: AttemptStatus.SUCCEEDED,
    StepStatus.FAILED: AttemptStatus.FAILED,
}


def _attempt_managed_function_node(graph: Graph, node_name: str) -> bool:
    from hypergraph.nodes.function import FunctionNode

    node = graph._nodes.get(node_name)
    return isinstance(node, FunctionNode) and (node.retry is not None or node.timeout is not None)


_LINKABLE_TERMINAL = frozenset(
    {
        AttemptStatus.FAILED,
        AttemptStatus.TIMED_OUT,
        AttemptStatus.OUTCOME_UNKNOWN,
    }
)


def _close_args(
    record: StepRecord,
    series: AttemptSeries,
    records: list[AttemptRecord],
    node_errors: dict[str, BaseException] | None,
) -> tuple[int, AttemptStatus, AttemptError | None, StepRecord] | None:
    """Pure close decision: (attempt_number, status, error, linked record)."""
    status = _CLOSE_STATUS.get(record.status)
    if status is None or not records:
        return None
    last = records[-1]
    linked = replace(record, attempt_series_id=series.id)
    if last.status is AttemptStatus.STARTED:
        # Ordinary close: settle the live reservation with this step's outcome.
        error = None
        raw = (node_errors or {}).get(record.node_name)
        if status is AttemptStatus.FAILED and isinstance(
            raw,
            (AttemptTimeoutError, RetryWindowExpiredError),
        ):
            status = AttemptStatus.TIMED_OUT
        if status in (AttemptStatus.FAILED, AttemptStatus.TIMED_OUT) and isinstance(raw, Exception):
            error = AttemptError.from_exception(raw)
        return last.attempt_number, status, error, linked
    if status is AttemptStatus.FAILED and last.status in _LINKABLE_TERMINAL:
        # Resume dead end (budget exhausted, window expired, OUTCOME_UNKNOWN
        # evidence): the logical step failed FROM already-terminal durable
        # evidence — link it and close without rewriting that evidence.
        return last.attempt_number, last.status, None, linked
    return None


def maybe_close_attempt_series_sync(
    checkpointer: Any,
    graph: Graph,
    record: StepRecord,
    node_errors: dict[str, BaseException] | None = None,
) -> bool:
    """Close the node's open attempt series atomically with this StepRecord.

    Returns True when the record was persisted BY the close (the caller must
    not save it again); False when the record is not series-linked and should
    follow the ordinary durability dispatch.
    """
    if not _attempt_managed_function_node(graph, record.node_name):
        return False
    if not isinstance(checkpointer, SyncAttemptLedger):
        return False
    series = checkpointer.get_open_attempt_series_sync(record.run_id, record.node_name)
    if series is None:
        return False
    records = checkpointer.get_attempt_records_sync(series.id)
    args = _close_args(record, series, records, node_errors)
    if args is None:
        return False
    attempt_number, status, error, linked = args
    checkpointer.close_attempt_series_sync(series.id, attempt_number, status, step_record=linked, error=error)
    return True


async def maybe_close_attempt_series_async(
    checkpointer: Checkpointer,
    graph: Graph,
    record: StepRecord,
    node_errors: dict[str, BaseException] | None = None,
) -> bool:
    """Async twin of :func:`maybe_close_attempt_series_sync`."""
    if not _attempt_managed_function_node(graph, record.node_name):
        return False
    series = await checkpointer.get_open_attempt_series(record.run_id, record.node_name)
    if series is None:
        return False
    records = await checkpointer.get_attempt_records(series.id)
    args = _close_args(record, series, records, node_errors)
    if args is None:
        return False
    attempt_number, status, error, linked = args
    await checkpointer.close_attempt_series(series.id, attempt_number, status, step_record=linked, error=error)
    return True
