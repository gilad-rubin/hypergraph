# Errors

Hypergraph rejects unsupported execution promises before user code runs and
preserves the exact exception that actually settled an attempt. This page
covers the retry/timeout exceptions and the stable
[diagnostic code registry](#diagnostic-code-registry); runner and graph APIs
document their other validation errors next to each operation.

## The privacy boundary

Local object surfaces keep the **exact exception object**: the raised
exception, `RunResult.error`, and `FailureEvidence.error`. Durable and
telemetry surfaces — events, `RunLog`, checkpoint `StepRecord`s, attempt
ledger rows, `RunResult.to_dict()`, and OpenTelemetry export — receive only a
privacy-safe `Diagnostic` projection: stable codes, exception type names,
node identity, counts/timing, booleans, and static help. Raw inputs, response
bodies, exception arguments, stack traces, and arbitrary `repr` never enter a
durable record.

```python
try:
    runner.run(graph, values)
except httpx.ReadTimeout as error:
    failure = get_failure_evidence(error)[0]
    print(failure.error is error)          # True — the exact object, locally
    print(failure.diagnostic.code)         # HG_RETRY_EXHAUSTED
    print(failure.diagnostic.docs_ref)     # docs/06-api-reference/errors.md#hg-retry-exhausted
```

The `Diagnostic` dataclass (`code`, `severity`, `problem`, `location`,
`context`, `how_to_fix`, `docs_ref`) serializes with
`schema="hypergraph.diagnostic/v1"` via `Diagnostic.to_wire()`. Codes and
context field meanings are stable; wording and additive fields may evolve.

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

For timeout rejection, `capability == "supports_cooperative_timeout"` and
`code == "HG_TIMEOUT_UNSUPPORTED"`. The error states that Hypergraph did not
run the node and gives both fixes: make the node async with
cancellation-aware I/O, or configure the client library's own timeout.

### AttemptOutcomeUnknownError (HG_ATTEMPT_OUTCOME_UNKNOWN)

```python
from hypergraph import AttemptOutcomeUnknownError
```

Raised on same-workflow resume when the last consumed durable attempt is
`OUTCOME_UNKNOWN`: the previous process durably reserved the attempt but was
lost before its outcome was witnessed, so external side effects may have
completed. There is no witnessed user exception to preserve, and Hypergraph
refuses to silently re-run the node.

```python
class AttemptOutcomeUnknownError(Exception):
    node_name: str
    series_id: str
    attempt_number: int
    code = "HG_ATTEMPT_OUTCOME_UNKNOWN"
```

Reconcile external side effects of the unknown attempt first; then resume the
workflow again to retry, or fork / start a new workflow for a fresh attempt
series.

## Diagnostic code registry

Every terminal execution failure carries a typed, privacy-safe `Diagnostic`
on its `FailureEvidence` (`failure.diagnostic`), on `NodeErrorEvent`, and in
`RunResult.to_dict()`. `diagnostic.docs_ref` anchors into this registry.
Codes are stable; wording may evolve.

### <a id="hg-node-failed"></a>HG_NODE_FAILED

An ordinary node failure: the exception was not retry-eligible (no policy, or
its type is not in the node's `retry_on` allowlist). The exact exception is
re-raised / kept on `RunResult.error`; inspect it locally.

### <a id="hg-retry-policy-invalid"></a>HG_RETRY_POLICY_INVALID

A `RetryPolicy` declaration was rejected at construction time: an empty or
non-`Exception` `retry_on` allowlist, a non-positive count, or an invalid
timing/jitter field. Fix the declaration; nothing was executed.

### <a id="hg-timeout-unsupported"></a>HG_TIMEOUT_UNSUPPORTED

A node declares `timeout=` that the selected runner/callable cannot enforce
cooperatively (sync callables, `SyncRunner`, delegated backends without the
capability). Raised as `IncompatibleRunnerError` before user code runs.

### <a id="hg-attempt-timeout"></a>HG_ATTEMPT_TIMEOUT

An async attempt crossed its per-attempt deadline, cancellation was
requested, and the callable settled cancelled (`AttemptTimeoutError`). The
deadline is cooperative — this is evidence of a cancelled settlement, not a
claim that external work stopped.

### <a id="hg-retry-exhausted"></a>HG_RETRY_EXHAUSTED

A retry-eligible failure could not be granted another attempt. The
diagnostic's `context.limit` names the boundary that stopped it:
`"max_attempts"` (budget consumed, including across resume) or
`"retry_window"` (the persisted wake time lies at or beyond the immutable
series deadline). The exact final underlying exception is preserved locally.

### <a id="hg-retry-window-expired"></a>HG_RETRY_WINDOW_EXPIRED

The series retry window elapsed during active async work, cancellation was
requested, and the callable settled cancelled (`RetryWindowExpiredError`).

### <a id="hg-attempt-outcome-unknown"></a>HG_ATTEMPT_OUTCOME_UNKNOWN

Resume found the last consumed durable attempt in `OUTCOME_UNKNOWN`
(`AttemptOutcomeUnknownError`). Reconcile external side effects before
retrying or forking.

### <a id="hg-retry-policy-changed"></a>HG_RETRY_POLICY_CHANGED

Same-workflow resume was rejected because the effective per-node
retry/timeout policy manifest changed (`RetryPolicyChangedError`). Resume
with the original policy, or adopt the new one on a fresh lineage.

### <a id="hg-attempt-persistence-failed"></a>HG_ATTEMPT_PERSISTENCE_FAILED

Persisting a durable attempt reservation or outcome failed. The original
storage exception propagates unchanged with this code attached; the
write-through gate stops before user code or another retry begins.

### <a id="hg-runner-policy-unsupported"></a>HG_RUNNER_POLICY_UNSUPPORTED

The selected runner cannot execute a node's declared retry/timeout policy
(for example, generic Hypergraph policy under `DaftRunner`). Run
policy-bearing nodes on `SyncRunner`/`AsyncRunner`, or use the runner's
native options where offered.
