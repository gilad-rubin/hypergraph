# Retry and Timeout

**Built-in resilience with full observability.**

---

## Overview

hypergraph provides built-in retry and timeout policies that integrate with the event system and execution model. Unlike external decorator libraries (tenacity, stamina), built-in policies emit events for every attempt, making retries visible in traces and logs.

```python
from hypergraph import node, RetryPolicy

@node(
    output_name="result",
    retry=RetryPolicy(attempts=5, on=httpx.HTTPError),
    timeout=30.0,
)
async def fetch(url: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        return response.json()
```

### Why Built-in?

From [Temporal's failure handling guide](https://temporal.io/blog/failure-handling-in-practice):

> "Be cautious when doing retries within your Activity because it lengthens the needed Activity timeout. Such internal retries also **prevent users from counting failure metrics** and make it harder for users to **debug in Temporal UI** when something is wrong."

This applies equally to external decorator libraries:

| Approach | Observability | Metrics | Timeout Accuracy |
|----------|:-------------:|:-------:|:----------------:|
| External decorators (stamina, tenacity) | ❌ Invisible | ❌ Undercounted failures | ❌ Must account for internal retries |
| Built-in policies | ✅ Full events | ✅ Every attempt counted | ✅ Timeout = actual timeout |

**Industry consensus:** [Temporal](https://docs.temporal.io/encyclopedia/retry-policies), [DBOS](https://docs.dbos.dev/python/tutorials/step-tutorial), and [Prefect](https://docs.prefect.io/v3/how-to-guides/workflows/retries) all provide built-in retry at the task/step/activity level for this reason.

---

## Retry Policy

### Basic Usage

```python
from hypergraph import node, RetryPolicy
import httpx

@node(
    output_name="result",
    retry=RetryPolicy(
        attempts=5,                    # Total attempts (not retries)
        on=httpx.HTTPError,            # Exception type(s) to retry
    ),
)
async def fetch(url: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()
```

### Convenience Shorthand

For common cases, use `retry=True` with sensible defaults:

```python
@node(output_name="result", retry=True)  # Retry all exceptions, 3 attempts
async def fetch(url: str) -> dict:
    ...
```

This is equivalent to `retry=RetryPolicy(attempts=3, on=Exception)`.

### Full Configuration

```python
@node(
    output_name="result",
    retry=RetryPolicy(
        attempts=5,                    # Max total attempts
        on=[httpx.HTTPError, asyncio.TimeoutError],  # Exceptions to retry
        non_retryable=[NotFoundError, ValidationError],  # Never retry these
        backoff="exponential",         # "exponential" | "linear" | "constant" | callable
        initial_delay=1.0,             # First retry delay (seconds)
        max_delay=100.0,               # Cap on delay between retries
        backoff_multiplier=2.0,        # Exponential growth factor
        jitter=True,                   # Add randomness to prevent thundering herd
    ),
)
async def fetch(url: str) -> dict:
    ...
```

### RetryPolicy Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `attempts` | `int` | `3` | Total attempts (1 = no retries) |
| `on` | `type \| tuple[type, ...]` | `Exception` | Exception type(s) that trigger retry |
| `non_retryable` | `type \| tuple[type, ...]` | `()` | Exception type(s) that **never** retry |
| `backoff` | `str \| Callable` | `"exponential"` | Backoff strategy |
| `initial_delay` | `float` | `1.0` | Delay before first retry (seconds) |
| `max_delay` | `float` | `100.0` | Maximum delay between retries |
| `backoff_multiplier` | `float` | `2.0` | Multiplier for exponential/linear backoff |
| `jitter` | `bool \| float` | `0.25` | Randomness factor (±25% by default) |

**Defaults match [Temporal's recommendations](https://docs.temporal.io/encyclopedia/retry-policies):** 1s initial, 2.0 multiplier, 100s max.

### Backoff Strategies

```python
# Exponential (default): 0.1s, 0.2s, 0.4s, 0.8s, ...
retry=RetryPolicy(attempts=5, on=Error, backoff="exponential")

# Linear: 0.1s, 0.2s, 0.3s, 0.4s, ...
retry=RetryPolicy(attempts=5, on=Error, backoff="linear")

# Constant: 1.0s, 1.0s, 1.0s, ...
retry=RetryPolicy(attempts=5, on=Error, backoff="constant", initial_delay=1.0)

# Custom callable: receives attempt number (1-indexed), returns delay
def custom_backoff(attempt: int) -> float:
    return min(2 ** attempt, 60)  # Custom exponential

retry=RetryPolicy(attempts=5, on=Error, backoff=custom_backoff)
```

### Non-Retryable Errors

The `non_retryable` parameter is critical for **permanent failures** that should surface immediately:

```python
from hypergraph import node, RetryPolicy

# Application-level errors that won't resolve with retries
class NotFoundError(Exception): pass
class ValidationError(Exception): pass
class InsufficientFundsError(Exception): pass

@node(
    output_name="result",
    retry=RetryPolicy(
        attempts=5,
        on=Exception,  # Retry most errors
        non_retryable=[NotFoundError, ValidationError, InsufficientFundsError],
    ),
)
async def process_order(order_id: str) -> dict:
    ...
```

From [Temporal's best practices](https://temporal.io/blog/failure-handling-in-practice):

> "What would happen if [InsufficientFundsError] was retried after a second? What are the chances the customer's account actually has enough money now?"

**Evaluation order:**
1. Exception raised
2. Check `non_retryable` → if match, **fail immediately** (no retry)
3. Check `on` → if match, retry
4. If no match, fail immediately

### HTTP Status Code Handling

For HTTP APIs, use `non_retryable` with custom exception types:

```python
import httpx

class ClientError(Exception):
    """4xx errors (except 429) - client's fault, don't retry."""
    pass

@node(
    output_name="result",
    retry=RetryPolicy(
        attempts=5,
        on=httpx.HTTPError,
        non_retryable=ClientError,
    ),
)
async def fetch(url: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        if 400 <= response.status_code < 500 and response.status_code != 429:
            raise ClientError(f"Client error: {response.status_code}")
        response.raise_for_status()
        return response.json()
```

**Why this pattern?** The node decides what's retryable based on domain logic. The retry policy just needs to know the categories.

---

## Timeout Policy

### Basic Usage

```python
@node(
    output_name="result",
    timeout=30.0,  # Seconds
)
async def slow_operation(data: str) -> str:
    return await expensive_llm_call(data)
```

### With Retry

Timeout applies to **each attempt**, not total execution:

```python
@node(
    output_name="result",
    retry=RetryPolicy(attempts=3, on=[httpx.HTTPError, asyncio.TimeoutError]),
    timeout=10.0,  # 10s per attempt, up to 3 attempts = 30s max
)
async def fetch(url: str) -> dict:
    ...
```

### Total Timeout

For a hard cap on total execution time (including all retries):

```python
@node(
    output_name="result",
    retry=RetryPolicy(attempts=5, on=httpx.HTTPError),
    timeout=10.0,           # Per-attempt timeout
    total_timeout=45.0,     # Hard cap on total time
)
async def fetch(url: str) -> dict:
    ...
```

If `total_timeout` is reached, the node fails with `TotalTimeoutError` (not retried).

### Timeout Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `timeout` | `float` | `None` | Per-attempt timeout in seconds |
| `total_timeout` | `float` | `None` | Total execution timeout including retries |

---

## Events

### RetryAttemptEvent

Emitted after each failed attempt (before retry):

```python
@dataclass
class RetryAttemptEvent:
    node_name: str
    attempt: int              # 1-indexed attempt that failed
    max_attempts: int         # Total configured attempts
    error: str                # Exception message
    error_type: str           # Exception class name
    delay_ms: float           # Delay before next attempt
    elapsed_ms: float         # Total time so far
```

### TimeoutEvent

Emitted when a timeout occurs:

```python
@dataclass
class TimeoutEvent:
    node_name: str
    timeout_type: Literal["attempt", "total"]
    timeout_seconds: float
    elapsed_ms: float
    attempt: int              # Which attempt timed out
```

### Extended NodeEndEvent

`NodeEndEvent` includes retry information:

```python
@dataclass
class NodeEndEvent:
    node_name: str
    duration_ms: float
    # ... existing fields ...

    # Retry information (when retry policy is configured)
    attempts: int | None = None        # Total attempts made
    retries: int | None = None         # Failed attempts (attempts - 1 if succeeded)
    final_error: str | None = None     # Error message if all attempts failed
```

### Event Stream Example

```python
async for event in runner.iter(graph, values={"url": "https://flaky-api.com"}):
    match event:
        case NodeStartEvent(node_name="fetch"):
            print("Starting fetch...")

        case RetryAttemptEvent(node_name="fetch", attempt=n, error=e, delay_ms=d):
            print(f"  Attempt {n} failed: {e}, retrying in {d}ms...")

        case TimeoutEvent(node_name="fetch", timeout_type=t):
            print(f"  {t} timeout!")

        case NodeEndEvent(node_name="fetch", attempts=a, retries=r):
            if r:
                print(f"Fetch succeeded after {r} retries ({a} total attempts)")
            else:
                print("Fetch succeeded on first attempt")
```

### Push-Based Events

```python
class RetryLogger(TypedEventProcessor):
    def on_retry_attempt(self, event: RetryAttemptEvent) -> None:
        logger.warning(
            f"Retry {event.attempt}/{event.max_attempts} for {event.node_name}: "
            f"{event.error_type}: {event.error}"
        )

    def on_timeout(self, event: TimeoutEvent) -> None:
        logger.error(
            f"Timeout ({event.timeout_type}) in {event.node_name} "
            f"after {event.elapsed_ms}ms"
        )

runner = AsyncRunner(event_processors=[RetryLogger()])
```

---

## Interaction with Checkpointing

### Default Behavior: Final Result Only

Retries happen **before** the step is saved. The checkpointer only sees the final result:

```
Node execution with retry:
  → attempt 1: fails (HTTPError)     ← Not checkpointed
  → attempt 2: fails (HTTPError)     ← Not checkpointed
  → attempt 3: succeeds              ← Checkpointed
```

**Why?** Individual retry attempts are transient. Persisting them would:
- Bloat storage (5 attempts × N nodes × M workflows)
- Complicate recovery logic
- Not match user expectations (retries are invisible in Temporal/DBOS too)

### On Crash Recovery

Retry state is **not persisted**. On crash recovery:

```
Before crash:
  → Node A: completed (checkpointed)
  → Node B: attempt 3 of 5 in progress
  💥 CRASH

After recovery:
  → Node A: skipped (loaded from checkpoint)
  → Node B: starts fresh from attempt 1  ← Retry count resets
```

This matches Temporal and DBOS behavior. Transient retry state lives in memory only.

### Persistent Retry Count (Future Consideration)

For use cases needing durable retry limits (e.g., "max 10 attempts across any number of crashes"), a future `persist_attempts=True` option could be added:

```python
# Potential future API - not implemented yet
@node(
    output_name="result",
    retry=RetryPolicy(
        attempts=10,
        on=httpx.HTTPError,
        persist_attempts=True,  # Track attempts in checkpoint
    ),
)
async def fetch(url: str) -> dict:
    ...
```

This would require storing attempt count in `StepRecord`. Not in initial implementation.

---

## Interaction with Runners

### SyncRunner

Sync nodes with retry/timeout work as expected:

```python
@node(output_name="result", retry=RetryPolicy(attempts=3, on=IOError))
def read_file(path: str) -> str:
    return Path(path).read_text()

runner = SyncRunner()
result = runner.run(graph, values={"path": "/tmp/file.txt"})
```

Timeout for sync nodes uses `signal.alarm` (Unix) or threading (Windows).

### AsyncRunner

Full support including per-attempt timeout via `asyncio.timeout`:

```python
@node(output_name="result", retry=RetryPolicy(attempts=3, on=httpx.HTTPError), timeout=10.0)
async def fetch(url: str) -> dict:
    ...

runner = AsyncRunner()
result = await runner.run(graph, values={"url": "..."})
```

### DaftRunner

Retry and timeout work per-partition in distributed execution:

```python
runner = DaftRunner()
df = runner.map(graph, values={"urls": url_list}, map_over="urls")
# Each URL processed independently with its own retry budget
```

---

## Using External Libraries

You can still use external retry libraries. They work, but retries are invisible to hypergraph:

```python
import stamina

@node(output_name="result")
@stamina.retry(on=httpx.HTTPError, attempts=5)  # Works, but invisible
async def fetch(url: str) -> dict:
    ...
```

**Trade-offs:**

| Aspect | Built-in | External (stamina, tenacity) |
|--------|----------|------------------------------|
| Events emitted | ✅ Yes | ❌ No |
| Visible in traces | ✅ Yes | ❌ No |
| NodeEndEvent.attempts | ✅ Populated | ❌ None |
| Checkpointing integration | ✅ Framework-aware | ❌ None |
| Custom strategies | Limited | ✅ Full flexibility |
| Circuit breakers | ❌ Not built-in | ✅ Available |

**Recommendation:** Use built-in for most cases. Use external for advanced patterns (circuit breakers, shared retry state across nodes).

---

## Best Practices

Based on [Temporal](https://temporal.io/blog/failure-handling-in-practice), [DBOS](https://docs.dbos.dev/python/tutorials/step-tutorial), and [Prefect](https://docs.prefect.io/v3/how-to-guides/workflows/retries) recommendations:

### 1. Classify Errors: Transient vs Permanent

This is the most important decision. From Temporal:

> "Errors fall into three categories: transient (temporary), intermittent (occasional), and permanent (require intervention). Permanent failures should immediately surface rather than trigger retries."

```python
# ✅ Good - explicit about what NOT to retry
@node(
    retry=RetryPolicy(
        attempts=5,
        on=Exception,
        non_retryable=[ValidationError, NotFoundError, AuthenticationError],
    ),
)
```

### 2. Always Set Maximums

From [Temporal's retry policy docs](https://docs.temporal.io/encyclopedia/retry-policies):

> "Set a couple of maximums on the retry policy, so that no matter what, the workflow won't just sit there spinning on something that'll never work."

```python
# ❌ Bad - no bounds
@node(retry=RetryPolicy(attempts=100, on=Error))

# ✅ Good - bounded attempts AND total timeout
@node(
    retry=RetryPolicy(attempts=5, on=Error),
    total_timeout=60.0,
)
```

### 3. Use Exponential Backoff (Not Linear)

From Temporal: "A linear retry policy isn't great."

```python
# ❌ Suboptimal - linear backoff
@node(retry=RetryPolicy(attempts=5, on=Error, backoff="linear"))

# ✅ Better - exponential (the default)
@node(retry=RetryPolicy(attempts=5, on=Error))  # backoff="exponential" is default
```

### 4. Design for Idempotency

Retried operations should be safe to repeat:

```python
# ⚠️ Dangerous - double charges on retry after partial success
@node(retry=RetryPolicy(attempts=3, on=httpx.HTTPError))
async def charge_and_notify(order_id: str):
    await payment_api.charge(order_id)  # Might succeed
    await email_api.notify(order_id)    # Then fail here
    # Retry charges again!

# ✅ Better - use idempotency keys
@node(retry=RetryPolicy(attempts=3, on=httpx.HTTPError))
async def charge_and_notify(order_id: str, idempotency_key: str):
    await payment_api.charge(order_id, idempotency_key=idempotency_key)
    await email_api.notify(order_id)
```

### 5. Set Start-to-Close Timeouts

From [Temporal](https://docs.temporal.io/activity-execution):

> "We strongly recommend setting a Start-To-Close Timeout. The server doesn't detect failures when a Worker loses communication... Therefore, the server relies on the timeout to force retries."

```python
# ✅ Good - timeout ensures stuck operations eventually fail and retry
@node(
    retry=RetryPolicy(attempts=3, on=httpx.HTTPError),
    timeout=30.0,  # Per-attempt timeout
)
```

### 6. Log Retries in Production

```python
runner = AsyncRunner(event_processors=[RetryLogger()])
```

Visibility into retry behavior is critical for understanding system health.

---

## API Reference

### RetryPolicy

```python
@dataclass
class RetryPolicy:
    """Retry configuration for node execution.

    Defaults match Temporal's recommendations for production use.
    """
    attempts: int = 3
    on: type | tuple[type, ...] = Exception
    non_retryable: type | tuple[type, ...] = ()
    backoff: Literal["exponential", "linear", "constant"] | Callable[[int], float] = "exponential"
    initial_delay: float = 1.0       # Temporal default
    max_delay: float = 100.0         # Temporal default
    backoff_multiplier: float = 2.0  # Temporal default
    jitter: bool | float = 0.25      # ±25% randomness
```

### Node Decorator Parameters

```python
@node(
    output_name: str,
    retry: RetryPolicy | bool | None = None,  # True = RetryPolicy() with defaults
    timeout: float | None = None,              # Per-attempt timeout
    total_timeout: float | None = None,        # Total execution timeout
)
```

### Exceptions

```python
class RetryExhaustedError(HypergraphError):
    """All retry attempts failed."""
    attempts: int
    last_error: Exception
    errors: list[Exception]  # All errors from each attempt

class NodeTimeoutError(HypergraphError):
    """Base class for timeout errors."""
    node_name: str
    timeout_seconds: float
    elapsed_seconds: float

class AttemptTimeoutError(NodeTimeoutError):
    """Single attempt timed out (may be retried)."""
    attempt: int

class TotalTimeoutError(NodeTimeoutError):
    """Total execution time exceeded (not retried)."""
    attempts_made: int
```

### Comparison with Other Frameworks

| Parameter | hypergraph | Temporal | DBOS | Prefect |
|-----------|------------|----------|------|---------|
| Max attempts | `attempts=3` | `MaximumAttempts=∞` | `max_attempts=3` | `retries=0` |
| Initial delay | `initial_delay=1.0` | `InitialInterval=1s` | `interval_seconds=1.0` | `retry_delay_seconds` |
| Backoff multiplier | `backoff_multiplier=2.0` | `BackoffCoefficient=2.0` | `backoff_rate=2.0` | `exponential_backoff()` |
| Max delay | `max_delay=100.0` | `MaximumInterval=100s` | — | — |
| Non-retryable | `non_retryable=[...]` | `NonRetryableErrorTypes` | — | `retry_condition_fn` |
| Jitter | `jitter=0.25` | — | — | `retry_jitter_factor` |

---

## See Also

- [Execution Types](execution-types.md) - Event definitions
- [Observability](observability.md) - Event processing
- [Durable Execution](durable-execution.md) - Checkpointing interaction
- [Node Types](node-types.md) - `@node` decorator reference
