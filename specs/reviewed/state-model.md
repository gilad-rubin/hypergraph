# State Model

**In hypergraph, there is no separate "state" to define. Your node outputs are the state.**

---

## The Question

> "Where do I define state in hypergraph?"

If you're coming from LangGraph or similar frameworks, you might expect to define an explicit state schema:

```python
# LangGraph - explicit state schema
class State(TypedDict):
    messages: Annotated[list, add]  # Reducer for combining
    query: str
    answer: str

graph = StateGraph(State)
```

**hypergraph doesn't work this way.** There's no `State` class to define.

---

## Outputs ARE State

In hypergraph, state emerges from your nodes' outputs:

```python
@node(output_name="response")
def get_response(messages: list, user_input: str) -> str:
    """Get LLM response for the conversation."""
    full_messages = messages + [{"role": "user", "content": user_input}]
    return llm.chat(full_messages)

@node(output_name="messages")
def update_messages(messages: list, user_input: str, response: str) -> list:
    """Append user message and response to conversation history."""
    return messages + [
        {"role": "user", "content": user_input},
        {"role": "assistant", "content": response},
    ]

graph = Graph(nodes=[get_response, update_messages])
```

- `messages` is an output that flows between nodes (and accumulates)
- `response` is an output returned to the user
- Together, they form the graph's "state"

---

## Value Resolution Hierarchy

When determining what value to use for a parameter, hypergraph checks these sources in order:

```
1. Edge value        ← Produced by upstream node (if it has executed)
2. Input value       ← From merged inputs (see below)
3. Bound value       ← Set via graph.bind()
4. Function default  ← Default in function signature
```

### How Inputs Are Merged

At the **start of a run**, the runner merges prior state with your inputs:

```python
# What you write:
result = await runner.run(graph, inputs={"user_input": "Hi"}, workflow_id="chat-123")

# What happens internally (three possible cases):

# Case 1: Resume existing workflow (workflow_id exists in checkpointer)
loaded_state = await checkpointer.get_state("chat-123")  # {"messages": [...]}
merged = {**loaded_state, **inputs}  # inputs override loaded state

# Case 2: Fork from checkpoint (explicit checkpoint parameter)
merged = {**checkpoint.values, **inputs}  # inputs override checkpoint

# Case 3: Fresh start (new workflow_id, no checkpoint)
merged = {**inputs}  # just inputs
```

**The hierarchy is:** `inputs > (checkpoint.values OR loaded_state) > bind() > function defaults`

Inputs always win on conflicts. This ensures new data takes precedence over restored state.

This means the value resolution during execution is simple — checkpoint values are just part of the merged inputs.

### Understanding Edge Values

**Edge values are about execution state, not priority.** An edge value only exists when an upstream node has already executed and produced output.

```python
# DAG: embed → retrieve → generate
@node(output_name="embedding")
def embed(query: str) -> list[float]: ...

@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]: ...
```

**Scenario 1: Normal execution**
```python
result = await runner.run(graph, inputs={"query": "hello"})
# embed runs → produces embedding → retrieve uses it
```

**Scenario 2: Skip upstream by providing its output**
```python
result = await runner.run(graph, inputs={
    "query": "hello",
    "embedding": [0.1, 0.2, ...],  # Provide embedding directly
})
# embed doesn't run (its output is already available)
# retrieve uses the input embedding
```

**Scenario 3: Cycles — fresh output wins**
```python
@node(output_name="messages")
def update_messages(messages: list, new_msg: str) -> list:
    return messages + [new_msg]

# In a cycle:
# - First iteration: messages from input/bound (no prior execution)
# - Second iteration: messages from prior update_messages output (edge wins)
```

The rule: **If an upstream node has executed, its output flows forward.** If it hasn't executed (because you provided the value or it's the first iteration), the value comes from the fallback hierarchy.

### Why This Order?

| Source | When It Applies | Example |
|--------|-----------------|---------|
| **Edge value** | Upstream node has executed | `embed` ran → `retrieve` gets its output |
| **Input value** | No edge, value in merged inputs | `user_input="Hello"` or `messages=[...]` from checkpoint |
| **Bound value** | No edge/input | `graph.bind(messages=[])` |
| **Function default** | None of the above | `def foo(x=10)` |

### Multi-Turn Conversations

This hierarchy enables effortless multi-turn conversations:

```python
# Graph author binds the initial seed value
chat_graph = Graph(nodes=[get_response, update_messages]).bind(messages=[])

# Turn 1: no checkpoint exists, messages from bound ([])
result = await runner.run(chat_graph,
    inputs={"user_input": "Hi"},
    workflow_id="chat-1"
)

# Turn 2: checkpoint loaded and merged, messages=[{...}, {...}]
result = await runner.run(chat_graph,
    inputs={"user_input": "Tell me more"},
    workflow_id="chat-1"
)
```

The user never passes `messages` — the merge handles it automatically.

---

## Why No Explicit State?

Frameworks require explicit state for specific reasons. hypergraph addresses each differently:

### 1. Dataflow (Values Between Nodes)

**LangGraph:** State channels carry values between nodes.

**hypergraph:** Outputs flow via edge inference. If node A produces `embedding` and node B takes `embedding` as input, they're connected automatically.

```python
@node(output_name="embedding")
def embed(text: str) -> list[float]: ...

@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]: ...
# Edge inferred: embed → retrieve
```

### 2. Persistence (Surviving Crashes)

**LangGraph:** Everything in the state schema is checkpointed.

**hypergraph:** When a checkpointer is present, all outputs are checkpointed. Full durability, no configuration needed.

```python
graph = Graph(nodes=[embed, retrieve, generate])
runner = AsyncRunner(checkpointer=SqliteCheckpointer("./db"))
# All outputs saved automatically
```

### 3. Memory (Across Conversation Turns)

**LangGraph:** Memory is part of the state schema, persisted to threads.

**hypergraph:** Pass conversation history as input, return updated history as output.

```python
@node(output_name="messages")
def chat(messages: list, user_input: str) -> list:
    response = llm.chat(messages + [{"role": "user", "content": user_input}])
    return messages + [
        {"role": "user", "content": user_input},
        {"role": "assistant", "content": response},
    ]

# Run with previous messages
result = runner.run(graph, inputs={
    "messages": previous_messages,
    "user_input": "Hello!",
})
```

### 4. Human-in-the-Loop (What Human Sees/Modifies)

**LangGraph:** Human edits state directly via `update_state()`.

**hypergraph:** Use `InterruptNode` with explicit parameters.

```python
approval = InterruptNode(
    name="approval",
    input_param="draft",        # What human sees
    response_param="decision",  # What human provides
)
```

### 5. Reducers (Combining Updates)

**LangGraph:** Reducers like `Annotated[list, add]` combine multiple updates to the same key.

**hypergraph:** Not needed. Each node produces distinctly-named outputs. No conflicts to resolve.

---

## Three Layers of "State"

```
┌─────────────────────────────────────────────────────────────────┐
│  Runtime State (GraphState) - INTERNAL                          │
│                                                                  │
│  ALL outputs from ALL nodes during execution.                   │
│  Tracked with version numbers for staleness detection.          │
│  Used internally by runners - not user-facing.                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ Checkpointer saves all values
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Persisted State (Workflow + StepRecord)                        │
│                                                                  │
│  All outputs saved atomically for full durability.              │
│  Survives crashes, enables resume.                              │
│  Checkpoint ID = workflow_id + step_index.                      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Returned State (RunResult) - USER-FACING                       │
│                                                                  │
│  All outputs returned to the user (or filtered with select=).  │
│  Nested graphs return nested RunResult objects.                 │
│  Dict-like access: result["answer"], result["rag"]["docs"]     │
└─────────────────────────────────────────────────────────────────┘
```

**Key insight:** `GraphState` holds everything during execution. When a checkpointer is present, all values are saved for durability. On crash recovery, all outputs are loaded — no nodes re-execute.

---

## Comparison with LangGraph

| Aspect | LangGraph | hypergraph |
|--------|-----------|------------|
| **Define state** | Explicit `TypedDict` | Implicit from outputs |
| **What's persisted** | Everything in schema | Everything (when checkpointer present) |
| **Reducers** | Required for shared keys | Not needed |
| **Memory** | Built into state | Pass as input/output |
| **Human edits** | `update_state()` | `InterruptNode` |
| **Philosophy** | "State is central" | "Outputs flow, full durability" |

---

## FAQ

### "How do I accumulate values across iterations (like a reducer)?"

Use a node that takes the previous value as input and returns the updated value:

```python
@node(output_name="messages")
def accumulate(messages: list, new_message: str) -> list:
    return messages + [new_message]
```

In cycles, hypergraph tracks versions to know when to re-execute.

### "How do I share state across multiple workflows?"

hypergraph' checkpointer handles state within a single workflow. For cross-workflow memory (like user preferences across conversations), use an external store (database, Redis, etc.) and pass values as inputs.

### "What if two nodes produce the same output name?"

This is a build-time error unless the nodes are mutually exclusive (via routing gates). hypergraph validates this when constructing the graph.

### "Can I update state from outside the graph?"

For human-in-the-loop, use `InterruptNode`. The interrupt surfaces a value, waits for a response, and the response becomes an output that flows to downstream nodes.

**hypergraph intentionally does not have a `update_state()` API** like LangGraph. This is a design choice:

| Need | Solution |
|------|----------|
| Inject human input | Use `InterruptNode` |
| Fix a bug and retry | Use `fork_workflow()` (DBOS) or `resume_from_step` |
| Modify state for testing | Create a new run with different inputs |

The philosophy: State flows through nodes. External mutation breaks the dataflow model and makes debugging harder.

### "Why no update_state()? LangGraph has it."

LangGraph's `update_state()` allows arbitrary state modification at any checkpoint. hypergraph chose not to include this because:

1. **Breaks dataflow** - State should flow through nodes, not be injected from outside
2. **Debugging complexity** - Hard to trace where a value came from if it can be externally modified
3. **Reducer conflicts** - LangGraph needs reducers to handle conflicts; we avoid the problem entirely
4. **Better alternatives exist** - `InterruptNode` for human input, `fork_workflow` for retries

If you're migrating from LangGraph and used `update_state()`, consider:
- For human input: Use `InterruptNode`
- For testing: Pass different inputs to `runner.run()`
- For recovery: Use `resume_from_step` or DBOS `fork_workflow()`

---

## Summary

- **No explicit state schema** - Outputs are inferred from nodes
- **Full durability** - When checkpointer is present, all outputs are saved
- **Memory is explicit** - Pass as input, return as output
- **Reducers not needed** - Each node has distinct outputs

**The mental model:** Nodes are pure functions. Outputs flow between them. When a checkpointer is present, everything is saved for crash recovery.
