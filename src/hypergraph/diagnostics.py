"""Typed diagnostics for terminal execution failures (#233).

The privacy boundary locked on #187: local object surfaces (the raised
exception, ``RunResult.error``, ``FailureEvidence.error``) keep the exact
exception object, while **durable** records — checkpoints, StepRecord,
attempt-ledger rows, RunLog, ``RunResult.to_dict()`` — receive only a safe
:class:`Diagnostic` projection: stable codes, exception type names, node
identity, counts/timing, booleans, and static help. Raw inputs, response
bodies, exception arguments, stack traces, and arbitrary ``repr`` never enter
a durable record.

In-memory events additionally carry :class:`ErrorDetail`, the *unredacted*
counterpart (message, type, traceback), for live consumers that must be able
to debug a failure — chiefly the OpenTelemetry export, which follows the
ecosystem norm of ``Span.record_exception`` and carries the real message by
default. ``OpenTelemetryProcessor(redact_errors=True)`` exports the safe
projection instead. ``ErrorDetail`` is never persisted.

Codes and context field meanings are stable; human wording and additive
fields may evolve. The wire form carries ``schema="hypergraph.diagnostic/v1"``.

This module is intentionally a leaf: it imports nothing from the rest of
hypergraph so events, checkpointers, and runners can all project through it.
"""

from __future__ import annotations

import traceback as _traceback
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, Literal

DIAGNOSTIC_WIRE_SCHEMA = "hypergraph.diagnostic/v1"

_ERRORS_DOC = "docs/06-api-reference/errors.md"

#: The stable code registry: code -> docs_ref anchor in the errors reference.
DIAGNOSTIC_CODES: dict[str, str] = {
    "HG_NODE_FAILED": f"{_ERRORS_DOC}#hg-node-failed",
    "HG_RETRY_POLICY_INVALID": f"{_ERRORS_DOC}#hg-retry-policy-invalid",
    "HG_TIMEOUT_UNSUPPORTED": f"{_ERRORS_DOC}#hg-timeout-unsupported",
    "HG_ATTEMPT_TIMEOUT": f"{_ERRORS_DOC}#hg-attempt-timeout",
    "HG_RETRY_EXHAUSTED": f"{_ERRORS_DOC}#hg-retry-exhausted",
    "HG_RETRY_WINDOW_EXPIRED": f"{_ERRORS_DOC}#hg-retry-window-expired",
    "HG_ATTEMPT_OUTCOME_UNKNOWN": f"{_ERRORS_DOC}#hg-attempt-outcome-unknown",
    "HG_RETRY_POLICY_CHANGED": f"{_ERRORS_DOC}#hg-retry-policy-changed",
    "HG_ATTEMPT_PERSISTENCE_FAILED": f"{_ERRORS_DOC}#hg-attempt-persistence-failed",
    "HG_RUNNER_POLICY_UNSUPPORTED": f"{_ERRORS_DOC}#hg-runner-policy-unsupported",
}


def qualified_type_name(exc_type: type[BaseException]) -> str:
    """Canonical ``module.qualname`` exception name (builtins stay bare).

    Mirrors the ledger's ``AttemptError.type_name`` convention so every safe
    projection names exception types identically.
    """
    if exc_type.__module__ in ("builtins", "__main__"):
        return exc_type.__qualname__
    return f"{exc_type.__module__}.{exc_type.__qualname__}"


@dataclass(frozen=True)
class DiagnosticLocation:
    """Where the diagnosed failure happened. Node identity only — no values."""

    node_name: str | None = None
    graph_name: str | None = None
    superstep: int | None = None
    item_index: int | None = None
    workflow_id: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "node_name": self.node_name,
            "graph_name": self.graph_name,
            "superstep": self.superstep,
            "item_index": self.item_index,
            "workflow_id": self.workflow_id,
        }


@dataclass(frozen=True)
class DiagnosticContext:
    """Closed, typed, privacy-safe context facts.

    Field meanings are stable. ``deadline_elapsed`` and
    ``cancellation_requested`` are independent witnessed facts; no field ever
    claims that arbitrary user work or external side effects stopped.
    """

    error_type: str | None = None
    attempt_count: int | None = None
    max_attempts: int | None = None
    limit: Literal["max_attempts", "retry_window"] | None = None
    timeout_seconds: float | None = None
    retry_window_seconds: float | None = None
    deadline_elapsed: bool | None = None
    cancellation_requested: bool | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "limit": self.limit,
            "timeout_seconds": self.timeout_seconds,
            "retry_window_seconds": self.retry_window_seconds,
            "deadline_elapsed": self.deadline_elapsed,
            "cancellation_requested": self.cancellation_requested,
        }


@dataclass(frozen=True)
class DiagnosticFix:
    """One static, actionable remediation step."""

    description: str


@dataclass(frozen=True)
class Diagnostic:
    """Stable, privacy-safe companion projection for a terminal failure.

    ``code`` and the ``context`` field meanings are stable; ``problem`` and
    ``how_to_fix`` wording may evolve. ``docs_ref`` anchors into the code
    registry in ``docs/06-api-reference/errors.md``.
    """

    code: str
    severity: Literal["error", "warning"]
    problem: str
    location: DiagnosticLocation
    context: DiagnosticContext
    how_to_fix: tuple[DiagnosticFix, ...]
    docs_ref: str

    def to_wire(self) -> dict[str, Any]:
        """The ``hypergraph.diagnostic/v1`` wire form. Additive evolution only."""
        return {
            "schema": DIAGNOSTIC_WIRE_SCHEMA,
            "code": self.code,
            "severity": self.severity,
            "problem": self.problem,
            "location": self.location.to_wire(),
            "context": self.context.to_wire(),
            "how_to_fix": [fix.description for fix in self.how_to_fix],
            "docs_ref": self.docs_ref,
        }


# ---------------------------------------------------------------------------
# Terminal-cause attachment (runner internals -> diagnostics)
# ---------------------------------------------------------------------------

_HINT_ATTR = "_hypergraph_attempt_diagnostic"


@dataclass(frozen=True)
class AttemptDiagnosticHint:
    """Facts only the attempt coordinator knows at its terminal raise sites.

    Attached to the exact escaping exception object (the object itself is
    never wrapped or replaced) so evidence built later can distinguish
    ineligible from exhausted failures and carry attempt counts and deadline
    flags. ``code`` never overrides a framework exception's own stable code.
    """

    code: str | None = None
    attempt_count: int | None = None
    max_attempts: int | None = None
    limit: Literal["max_attempts", "retry_window"] | None = None
    deadline_elapsed: bool | None = None
    cancellation_requested: bool | None = None


def attach_attempt_diagnostic(error: BaseException, hint: AttemptDiagnosticHint) -> None:
    """Attach coordinator facts to the exact exception object (best-effort).

    Exceptions with ``__slots__`` and no ``__dict__`` simply keep their
    type-derived diagnostic — attachment must never mask the real failure.
    """
    # Slotted exceptions without __dict__ keep their type-derived diagnostic.
    with suppress(AttributeError, TypeError):
        error.__dict__[_HINT_ATTR] = hint


def get_attempt_diagnostic(error: BaseException) -> AttemptDiagnosticHint | None:
    """Return the attached coordinator facts, if any."""
    try:
        hint = error.__dict__.get(_HINT_ATTR)
    except AttributeError:  # pragma: no cover - slotted exceptions
        return None
    return hint if isinstance(hint, AttemptDiagnosticHint) else None


def merge_deadline_evidence(
    error: BaseException,
    *,
    deadline_elapsed: bool,
    cancellation_requested: bool,
) -> None:
    """Record witnessed deadline facts on an escaping terminal exception.

    Used for the locked cancellation-cleanup precedence row: the cleanup
    exception's own type decides the code while the deadline flags survive.
    """
    hint = get_attempt_diagnostic(error) or AttemptDiagnosticHint()
    attach_attempt_diagnostic(
        error,
        replace(
            hint,
            deadline_elapsed=deadline_elapsed,
            cancellation_requested=cancellation_requested,
        ),
    )


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def _problem_for(code: str, error: BaseException, node_name: str | None) -> str:
    type_name = qualified_type_name(type(error))
    where = f"Node {node_name!r}" if node_name else "A node"
    problems = {
        "HG_NODE_FAILED": f"{where} raised {type_name}.",
        "HG_RETRY_EXHAUSTED": (f"{where} raised {type_name} and no further retry attempt may start."),
        "HG_ATTEMPT_TIMEOUT": (f"{where} exceeded its per-attempt timeout; cancellation was requested and the async callable settled cancelled."),
        "HG_RETRY_WINDOW_EXPIRED": (
            f"{where} exceeded its retry window during active work; cancellation was requested and the async callable settled cancelled."
        ),
        "HG_ATTEMPT_OUTCOME_UNKNOWN": (
            f"{where} has a durably reserved attempt whose outcome was lost with its process; external side effects may have completed."
        ),
        "HG_RETRY_POLICY_CHANGED": ("The node retry/timeout policy changed for an existing workflow lineage."),
        "HG_ATTEMPT_PERSISTENCE_FAILED": (f"Persisting the durable attempt record for {where.lower()} failed before user code could (re)start."),
        "HG_RUNNER_POLICY_UNSUPPORTED": ("The selected runner cannot execute this node's declared retry/timeout policy."),
        "HG_TIMEOUT_UNSUPPORTED": (f"{where} declares a timeout this runner/callable cannot enforce cooperatively."),
        "HG_RETRY_POLICY_INVALID": ("The declared RetryPolicy is invalid."),
    }
    return problems[code]


_FIXES: dict[str, tuple[str, ...]] = {
    "HG_NODE_FAILED": ("Inspect the exact exception locally via RunResult.error or get_failure_evidence(error).",),
    "HG_RETRY_EXHAUSTED": (
        "Inspect the exact final exception locally via RunResult.error or get_failure_evidence(error).",
        "Fork or start a new workflow to grant a fresh retry budget.",
    ),
    "HG_ATTEMPT_TIMEOUT": (
        "Raise the node's timeout, or make the callable settle faster under cancellation.",
        "List the timeout type in retry_on if a timed-out attempt should retry.",
    ),
    "HG_RETRY_WINDOW_EXPIRED": ("Raise retry_window, or reduce per-attempt latency and backoff so attempts fit the window.",),
    "HG_ATTEMPT_OUTCOME_UNKNOWN": (
        "Reconcile external side effects first, then resume to retry with the remaining budget, or fork for a fresh budget.",
    ),
    "HG_RETRY_POLICY_CHANGED": ("Resume with the original policy, or adopt the new policy on a fresh lineage (fork or new workflow_id).",),
    "HG_ATTEMPT_PERSISTENCE_FAILED": ("Restore checkpointer storage health, then resume; the reservation write-through gate ran before user code.",),
    "HG_RUNNER_POLICY_UNSUPPORTED": ("Run policy-bearing nodes on SyncRunner/AsyncRunner, or use the runner's native options where offered.",),
    "HG_TIMEOUT_UNSUPPORTED": ("Make the node async and await a cancellation-aware client, or configure the client library's own request timeout.",),
    "HG_RETRY_POLICY_INVALID": ("Follow the RetryPolicy constructor error: explicit retry_on Exception types and positive finite timing fields.",),
}


def derive_diagnostic(
    error: BaseException,
    *,
    node_name: str | None = None,
    graph_name: str | None = None,
    superstep: int | None = None,
    item_index: int | None = None,
    workflow_id: str | None = None,
) -> Diagnostic:
    """Project a terminal exception into its stable, privacy-safe Diagnostic.

    Precedence: a framework exception's own stable ``code`` attribute wins;
    otherwise the coordinator-attached hint decides; otherwise the failure is
    an ordinary ``HG_NODE_FAILED``. The hint's context facts (attempt counts,
    limit, deadline flags) merge in regardless of where the code came from.
    """
    hint = get_attempt_diagnostic(error)
    own_code = getattr(error, "code", None)
    code = own_code if own_code in DIAGNOSTIC_CODES else None
    if code is None and hint is not None and hint.code in DIAGNOSTIC_CODES:
        code = hint.code
    if code is None:
        code = "HG_NODE_FAILED"

    timeout_seconds = getattr(error, "timeout_seconds", None)
    retry_window_seconds = getattr(error, "retry_window_seconds", None)
    settled_cancelled = code in ("HG_ATTEMPT_TIMEOUT", "HG_RETRY_WINDOW_EXPIRED")
    context = DiagnosticContext(
        error_type=qualified_type_name(type(error)),
        attempt_count=hint.attempt_count if hint else None,
        max_attempts=hint.max_attempts if hint else None,
        limit=hint.limit if hint else None,
        timeout_seconds=timeout_seconds if isinstance(timeout_seconds, (int, float)) else None,
        retry_window_seconds=(retry_window_seconds if isinstance(retry_window_seconds, (int, float)) else None),
        deadline_elapsed=True if settled_cancelled else (hint.deadline_elapsed if hint else None),
        cancellation_requested=(True if settled_cancelled else (hint.cancellation_requested if hint else None)),
    )
    return Diagnostic(
        code=code,
        severity="error",
        problem=_problem_for(code, error, node_name),
        location=DiagnosticLocation(
            node_name=node_name,
            graph_name=graph_name,
            superstep=superstep,
            item_index=item_index,
            workflow_id=workflow_id,
        ),
        context=context,
        how_to_fix=tuple(DiagnosticFix(text) for text in _FIXES[code]),
        docs_ref=DIAGNOSTIC_CODES[code],
    )


def safe_error_text(error: BaseException, *, node_name: str | None = None) -> str:
    """The one-line safe projection durable surfaces store instead of str(e).

    Contains the exception type name, the stable code, and the static problem
    wording — never raw exception message text.
    """
    diagnostic = derive_diagnostic(error, node_name=node_name)
    return f"{qualified_type_name(type(error))} [{diagnostic.code}]: {diagnostic.problem}"


_MAX_TRACEBACK_CHARS = 16_384


@dataclass(frozen=True)
class ErrorDetail:
    """The real exception, for live surfaces that must be able to debug it.

    The deliberate counterpart to :func:`safe_error_text`: it carries exactly
    what the safe projection withholds. Rides in-memory events only
    (``NodeErrorEvent.error_detail``, ``RunEndEvent.error_detail``) and is
    never persisted — every durable Hypergraph record keeps the safe
    projection regardless of what this holds.

    Attributes:
        message: ``str(exception)`` — the raw message, verbatim.
        type_name: Canonical ``module.qualname`` exception name.
        traceback: Formatted traceback, truncated to 16 KiB, or ``None`` when
            the exception carries no traceback (never raised) or formatting
            it failed.
    """

    message: str
    type_name: str
    traceback: str | None = None


def full_error_detail(error: BaseException) -> ErrorDetail:
    """Capture the unredacted detail of an exception. Never raises."""
    try:
        message = str(error)
    except Exception:  # pragma: no cover - a __str__ that itself fails
        message = ""
    text: str | None = None
    if error.__traceback__ is not None:
        try:
            text = "".join(_traceback.format_exception(type(error), error, error.__traceback__))
        except Exception:  # pragma: no cover - defensive: formatting is best-effort
            text = None
        else:
            if len(text) > _MAX_TRACEBACK_CHARS:
                text = text[:_MAX_TRACEBACK_CHARS] + "\n... [truncated]"
    return ErrorDetail(message=message, type_name=qualified_type_name(type(error)), traceback=text)
