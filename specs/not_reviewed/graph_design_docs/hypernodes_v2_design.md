# HyperNodes V2 — Complete System Design

This document captures the comprehensive design for HyperNodes V2, a reactive dataflow graph framework for building AI/ML workflows with support for cycles, human-in-the-loop interactions, and durable execution.

---

## Table of Contents

1. [Core Philosophy](#core-philosophy)
2. [Graph Definition](#graph-definition)
3. [Execution API](#execution-api)
4. [Identity Model](#identity-model)
5. [Checkpoints & State](#checkpoints--state)
6. [Three-Layer Architecture](#three-layer-architecture)
7. [Callbacks & Events](#callbacks--events)
8. [Engines](#engines)
9. [Caching](#caching)
10. [Type Reference](#type-reference)
11. [Summary: Progressive Complexity](#summary-progressive-complexity)

---

## Core Philosophy

### Reactive Dataflow, Not State Objects

HyperNodes uses **implicit edge construction** from function signatures. Data flows through named parameters — no explicit state objects or routers.

```python
# Edges are inferred: embed produces "embedding", retrieve consumes it
@node(output_name="embedding")
def embed(text: str) -> list[float]: ...

@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]: ...

# Graph automatically wires: text → embed → retrieve
graph = Graph(nodes=[embed, retrieve])
```

### Why No State Objects?

| Pattern | Problem |
|---------|---------|
| `state.messages.append(...)` | Hidden mutation, hard to trace |
| `return {"messages": [...]}` | Explicit, visualizable, cacheable |

Every value has a name, every edge is traceable.

---

## Graph Definition

### Nodes

Regular computation nodes using the `@node` decorator:

```python
from hypernodes import node

@node(output_name="answer")
def generate(query: str, context: str, llm: Any) -> str:
    return llm.invoke(f"Context: {context}\n\nQuestion: {query}")

# Multiple outputs
@node(output_name=("mean", "std"))
def statistics(data: list[float]) -> tuple[float, float]:
    return (sum(data) / len(data), compute_std(data))
```

### Branch (Boolean Routing)

For simple true/false decisions with **string-based targets**:

```python
from hypernodes import branch

@branch(when_true="process_valid", when_false="handle_error")
def is_valid(data: dict) -> bool:
    return data.get("valid", False)
```

- Targets are **strings** referencing node names
- Validated at graph initialization (fail-fast if target doesn't exist)
- Mutually exclusive branches can produce the same output name

### Gate (Multi-way Routing)

For multi-way routing using `Literal` types:

```python
from typing import Literal
from hypernodes import gate, END

# Simple targets
AgentAction = Literal["research", "retrieve", "respond", END]

@gate
def decide(state: dict) -> AgentAction:
    if state.get("ready"):
        return "respond"
    return "research"
```

**With descriptions** (for visualization):

```python
# Tuple format: (target, description)
AgentAction = Literal[
    "research",
    ("retrieve", "Fetch from vector store"),
    ("respond", "Generate final answer"),
    END,
]
```

### InterruptNode (Human-in-the-Loop)

Declares a pause point where the graph surfaces a value and waits for user input:

```python
from hypernodes import InterruptNode

approval_interrupt = InterruptNode(
    name="approval",                  # Interrupt identifier (for handlers, events)
    input_param="approval_prompt",    # Read prompt value from this parameter
    response_param="user_decision",   # Write response to this parameter
    response_type=ApprovalResponse,   # Optional: validate response type
)
```

**Why separate `name` from `input_param`/`response_param`?**

| Field | Purpose | Example |
|-------|---------|---------|
| `name` | Identify the interrupt point | `"approval"`, `"human_review"` |
| `input_param` | Which state value to show user | `"approval_prompt"` (the prompt object) |
| `response_param` | Where to write user's response | `"user_decision"` (feeds downstream nodes) |

The interrupt `name` is stable across refactors — you can rename parameters without breaking handler registration or checkpoint compatibility.

```python
# Handler registered by name, not parameter
@graph.on_interrupt("approval")  # Uses name, not input_param
async def handle_approval(prompt: ApprovalPrompt) -> ApprovalResponse:
    return await external_service.check(prompt)
```

**Design principle**: The framework provides **plumbing**, the user provides **semantics**. Prompt and response types are user-defined — the framework doesn't dictate their structure.

```python
@dataclass
class ApprovalPrompt:
    message: str
    draft: str
    options: list[str]

@dataclass
class ApprovalResponse:
    choice: str  # "approve", "edit", "reject"
    feedback: str | None = None
```

### Building a Graph

```python
from hypernodes import Graph, node, InterruptNode, branch

@node(output_name="draft")
def generate_draft(topic: str) -> str: ...

@node(output_name="approval_prompt")
def create_prompt(draft: str) -> ApprovalPrompt: ...

approval = InterruptNode(
    name="approval",
    input_param="approval_prompt",
    response_param="user_decision",
)

@branch(when_true="finalize", when_false="revise")
def check_approval(user_decision: ApprovalResponse) -> bool:
    return user_decision.choice == "approve"

@node(output_name="final")
def finalize(draft: str) -> str: ...

@node(output_name="draft")  # Overwrites draft → creates cycle
def revise(draft: str, user_decision: ApprovalResponse) -> str: ...

graph = Graph(nodes=[
    generate_draft, create_prompt, approval,
    check_approval, finalize, revise,
])
```

---

## Execution API

Three distinct execution patterns for different use cases:

| Method | Use Case | Interrupts | Parallelizable | Returns |
|--------|----------|------------|----------------|---------|
| `.run()` | Simple execution, automation | Via handlers | ❌ | `GraphResult` |
| `.map()` | Batch processing | ❌ Not supported | ✅ | `list[GraphResult]` |
| `.iter()` | Interactive, streaming | ✅ Full control | ❌ | `GraphRun` |

### `.run()` — Simple Execution

```python
# Basic usage (no interrupts)
result = graph.run(inputs={"query": "What is RAG?"})
print(result["answer"])  # Dict-like access

# With pre-registered handlers (interrupts handled automatically)
@graph.on_interrupt("user_decision")
async def handle_approval(prompt: ApprovalPrompt) -> ApprovalResponse:
    return await external_approval_service.check(prompt.draft)

result = await graph.run(inputs={"topic": "AI Safety"})

# With inline handlers
result = await graph.run(
    inputs={"topic": "AI Safety"},
    handlers={
        "user_decision": lambda p: ApprovalResponse(choice="approve")
    }
)
```

### `.map()` — Batch Processing

```python
# Process multiple inputs in parallel
results = graph.map(
    inputs={"query": ["Q1", "Q2", "Q3"]},
    map_over="query",
)

# Multiple parameters with zip/product
results = graph.map(
    inputs={"x": [1, 2, 3], "y": [10, 20, 30]},
    map_over=["x", "y"],
    map_mode="zip",      # (1,10), (2,20), (3,30)
    # map_mode="product" # All 9 combinations
)
```

**Note**: `.map()` raises `TypeError` if graph contains `InterruptNode`. Batch human interaction requires explicit loops.

### `.iter()` — Interactive Execution

Full control for human-in-the-loop, streaming, and debugging:

```python
async with graph.iter(inputs={"topic": "AI Safety"}) as run:
    async for event in run:
        if isinstance(event, StreamingChunkEvent):
            print(event.chunk, end="")  # Token-by-token
        
        if run.interrupted:
            print(f"Review: {run.interrupt.value}")
            user_input = await get_user_input()
            await run.respond({"user_decision": user_input})

print(run.result["final"])
```

**Events emitted by `.iter()`:**

| Event | When | Key Fields |
|-------|------|------------|
| `RunStartEvent` | Execution begins | `session_id`, `run_id`, `inputs` |
| `NodeStartEvent` | Node begins execution | `node_id`, `step_index`, `inputs` |
| `NodeEndEvent` | Node completes | `node_id`, `outputs`, `cached`, `duration_ms` |
| `NodeSkippedEvent` | Node skipped (branch) | `node_id`, `reason`, `skipped_by` |
| `GateDecisionEvent` | Gate/branch decides | `gate_id`, `decision`, `activated_targets` |
| `StreamingStartEvent` | Streaming output begins | `node_id`, `output_name` |
| `StreamingChunkEvent` | Token/chunk arrives | `node_id`, `chunk`, `chunk_index` |
| `StreamingEndEvent` | Streaming completes | `node_id`, `final_value` |

**Streaming: Detection Strategy**

Rather than hardcoding type exclusions, we use a **generator-first** approach:

**Rule 1: Generators are always streaming**
```python
import inspect

def is_streaming(result):
    # Generators (yield keyword) are unambiguously streaming
    return inspect.isgenerator(result) or inspect.isasyncgen(result)
```

This catches the common LLM patterns:
```python
# ✅ Streaming - uses yield
@node(output_name="response")
async def generate(query: str, llm):
    async for chunk in llm.stream(query):
        yield chunk

# ✅ Streaming - generator expression
@node(output_name="tokens")
def tokenize(text: str):
    yield from text.split()

# ❌ NOT streaming - regular return
@node(output_name="response")
def generate(query: str, llm) -> str:
    return llm.invoke(query)
```

**Rule 2: Explicit opt-in for edge cases**

For non-generator streaming (e.g., custom iterator objects), users can opt-in:

```python
# Edge case: LLM client returns custom Stream object, not a generator
@node(output_name="response", streaming=True)  # Explicit opt-in
def generate(query: str, llm) -> Stream[str]:
    return llm.stream(query)  # Returns Stream object with __iter__
```

When `streaming=True`:
- Engine calls `iter()` or `aiter()` on the result
- Emits `StreamingChunkEvent` for each item
- Accumulates and joins at the end

**Rule 3: Type hint as documentation (not enforcement)**

```python
from typing import Iterator, AsyncIterator

# Type hint documents intent, but detection is runtime
@node(output_name="response")
async def generate(query: str) -> AsyncIterator[str]:
    async for chunk in llm.stream(query):
        yield chunk  # ← This yield is what makes it streaming
```

**Summary:**

| Pattern | Detected As | Why |
|---------|-------------|-----|
| `yield` / `yield from` | ✅ Streaming | `inspect.isgenerator()` |
| `async for ... yield` | ✅ Streaming | `inspect.isasyncgen()` |
| `return "string"` | ❌ Not streaming | Not a generator |
| `return [1, 2, 3]` | ❌ Not streaming | Not a generator |
| `return custom_stream` | ❌ Not streaming | Use `streaming=True` |
| `@node(streaming=True)` | ✅ Streaming | Explicit opt-in |

**Chunk accumulation:**

```python
async def execute_node(node, inputs):
    result = await node.func(**inputs)
    
    if inspect.isgenerator(result) or inspect.isasyncgen(result) or node.streaming:
        chunks = []
        async for chunk in ensure_async_iter(result):
            yield StreamingChunkEvent(node_id=node.name, chunk=chunk)
            chunks.append(chunk)
        
        # Join chunks based on first chunk type
        final = join_chunks(chunks)
        yield StreamingEndEvent(node_id=node.name, final_value=final)
        return final
    else:
        return result

def join_chunks(chunks):
    if not chunks:
        return None
    first = chunks[0]
    if isinstance(first, str):
        return "".join(chunks)
    if isinstance(first, bytes):
        return b"".join(chunks)
    if isinstance(first, dict):
        merged = {}
        for c in chunks:
            merged.update(c)
        return merged
    return chunks  # Keep as list for unknown types
```

**LLM-specific patterns:**

Most LLM libraries work naturally with this approach:

```python
# OpenAI - returns generator
@node(output_name="response")
async def chat_openai(messages: list, client: OpenAI):
    stream = await client.chat.completions.create(
        model="gpt-4",
        messages=messages,
        stream=True,
    )
    async for chunk in stream:
        if chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content

# Anthropic - returns generator
@node(output_name="response")  
async def chat_anthropic(messages: list, client: Anthropic):
    with client.messages.stream(...) as stream:
        for text in stream.text_stream:
            yield text

# LiteLLM - same pattern
@node(output_name="response")
async def chat_lite(messages: list):
    response = await litellm.acompletion(
        model="gpt-4",
        messages=messages,
        stream=True,
    )
    async for chunk in response:
        yield chunk.choices[0].delta.content or ""
```

---

### Stream Routing: Which Stream Goes Where?

In a multi-node graph, multiple nodes might stream (possibly concurrently). The UI needs to know where to display each stream.

**The Problem:**

```python
# RAG with chain-of-thought
@node(output_name="thinking")
async def reason(query: str):
    async for chunk in llm.stream(f"Think about: {query}"):
        yield chunk  # Stream 1: reasoning trace

@node(output_name="answer") 
async def generate(thinking: str, context: str):
    async for chunk in llm.stream(f"Based on {thinking}..."):
        yield chunk  # Stream 2: final answer

# UI question: Which stream is the chat response?
```

**Solution: Events carry full context**

Every `StreamingChunkEvent` includes:
```python
@dataclass
class StreamingChunkEvent:
    node_id: str       # "generate"
    output_name: str   # "answer"
    tags: list[str]    # ["response", "user-facing"]
    chunk: Any         # "The answer is..."
    chunk_index: int   # 0, 1, 2, ...
```

---

### Node Tags

Optional metadata for classification, filtering, and routing:

```python
@node(output_name="thinking", tags=["debug", "llm"])
async def reason(query: str):
    async for chunk in llm.stream(...):
        yield chunk

@node(output_name="answer", tags=["response", "llm"])
async def generate(thinking: str):
    async for chunk in llm.stream(...):
        yield chunk

@node(output_name="tool_output", tags=["tool"])
async def execute_tool(tool_call: dict):
    async for line in run_tool_streaming(tool_call):
        yield line
```

Tags propagate to all events from that node:
- `NodeStartEvent`, `NodeEndEvent` include `tags`
- `StreamingChunkEvent`, `StreamingEndEvent` include `tags`

**Framework is tag-agnostic** — it just carries metadata. Tag conventions are app-defined.

---

#### Pattern 1: Manual routing with `.iter()`

Full control — you decide where each stream goes:

```python
async with graph.iter(inputs={"query": "What is RAG?"}) as run:
    async for event in run:
        if isinstance(event, StreamingChunkEvent):
            if "response" in event.tags:
                ui.append_to_chat(event.chunk)
            elif "debug" in event.tags:
                ui.update_thinking_panel(event.chunk)
            elif "tool" in event.tags:
                ui.show_tool_activity(event.chunk)
```

**Multi-turn conversation:**

```python
async with graph.iter(inputs={"messages": conversation}) as run:
    async for event in run:
        if isinstance(event, StreamingChunkEvent) and "response" in event.tags:
            yield event.chunk  # Send to frontend
        
        if isinstance(event, InterruptEvent):
            user_input = await get_user_input()
            await run.respond(user_input)
```

---

#### Pattern 2: Concurrent/Parallel Streams

When nodes stream concurrently (e.g., parallel retrievers or multi-agent):

```python
# Two retrievers streaming simultaneously
@node(output_name="web_results", tags=["retrieval", "web"])
async def search_web(query: str):
    async for result in web_search.stream(query):
        yield result

@node(output_name="db_results", tags=["retrieval", "db"])
async def search_db(query: str):
    async for result in db.stream_search(query):
        yield result
```

Events are **interleaved** but fully distinguishable:

```
StreamingChunkEvent(node_id="search_web", output_name="web_results", tags=["retrieval", "web"], ...)
StreamingChunkEvent(node_id="search_db", output_name="db_results", tags=["retrieval", "db"], ...)
StreamingChunkEvent(node_id="search_web", output_name="web_results", tags=["retrieval", "web"], ...)
...
```

**UI buffers by identity:**

```python
from collections import defaultdict

buffers = defaultdict(list)

async for event in run:
    if isinstance(event, StreamingChunkEvent):
        key = (event.node_id, event.output_name)  # Unique stream identity
        buffers[key].append(event.chunk)
        ui.update_panel(event.output_name, buffers[key])
```

---

#### Pattern 3: AG-UI Protocol Integration

AG-UI is a **protocol**, not a callback. You translate HyperNodes events to AG-UI events:

```python
async def stream_to_agui(graph, inputs, thread_id: str):
    """Translate HyperNodes events → AG-UI protocol."""
    async with graph.iter(inputs=inputs) as run:
        async for event in run:
            match event:
                case StreamingChunkEvent(tags=tags, chunk=chunk) if "response" in tags:
                    # AG-UI: stream text to chat
                    yield {
                        "type": "TEXT_MESSAGE_CONTENT",
                        "threadId": thread_id,
                        "content": chunk,
                    }
                
                case StreamingChunkEvent(node_id=node_id, chunk=chunk):
                    # AG-UI: other streams go to step activity
                    yield {
                        "type": "STEP_PROGRESS",
                        "threadId": thread_id,
                        "stepId": node_id,
                        "content": str(chunk),
                    }
                
                case NodeStartEvent(node_id=node_id):
                    yield {
                        "type": "STEP_STARTED",
                        "threadId": thread_id,
                        "stepId": node_id,
                    }
                
                case NodeEndEvent(node_id=node_id):
                    yield {
                        "type": "STEP_FINISHED",
                        "threadId": thread_id,
                        "stepId": node_id,
                    }
                
                case RunEndEvent(outputs=outputs):
                    yield {
                        "type": "RUN_FINISHED",
                        "threadId": thread_id,
                    }
```

**The translation is app-owned** — HyperNodes provides events, the app decides the protocol mapping.

---

#### Summary: Stream Routing

| Use Case | Pattern | How |
|----------|---------|-----|
| Full control | `.iter()` + manual routing | Filter by `tags` or `output_name` |
| Concurrent streams | Buffer by `(node_id, output_name)` | Events are interleaved but identifiable |
| AG-UI / protocols | Event translation function | App-owned mapping |
| Simple cases | Filter by `output_name` | No tags needed |

**The key insight:** Streams are identified by `(node_id, output_name, tags)`. The framework provides the data; routing conventions are app-defined.

| `InterruptEvent` | Interrupt reached | `interrupt_name`, `checkpoint_id` |
| `ResumeEvent` | After `respond()` called | `interrupt_name`, `response_value` |
| `StateSnapshotEvent` | Full state sync | `state` (dict) |
| `StateDeltaEvent` | Incremental state update | `operations` (JSON Patch) |
| `RunEndEvent` | Execution completes | `status`, `outputs`, `duration_ms` |

**Resume from checkpoint**:

```python
# Save checkpoint during interrupt
async with graph.iter(inputs={...}) as run:
    async for event in run:
        if run.interrupted:
            saved = run.checkpoint()
            save_to_db(saved)
            break

# Later: resume
loaded = load_from_db(checkpoint_id)
async with graph.iter(checkpoint=loaded) as run:
    async for event in run:
        if run.interrupted:
            await run.respond({"user_decision": "approve"})
    
    print(run.result["output"])
```

---

## Identity Model

### Session ID

Groups related runs (conversations, workflows). **Auto-generated if not provided**.

```python
# Auto-generated session
result = graph.run(inputs={"query": "Hello"})
print(result.session_id)  # "sess_a1b2c3d4" (auto-generated)

# Continue the session
result2 = graph.run(
    inputs={"query": "Follow up"},
    session_id=result.session_id,  # Same session, new run
)

# User-provided for business semantics
result = graph.run(
    inputs={"order_id": "12345"},
    session_id="order-12345",  # Semantic identifier
)
```

### Run ID

Identifies a specific execution within a session. Always auto-generated (or user-provided for idempotency).

### Resume by Session ID

For convenience, you can resume from the last checkpoint of a session:

```python
# Resume from last saved checkpoint in session
result = graph.run(
    session_id="order-12345",
    resume=True,  # Load last checkpoint for this session
    inputs={"user_decision": "approve"},
)
```

**How it works:**
1. If `resume=True` and `session_id` provided → fetch last checkpoint from store
2. If checkpoint found → resume from it
3. If no checkpoint → start fresh run

**Requires a checkpointer:**
```python
graph = Graph(
    nodes=[...],
    checkpointer=SQLiteCheckpointer("./app.db"),  # Required for resume=True
)
```

**Explicit checkpoint still supported:**
```python
# Explicit checkpoint takes precedence
result = graph.run(
    checkpoint=specific_checkpoint,  # Use this exact checkpoint
    inputs={...},
)
```

| Parameter | Behavior |
|-----------|----------|
| `checkpoint=...` | Resume from specific checkpoint |
| `session_id=... + resume=True` | Fetch and resume last checkpoint |
| `session_id=...` (no resume) | New run, grouped in session |
| Neither | New session, new run |

### Mapping to External Systems

| HyperNodes | AG-UI | Langfuse | Temporal | DBOS |
|------------|-------|----------|----------|------|
| `session_id` | `threadId` | `sessionId` | `workflowId` | — |
| `run_id` | `runId` | `traceId` | `runId` | `workflowId` |
| `step_index` | — | span | event index | step number |

---

## Checkpoints & State

### Checkpoint Structure

```python
@dataclass
class Checkpoint:
    """Frozen execution state for resumption."""
    
    # Identity
    checkpoint_id: str
    session_id: str
    run_id: str
    
    # Execution history (handles cycles naturally)
    history: list[NodeExecution]  # Append-only log of ALL executions
    
    # Current accumulated state
    state: dict[str, Any]  # Latest value for each output
    
    # Position
    step_index: int  # Global counter, always increments
    
    # If paused at interrupt
    pending_interrupt: str | None  # Just the name (value is in state)
    
    # Metadata
    created_at: datetime
    graph_hash: str  # For version checking


@dataclass
class NodeExecution:
    """Single node execution record."""
    
    node_id: str
    step_index: int       # Logical ordering (see Parallel Execution below)
    iteration: int        # How many times this node has run (for cycles)
    inputs_hash: str      # Hash of inputs (not full values)
    outputs: dict[str, Any]
    cached: bool
    duration_ms: float
```

### Parallel Execution & Step Ordering

When nodes execute in parallel (e.g., via `AsyncEngine`), we need to handle `step_index` carefully.

**Approach: Batch-Based Indexing** (inspired by Temporal/DBOS)

Both Temporal and DBOS solve this by treating parallel operations as a single logical step:

| System | Parallel Model | Ordering |
|--------|----------------|----------|
| **Temporal** | Activities in parallel share same event batch | Event IDs within batch unordered |
| **DBOS** | Queue.enqueue → get_result pattern | Handles track individual items, workflow sees batch |

**HyperNodes approach:**

```python
@dataclass
class NodeExecution:
    node_id: str
    step_index: int       # Increments per "batch" of parallel nodes
    parallel_index: int   # Position within parallel batch (0 if sequential)
    iteration: int        # Cycle count
    ...
```

**Sequential execution:**
```
step_index=0: embed
step_index=1: retrieve  
step_index=2: generate
```

**Parallel execution:**
```
step_index=0: embed
step_index=1: retrieve_a (parallel_index=0)
step_index=1: retrieve_b (parallel_index=1)  # Same step_index!
step_index=1: retrieve_c (parallel_index=2)
step_index=2: generate
```

**Key properties:**
- `step_index` defines the **happens-before** relationship
- Within a step, `parallel_index` provides stable ordering (for replay determinism)
- Checkpoints save at step boundaries, not mid-parallel-batch
- Resume replays entire parallel batch if interrupted mid-batch

### Checkpoint vs Session ID

| Concept | Purpose | Use Case |
|---------|---------|----------|
| `session_id` | Identity/grouping | "Which conversation is this?" |
| `checkpoint` | State snapshot | "Resume exactly from here" |

```python
# session_id alone = new run, fresh state
graph.run(session_id="conv-123", inputs={"message": "Hi"})

# checkpoint = restore state and continue
graph.run(checkpoint=saved_checkpoint, inputs={"user_decision": "approve"})
```

### State Declaration: Auto-Inference

**Design principle:** Don't require users to declare state. The graph structure tells us exactly what's needed to resume.

**The algorithm:**

```python
def compute_checkpoint_outputs(graph, current_node, state):
    """Compute minimal outputs needed to resume from current_node."""
    
    # What does current_node (and downstream) need?
    required = graph.get_required_inputs(from_node=current_node)
    
    # What's already in state?
    available = set(state.keys())
    
    # Save only what's needed and available
    return {k: state[k] for k in required if k in available}
```

**Example 1: Large Intermediate Outputs (NOT saved)**

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐     ┌────────────┐
│ load_corpus │ ──► │ embed_all_docs   │ ──► │   index     │ ──► │   query    │
│ 10GB text   │     │ 100GB embeddings │     │ 1GB index   │     │   result   │
└─────────────┘     └──────────────────┘     └─────────────┘     └────────────┘
                            ▲
                            │
                    INTERRUPT HERE
```

If we interrupt after `embed_all_docs`:
- `query` needs: `index`
- `index` needs: `embeddings`
- `embeddings` is the current output → **must save (100GB)** 😢

But if we interrupt after `index`:
- `query` needs: `index` (1GB)
- `embeddings` NOT needed → **don't save** ✅

**Lesson:** Interrupt points matter. Place interrupts after "compression" nodes.

---

**Example 2: Branching Paths (selective saving)**

```
                              ┌─────────────┐
                         ┌──► │ summarize_a │ ──► result
┌──────────┐   ┌─────┐  │    └─────────────┘
│ fetch_doc│ ─►│route│ ─┤
│  50MB    │   └─────┘  │    ┌─────────────┐
└──────────┘            └──► │ summarize_b │ ──► result
                             └─────────────┘
                                    ▲
                               INTERRUPT
```

If interrupt is in `summarize_b` branch:
- `summarize_b` needs: `document`
- Save: `document` (50MB)
- `summarize_a` NOT on resume path → its inputs NOT saved

---

**Example 3: Cycles with Accumulating State**

```
                    ┌────────────────────────────────────┐
                    │                                    │
                    ▼                                    │
┌─────────┐   ┌──────────┐   ┌──────────┐   ┌────────┐  │   ┌────────┐
│  init   │ ─►│ generate │ ─►│ evaluate │ ─►│ revise │ ─┘   │ final  │
│messages │   │  draft   │   │  score   │   │ draft  │      │        │
└─────────┘   └──────────┘   └──────────┘   └────────┘      └────────┘
                                   │
                              INTERRUPT (human review)
```

At interrupt in `evaluate`:
- Resume will go to either `revise` (loop) or `final` (exit)
- `revise` needs: `draft`, `score`, `messages`
- `final` needs: `draft`
- Save: `messages`, `draft`, `score`

After 5 iterations:
- Checkpoint has: current `messages` (all 5 turns), current `draft`, current `score`
- Previous drafts NOT saved (not needed for resume)

---

**Example 4: Parallel Fan-Out (save at boundaries)**

```
                    ┌─────────────┐
               ┌──► │ analyze_1   │ ──┐
               │    │ (5min, 2GB) │   │
┌─────────┐   │    └─────────────┘   │    ┌──────────┐
│  split  │ ──┤                       ├──► │  merge   │
│         │   │    ┌─────────────┐   │    │          │
└─────────┘   └──► │ analyze_2   │ ──┘    └──────────┘
                   │ (5min, 2GB) │              ▲
                   └─────────────┘         INTERRUPT
```

If interrupt after `merge`:
- Resume needs: `merged_result`
- `analyze_1` output, `analyze_2` output NOT needed
- Save: just `merged_result`

If interrupt DURING parallel (one done, one pending):
- Must save completed analysis
- Resume will re-run pending one

**Rule:** Prefer interrupts at "join" points, not mid-parallel.

---

**Summary: Minimal Checkpoint Rules**

| Scenario | What's Saved | Why |
|----------|--------------|-----|
| Linear pipeline | Current node's inputs | Needed to resume |
| After compression | Compressed output only | Intermediate not needed |
| Branching | Active branch inputs only | Other branch irrelevant |
| Cycles | Latest values only | Previous iterations not needed |
| Parallel (at join) | Merged result only | Individual outputs not needed |
| Parallel (mid-execution) | Completed outputs | Will re-run incomplete |

---

## Three-Layer Architecture

HyperNodes supports three complementary layers without coupling to specific implementations:

| Layer | Purpose | Key Identifiers | Implementation |
|-------|---------|-----------------|----------------|
| **UI Protocol** | Stream events to frontend | `session_id`, `run_id` | Callback (e.g., `AGUIAdapter`) |
| **Observability** | Debugging, analytics | `trace_id`, `span_id` | Callback (e.g., `LangfuseAdapter`) |
| **Durability** | Resume after crash | `checkpoint_id` | Store (e.g., `PostgresCheckpointStore`) |

**Key distinction:** UI and Observability are callbacks (can fail gracefully). Durability is a store (integrated into engine, must succeed).

### Adapters

Each layer gets an adapter that translates HyperNodes events:

```python
graph = Graph(
    nodes=[...],
    callbacks=[
        # UI: stream to frontend
        AGUIAdapter(send_fn=websocket.send),
        
        # Observability: trace to Langfuse
        LangfuseAdapter(client=langfuse_client),
    ],
    
    # Durability: checkpointer at graph level (like LangGraph)
    checkpointer=SQLiteCheckpointer("./app.db"),
)
```

### Checkpointer Backends

**Protocol** (inspired by LangGraph's `BaseCheckpointSaver`):

```python
from typing import Protocol

class Checkpointer(Protocol):
    """Checkpoint persistence protocol."""
    
    def put(self, config: dict, checkpoint: Checkpoint) -> None:
        """Save a checkpoint."""
        ...
    
    def get(self, config: dict) -> Checkpoint | None:
        """Get checkpoint by config (thread_id, checkpoint_id)."""
        ...
    
    def get_latest(self, thread_id: str) -> Checkpoint | None:
        """Get most recent checkpoint for thread."""
        ...
    
    def list(self, thread_id: str) -> Iterator[Checkpoint]:
        """List all checkpoints for thread."""
        ...
```

**Built-in implementations:**

```python
from hypernodes.persistence import (
    MemoryCheckpointer,    # In-memory (dev/testing)
    FileCheckpointer,      # JSON files (simple, portable)
    SQLiteCheckpointer,    # SQLite (lightweight, single-file)
)

# Dev/testing
graph = Graph(nodes=[...], checkpointer=MemoryCheckpointer())

# Simple persistence
graph = Graph(nodes=[...], checkpointer=FileCheckpointer("./checkpoints"))

# Production-ready lightweight
graph = Graph(nodes=[...], checkpointer=SQLiteCheckpointer("./app.db"))
```

**Bring your own:** Implement the protocol for Postgres, Redis, S3, etc.

### Why Checkpointer is NOT a Callback

**Question:** Should checkpointing be a callback like other adapters?

**Answer:** No. Checkpointing is **infrastructure**, not **observation**.

| Aspect | Callbacks (UI, Observability) | Checkpoint Store |
|--------|-------------------------------|------------------|
| Purpose | React to events | Persist execution state |
| Failure mode | Degraded experience | Data loss |
| Ordering | Fire-and-forget OK | Must be synchronous |
| Streaming concern | ❌ None | ✅ Critical (see below) |

**The Streaming Problem:**

If checkpointing were a callback:
```python
# ❌ Dangerous: Callback might miss streaming data
class CheckpointCallback(GraphCallback):
    def on_streaming_chunk(self, event):
        self.pending_chunks.append(event.chunk)
    
    def on_streaming_end(self, event):
        self.save_checkpoint(...)  # What if crash before this?
```

If crash during streaming:
1. Chunks 1-50 received
2. Crash before `on_streaming_end`
3. Resume → Lost chunks, inconsistent state

**Solution: Integrated Checkpointer**

Checkpointing is a first-class execution concern, not an observer:

```python
class Checkpointer(Protocol):
    """Durability layer - integrated into engine, not a callback."""
    
    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        """Save checkpoint at step boundary or interrupt."""
        ...
    
    def load_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        """Load specific checkpoint."""
        ...
    
    def load_latest(self, thread_id: str) -> Checkpoint | None:
        """Load last checkpoint for thread."""
        ...
    
    def list_checkpoints(self, thread_id: str) -> list[Checkpoint]:
        """List all checkpoints for thread."""
        ...
```

**Engine calls checkpointer directly:**
```python
# Inside GraphEngine execution loop
for node in execution_order:
    result = execute_node(node, state)
    
    # Checkpoint at step boundaries
    if checkpointer and is_checkpoint_boundary(node):
        checkpointer.save_checkpoint(Checkpoint(...))
    
    if is_streaming(result):
        accumulated = []
        for chunk in result:
            yield StreamingChunkEvent(chunk=chunk)
            accumulated.append(chunk)
        # Checkpoint saves final accumulated value
```

**Key guarantees:**
1. ✅ Streaming values accumulated, checkpointed at completion
2. ✅ Checkpoints saved synchronously at step boundaries
3. ✅ Resume sees exactly what was produced before interruption
4. ✅ Callbacks still receive events (for UI streaming)

**Summary:**
- `callbacks=` for observation (UI, tracing) — can fail gracefully
- `checkpointer=` for durability — integrated into engine, must succeed

---

## Callbacks & Events

### Event Types

```python
# Lifecycle
RunStartEvent(session_id, run_id, inputs, graph_id)
RunEndEvent(session_id, run_id, status, outputs, duration_ms)
NodeStartEvent(node_id, step_index, inputs, node_type)
NodeEndEvent(node_id, step_index, outputs, duration_ms, cached)
NodeSkippedEvent(node_id, reason)

# Control flow
GateDecisionEvent(gate_id, decision, activated_targets)
InterruptEvent(interrupt_name, checkpoint_id)  # No prompt_value (already in state)
ResumeEvent(interrupt_name, response_value)

# State sync (for UI)
StateSnapshotEvent(state)
StateDeltaEvent(operations)  # JSON Patch

# Streaming
StreamingStartEvent(node_id, output_name)
StreamingChunkEvent(node_id, output_name, chunk, chunk_index)
StreamingEndEvent(node_id, output_name, final_value)
```

### Callback Protocol

```python
class GraphCallback:
    """Base class for graph callbacks."""
    
    @property
    def name(self) -> str: ...
    
    def on_run_start(self, event: RunStartEvent) -> None: ...
    def on_run_end(self, event: RunEndEvent) -> None: ...
    def on_node_start(self, event: NodeStartEvent) -> None: ...
    def on_node_end(self, event: NodeEndEvent) -> None: ...
    def on_interrupt(self, event: InterruptEvent) -> None: ...
    def on_resume(self, event: ResumeEvent) -> None: ...
    def on_streaming_chunk(self, event: StreamingChunkEvent) -> None: ...
    # ... etc
```

---

## Engines

Engines determine **how** nodes execute (sequential, parallel, distributed). Orthogonal to interaction mode.

### GraphEngine (Default)

The default engine handles all three execution methods:

```python
from hypernodes import Graph, GraphEngine

# GraphEngine is the default — no need to specify
graph = Graph(nodes=[embed, retrieve, generate])

# Supports all methods
result = graph.run(inputs={...})           # Sequential execution
results = graph.map(inputs={...}, ...)     # Batch with optional parallelism
async with graph.iter(inputs={...}) as r:  # Interactive streaming
    ...
```

**GraphEngine configuration:**

```python
engine = GraphEngine(
    # Parallelism for independent nodes
    parallel_nodes=True,     # Execute independent nodes concurrently
    max_workers=4,           # Thread/async limit
    
    # Batch optimization
    batch_size=100,          # For .map() operations
)

graph = Graph(nodes=[...], engine=engine)
```

### Specialized Engines

| Engine | Best For | Key Feature |
|--------|----------|-------------|
| `GraphEngine` | General use | Handles `.run()`, `.map()`, `.iter()` |
| `DaftEngine` | Distributed, large data | DataFrame-based execution |
| `DaskEngine` | Parallel batch | Dask Bag parallelism |

```python
# Distributed engine for large-scale batch
from hypernodes.engines import DaftEngine

graph = Graph(nodes=[...], engine=DaftEngine())
results = graph.map(inputs={"x": large_dataset}, map_over="x")
```

**Engine compatibility:**

| Engine | `.run()` | `.map()` | `.iter()` |
|--------|----------|----------|-----------|
| `GraphEngine` | ✅ | ✅ | ✅ |
| `DaftEngine` | ✅ | ✅ | ❌ (no streaming) |
| `DaskEngine` | ✅ | ✅ | ❌ |

---

## Caching

### Cache Scope

```python
from hypernodes import Graph, DiskCache

graph = Graph(
    nodes=[...],
    cache=DiskCache(
        path=".cache",
        scope="global",  # "global" | "session" | "run"
    ),
)
```

| Scope | Key Formula | Use Case |
|-------|-------------|----------|
| `global` | `hash(code + inputs)` | Same inputs → same result |
| `session` | `hash(code + inputs + session_id)` | Per-conversation cache |
| `run` | `hash(code + inputs + run_id)` | No cross-run caching |

### Cache vs Checkpoint on Resume

```python
# On resume from checkpoint:
# 1. Restore state (only state=True outputs) from checkpoint
# 2. For non-state outputs, check cache
# 3. Recompute if cache miss
```

---

## Type Reference

### GraphResult

Dict-like result for seamless simple-case usage:

```python
@dataclass
class GraphResult:
    # Always present
    outputs: dict[str, Any]
    session_id: str
    run_id: str
    status: Literal["complete", "interrupted", "error"]
    
    # Optional
    checkpoint: Checkpoint | None
    interrupt: Interrupt | None
    error: Exception | None
    history: list[NodeExecution] | None
    
    # Dict-like interface
    def __getitem__(self, key: str) -> Any:
        return self.outputs[key]
    
    def __contains__(self, key: str) -> bool:
        return key in self.outputs
    
    def keys(self): return self.outputs.keys()
    def values(self): return self.outputs.values()
    def items(self): return self.outputs.items()
    def get(self, key, default=None): return self.outputs.get(key, default)
    
    # Convenience
    @property
    def interrupted(self) -> bool:
        return self.status == "interrupted"
    
    def resume(self, response: dict) -> "GraphResult":
        """Resume from interrupt with response."""
        return self._graph.run(checkpoint=self.checkpoint, inputs=response)
```

### Graph

```python
class Graph:
    def __init__(
        self,
        nodes: list[HyperNode],
        engine: Engine | None = None,           # Default: GraphEngine()
        cache: Cache | None = None,
        callbacks: list[GraphCallback] | None = None,
        checkpointer: Checkpointer | None = None,  # For durability
    ): ...
    
    def run(
        self,
        inputs: dict[str, Any],
        session_id: str | None = None,
        checkpoint: Checkpoint | None = None,    # Explicit checkpoint
        resume: bool = False,                    # Load last checkpoint for session_id
        handlers: dict[str, Callable] | None = None,
    ) -> GraphResult: ...
    
    def map(
        self,
        inputs: dict[str, Any],
        map_over: str | list[str],
        map_mode: Literal["zip", "product"] = "zip",
    ) -> list[GraphResult]: ...
    
    def iter(
        self,
        inputs: dict[str, Any] | None = None,
        checkpoint: Checkpoint | None = None,
        resume: bool = False,
        session_id: str | None = None,
    ) -> GraphRun: ...
    
    def on_interrupt(self, name: str) -> Callable:
        """Decorator to register interrupt handler."""
        ...
```

### GraphRun

```python
class GraphRun:
    """Context manager for interactive execution."""
    
    session_id: str
    run_id: str
    
    async def __aenter__(self) -> "GraphRun": ...
    async def __aexit__(self, *args): ...
    def __aiter__(self) -> AsyncIterator[GraphEvent]: ...
    
    @property
    def interrupted(self) -> bool: ...
    
    @property
    def interrupt(self) -> Interrupt | None: ...
    
    @property
    def state(self) -> dict[str, Any]: ...
    
    @property
    def result(self) -> GraphResult | None: ...
    
    async def respond(self, response: dict[str, Any]) -> None: ...
    
    def checkpoint(self) -> Checkpoint: ...
```

---

## Summary: Progressive Complexity

```python
# Level 1: Simple DAG
result = graph.run(inputs={"x": 5})
print(result["output"])

# Level 2: Add caching
graph = Graph(nodes=[...], cache=DiskCache("./cache"))

# Level 3: Add branching/gates
# (same API, routing happens internally)

# Level 4: Add cycles
# (history tracks iterations)

# Level 5: Add interrupts with handlers
@graph.on_interrupt("approval")
async def handle(prompt): ...
result = await graph.run(inputs={...})

# Level 6: Full interactive control
async with graph.iter(inputs={...}) as run:
    async for event in run:
        if run.interrupted:
            await run.respond({...})

# Level 7: Batch processing
results = graph.map(inputs={"x": [1,2,3]}, map_over="x")

# Level 8: Add durability (resume after crash)
graph = Graph(
    nodes=[...],
    checkpointer=SQLiteCheckpointer("./app.db"),
)
result = graph.run(session_id="conv-123", resume=True, inputs={...})
# Checkpoint auto-saves minimal outputs needed to resume

# Level 9: Distributed execution
graph = Graph(nodes=[...], engine=DaftEngine())
```

The simple case stays simple. Complex features are available when needed.
