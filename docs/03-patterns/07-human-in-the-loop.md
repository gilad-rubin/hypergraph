# Human-in-the-Loop

Pause execution for human input. Resume when ready.

- **`@interrupt`** - Decorator to create a pause point (like `@node` but pauses on `None`)
- **PauseInfo** - Metadata about the pause (what value is surfaced, where to resume)
- **Handlers** - Auto-resolve interrupts programmatically (testing, automation)

## The Problem

Many workflows need human judgment at key points: approving a draft, confirming a destructive action, providing feedback on generated content. You need a way to pause the graph, surface a value to the user, and resume with their response.

## Basic Pause and Resume

The `@interrupt` decorator creates a pause point. Inputs come from the function signature, outputs from `output_name`. When the handler returns `None`, execution pauses.

```python
from hypergraph import Graph, node, AsyncRunner, interrupt

@node(output_name="draft")
def generate_draft(prompt: str) -> str:
    return f"Blog post about: {prompt}"

@interrupt(output_name="decision")
def approval(draft: str) -> str | None:
    return None  # pause for human review

@node(output_name="result")
def finalize(decision: str) -> str:
    return f"Published: {decision}"

graph = Graph([generate_draft, approval, finalize])
runner = AsyncRunner()

# First run: pauses at the interrupt
result = await runner.run(graph, {"prompt": "Python async"})

assert result.paused
assert result.pause.value == "Blog post about: Python async"
assert result.pause.response_key == "decision"

# Resume with the user's response
result = await runner.run(graph, {
    "prompt": "Python async",
    result.pause.response_key: "Looks great, publish it!",
})

assert result["result"] == "Published: Looks great, publish it!"
```

The key flow:

1. **Run** the graph. Execution pauses at the interrupt.
2. **Inspect** `result.pause.value` to see what the user needs to review.
3. **Resume** by passing the response via `result.pause.response_key`.

### RunResult Properties

When paused, the `RunResult` has:

| Property | Description |
|----------|-------------|
| `result.paused` | `True` when execution is paused |
| `result.pause.value` | The input value surfaced to the caller |
| `result.pause.node_name` | Name of the interrupt node that paused |
| `result.pause.output_param` | Output parameter name |
| `result.pause.response_key` | Key to use in the values dict when resuming |

## Multi-Turn Chat with Human Input

Combine `@interrupt` with agentic loops for a multi-turn conversation where the user provides input each turn.

```python
from hypergraph import Graph, node, route, END, AsyncRunner, interrupt

# Pause and wait for user input
@interrupt(output_name="user_input")
def ask_user(messages: list) -> str | None:
    return None  # always pause for human input

@node(output_name="messages")
def add_user_message(messages: list, user_input: str) -> list:
    return messages + [{"role": "user", "content": user_input}]

@node(output_name="response")
def generate(messages: list) -> str:
    return llm.chat(messages)  # your LLM client

@node(output_name="messages", emit="turn_done")
def accumulate(messages: list, response: str) -> list:
    return messages + [{"role": "assistant", "content": response}]

@route(targets=["ask_user", END], wait_for="turn_done")
def should_continue(messages: list) -> str:
    if len(messages) >= 20:
        return END
    return "ask_user"

graph = Graph([ask_user, add_user_message, generate, accumulate, should_continue])

# Pre-fill messages so the first step (ask_user) can run immediately
chat = graph.bind(messages=[])

runner = AsyncRunner()

# Turn 1: graph pauses at ask_user (shows empty messages)
result = await runner.run(chat, {})
assert result.paused
assert result.pause.node_name == "ask_user"

# Resume with user's first message
result = await runner.run(chat, {"user_input": "Hello!"})
# Pauses again at ask_user for the next turn
assert result.paused

# Resume with second message
result = await runner.run(chat, {"user_input": "Tell me more"})
```

Key patterns:
- **`.bind(messages=[])`** pre-fills the seed input so `.run({})` works with no values
- **Interrupt as first step**: the graph pauses immediately, asking the user for input
- **`emit="turn_done"` + `wait_for="turn_done"`**: ensures `should_continue` sees the fully updated messages
- Each resume replays the graph, providing all previous responses

## Auto-Resolve with Handlers

For testing or automation, the handler function resolves the interrupt without human input. Return a value to auto-resolve, return `None` to pause.

```python
@interrupt(output_name="decision")
def approval(draft: str) -> str:
    return "auto-approved"  # always resolves, never pauses

graph = Graph([generate_draft, approval, finalize])
result = await runner.run(graph, {"prompt": "Python async"})

# No pause — handler resolved it
assert result["result"] == "Published: auto-approved"
```

### Conditional Pause

Return `None` to pause, return a value to auto-resolve:

```python
@interrupt(output_name="decision")
def approval(draft: str) -> str | None:
    if "LGTM" in draft:
        return "auto-approved"
    return None  # pause for human review
```

### Async Handlers

Handlers can be async:

```python
@interrupt(output_name="decision")
async def approval(draft: str) -> str:
    """Use an LLM to auto-review the draft."""
    return await call_llm(f"Review this draft: {draft}")
```

## Multiple Sequential Interrupts

A graph can have multiple interrupts. Execution pauses at each one in topological order.

```python
@node(output_name="draft")
def generate(prompt: str) -> str:
    return f"Draft: {prompt}"

@interrupt(output_name="feedback")
def review(draft: str) -> str | None:
    return None  # pause for reviewer

@interrupt(output_name="final_draft")
def edit(feedback: str) -> str | None:
    return None  # pause for editor

@node(output_name="result")
def publish(final_draft: str) -> str:
    return f"Published: {final_draft}"

graph = Graph([generate, review, edit, publish])
runner = AsyncRunner()

# Pause 1: review
r1 = await runner.run(graph, {"prompt": "hello"})
assert r1.pause.node_name == "review"

# Pause 2: edit (provide review response)
r2 = await runner.run(graph, {
    "prompt": "hello",
    "feedback": "Needs more detail",
})
assert r2.pause.node_name == "edit"
assert r2.pause.value == "Needs more detail"

# Complete (provide both responses)
r3 = await runner.run(graph, {
    "prompt": "hello",
    "feedback": "Needs more detail",
    "final_draft": "Detailed draft about hello",
})
assert r3["result"] == "Published: Detailed draft about hello"
```

Each resume call replays the graph from the start, providing previously-collected responses as input values. The interrupt detects that its output is already in the state and skips the pause.

## Nested Graph Interrupts

Interrupts inside nested graphs propagate the pause to the outer graph. The `node_name` is prefixed with the nested graph's name.

```python
# Inner graph with an interrupt
@interrupt(output_name="y")
def approval(x: str) -> str | None:
    return None

inner = Graph([approval], name="inner")

@node(output_name="x")
def produce(query: str) -> str:
    return query

@node(output_name="result")
def consume(y: str) -> str:
    return f"got: {y}"

outer = Graph([produce, inner.as_node(), consume])
runner = AsyncRunner()

result = await runner.run(outer, {"query": "hello"})

assert result.paused
assert result.pause.node_name == "inner/approval"
assert result.pause.response_key == "inner.y"
```

The `response_key` uses dot-separated paths for nested interrupts: `"inner.y"` means the output `y` inside the graph node `inner`.

## Runner Compatibility

Only `AsyncRunner` supports interrupts. `SyncRunner` raises `IncompatibleRunnerError` at runtime if the graph contains interrupt nodes.

```python
from hypergraph import SyncRunner
from hypergraph.exceptions import IncompatibleRunnerError

runner = SyncRunner()

# Raises IncompatibleRunnerError
runner.run(graph_with_interrupt, {"query": "hello"})
```

Similarly, `AsyncRunner.map()` does not support interrupts — a graph with interrupts cannot be used with `map()`.

## With emit/wait_for

InterruptNode supports ordering signals like FunctionNode:

```python
@interrupt(output_name="decision", emit="reviewed")
def approval(draft: str) -> str:
    ...

@node(output_name="result", wait_for="reviewed")
def finalize(decision: str) -> str:
    return f"Final: {decision}"
```

## API Reference

### `@interrupt` Decorator

The `@interrupt` decorator is the preferred way to create an interrupt node. Like `@node`, inputs come from the function signature, outputs from `output_name`, and types from annotations.

```python
from hypergraph import interrupt

@interrupt(output_name="decision")
def approval(draft: str) -> str | None:
    return None  # pause for human review
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `output_name` | `str \| tuple[str, ...]` | Name(s) for output value(s) — **required** |
| `rename_inputs` | `dict[str, str] \| None` | Mapping to rename inputs {old: new} |
| `cache` | `bool` | Enable result caching (default: `False`) |
| `emit` | `str \| tuple[str, ...] \| None` | Ordering-only output name(s) |
| `wait_for` | `str \| tuple[str, ...] \| None` | Ordering-only input name(s) |
| `hide` | `bool` | Whether to hide from visualization |

### InterruptNode Constructor

InterruptNode can also be created directly (like `FunctionNode`):

```python
from hypergraph import InterruptNode

InterruptNode(my_func, output_name="decision")
InterruptNode(my_func, name="review", output_name="decision",
             emit="done", wait_for="ready")
```

`output_name` is required — `InterruptNode(func)` without it raises `TypeError`.

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `inputs` | `tuple[str, ...]` | Input parameter names (from function signature) |
| `outputs` | `tuple[str, ...]` | All output names (data + emit) |
| `data_outputs` | `tuple[str, ...]` | Data-only outputs (excluding emit) |
| `is_interrupt` | `bool` | Always `True` |
| `cache` | `bool` | Whether caching is enabled (default: `False`) |
| `hide` | `bool` | Whether hidden from visualization |
| `wait_for` | `tuple[str, ...]` | Ordering-only inputs |
| `is_async` | `bool` | True if handler is async |
| `is_generator` | `bool` | True if handler yields |
| `definition_hash` | `str` | SHA256 hash of function source |

**Methods:**

| Method | Returns | Description |
|--------|---------|-------------|
| `with_name(name)` | `InterruptNode` | New instance with a different name |
| `with_inputs(**kwargs)` | `InterruptNode` | New instance with renamed inputs |
| `with_outputs(**kwargs)` | `InterruptNode` | New instance with renamed outputs |

### PauseInfo

```python
@dataclass
class PauseInfo:
    node_name: str      # Name of the interrupt node (uses "/" for nesting)
    output_param: str   # Output parameter name
    value: Any          # Input value surfaced to the caller
```

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `response_key` | `str` | Key to use when resuming. Top-level: `output_param`. Nested: dot-separated path (e.g., `"inner.decision"`) |
