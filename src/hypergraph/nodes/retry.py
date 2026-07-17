"""Node-owned retry declarations: RetryPolicy and RetryAfterError.

The locked #187 contract makes retry a node declaration: only the node that
owns a side effect may make its callable repeat. The policy is a frozen value
object; the retry loop itself lives in the runner execution layer
(``hypergraph.runners._shared.attempts``). Direct ``FunctionNode`` calls stay
raw single-shot invocations regardless of policy.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

#: Fingerprint schema/algorithm tag. The random jitter sample is attempt
#: state, not policy identity, so it never enters the fingerprint.
_FINGERPRINT_SCHEMA = "hypergraph.retry/v1|capped-exponential"

_JITTER_MODES = ("full", "none")


def _qualified_type_name(exc_type: type[BaseException]) -> str:
    """Canonical module-qualified exception name (builtins stay bare)."""
    if exc_type.__module__ in ("builtins", "__main__"):
        return exc_type.__qualname__
    return f"{exc_type.__module__}.{exc_type.__qualname__}"


def _normalize_retry_on(retry_on: object) -> tuple[type[Exception], ...]:
    """Validate and normalize the eligibility allowlist to a tuple of types."""
    if isinstance(retry_on, type):
        entries: tuple[object, ...] = (retry_on,)
    elif isinstance(retry_on, Iterable) and not isinstance(retry_on, (str, bytes)):
        entries = tuple(retry_on)
    else:
        raise TypeError(f"retry_on must be an exception type or an iterable of exception types, got {retry_on!r}.")

    if not entries:
        raise ValueError(
            "retry_on must name at least one exception type.\n\n"
            "How to fix:\n"
            "  Declare exactly which transient failures are safe to repeat, e.g.\n"
            "  RetryPolicy(max_attempts=3, retry_on=(httpx.ReadTimeout,)).\n"
            "  There is no retry-all default and no retry=True shorthand."
        )

    for entry in entries:
        if not isinstance(entry, type) or not issubclass(entry, BaseException):
            raise TypeError(f"retry_on entries must be exception types, got {entry!r}.")
        if not issubclass(entry, Exception):
            raise ValueError(
                f"retry_on must not contain BaseException-family control flow: {_qualified_type_name(entry)}.\n\n"
                "How to fix:\n"
                "  KeyboardInterrupt, SystemExit, cancellation, and other BaseException\n"
                "  control flow are never retryable. List Exception subclasses only."
            )
    return entries  # type: ignore[return-value]


def _require_positive_finite(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a positive finite number, got {value!r}.")
    return number


@dataclass(frozen=True)
class RetryPolicy:
    """Frozen, node-owned retry declaration with capped-exponential backoff.

    ``max_attempts`` counts the initial invocation: ``max_attempts=3`` means at
    most three callable invocations. ``retry_on`` is a required, explicit
    allowlist of ``Exception`` subclasses — only listed failures may repeat,
    and ``BaseException`` control flow is never eligible.

    After failed one-based attempt ``n`` the nominal delay is
    ``min(max_delay, initial_delay * backoff_multiplier ** (n - 1))``;
    ``jitter="full"`` samples uniformly from ``[0, nominal]`` while
    ``jitter="none"`` uses the nominal delay directly. A
    ``backoff_multiplier`` of ``1.0`` expresses constant delay.

    ``retry_window`` (seconds) bounds one attempt series with a single
    immutable absolute deadline fixed when the series opens. Attempt
    execution, backoff, persistence overhead, and process downtime all
    consume it; it OR-combines with ``max_attempts``.

    Example:
        >>> policy = RetryPolicy(max_attempts=3, retry_on=(ConnectionError,))
        >>> policy.max_attempts
        3
    """

    max_attempts: int
    retry_on: tuple[type[Exception], ...]
    retry_window: float | None = None
    initial_delay: float = 1.0
    backoff_multiplier: float = 2.0
    max_delay: float = 60.0
    jitter: Literal["full", "none"] = "full"

    def __post_init__(self) -> None:
        object.__setattr__(self, "retry_on", _normalize_retry_on(self.retry_on))
        if not isinstance(self.max_attempts, int) or isinstance(self.max_attempts, bool) or self.max_attempts < 1:
            raise ValueError(f"max_attempts must be an integer >= 1 (it counts the initial invocation), got {self.max_attempts!r}.")
        object.__setattr__(self, "initial_delay", _require_positive_finite("initial_delay", self.initial_delay))
        object.__setattr__(self, "backoff_multiplier", _require_positive_finite("backoff_multiplier", self.backoff_multiplier))
        object.__setattr__(self, "max_delay", _require_positive_finite("max_delay", self.max_delay))
        if self.retry_window is not None:
            object.__setattr__(self, "retry_window", _require_positive_finite("retry_window", self.retry_window))
        if self.jitter not in _JITTER_MODES:
            raise ValueError(f'jitter must be "full" or "none", got {self.jitter!r}.')

    @property
    def fingerprint(self) -> str:
        """Canonical policy identity for the durable attempt ledger.

        Normalized timing fields plus a schema/algorithm tag; exception types
        as sorted module-qualified names. Equal policies share a fingerprint
        regardless of ``retry_on`` ordering.
        """
        names = ",".join(sorted(_qualified_type_name(entry) for entry in self.retry_on))
        return (
            f"{_FINGERPRINT_SCHEMA}"
            f"|max_attempts={self.max_attempts}"
            f"|retry_on={names}"
            f"|retry_window={self.retry_window!r}"
            f"|initial_delay={self.initial_delay!r}"
            f"|backoff_multiplier={self.backoff_multiplier!r}"
            f"|max_delay={self.max_delay!r}"
            f"|jitter={self.jitter}"
        )


class RetryAfterError(Exception):
    """Typed carrier for a server-supplied retry delay. Never authorizes a retry.

    Raise it around the real failure when a response carries an exact
    ``Retry-After`` delay::

        try:
            return await client.send(message)
        except RateLimited as error:
            raise RetryAfterError(error, retry_after=30) from error

    Eligibility is still decided by the node's ``retry_on`` allowlist against
    the exact underlying ``error``. When a retry may start, the server delay
    is honored exactly — no jitter, no ``max_delay`` cap — but stays bounded
    by ``max_attempts`` and ``retry_window``. When no retry may start, the
    exact underlying exception is re-raised, never this carrier.
    """

    def __init__(self, error: Exception, *, retry_after: float) -> None:
        if isinstance(error, RetryAfterError):
            raise TypeError("RetryAfterError cannot wrap another RetryAfterError; pass the underlying exception.")
        if not isinstance(error, Exception):
            raise TypeError(f"error must be an Exception instance (the real underlying failure), got {error!r}.")
        delay = float(retry_after)
        if not math.isfinite(delay) or delay < 0:
            raise ValueError(f"retry_after must be a finite delay >= 0 seconds, got {retry_after!r}.")
        self.error = error
        self.retry_after = delay
        super().__init__(f"retry after {delay}s: {error!r}")
