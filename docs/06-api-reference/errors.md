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

### RetryPolicyChangedError (HG_RETRY_POLICY_CHANGED)

```python
from hypergraph import RetryPolicyChangedError
```

Raised when a run resumes an existing `workflow_id` whose stored per-node
retry/timeout policy manifest no longer matches the graph. The rejection
happens before checkpoint restoration, before the stored run configuration
can be overwritten, and before any user code runs.

```python
class RetryPolicyChangedError(Exception):
    workflow_id: str
    changes: tuple[PolicyFieldChange, ...]  # node_name, field, stored, current
    code = "HG_RETRY_POLICY_CHANGED"
```

```python
try:
    runner.run(graph, workflow_id="onboard-u-42")
except RetryPolicyChangedError as error:
    print(error)
# Retry/timeout policy changed for workflow 'onboard-u-42' [HG_RETRY_POLICY_CHANGED].
#
# Field-level changes against the stored policy manifest:
#   charge_card.max_attempts: stored 3 -> current 5
# ...
```

Same-workflow resume continues the persisted attempt series and its
remaining budget, so the effective policy must stay identical. A deliberate
new lineage is free to change it: `fork_from=...`, `override_workflow=True`,
or a new `workflow_id`.

Policy identity is separate from graph and cache identity: a policy change
never triggers `GraphChangedError` and never invalidates cached successful
outputs. Runs recorded before the manifest existed skip this validation; the
attempt ledger still rejects a mismatched fingerprint at the next durable
attempt reservation. See
[Checkpointers — Policy Compatibility on Resume](checkpointers.md#policy-compatibility-on-resume).

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
