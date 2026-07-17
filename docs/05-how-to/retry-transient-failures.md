# How to Retry Transient Failures

External calls fail transiently — a model API times out, a rate limit trips, a connection drops. Declare on the node which failures are safe to repeat, and hypergraph runs the attempts for you: one logical step, capped-exponential backoff, and (with a checkpointer) a retry budget that survives restarts.

## Before / After

Before — one transient failure kills the run:

```python
from hypergraph import Graph, SyncRunner, node

@node(output_name="profile")
def fetch_profile(user_id: str) -> dict:
    return api.get_profile(user_id)   # raises ConnectionError once in a while

runner = SyncRunner()
runner.run(Graph([fetch_profile]), {"user_id": "u-42"})
# ConnectionError: connection reset by peer
```

After — the node declares which failures may repeat:

```python
from hypergraph import Graph, RetryPolicy, SyncRunner, node

@node(
    output_name="profile",
    retry=RetryPolicy(
        max_attempts=3,                    # includes the first invocation
        retry_on=(ConnectionError,),       # ONLY these exceptions retry
    ),
)
def fetch_profile(user_id: str) -> dict:
    return api.get_profile(user_id)

runner = SyncRunner()
result = runner.run(Graph([fetch_profile]), {"user_id": "u-42"})
# attempt 1 · ConnectionError → wait → attempt 2 · succeeded
result["profile"]
```

The graph still sees **one** `fetch_profile` step. Downstream nodes run once, after the final success. If all three attempts fail, the exact final `ConnectionError` is re-raised — never a wrapper.

## Runnable example

```python
from hypergraph import Graph, RetryPolicy, SyncRunner, node

attempts = []

@node(
    output_name="fetched",
    retry=RetryPolicy(
        max_attempts=3,
        retry_on=(ConnectionError,),
        initial_delay=0.01,   # keep the example fast
    ),
)
def flaky(x: int) -> int:
    attempts.append(x)
    if len(attempts) < 2:
        raise ConnectionError("transient")
    return x * 10

@node(output_name="done")
def downstream(fetched: int) -> int:
    return fetched + 1

result = SyncRunner().run(Graph([flaky, downstream]), {"x": 4})
print(result["done"])     # 41
print(len(attempts))      # 2 — one failure, one success, one logical step
```

## Only listed failures retry

`retry_on` is a required, explicit allowlist. A `KeyError` from broken parsing is a bug, not a transient condition — it fails once, immediately:

```python
@node(
    output_name="fetched",
    retry=RetryPolicy(max_attempts=3, retry_on=(ConnectionError,)),
)
def parse_response(raw: str) -> dict:
    return {"value": raw["field"]}   # TypeError — NOT in retry_on

# → exactly one invocation; the TypeError escapes unchanged.
```

There is no `retry=True` shorthand and no retry-all default. `KeyboardInterrupt`, cancellation, and other `BaseException` control flow are never eligible, even if you try to list them — the policy rejects them at construction, before anything runs.

## Backoff

Delays grow exponentially and are jittered by default:

```python
RetryPolicy(
    max_attempts=5,
    retry_on=(ConnectionError,),
    initial_delay=1.0,        # nominal delay after the 1st failure
    backoff_multiplier=2.0,   # 1s, 2s, 4s, ... nominal
    max_delay=60.0,           # nominal never exceeds this
    jitter="full",            # sleep uniform in [0, nominal]; "none" = exact nominal
)
```

## Bound the whole series with `retry_window`

`retry_window` (seconds) caps one attempt series with a single absolute deadline, fixed when the series opens. Backoff waits, persistence overhead, and even process downtime consume it. It combines with `max_attempts` as independent OR limits — whichever ends first, ends the series:

```python
RetryPolicy(
    max_attempts=5,
    retry_on=(ConnectionError,),
    retry_window=45,   # no attempt may start 45s after the series opened
)
```

## Honor a server's Retry-After

When a response tells you exactly how long to wait, raise `RetryAfterError` around the real failure instead of sleeping inside your node:

```python
from hypergraph import RetryAfterError

@node(
    output_name="response",
    retry=RetryPolicy(max_attempts=5, retry_on=(RateLimited,)),
)
def send(message: str) -> str:
    try:
        return client.send(message)
    except RateLimited as error:
        raise RetryAfterError(error, retry_after=30) from error
```

The carrier never authorizes anything: eligibility is still decided by `retry_on` against the underlying `RateLimited`. When a retry may start, the 30s is honored exactly (no jitter, no `max_delay` cap). When it may not — ineligible type, exhausted budget, or a wait that cannot end before the `retry_window` deadline — the exact underlying exception is re-raised without sleeping.

## Durable budgets across restarts

Budget durability follows your checkpointer:

```python
from hypergraph import SyncRunner
from hypergraph.checkpointers import SqliteCheckpointer

runner = SyncRunner(checkpointer=SqliteCheckpointer("runs.db"))
runner.run(graph, {"user_id": "u-42"}, workflow_id="onboard-u-42")
```

| Setup | `max_attempts` promise |
|-------|------------------------|
| No checkpointer | Process-local: each run gets a fresh budget |
| `MemoryCheckpointer` | Survives in-process resume, not process exit |
| `SqliteCheckpointer` + `workflow_id` | Hard cap across crash and restart |

With persistence, every attempt is durably reserved **before** your code runs, and each failure persists its sampled backoff plus the absolute wake-up time. Re-running the same `workflow_id` after a crash continues the same series: consumed attempts stay consumed, and a pending backoff wait resumes from the persisted wake time — it is never redrawn and never restarted. Attempt history is inspectable per step in the checkpointer's attempt ledger.

This is an at-most-N-invocations guarantee, not exactly-once side effects: a crash mid-attempt cannot know whether the external call landed (the attempt is recorded as outcome-unknown). Payment-style APIs still need idempotency keys.

Retry evidence deliberately overrides `CheckpointPolicy` durability timing: a retrying node's attempt records AND its series-closing StepRecord write through immediately under every durability mode (including `"async"` and `"exit"`), because the final outcome, its linked step, and the series closure must commit atomically. Non-retrying nodes buffer according to the configured durability as usual.

## What retry does NOT change

- **Cache**: one lookup before any attempt; a hit invokes nothing and consumes no budget; one write after final success. Changing the policy never invalidates cached results.
- **Events and progress**: one `NodeStartEvent` and one `NodeEndEvent`/`NodeErrorEvent` per logical node, regardless of attempts.
- **Direct calls**: `fetch_profile("u-42")` and `fetch_profile.func("u-42")` invoke exactly once — retry runs only inside a runner.
- **Map fan-out**: each `.map()` item owns its own series and budget; one item's failures never consume another's.
- **Other node types**: gates, interrupts, and nested-graph (GraphNode) boundaries are not retryable; a FunctionNode inside a nested graph carries its own declaration.

## API reference

See [RetryPolicy](../06-api-reference/nodes.md#retrypolicy) and [RetryAfterError](../06-api-reference/nodes.md#retryaftererror).
