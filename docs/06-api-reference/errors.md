# Errors

Hypergraph rejects unsupported execution promises before user code runs and
preserves the exact exception that actually settled an attempt. This page
covers the retry/timeout exceptions; runner and graph APIs document their
other validation errors next to each operation.

## Retry and timeout errors

### AttemptTimeoutError

```python
from hypergraph import AttemptTimeoutError
```

Raised when an async attempt crosses its `@node(timeout=...)` deadline,
Hypergraph requests cancellation, and the callable settles cancelled.

```python
class AttemptTimeoutError(TimeoutError):
    node_name: str
    timeout_seconds: float
```

The exception means cancellation was requested and awaited. It does not claim
that external work or side effects stopped at the deadline. It enters the
retry loop only when its type (or an intentional superclass) is listed in the
node's `RetryPolicy.retry_on` allowlist.

If the callable suppresses cancellation and returns a value, Hypergraph accepts
that late success instead. If cancellation cleanup raises another exception,
that exact exception replaces `AttemptTimeoutError` and its own type decides
retry eligibility.

### RetryWindowExpiredError

```python
from hypergraph import RetryWindowExpiredError
```

Raised when `RetryPolicy.retry_window` expires during active async work,
Hypergraph requests cancellation, and the callable settles cancelled.

```python
class RetryWindowExpiredError(TimeoutError):
    node_name: str
    retry_window_seconds: float
```

The same late-value and cleanup-exception precedence as
`AttemptTimeoutError` applies. The retry window is a whole-series deadline;
attempt execution, backoff, persistence overhead, cancellation settlement,
and process downtime all consume it.

### IncompatibleRunnerError for timeout

Framework `timeout=` is rejected before execution when Hypergraph cannot make
the cooperative promise: under `SyncRunner`, on a sync function/generator
under `AsyncRunner`, or through a delegated/backend runner without native
capability.

```python
class IncompatibleRunnerError(Exception):
    node_name: str | None
    capability: str | None
```

For timeout rejection, `capability == "supports_cooperative_timeout"`. The
error states that Hypergraph did not run the node and gives both fixes: make
the node async with cancellation-aware I/O, or configure the client library's
own timeout.
