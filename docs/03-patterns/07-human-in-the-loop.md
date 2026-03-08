# Human-in-the-Loop

Pause execution for human input. Resume when ready.

- **`@interrupt`** - Decorator to create a pause point (like `@node` but pauses on `None`)
- **PauseInfo** - Metadata about the pause (what value is surfaced, where to resume)
- **Handlers** - Auto-resolve interrupts programmatically (testing, automation)

## The Problem

Many workflows need human judgment at key points: approving a draft, confirming a destructive action, providing feedback on generated content. You need a way to pause the graph, surface a value to the user, and resume with their response.

This page focuses on the **graph-level** pause/resume pattern:

- a run pauses at an interrupt
- the caller inspects the pause payload
- the caller later resumes the workflow with a response

That makes interrupts a strong fit for:

- interactive apps
- review UIs
- multi-step assistants
- application-managed human approval flows

For longer-lived event-driven orchestration, checkpointing gives you the persistence foundation, but more of the surrounding runtime shell still lives in your app today.

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

This is intentionally different from event-driven workflow systems like Inngest, DBOS, or Restate, where the runtime often owns external event delivery directly. In hypergraph, the pause/resume primitive is already there; the application typically decides how the later response gets routed back into the run.

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

### Alternative: Explicit Edges

The same chat loop without `emit`/`wait_for` signals. When multiple nodes produce `messages`, explicit edges declare the topology directly:

```python
from hypergraph import Graph, node, route, END, AsyncRunner, interrupt

@interrupt(output_name="query")
def ask_user(response: str) -> str | None:
    return None  # always pause for human input

@node(output_name="messages")
def add_query(messages: list, query: str) -> list:
    return [*messages, {"role": "user", "content": query}]

@node(output_name="response")
def generate(messages: list) -> str:
    return llm.chat(messages)

@node(output_name="messages")
def add_response(messages: list, response: str) -> list:
    return [*messages, {"role": "assistant", "content": response}]

graph = Graph(
    [ask_user, add_query, generate, add_response],
    edges=[
        (ask_user, add_query),                   # query
        (add_query, generate),                    # messages
        (generate, add_response),                 # response
        (add_response, ask_user),                 # ordering only
        (add_response, add_query),                # messages (cycle)
    ],
)

chat = graph.bind(messages=[])
runner = AsyncRunner()

# Turn 1: pauses at ask_user
result = await runner.run(chat, {})
assert result.paused

# Resume with user's first message
result = await runner.run(chat, {"query": "Hello!"})
assert result.paused  # pauses again for next turn
```

No ordering signals needed — the edge list makes execution order unambiguous. Both `add_query` and `add_response` produce `messages`, but the edges declare which runs first.

## Persistent Multi-Turn with Checkpointer

The examples above are stateless — each resume replays from scratch, passing all previous responses. For production multi-turn workflows, use a **checkpointer** to persist state between calls. Each `.run()` only needs the new user input.

```python
from hypergraph import Graph, AsyncRunner, END, node, route, interrupt
from hypergraph.checkpointers import SqliteCheckpointer

@interrupt(output_name="user_input")
def wait_for_user() -> None:
    return None

@node(output_name="messages")
def add_user_message(messages: list, user_input: str) -> list:
    return [*messages, {"role": "user", "content": user_input}]

@node(output_name="response")
async def llm_reply(messages: list, llm_client) -> str:
    return await llm_client.chat(messages)

@node(output_name="messages")
def add_response(messages: list, response: str) -> list:
    return [*messages, {"role": "assistant", "content": response}]

@route(targets=["wait_for_user", END])
def should_continue(messages: list, max_turns: int) -> str:
    turns = sum(1 for m in messages if m["role"] == "assistant")
    return END if turns >= max_turns else "wait_for_user"

chat = Graph(
    [wait_for_user, add_user_message, llm_reply, add_response, should_continue],
    edges=[
        (add_user_message, llm_reply),
        (llm_reply, add_response),
        (add_response, should_continue),
    ],
    name="chat",
    shared=["messages"],
    entrypoint="add_user_message",
).bind(messages=[], llm_client=my_llm, max_turns=5)

# Checkpointer persists state between calls
checkpointer = SqliteCheckpointer("chat.db")
runner = AsyncRunner(checkpointer=checkpointer)

# Turn 1
r1 = await runner.run(chat, workflow_id="conv-1", user_input="hello")
assert r1.paused
print(r1["messages"][-1])  # {"role": "assistant", "content": "..."}

# Turn 2 — only pass the new input, state is restored from checkpoint
r2 = await runner.run(chat, workflow_id="conv-1", user_input="tell me more")
assert r2.paused
```

Key differences from the stateless pattern:

- **`workflow_id`** identifies the conversation — same ID resumes, different ID starts fresh
- **`shared=["messages"]`** accumulates the message list across the cycle
- **`entrypoint="add_user_message"`** skips `wait_for_user` on the first call (no need to pause before the user has spoken)
- Each `.run()` only needs the new `user_input`, not all previous messages

### When the Conversation Ends

When `should_continue` routes to `END`, the workflow completes. Further `.run()` calls with the same `workflow_id` raise `WorkflowAlreadyCompletedError`:

```python
from hypergraph.exceptions import WorkflowAlreadyCompletedError

try:
    await runner.run(chat, workflow_id="conv-1", user_input="one more?")
except WorkflowAlreadyCompletedError:
    print("Conversation ended — use a new workflow_id")
```

### Inspecting Checkpoint History

The checkpointer records every node execution. You can inspect the full step log:

```python
checkpointer = SqliteCheckpointer("chat.db")

# Sync reads — no await needed
run = checkpointer.get_run("conv-1")
print(run.status)  # "active" (paused) or "completed"

steps = checkpointer.steps("conv-1")
for s in steps:
    print(f"  ss={s.superstep}  {s.node_name:25s}  {s.status}")
```

A two-turn conversation produces steps like:

```
  ss=0   add_user_message           completed
  ss=1   llm_reply                  completed
  ss=2   add_response               completed
  ss=3   should_continue            completed    → wait_for_user
  ss=4   wait_for_user              paused
  ss=5   wait_for_user              completed    (user_input resolved)
  ss=6   add_user_message           completed
  ss=7   llm_reply                  completed
  ss=8   add_response               completed
  ss=9   should_continue            completed    → wait_for_user
  ss=10  wait_for_user              paused
```

Notice the interrupt appears twice per turn: first as `paused` (waiting), then as `completed` (resolved with the user's input on the next `.run()` call).

> **Full example**: See [`examples/chat_app.py`](../../examples/chat_app.py) for a complete FastAPI integration with durable multi-turn chat, error handling, and checkpoint inspection endpoint.

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

Think of `response_key` as a **resume slot identifier**. It is precise and stable, but it is primarily a runtime-facing detail. In user-facing applications, you will often wrap it behind your own inbox, form, or webhook layer.

## Checkpointed Pauses

For durable pause/resume across process restarts, pair interrupts with a checkpointer and a `workflow_id`:

```python
from hypergraph import AsyncRunner, Graph, interrupt, node
from hypergraph.checkpointers import SqliteCheckpointer

@interrupt(output_name="decision")
def approval(draft: str) -> str | None:
    return None

@node(output_name="result")
def finalize(decision: str) -> str:
    return f"Final: {decision}"

graph = Graph([approval, finalize])
runner = AsyncRunner(checkpointer=SqliteCheckpointer("./runs.db"))

paused = await runner.run(graph, {"draft": "hello"}, workflow_id="review-1")

# ... later, possibly in another process ...
resumed = await runner.run(
    graph,
    {paused.pause.response_key: "approved"},
    workflow_id="review-1",
)
```

This is the current durable HITL story:

- checkpoint state stores the paused execution
- `workflow_id` identifies the workflow instance
- `response_key` identifies the waiting output slot

If your application needs approval inboxes, event matching, or webhook-driven resume, build those on top of this pause primitive today.

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

The same restriction applies to `GraphNode.map_over(...)`: a nested graph that
contains interrupts cannot be wrapped in `map_over()`. If you need batched
human-in-the-loop processing, use `AsyncRunner.map()` on the item graph itself
rather than mapping a nested interrupting graph inside a larger workflow.

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
    node_name: str                          # Name of the interrupt node (uses "/" for nesting)
    output_param: str                       # First output parameter name
    value: Any                              # First input value surfaced to the caller
    output_params: tuple[str, ...] | None   # All output names (multi-output), else None
    values: dict[str, Any] | None           # All input values (multi-input), else None
```

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `response_key` | `str` | Key to use when resuming (first output). Top-level: `output_param`. Nested: dot-separated path (e.g., `"inner.decision"`) |
| `response_keys` | `dict[str, str]` | Map of all output names to resume keys (for multi-output interrupts) |
