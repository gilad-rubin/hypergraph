# LangGraph Streaming Reference

> LangGraph v1.0 (October 2025) - Reference for hypergraph design

## Overview

LangGraph provides **5 distinct streaming modes** that answer different questions about execution. Unlike a single unified event stream, LangGraph lets you subscribe to specific "lenses" into execution.

---

## The Five Streaming Modes

| Mode | Question Answered | Data Format |
|------|-------------------|-------------|
| `values` | "What's the complete state now?" | Full state snapshot after each node |
| `updates` | "What just changed?" | Delta/diff of state changes only |
| `messages` | "What tokens is the LLM generating?" | `(token, metadata)` tuples |
| `custom` | "What's my app-specific progress?" | User-defined events |
| `debug` | "What's happening internally?" | Full execution trace |

---

## Mode Details

### 1. `values` Mode - Full State Snapshots

Streams the complete graph state after every node execution.

```python
async for chunk in graph.astream(inputs, stream_mode="values"):
    # chunk = entire state after each node
    print(chunk["messages"])        # All messages so far
    print(chunk["classification"])  # All state fields
```

**Use case:** Applications requiring complete context at each step.

**Trade-off:** High bandwidth - sends everything, not just changes.

---

### 2. `updates` Mode - State Deltas

Streams only the state changes (deltas) after each node runs.

```python
async for chunk in graph.astream(inputs, stream_mode="updates"):
    # chunk = {"node_name": {"field": new_value}}
    for node_name, updates in chunk.items():
        print(f"{node_name} changed: {updates}")
```

**Use case:** Progress dashboards, showing "Step 2/5 complete."

**Trade-off:** You must track cumulative state yourself.

---

### 3. `messages` Mode - Token-by-Token LLM Output

Streams LLM output tokens as they're generated, creating the "typing" effect.

```python
async for chunk in graph.astream(inputs, stream_mode="messages"):
    token, metadata = chunk
    print(token.content, end="", flush=True)  # Typing effect
    print(f"from node: {metadata['langgraph_node']}")
```

**Use case:** Chat interfaces with "AI is typing..." effect.

**Trade-off:** Only works with streaming-capable LLMs.

**Note:** Assumes your graph has a `messages` key containing a list of messages.

---

### 4. `custom` Mode - App-Specific Events

Allows tools and nodes to emit custom events during execution.

**Emitting custom events:**

```python
from langgraph.config import get_stream_writer

def my_tool(query: str):
    writer = get_stream_writer()
    writer({"progress": "Fetched 10/100 records"})  # Custom event
    # ... do work ...
    writer({"progress": "Fetched 50/100 records"})
    return result
```

**Consuming custom events:**

```python
async for mode, chunk in graph.astream(inputs, stream_mode=["updates", "custom"]):
    if mode == "custom":
        print(f"Progress: {chunk['progress']}")
```

**Use case:** Long-running tools reporting progress.

---

### 5. `debug` Mode - Execution Trace

Streams a full execution trace including node entry/exit, state before/after, tool inputs/outputs, and errors.

```python
async for chunk in graph.astream(inputs, stream_mode="debug"):
    # Detailed trace: node entry/exit, state before/after, errors
    print(chunk)  # Very verbose!
```

**Use case:** Development debugging only.

**Trade-off:** Generates substantial data; unsuitable for production UIs.

---

## Combining Multiple Modes

You can subscribe to multiple modes simultaneously:

```python
async for mode, chunk in graph.astream(
    inputs,
    stream_mode=["messages", "updates", "custom"]
):
    match mode:
        case "messages":
            print(chunk[0].content, end="")
        case "updates":
            print(f"\nState changed: {chunk}")
        case "custom":
            print(f"\nProgress: {chunk}")
```

The output format becomes tuples of `(mode_name, chunk)`.

---

## Low-Level API: `astream_events()`

For maximum control over all internal events:

```python
async for event in graph.astream_events(inputs, version="v2"):
    match event["event"]:
        case "on_chat_model_stream":
            print(event["data"]["chunk"].content, end="")
        case "on_tool_start":
            print(f"Starting tool: {event['name']}")
        case "on_tool_end":
            print(f"Tool result: {event['data']['output']}")
        case "on_chain_start":
            print(f"Node starting: {event['name']}")
        case "on_chain_end":
            print(f"Node finished: {event['name']}")
```

### Event Types

| Event | When Emitted |
|-------|--------------|
| `on_chat_model_stream` | LLM generates a token |
| `on_chat_model_start` | LLM call begins |
| `on_chat_model_end` | LLM call completes |
| `on_tool_start` | Tool execution begins |
| `on_tool_end` | Tool execution completes |
| `on_chain_start` | Node/chain begins |
| `on_chain_end` | Node/chain completes |

### Filtering Events

Use parameters for selective subscription:

```python
async for event in graph.astream_events(
    inputs,
    version="v2",
    include_tags=["important"],
    include_names=["generate", "retrieve"],
    include_types=["on_chat_model_stream"],
):
    # Only matching events
    ...
```

---

## Disabling Streaming

For specific models in multi-agent systems:

```python
# Disable streaming for a specific model
llm = ChatOpenAI(model="gpt-4", streaming=False)

# Or use disable_streaming parameter
llm = SomeModel(disable_streaming=True)
```

**Use cases:**
- Mixing streaming and non-streaming models
- Deploying to LangSmith where streaming isn't needed
- Models that don't support streaming

---

## Human-in-the-Loop Integration

Interrupts work within the streaming loop:

```python
async for mode, chunk in graph.astream(inputs, stream_mode=["messages", "updates"]):
    if mode == "messages":
        print(chunk[0].content, end="")
    elif mode == "updates":
        # Check for interrupt
        if "__interrupt__" in chunk:
            user_response = await get_user_input()
            # Resume with response
```

---

## Key Design Decisions

1. **Multiple modes vs. unified stream:** LangGraph chose mode-based filtering to reduce noise
2. **`get_stream_writer()` for custom events:** Requires explicit opt-in within tools
3. **`messages` mode assumes state shape:** Requires `messages` key in state
4. **`astream_events()` for low-level access:** Full control when modes aren't enough

---

## Implications for hypergraph

### What hypergraph has:
- Unified event stream via `AsyncRunner.iter()`
- Typed events (`StreamingChunkEvent`, `NodeStartEvent`, etc.)
- Built-in span hierarchy (`span_id`, `parent_span_id`)

### What hypergraph could adopt:
1. **State snapshot mode** - Full state after each node (like `values`)
2. **Custom stream writer** - Let tools emit progress events (like `custom`)
3. **Mode filtering** - Subscribe only to what you need
4. **Event type filtering** - `include_types`, `include_names` parameters

### Design trade-offs:
- LangGraph: More flexible, more complex API surface
- hypergraph: Simpler mental model, filter in user code

---

## Sources

- [LangGraph Streaming Concepts](https://github.com/langchain-ai/langgraph/blob/main/docs/docs/concepts/streaming.md)
- [LangChain Streaming Docs](https://docs.langchain.com/oss/python/langchain/streaming)
- [LangGraph Streaming 101](https://dev.to/sreeni5018/langgraph-streaming-101-5-modes-to-build-responsive-ai-applications-4p3f)
