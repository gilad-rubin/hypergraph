# Human-in-the-Loop

Pause execution for human input. Resume when ready.

- **InterruptNode** - Declarative pause point with one input and one output
- **PauseInfo** - Metadata about the pause (what value is surfaced, where to resume)
- **Handlers** - Auto-resolve interrupts programmatically (testing, automation)

## The Problem

Many workflows need human judgment at key points: approving a draft, confirming a destructive action, providing feedback on generated content. You need a way to pause the graph, surface a value to the user, and resume with their response.

## Basic Pause and Resume

An `InterruptNode` takes one input (the value shown to the user) and produces one output (the user's response). When execution reaches an interrupt without a response, it pauses.

```python
from hypergraph import Graph, node, AsyncRunner, InterruptNode

@node(output_name="draft")
def generate_draft(prompt: str) -> str:
    return f"Blog post about: {prompt}"

approval = InterruptNode(
    name="approval",
    input_param="draft",
    output_param="decision",
)

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

1. **Run** the graph. Execution pauses at the `InterruptNode`.
2. **Inspect** `result.pause.value` to see what the user needs to review.
3. **Resume** by passing the response via `result.pause.response_key`.

### RunResult Properties

When paused, the `RunResult` has:

| Property | Description |
|----------|-------------|
| `result.paused` | `True` when execution is paused |
| `result.pause.value` | The input value surfaced to the caller |
| `result.pause.node_name` | Name of the InterruptNode that paused |
| `result.pause.output_param` | Output parameter name |
| `result.pause.response_key` | Key to use in the values dict when resuming |

## Multi-Turn Chat with Human Input

Combine InterruptNode with agentic loops for a multi-turn conversation where the user provides input each turn.

```python
from hypergraph import Graph, node, route, END, AsyncRunner, InterruptNode

# Pause and wait for user input
ask_user = InterruptNode(
    name="ask_user",
    input_param="messages_with_response",
    output_param="user_input",
)

@node(output_name="messages_with_user")
def add_user_message(messages_with_response: list, user_input: str) -> list:
    return messages_with_response + [{"role": "user", "content": user_input}]

@node(output_name="response")
def generate(messages_with_user: list) -> str:
    return llm.chat(messages_with_user)  # your LLM client

@node(output_name="messages_with_response")
def accumulate(messages_with_user: list, response: str) -> list:
    return messages_with_user + [{"role": "assistant", "content": response}]

@route(targets=["ask_user", END])
def should_continue(messages_with_response: list) -> str:
    if len(messages_with_response) >= 20:
        return END
    return "ask_user"

graph = Graph([ask_user, add_user_message, generate, accumulate, should_continue])

# Pre-fill messages so the first step (ask_user) can run immediately
chat = graph.bind(messages_with_response=[])

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
- **`.bind(messages_with_response=[])`** pre-fills the seed input so `.run({})` works with no values
- **InterruptNode as first step**: the graph pauses immediately, asking the user for input
- **Different output names** (`messages_with_user`, `messages_with_response`) give each accumulator its own name, so auto-wiring creates proper data edges with no `emit`/`wait_for` needed
- Each resume replays the graph, providing all previous responses

## Auto-Resolve with Handlers

For testing or automation, attach a handler that resolves the interrupt without human input.

### Handler in Constructor

```python
approval = InterruptNode(
    name="approval",
    input_param="draft",
    output_param="decision",
    handler=lambda draft: "auto-approved",
)

graph = Graph([generate_draft, approval, finalize])
result = await runner.run(graph, {"prompt": "Python async"})

# No pause — handler resolved it
assert result["result"] == "Published: auto-approved"
```

### Handler via with_handler()

Use `with_handler()` to attach a handler after construction. This returns a new instance (immutable pattern).

```python
# Define the interrupt without a handler
approval = InterruptNode(
    name="approval",
    input_param="draft",
    output_param="decision",
)

# For testing: attach a handler
test_approval = approval.with_handler(lambda draft: "test-approved")

# Original unchanged
assert approval.handler is None
assert test_approval.handler is not None
```

This is useful when the same interrupt needs human input in production but auto-resolution in tests.

### Async Handlers

Handlers can be async:

```python
async def llm_review(draft: str) -> str:
    """Use an LLM to auto-review the draft."""
    return await call_llm(f"Review this draft: {draft}")

approval = InterruptNode(
    name="approval",
    input_param="draft",
    output_param="decision",
    handler=llm_review,
)
```

## Multiple Sequential Interrupts

A graph can have multiple interrupts. Execution pauses at each one in topological order.

```python
@node(output_name="draft")
def generate(prompt: str) -> str:
    return f"Draft: {prompt}"

review = InterruptNode(
    name="review",
    input_param="draft",
    output_param="feedback",
)

edit = InterruptNode(
    name="edit",
    input_param="feedback",
    output_param="final_draft",
)

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

InterruptNodes inside nested graphs propagate the pause to the outer graph. The `node_name` is prefixed with the nested graph's name.

```python
# Inner graph with an interrupt
approval = InterruptNode(name="approval", input_param="x", output_param="y")
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

Only `AsyncRunner` supports interrupts. `SyncRunner` raises `IncompatibleRunnerError` at runtime if the graph contains InterruptNodes.

```python
from hypergraph import SyncRunner
from hypergraph.exceptions import IncompatibleRunnerError

runner = SyncRunner()

# Raises IncompatibleRunnerError
runner.run(graph_with_interrupt, {"query": "hello"})
```

Similarly, `AsyncRunner.map()` does not support interrupts — a graph with interrupts cannot be used with `map()`.

## The `@interrupt` Decorator

The `@interrupt` decorator is the preferred way to create an InterruptNode. Like `@node`, inputs come from the function signature, outputs from `output_name`, and types from annotations.

```python
from hypergraph import interrupt

@interrupt(output_name="decision")
def approval(draft: str) -> str:
    return "auto-approved"      # returns value -> auto-resolve
    # return None               # returns None -> pause

# Inputs from signature, outputs from output_name
assert approval.inputs == ("draft",)
assert approval.outputs == ("decision",)

# Test the handler directly
assert approval("my draft") == "auto-approved"
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

### With emit/wait_for

InterruptNode supports ordering signals like FunctionNode:

```python
@interrupt(output_name="decision", emit="reviewed")
def approval(draft: str) -> str:
    ...

@node(output_name="result", wait_for="reviewed")
def finalize(decision: str) -> str:
    return f"Final: {decision}"
```

### Decorator Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `output_name` | `str \| tuple[str, ...]` | Name(s) for output value(s) |
| `rename_inputs` | `dict[str, str] \| None` | Mapping to rename inputs {old: new} |
| `emit` | `str \| tuple[str, ...] \| None` | Ordering-only output name(s) |
| `wait_for` | `str \| tuple[str, ...] \| None` | Ordering-only input name(s) |
| `hide` | `bool` | Whether to hide from visualization |

## API Reference

### InterruptNode

InterruptNode can be created two ways:

**With a source function** (like FunctionNode):

```python
InterruptNode(my_func, output_name="decision")
InterruptNode(my_func, name="review", output_name="decision",
             emit="done", wait_for="ready")
```

**Legacy constructor** (handler-less pause points):

```python
InterruptNode(name="approval", input_param="draft", output_param="decision")
```

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `input_param` | `str` | The first input parameter name |
| `output_param` | `str` | The first output parameter name |
| `cache` | `bool` | Always `False` |
| `hide` | `bool` | Whether hidden from visualization |
| `wait_for` | `tuple[str, ...]` | Ordering-only inputs |
| `data_outputs` | `tuple[str, ...]` | Outputs excluding emit-only |
| `definition_hash` | `str` | SHA256 hash of function source or metadata |

**Methods:**

| Method | Returns | Description |
|--------|---------|-------------|
| `with_handler(handler)` | `InterruptNode` | New instance with the given handler attached |
| `with_name(name)` | `InterruptNode` | New instance with a different name |
| `with_inputs(**kwargs)` | `InterruptNode` | New instance with renamed inputs |
| `with_outputs(**kwargs)` | `InterruptNode` | New instance with renamed outputs |

**Raises:**

- `ValueError` if parameter names are not valid Python identifiers or are reserved keywords.
- `ValueError` if emit names overlap with output names, or wait_for names overlap with input parameters.

### PauseInfo

```python
@dataclass
class PauseInfo:
    node_name: str      # Name of the InterruptNode (uses "/" for nesting)
    output_param: str   # Output parameter name
    value: Any          # Input value surfaced to the caller
```

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `response_key` | `str` | Key to use when resuming. Top-level: `output_param`. Nested: dot-separated path (e.g., `"inner.decision"`) |
