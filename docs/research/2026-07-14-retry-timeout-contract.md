# Retry and timeout contract against the current runner architecture

- Date: 2026-07-14
- Issue: [#187 — Lock retry and timeout semantics against current runner architecture](https://github.com/gilad-rubin/hypergraph/issues/187)
- Implementation: none; research capture only
- Measured revision: `eb74e48f38ea1d777d0997fa43bca6ecffb721f9`
- Status: all nine contract questions are locked — attempt model, cooperative-async timeout, durable crash budget, node-owned configuration, explicit retry eligibility, durable retry window, closed backoff surface, diagnostics/events, and the Daft boundary. Questions 7–9 were locked on 2026-07-17 during the maintainer's delegated autonomous session and are flagged for post-hoc review. Remaining output: implementation tickets.

> **Intent, not canon.** This note records evidence, one approved semantic core,
> and candidates for later contract tickets. It does not document a working API.
> Current behavior remains whatever code and canonical docs implement.

## Answer

**Approved:** a retrying node is one logical graph step. Its real callable
invocations are persisted in a dedicated durable attempt ledger, not as extra
`StepRecord` rows.

```text
Before the approved model:
call_model may appear as steps 4, 5, and 6
or only its final failure may survive

After the approved model:
step 4 = call_model
├─ attempt 1 · failed
├─ attempt 2 · timed out
└─ attempt 3 · succeeded
downstream scheduling, state application, and cache write happen once
```

**Measured:** none of the target public API exists yet:

```python
@node(timeout=1.0)
def charge_card(order_id: str) -> str:
    return order_id
```

```text
TypeError: node() got an unexpected keyword argument 'timeout'
```

**Approved:** framework-level timeout initially supports cooperative async
callables and rejects generic synchronous enforcement before execution.

**Approved (2026-07-17, delegated):** the closed backoff surface,
diagnostic/event/cleanup shape, and the Daft boundary are locked per the
researched candidates below. The exact storage schema for the durable attempt
series remains an implementation decision.

## Evidence labels

- **Approved** — explicitly accepted by the maintainer in #187.
- **Physical** — witnessed by executing the real runtime or fault injection.
- **Measured** — directly observed in source/current API at the revision above.
- **Proposed** — a researched candidate requiring a decision.
- **Inferred** — follows from approved or measured facts.

## Approved attempt model

**Approved:** retry attempts do not become graph steps. A dedicated ledger makes
attempt history and crash budgeting inspectable while preserving one-time graph
scheduling, state application, and cache writes.

```text
logical node execution
├─ cache lookup, once
├─ attempt 1
├─ persisted backoff
├─ attempt 2
├─ final successful output
├─ one state application + StepRecord
└─ cache write, once
```

**Inferred:** attempts do not enter state folding, step counts, baselines,
staleness, cycles, or downstream readiness. Retry catches eligible `Exception`
values, never `BaseException`; `PauseExecution` and process control flow retain
their current meaning.

**Inferred:** cache lookup stays outside the attempt loop and cache write follows
final success. A cache hit consumes zero attempts. Each map item owns a child run
and therefore its own budget.

## Retry-loop owner determined by the approved boundaries

**Inferred from the approved direct-call, step, and node-ownership decisions:**
the runner execution layer owns retry orchestration. A shared internal attempt
coordinator sits inside the sync/async FunctionNode executor path, after cache
lookup and before the one logical execution result returns to the superstep
scheduler.

```text
superstep scheduler
  -> cache lookup
  -> FunctionNode executor
       -> attempt coordinator
            -> reserve attempt
            -> invoke callable
            -> settle / persist / schedule retry
  -> one logical success or terminal failure
  -> state application + cache write
```

The FunctionNode declaration owns the policy but `FunctionNode.__call__` does
not execute it. The superstep scheduler sees no intermediate attempt as a graph
step. The exact helper/module split is an implementation detail; moving the
loop above the executor or into raw node invocation would contradict an already
approved boundary.

## Current runtime

**Measured:** `FunctionNode.__init__` and `@node` have no retry/timeout parameters
(`src/hypergraph/nodes/function.py:105-116`, `:291-300`). Direct `FunctionNode`
calls delegate to the raw function once (`:266-271`).

```python
await call_model("hello")
# Direct call remains raw: no runner, cache, event, checkpoint, retry, or timeout.
```

**Approved:** preserve that pure-function behavior after policies exist.

**Measured:** the sync executor calls `node.func` once and consumes a sync
generator
(`src/hypergraph/runners/sync/executors/function_node.py:58-74`). The async
executor also calls inline, then awaits a coroutine or consumes either generator
kind (`src/hypergraph/runners/async_/executors/function_node.py:83-104`).
Blocking sync code therefore blocks the async runner's event-loop thread.

**Measured:** `StepRecord` has no attempt identity
(`src/hypergraph/checkpointers/types.py:37-61`); `Run.config` is an untyped dictionary
(`src/hypergraph/checkpointers/types.py:103-118`). Runner constructors and
`run`, `map`, `map_iter`, `start_run`, and `start_map` expose no execution
policy.

**Measured:** cache lookup precedes the executor boundary and cache write follows
success. Definition/code/structural hashes and cache keys have no policy
fingerprint.

## Approved timeout truth: cooperative async only

### Physical witnesses

**Physical:** a worker-thread deadline returned control, but `Future.cancel()`
returned `False`; the sync function later completed and performed its side effect.

```text
May-intent claim: timeout=0.03 → function stopped
Actual:           timeout=0.03 → caller stopped waiting
                                → cancel() returned false
                                → work and side effect finished later
```

**Physical:** cancellation of a yielding async function ran cleanup, but
settlement can occur after the deadline and a coroutine can suppress cancellation.

**Inferred:** timeout evidence has four independent facts: deadline elapsed,
cancellation requested, work stopped, and cleanup completed. One `timed_out`
boolean cannot truthfully collapse them.

### Architecture constraints and official evidence

**Measured:** SyncRunner cannot portably stop arbitrary running Python. A worker
thread exposes a deadline but cannot kill running work. Python's official
[`Future.cancel()` contract](https://docs.python.org/3.10/library/concurrent.futures.html#concurrent.futures.Future.cancel)
confirms that running calls cannot be cancelled.

**Measured:** Python 3.10 lacks `asyncio.timeout`. Compatible
[`asyncio.wait_for()`](https://docs.python.org/3.10/library/asyncio-task.html#asyncio.wait_for)
requests cooperative cancellation and waits for settlement, so wall time can
exceed the configured deadline.

**Inferred:** subprocess enforcement introduces serialization, dependency,
event, cache, and checkpointer boundaries. It is not a small executor wrapper.
The same cooperative boundary appears in
[`tokio::time::timeout`](https://docs.rs/tokio/latest/tokio/time/fn.timeout.html)
and [Prefect task timeout behavior](https://docs.prefect.io/v3/how-to-guides/workflows/write-and-run#task-timeout-behavior).

### Options

| Option | Truthful promise | Consequence | Status |
|---|---|---|---|
| Cooperative async | Deadline elapsed; cancellation requested; runner waited for settlement | Reject sync callables/runners unless a backend has native capability | **Approved** |
| Advisory thread | Runner stopped waiting; work may continue | Late side effects; retries may overlap work | **Rejected initially** |
| Isolated subprocess | Worker terminated and joined | New process boundary; cleanup still not guaranteed | **Rejected initially** |

```python
@node(timeout=30)
async def call_model(prompt: str) -> str:
    return await client.generate(prompt)
```

```text
Node 'charge_card' uses synchronous Python, so timeout=30 cannot stop it safely.
Make the node async and await a cancellation-aware client, or configure the
client library's own request timeout.
```

**Approved:** unsupported timeout is an actionable validation error before user
code starts, not a warning or advisory behavior disguised as enforcement.

**Approved:** a timed-out attempt cannot overlap work whose cancellation is
still settling. Attempt evidence distinguishes deadline elapsed, cancellation
requested, and settled outcome; it never invents `work_stopped=True`.

**Approved:** a timeout enters the retry loop only when its eventual exception
type is in the node's explicit `retry_on` allowlist.

**Approved:** optional `retry_window=` bounds elapsed time for one attempt
series, including backoff and process downtime, without claiming a hard return
deadline. Cleanup-failure reporting remains open.

## Approved crash budget; storage shape still proposed

**Physical:** fault injection disproved `(run_id, superstep, node_name)` as the
attempt identity:

```text
first run: prepare@0 persisted
           good@1 persisted
           saved_late@1 ran; StepRecord write failed
resume:    saved_late runs again at superstep 2
```

The logical node moved because its successful sibling was durable.

**Inferred requirement of the approved contract:** an open sequence needs a
stable identity across scheduler and superstep drift. The exact
`attempt_series_id` storage shape below remains proposed.

```text
AttemptSeries
  id · run_id · node_name · policy_fingerprint · max_attempts
  opened_at · deadline_at? · committed_superstep?

AttemptRecord
  series_id · attempt_number · scheduled_superstep
  STARTED | FAILED | TIMED_OUT | SUCCEEDED | CANCELLED | OUTCOME_UNKNOWN
  started/completed times · bounded_error?
  retry_not_before? · sampled_delay?

StepRecord
  ... · attempt_series_id?
```

**Approved:** with a persistent checkpointer, `STARTED` writes through before
user code and consumes budget. A crash-stranded `STARTED` becomes
`OUTCOME_UNKNOWN`, because external side effects may have completed. A crash
before reservation commits consumes nothing. Without restart-durable
persistence, the budget remains process-local.

**Inferred requirement of the approved contract:** final attempt outcome,
linked `StepRecord`, series close, and retention effects commit atomically.
Otherwise “attempt succeeded” can survive without the output needed to restore
the logical step.

**Approved:** sample jitter once and persist `sampled_delay` plus
`retry_not_before`; restart neither redraws jitter nor restarts the full delay.
Open series are never pruned; closed history follows its linked `StepRecord`.

**Approved:** same-workflow resume continues the open series and remaining
budget. A deliberate new/forked workflow may open a new series.

```text
no checkpointer    → in-memory budget only
MemoryCheckpointer → in-process resume, not process exit
SqliteCheckpointer → restart-safe only if reservation writes through
```

**Approved:** attempt reservation deliberately writes through independently of
ordinary `StepRecord` durability. A reservation-persistence failure stops before
user code or another retry begins.

## Approved configuration ownership: node-only

**Approved:** retry and per-attempt timeout live on `FunctionNode` / `@node` in
the first public contract. Only a node declaration can make its callable
repeat. Runner, graph, `run`, `map`, `start_run`, and `start_map` defaults or
blanket overrides are rejected initially.

```python
@node(
    retry=RetryPolicy(
        max_attempts=3,
        retry_on=(httpx.ReadTimeout,),
    ),
    timeout=30,
)
async def call_model(prompt: str) -> str:
    return await client.generate(prompt)
```

```text
Before the rejected layered candidate:
the caller can make charge_card retry by setting a runner or call default

After the approved boundary:
charge_card repeats only if its own node declaration explicitly says so
```

Direct `FunctionNode.__call__` remains raw and single-shot. A node carries its
declaration through ordinary graphs, nested graphs, map items, and background
execution; `.with_runner()` changes the executor, not the declaration. Each map
item retains its own attempt series and budget. Gates, interrupts, and the
`GraphNode` orchestration boundary are not retryable in the first contract.

Environment-specific variants explicitly construct or immutably clone a
different node/graph. The convenience API for that operation is not locked by
this decision.

**Rejected initially:** a runner → node → call overlay and an additional graph
default layer. Both allow callers to make side-effecting functions repeat
without changing the node that owns the safety decision. There is no public
`AttemptPolicy` or `ExecutionPolicy` bundle; `ExecutionPolicy` would also
collide with the canonical **Retrieval policy** vocabulary in `CONTEXT.md`.

**Approved:** configuration has separate identities:

```text
definition/code/structural hash → graph and successful-output identity
attempt-policy fingerprint      → resume compatibility and retry budget
```

**Inferred:** a changed retry budget does not invalidate successful cache data;
a cache hit consumes zero attempts.

**Approved:** persist a canonical per-node policy manifest separately from
successful-output and graph identity. Validate same-workflow resume before
restoration and before `create_run()` can overwrite config; a new/forked
workflow may choose a new policy. A future run-level total deadline is a
separate scope and spelling, not a per-attempt timeout override.

**Proposed:** report field-level policy mismatches and use a closed typed
backoff vocabulary because arbitrary callables are not fingerprintable.

## Approved retry eligibility: explicit allowlist

**Approved:** a retry policy requires an explicit exception allowlist. It has
one typed spelling and makes repetition visible to users and AI agents:

```python
@node(
    retry=RetryPolicy(
        max_attempts=3,
        retry_on=(httpx.ReadTimeout,),
    )
)
async def fetch_profile(user_id: str) -> Profile:
    return await client.get_profile(user_id)
```

`max_attempts` counts the initial invocation. Omitting `retry_on` is invalid,
there is no `retry=True` shorthand, and a framework timeout only retries when
its eventual exception type is explicitly listed. `BaseException` control flow
remains ineligible regardless of configuration; invalid allowlist entries are
rejected before execution.

**Rejected:** retrying every ordinary `Exception` when `retry_on` is omitted,
and adding `retry=True` as retry-all shorthand. Both make programmer and
permanent errors repeat without naming that decision.

## Interaction matrix; terminal failure surface still under review

| Surface | Contract | Status |
|---|---|---|
| Cache | One lookup before the series; one write after final success; a hit opens no series | **Inferred** |
| State/staleness | Intermediate attempts never fold state, advance versions, or schedule downstream work | **Inferred** |
| Map | Each item/child run owns its own attempt series; one item's budget cannot consume another's | **Approved** |
| Nested graph | FunctionNodes carry their policy inward; the GraphNode orchestration boundary itself is not retried | **Approved** |
| `error_handling="raise"` | The series closes, then the exact terminal underlying exception escapes with companion evidence | **Approved (2026-07-17)** |
| `error_handling="continue"` | The series closes, then one failed logical result/evidence entry is collected; attempts stay in the ledger | **Approved (2026-07-17)** |
| Resume | Same workflow continues the persisted series; fork/new workflow starts a fresh series | **Approved** |
| Policy change | Field-level fingerprint mismatch rejects same-workflow resume before user code; successful cache identity is unchanged | **Approved** |
| Background execution | A node carries the same policy; handles do not introduce an override or second budget | **Approved** |

Retry/backoff is local to one Attempt series. It does not implement the map's
separate global rate-limit/concurrency fog: “no more than N external calls
across the whole graph” needs a shared admission-control contract, not another
retry-policy field.

## Framework benchmark: Inngest, Temporal, and adjacent systems

**Measured on 2026-07-14 from official documentation:** established workflow
systems split into retry-all-with-opt-out and explicit-match camps. Their
timeout guarantees depend on the execution boundary they own.

| System | Eligibility/default | Count and delay | Timeout/durability truth |
|---|---|---|---|
| [Inngest](https://www.inngest.com/docs/features/inngest-functions/error-retries/retries) | Every ordinary error retries by default; `NonRetriableError` opts out | `retries=4` means five calls; exponential backoff with some jitter; `RetryAfterError` overrides one delay | Steps persist independently, but [run timeout does not interrupt an active step](https://www.inngest.com/docs/features/inngest-functions/cancellation/cancel-on-timeouts) |
| [Temporal](https://docs.temporal.io/encyclopedia/retry-policies) | Activities retry broadly by default; Workflows do not; non-retryable types opt out | `maximum_attempts` includes the initial call; 1s × 2 exponential backoff capped at 100s by default | [Start-To-Close](https://docs.temporal.io/encyclopedia/detecting-activity-failures) bounds one attempt; Schedule-To-Close bounds the whole Activity including retries and waits |
| [Restate](https://docs.restate.dev/develop/python/durable-steps) | Ordinary failures retry; `TerminalError` opts out | `max_attempts` includes the initial call; exponential policy plus optional `max_duration` and per-error `retry_after` | Journaled run blocks survive replay; count and elapsed-time limits can be combined |
| [AWS Step Functions](https://docs.aws.amazon.com/step-functions/latest/dg/concepts-error-handling.html) | No `Retry` means no retry; `ErrorEquals` is required when retry is configured | `MaxAttempts` counts retries after the first call; exponential delay, optional cap and full jitter | `States.Timeout` is matchable; retries are durable state transitions and explicit redrive resets the retry count |
| [Prefect](https://docs.prefect.io/v3/how-to-guides/workflows/retries) | Retries default to zero; once enabled, failures retry unless a condition rejects them | `retries` excludes the initial call; constant/list/generated delays and jitter | [Async cancellation is cooperative and blocking thread work cannot be interrupted](https://docs.prefect.io/v3/how-to-guides/workflows/write-and-run) |
| [Celery](https://docs.celeryq.dev/en/stable/userguide/tasks.html) | `autoretry_for` defaults empty; classes must be named | `max_retries` excludes the initial call; exponential backoff and full jitter are optional | A [hard timeout](https://docs.celeryq.dev/en/stable/userguide/workers.html) terminates an isolated worker process and is pool/platform dependent |

**Inferred for Hypergraph:** the approved positive allowlist is not an unusual
model: AWS and Celery use it. It deliberately rejects Inngest/Temporal/Restate's
broad default because a Hypergraph `FunctionNode` is an ordinary Python
callable, not a separately declared remote Activity whose platform assumes
idempotent re-execution. The declaration is safer for side effects and more
complete for AI consumers.

**Inferred:** keep `max_attempts` as a total including the initial invocation,
matching Temporal and Restate and avoiding the off-by-one translation required
by `retries=N` APIs.

**Inferred:** Inngest's separate `steps`/`step_attempts` views and Temporal's
one Activity Execution with multiple Activity Task Executions reinforce the
approved one-logical-step plus attempt-ledger model. Attempt rows/spans need not
multiply logical node completion/error events.

## Approved retry-series window

**Approved:** borrow Temporal's dual time scopes and Restate's
count-or-duration stop model without copying their stronger-sounding timeout
language. Node `timeout=` remains per attempt; optional `retry_window=` covers
one attempt series.

```python
@node(
    timeout=10,
    retry=RetryPolicy(
        max_attempts=5,
        retry_on=(httpx.ReadTimeout,),
        retry_window=45,
    ),
)
async def call_model(prompt: str) -> str:
    return await client.generate(prompt)
```

The window starts when the series opens. Attempt execution, backoff,
persistence overhead, cancellation settlement, and process downtime consume
it. `max_attempts` and the window are independent OR limits. A persistent
checkpointer stores one immutable absolute `deadline_at`; resume never grants a
fresh window.

At or after the deadline, Hypergraph atomically refuses another `STARTED`
reservation. If active cooperative async work is still settling, cancellation
is requested and awaited, so the public call may return later. A real late
success is accepted and recorded with deadline evidence rather than discarded.
The window is not proof that work stopped and is not a hard wall-clock return
cap.

A cache hit opens no series. This is a node-local attempt-series boundary, not
a whole-graph/run deadline. Exact deadline diagnostic/error names remain open.

## Approved backoff surface

**Measured:** the comparable durable systems keep the ordinary timing formula
closed and inspectable. Temporal and Restate colocate exponential timing fields
with their retry policy; AWS Step Functions exposes direct interval,
multiplier, cap, and `FULL | NONE` jitter fields; Inngest and Restate also let
one failure supply an exact retry-after delay. None of those facts requires a
separate public strategy object in Hypergraph's first release.

**Approved (2026-07-17, delegated):** keep one capped-exponential formula
directly on the frozen `RetryPolicy`:

```python
@node(
    retry=RetryPolicy(
        max_attempts=5,
        retry_on=(httpx.ReadTimeout, RateLimited),
        initial_delay=1.0,
        backoff_multiplier=2.0,
        max_delay=60.0,
        jitter="full",
        retry_window=120,
    )
)
async def call_model(prompt: str) -> str:
    return await client.generate(prompt)
```

The proposed defaults are `1.0`, `2.0`, `60.0`, and `"full"`; `"none"` is
the only other jitter mode. A multiplier of `1.0` expresses constant delay.
There is no `Backoff` wrapper, strategy name, boolean/float jitter shorthand,
or arbitrary callable in v1. A wrapper around one algorithm would add a public
seam without hiding meaningful complexity, while a callable cannot be
canonically fingerprinted or replayed.

After failed one-based attempt `n`, the nominal cap is:

```text
min(max_delay, initial_delay * backoff_multiplier ** (n - 1))
```

Full jitter samples uniformly from zero through that cap. The policy
fingerprint includes normalized timing fields and an internal algorithm/schema
tag; exception types use module plus qualified name. The random sample is
attempt state, not policy identity. Hypergraph atomically persists nominal,
sampled, and effective delay plus `retry_not_before`, so restart never redraws
or restarts the wait.

**Approved (2026-07-17, delegated):** include one typed per-error override for
real `Retry-After` responses rather than forcing users to hide sleeping and
retry loops inside a node:

```python
try:
    return await client.send(message)
except RateLimited as error:
    raise RetryAfterError(error, retry_after=30) from error
```

The carrier never makes an error eligible. Hypergraph checks its exact
underlying error against the approved `retry_on` allowlist. The finite,
non-negative server delay is persisted exactly, is not jittered or capped down
by `max_delay`, and remains bounded by `max_attempts` and `retry_window`. If it
cannot fit before the series deadline, Hypergraph skips the pointless sleep.
On an ineligible or terminal path, it re-raises the exact underlying exception
object, not the carrier.

```text
Before: callable delay logic can change across restart and jitter can redraw.
After:  the policy is fingerprintable; the chosen wake-up time survives restart.
```

## Daft boundary

**Measured:** Daft `Options` has `max_retries` and `on_error`, but no timeout
(`src/hypergraph/runners/daft/_options.py:11-32`), and forwards them natively
(`:61-129`). `DaftRunner` has no Hypergraph events or checkpointing
(`src/hypergraph/runners/daft/runner.py:82-93`) and rejects nested runner
overrides (`:483-496`).

```python
@daft_node(max_retries=2)  # Daft-native semantics remain available.
def embed(text: str) -> list[float]: ...

@node(
    retry=RetryPolicy(
        max_attempts=3,
        retry_on=(httpx.ReadTimeout,),
    )
)
def call_model(prompt: str) -> str: ...

DaftRunner().run(Graph([call_model]))
# Proposed: actionable IncompatibleRunnerError.
```

**Approved (2026-07-17, delegated):** reject generic Hypergraph policy
recursively under Daft and reject duplicate native/generic settings. Translating
`max_attempts - 1` would still discard filters, backoff, timeout, attempt
events, fingerprints, and the ledger.

**Measured against Daft 0.7.14:** native `max_retries=2` means three calls and
retries every raised exception with Daft-owned exponential delay and jitter.
It has no exception allowlist, retry window, Hypergraph attempt events, policy
fingerprint, or checkpointer ledger. `on_error="log" | "ignore"` can convert a
failure to `None`. Those semantics remain useful only when named as
Daft-native behavior; they are not a subset of the approved Hypergraph policy.

**Physical:** nesting currently changes the meaning of the same Daft node:

```text
@daft_node(max_retries=2), lowered directly  -> 3 invocations
same node inside GraphNode                   -> 1 invocation

@daft_node(on_error="ignore"), direct        -> completes with None
same node inside GraphNode                   -> raises
```

`GraphNodeOperation` wraps the whole child graph in one Daft UDF and executes
its contents with `SyncRunner`, so Daft-native options on inner nodes are
silently ignored. The proposed boundary therefore validates the active plan
recursively before query execution: generic retry/timeout anywhere under
`DaftRunner` is rejected; Daft-native retry/error options inside a GraphNode
are rejected; mixed native/generic policy is rejected. Inactive nodes outside
the selected execution scope do not block a valid plan.

**Physical fog graduated by this investigation:**
`DaftRunner.map(error_handling="continue")` first executes the complete
columnar plan and, after any failure, reruns every item individually:

```text
inputs = [1, 2, 3]
observed calls = [1, 2, 3, 1, 2, 3]
```

An always-failing Daft node with `max_retries=2` can consequently run six times
for one logical item: three native attempts in the columnar pass plus three in
fallback. This is a separate map-error-isolation decision, not something retry
translation can make truthful. Graduate it as its own grilling/implementation
route when #187 resolves.

## Approved diagnostic, event, and warning contract (locked 2026-07-17, delegated)

**Measured:** the runner preserves and re-raises the exact final node exception;
`get_failure_evidence(error)` is its typed companion seam.

**Proposed:** preserve a final `httpx.ReadTimeout` rather than wrap it in
`RetryExhaustedError`. Attach stable diagnostics through failure evidence:

```python
try:
    runner.run(graph, values)
except httpx.ReadTimeout as error:
    failure = get_failure_evidence(error)[0]
    print(failure.diagnostic.code)
    print(failure.diagnostic.docs_ref)
```

```text
HG_RETRY_EXHAUSTED
docs/06-api-reference/errors.md#hg-retry-exhausted
```

**Proposed:** stable code/context names, evolvable human wording, additive fields,
and no raw inputs, exception arguments, bodies, stacks, or arbitrary `repr` in
durable records/telemetry. Primary analogues: [rustc diagnostics](https://rustc-dev-guide.rust-lang.org/diagnostics.html),
[rustc JSON](https://doc.rust-lang.org/nightly/rustc/json.html),
[Pydantic errors](https://docs.pydantic.dev/latest/errors/errors/), and
[OpenTelemetry exception privacy](https://opentelemetry.io/docs/specs/semconv/registry/attributes/exception/).

**Measured privacy contradiction on current master:** the default durable and
telemetry path copies raw exception text across every layer:

```text
str(exception)
  -> NodeErrorEvent.error
  -> RunLog / StepRecord
  -> RunResult.to_dict()
  -> OpenTelemetry exception.message
```

OpenTelemetry explicitly warns that `exception.message` may contain sensitive
information. Protecting only new retry events would therefore leave the
existing checkpoint/serialization/telemetry contract unsafe.

**Proposed:** preserve the exact exception object on local object surfaces--the
raised exception, `RunResult.error`, and `FailureEvidence.error`--while
projecting a typed, privacy-safe `Diagnostic` to events, checkpoints,
serialization, and telemetry:

```python
@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: Literal["error", "warning"]
    problem: str
    location: DiagnosticLocation
    context: DiagnosticContext
    how_to_fix: tuple[DiagnosticFix, ...]
    docs_ref: str
```

Its wire form carries `schema="hypergraph.diagnostic/v1"`. Codes and context
field meanings are stable; wording and additive fields may evolve. Safe
projections contain codes, exception type names, node identity, attempt
counts/timing, booleans, and static help--never raw inputs, response bodies,
exception arguments, stack traces, or arbitrary `repr`.

Candidate initial codes are `HG_NODE_FAILED`, `HG_RETRY_POLICY_INVALID`,
`HG_TIMEOUT_UNSUPPORTED`, `HG_ATTEMPT_TIMEOUT`, `HG_RETRY_EXHAUSTED`,
`HG_RETRY_WINDOW_EXPIRED`, `HG_ATTEMPT_OUTCOME_UNKNOWN`,
`HG_RETRY_POLICY_CHANGED`, `HG_ATTEMPT_PERSISTENCE_FAILED`, and
`HG_RUNNER_POLICY_UNSUPPORTED`.
If the last consumed durable attempt is `OUTCOME_UNKNOWN`, there is no witnessed
user exception to preserve; a focused `AttemptOutcomeUnknownError` instructs
the operator to reconcile external side effects before retrying or forking.

### Proposed terminal exception precedence

| Final observed condition | Local exception/result | Diagnostic |
|---|---|---|
| Ordinary ineligible or exhausted user failure | Re-raise the exact underlying exception object | `HG_NODE_FAILED` or `HG_RETRY_EXHAUSTED` |
| Retry window prevents a next reservation after a user failure | Re-raise that exact last underlying exception | `HG_RETRY_EXHAUSTED`, `limit="retry_window"` |
| Attempt timeout requests cancellation and the task settles cancelled | Raise public `AttemptTimeoutError` | `HG_ATTEMPT_TIMEOUT` |
| Retry window expires during active work and the task settles cancelled | Raise public `RetryWindowExpiredError` | `HG_RETRY_WINDOW_EXPIRED` |
| Cancellation is suppressed and the task returns | Accept the real value; record deadline/cancellation evidence | No terminal error |
| Cancellation cleanup or suppression raises another exception | Preserve that exact new exception; eligibility is based on its type | Code reflects the terminal cause, with deadline flags |
| Process loss leaves the final committed attempt without settlement | Raise `AttemptOutcomeUnknownError` on resume | `HG_ATTEMPT_OUTCOME_UNKNOWN` |
| `RetryAfterError` cannot schedule another attempt | Unwrap and re-raise its exact underlying exception | Code reflects exhaustion/ineligibility |

External cancellation and `BaseException` control flow are never converted to
retryable failures. This table is locked as written (2026-07-17, delegated),
including the three framework exception names and cancellation-cleanup
precedence.

**Proposed:** for `N` callable invocations:

```text
1 × NodeStartEvent
N × NodeAttemptStartEvent
N × NodeAttemptEndEvent
1 × NodeEndEvent OR 1 × NodeErrorEvent
```

A cache hit has zero attempt events. Intermediate failures do not close the
logical node, increment its error count, or finish progress. A timed-out attempt
has one end event whose fields distinguish deadline, cancellation request, and
settled outcome; it never invents `work_stopped=True`.

**Proposed event fields:**

```python
NodeAttemptStartEvent(
    attempt_series_id=...,
    attempt_number=2,             # one-based
    max_attempts=5,
    timeout_seconds=10,
    attempt_deadline_at=...,
    series_deadline_at=...,
)

NodeAttemptEndEvent(
    attempt_series_id=...,
    attempt_number=2,
    outcome="timed_out",          # succeeded | failed | timed_out | cancelled
    settlement="cancelled",       # returned | raised | cancelled
    deadline_scope="attempt",     # attempt | series | None
    deadline_elapsed=True,
    cancellation_requested=True,
    duration_ms=...,
    error_type="hypergraph.AttemptTimeoutError",
    retry_scheduled=True,
    retry_not_before=...,
)
```

There is no live `NodeAttemptEndEvent` fabricated after process death; the
durable record alone transitions to `OUTCOME_UNKNOWN`. OpenTelemetry initially
projects attempt start/end as events on the single logical node span. Only the
terminal escaping failure marks that node span as error.

| Situation | Proposed surface |
|---|---|
| Invalid/unsupported policy | Exception before execution |
| Retry scheduled/handled timeout | Typed event, progress, telemetry |
| Accepted-but-ignored/deprecated config | Dedicated Python warning |
| Internal best-effort subsystem failure | Logger warning |

**Proposed:** normal retry activity never uses Python warnings; `-W error` must
not change successful workflow behavior. Python's official
[`warnings` contract](https://docs.python.org/3.10/library/warnings.html)
allows warnings to be filtered, repeated once, or promoted to exceptions, so
they are the wrong transport for ordinary retry lifecycle events.

## May intent spec falsifications

`specs/reviewed/retry-timeout.md:3` labels itself design intent, not current
behavior. It is history, not runtime evidence.

| May claim | Current evidence | Disposition |
|---|---|---|
| `RetryPolicy(attempts=...)` | Approved contract uses `max_attempts`; no class exists yet | **Approved:** replace with explicit total-attempt name |
| Failed attempts are not checkpointed (`:346-379`) | Approved durable attempt ledger | **Approved:** superseded |
| Crash restarts at attempt 1 (`:362-379`) | Stable-series fault injection | **Physical:** falsified as desired behavior |
| `persist_attempts=True` is future work (`:379-388`) | Durability is approved core | **Approved:** superseded |
| Sync timeout uses signal/threading (`:401-414`) | Thread witness had late side effect | **Physical:** enforcement falsified |
| `runner.iter(...)` yields events (`:300-315`) | Current delivery is processor-based | **Measured:** stale API |
| Wrap final error in `RetryExhaustedError` (`:590-606`) | Exact exception/failure evidence exists | **Approved:** preserve and re-raise the final underlying exception |
| Callable backoff (`:105`, `:125-129`) | Not canonically durable/fingerprintable | **Inferred:** defer |
| Generic Daft retry/timeout parity (`:429-460`) | Native retry; no timeout/events/ledger | **Measured:** falsified |
| Top-level `total_timeout` hard cap (`:213-240`) | Cooperative settlement may return late | **Approved:** replace with truthful `retry_window` |
| Constructor event processors | No `event_processors=` constructor arg | **Measured:** stale API |

## Ticket boundaries and nested fog

**Proposed:** after their policy gates, implementation should split into:

1. attempt schema/checkpointer operations, migration, atomic close, retention;
2. retry executor, selection, budget, persisted backoff, cache/state boundary;
3. cooperative async timeout and unsupported-case validation;
4. policy transport, nesting, fingerprints, and resume validation;
5. diagnostics, events, progress, telemetry, privacy;
6. Daft capability validation and native retry preservation.

The measured `DaftRunner.map(error_handling="continue")` replay behavior is not
folded into item 6. It needs its own decision ticket because truthful isolation
can trade columnar performance for per-item execution or require a typed
columnar result/error envelope.

**Map requirement:** each implementation child must update and verify its
affected canonical docs,
runnable before/after examples, and documentation contract tests in the same
PR. There is no trailing documentation ticket; #189 is only the final
independent drift audit.

**Physical:** nested recovery has a separate gap:

```text
child workflow: COMPLETED
parent GraphNode StepRecord: missing
resume: WorkflowAlreadyCompletedError
```

The runner reinvokes the terminal child instead of restoring outputs and
committing the missing parent step.

**Inferred:** this is parent/child checkpoint recovery, not retry-loop behavior.
Give it a separate ticket. Until fixed, GraphNode retry semantics remain foggy;
the first retry ticket should cover `FunctionNode` attempts, not gates,
interrupts, or whole nested graphs.

## Decision sequence

1. **Approved:** one logical step plus durable attempt records.
2. **Approved:** cooperative async timeout and unsupported-sync rejection.
3. **Approved:** committed `STARTED` consumes budget and writes through.
4. **Approved:** node-owned retry/per-attempt-timeout configuration only.
5. **Approved:** explicit `retry_on`; no shorthand or retry-all default.
6. **Approved:** durable `retry_window` includes backoff and crash downtime.
7. **Approved (2026-07-17, delegated):** closed backoff/jitter and per-error delay shape.
8. **Approved (2026-07-17, delegated):** diagnostics, events, warnings, and safe durable projections.
9. **Approved (2026-07-17, delegated):** Daft capability boundary and native-option nesting.
10. **Active:** resolution, implementation tickets, and graduated fog.

Decisions 7–9 were locked during the maintainer's delegated autonomous session
and are flagged for post-hoc review; the locking comments on #187 are the
authoritative wording.

## Evidence index

- [Issue #187 and approved attempt discussion](https://github.com/gilad-rubin/hypergraph/issues/187)
- [Approved cooperative-async timeout contract](https://github.com/gilad-rubin/hypergraph/issues/187#issuecomment-4970991084)
- [Approved durable crash-budget contract](https://github.com/gilad-rubin/hypergraph/issues/187#issuecomment-4971022708)
- [Crash identity / proposed AttemptSeries](https://github.com/gilad-rubin/hypergraph/issues/187#issuecomment-4970804494)
- [Configuration ownership grilling](https://github.com/gilad-rubin/hypergraph/issues/187#issuecomment-4971042119)
- [Approved node-owned configuration](https://github.com/gilad-rubin/hypergraph/issues/187#issuecomment-4971060633)
- [Retry eligibility grilling](https://github.com/gilad-rubin/hypergraph/issues/187#issuecomment-4971093429)
- [Approved explicit retry allowlist](https://github.com/gilad-rubin/hypergraph/issues/187#issuecomment-4971178140)
- [Cross-framework retry/timeout benchmark](https://github.com/gilad-rubin/hypergraph/issues/187#issuecomment-4971233390)
- [Retry-series window grilling](https://github.com/gilad-rubin/hypergraph/issues/187#issuecomment-4971236534)
- [Approved durable retry window](https://github.com/gilad-rubin/hypergraph/issues/187#issuecomment-4973336584)
- [Backoff and per-error-delay grilling](https://github.com/gilad-rubin/hypergraph/issues/187#issuecomment-4973410021)
- [Diagnostic/event/warning candidates](https://github.com/gilad-rubin/hypergraph/issues/187#issuecomment-4970851004)
- [Configuration/Daft candidates](https://github.com/gilad-rubin/hypergraph/issues/187#issuecomment-4970853373)

**Measured:** source inspection covered FunctionNode/direct calls, sync/async
executors, cache boundaries, step/run persistence, resume hashes, map children,
nested runner overrides, Daft options/capabilities, and the May intent spec.

This capture changes no runtime or public API documentation. The adjacent
`CONTEXT.md` update records terms from approved decisions; neither file claims
that the proposed API is implemented.
