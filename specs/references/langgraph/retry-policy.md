# LangGraph Retry Policy Reference

> LangGraph v1.0 (October 2025) - Reference for HyperNodes design

## Overview

LangGraph provides configurable retry policies that automatically retry failed node executions using exponential backoff with optional jitter. Introduced in v0.2.24 (September 2024).

---

## RetryPolicy Class

```python
from langgraph.types import RetryPolicy

RetryPolicy(
    initial_interval: float = 0.5,    # Seconds before first retry
    backoff_factor: float = 2.0,      # Exponential multiplier
    max_interval: float = 128.0,      # Maximum wait time (seconds)
    max_attempts: int = 3,            # Total attempts (including first)
    jitter: bool = True,              # Add randomization
    retry_on: type | tuple = default_retry_on,  # Which exceptions to retry
)
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `initial_interval` | `float` | `0.5` | Seconds before first retry |
| `backoff_factor` | `float` | `2.0` | Multiplier for exponential backoff |
| `max_interval` | `float` | `128.0` | Maximum wait time between retries |
| `max_attempts` | `int` | `3` | Total attempts including the first |
| `jitter` | `bool` | `True` | Add randomization to prevent thundering herd |
| `retry_on` | `type\|tuple` | See below | Exception types to retry |

### Backoff Calculation

```
wait_time = min(initial_interval * (backoff_factor ^ attempt), max_interval)
if jitter:
    wait_time += random(0, wait_time * 0.1)  # Add up to 10% jitter
```

**Example with defaults:**
- Attempt 1 fails → wait 0.5s
- Attempt 2 fails → wait 1.0s
- Attempt 3 fails → wait 2.0s
- Attempt 4 fails → give up (max_attempts=3 means 3 retries after initial)

---

## Default Retry Behavior

The `default_retry_on` function determines which exceptions trigger retries:

```python
def default_retry_on(exc: Exception) -> bool:
    """
    Default retry logic:
    - requests/httpx: Only retry on 5xx status codes
    - Other exceptions: Retry all
    """
    # For HTTP libraries, only retry server errors
    if hasattr(exc, 'response') and hasattr(exc.response, 'status_code'):
        return exc.response.status_code >= 500
    return True
```

**Rationale:** 4xx errors (client errors) are not transient and shouldn't be retried.

---

## Applying Retry Policies

### On Individual Nodes (StateGraph)

```python
from langgraph.graph import StateGraph
from langgraph.types import RetryPolicy

workflow = StateGraph(MyState)

# Add node with retry policy
workflow.add_node(
    "search_documentation",
    search_documentation,
    retry=RetryPolicy(max_attempts=3, initial_interval=1.0)
)

# Different policy for different nodes
workflow.add_node(
    "call_external_api",
    call_api,
    retry=RetryPolicy(
        max_attempts=5,
        initial_interval=2.0,
        max_interval=60.0,
    )
)
```

### On Tasks (Functional API)

```python
from langgraph.func import task
from langgraph.types import RetryPolicy

@task(retry=RetryPolicy(max_attempts=3))
def fetch_data(url: str) -> dict:
    return requests.get(url).json()
```

### Graph-Level Default

```python
# Apply default policy to all nodes
workflow = StateGraph(
    MyState,
    retry=RetryPolicy(max_attempts=2)  # Default for all nodes
)

# Override per-node
workflow.add_node(
    "critical_node",
    critical_func,
    retry=RetryPolicy(max_attempts=5)  # Override default
)
```

---

## Custom Retry Logic

### Custom Exception Filter

```python
def my_retry_filter(exc: Exception) -> bool:
    """Only retry specific exceptions."""
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, ConnectionError):
        return True
    return False

policy = RetryPolicy(
    max_attempts=5,
    retry_on=my_retry_filter
)
```

### Multiple Exception Types

```python
# Retry only these exception types
policy = RetryPolicy(
    retry_on=(RateLimitError, TimeoutError, ConnectionError)
)
```

---

## State-Modifying Retry Policies (v1.0+)

Advanced feature allowing state modification before retry:

```python
def handle_rate_limit(state: MyState, exc: RateLimitError) -> dict:
    """Modify state when rate limited."""
    return {
        "retry_after": exc.retry_after,
        "attempt_count": state.get("attempt_count", 0) + 1
    }

policy = RetryPolicy(
    max_attempts=5,
    state_modifiers={
        RateLimitError: handle_rate_limit,
    }
)
```

**Use case:** Adjust state based on error type before retrying.

---

## Error Persistence

When retries are exhausted:

1. **Error stored in checkpoint:** Failed state is persisted for inspection
2. **Pending writes preserved:** Successful nodes' outputs are saved
3. **Recovery possible:** Can resume from last good checkpoint

```python
# After failure, inspect checkpoint
state = graph.get_state(config)
if state.tasks:
    for task in state.tasks:
        if task.error:
            print(f"Node {task.name} failed: {task.error}")
```

---

## Best Practices

### 1. Different Policies for Different Failure Modes

```python
# External APIs: More retries, longer backoff
api_policy = RetryPolicy(
    max_attempts=5,
    initial_interval=2.0,
    max_interval=120.0,
)

# Database: Fewer retries, shorter backoff
db_policy = RetryPolicy(
    max_attempts=3,
    initial_interval=0.1,
    max_interval=5.0,
)

# LLM calls: Account for rate limits
llm_policy = RetryPolicy(
    max_attempts=3,
    initial_interval=1.0,
    backoff_factor=3.0,  # Aggressive backoff
)
```

### 2. Don't Retry Non-Transient Errors

```python
def smart_retry(exc: Exception) -> bool:
    # Never retry validation errors
    if isinstance(exc, ValueError):
        return False
    # Never retry auth errors
    if isinstance(exc, AuthenticationError):
        return False
    # Retry everything else
    return True
```

### 3. Log Retry Attempts

The `log_warning` parameter (default `True`) logs each retry:

```python
policy = RetryPolicy(
    max_attempts=3,
    log_warning=True  # Logs "Retrying node X, attempt 2/3"
)
```

---

## Implications for HyperNodes

### Current HyperNodes approach:
- Mentioned in runners.md but not fully specified
- No per-node retry configuration

### What to adopt:

1. **RetryPolicy dataclass:**
```python
@dataclass
class RetryPolicy:
    max_attempts: int = 3
    initial_interval: float = 0.5
    backoff_factor: float = 2.0
    max_interval: float = 128.0
    jitter: bool = True
    retry_on: Callable[[Exception], bool] | tuple[type, ...] = Exception
```

2. **Per-node configuration:**
```python
@node(outputs="result", retry=RetryPolicy(max_attempts=5))
def fetch_data(url: str) -> dict:
    ...
```

3. **Runner-level defaults:**
```python
runner = AsyncRunner(
    default_retry=RetryPolicy(max_attempts=2)
)
```

4. **Event emission for observability:**
```python
@dataclass
class RetryEvent:
    node_name: str
    attempt: int
    max_attempts: int
    exception: Exception
    wait_time: float
```

---

## Sources

- [LangGraph RetryPolicy API Reference](https://langchain-ai.github.io/langgraphjs/reference/types/langgraph.RetryPolicy.html)
- [Error Handling and Retry Policies (DeepWiki)](https://deepwiki.com/langchain-ai/langgraph/3.7-error-handling-and-retry-policies)
- [How to Add Node Retry Policies](https://langchain-ai.lang.chat/langgraph/how-tos/node-retries/)
