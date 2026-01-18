# Multi-Turn RAG

A conversational RAG system where users can ask follow-up questions. The system retrieves new context based on the evolving conversation.

## Why This Example?

This showcases hypergraph's key strength: **a DAG (retrieval) nested inside a cycle (conversation)**.

Pure DAG frameworks can't do this — they can't loop back for follow-up questions. Hypergraph handles it naturally.

## The Architecture

```
┌────────────────────────────────────────────────────────────┐
│                   CONVERSATION LOOP                        │
│                                                            │
│  user_input → RAG_PIPELINE → accumulate → should_continue  │
│       ↑           │                            │           │
│       │           ▼                            │           │
│       │    ┌─────────────┐                     │           │
│       │    │ embed       │                     │           │
│       │    │     ↓       │                     │           │
│       │    │ retrieve    │ (DAG)               │           │
│       │    │     ↓       │                     │           │
│       │    │ generate    │                     │           │
│       │    └─────────────┘                     │           │
│       │                                        │           │
│       └────────────────────────────────────────┘           │
│                                        │                   │
│                                        ▼                   │
│                                       END                  │
└────────────────────────────────────────────────────────────┘
```

## Complete Implementation

```python
from hypergraph import Graph, node, route, END, AsyncRunner

# ═══════════════════════════════════════════════════════════════
# RAG PIPELINE (DAG) — Runs once per conversation turn
# ═══════════════════════════════════════════════════════════════

@node(output_name="query_embedding")
async def embed_query(user_input: str, history: list) -> list[float]:
    """
    Embed the query with conversation context.
    Include recent history for better retrieval.
    """
    # Build context-aware query
    recent_context = ""
    if history:
        recent_exchanges = history[-4:]  # Last 2 exchanges
        recent_context = "\n".join(
            f"{msg['role']}: {msg['content']}" for msg in recent_exchanges
        )

    contextualized_query = f"{recent_context}\n\nCurrent question: {user_input}"
    return await embedder.embed(contextualized_query)


@node(output_name="retrieved_docs")
async def retrieve(query_embedding: list[float], history: list) -> list[str]:
    """
    Retrieve relevant documents.
    Adjust retrieval based on conversation state.
    """
    # More documents for follow-up questions (they're often more specific)
    k = 3 if len(history) == 0 else 5

    results = await vector_db.search(query_embedding, k=k)
    return [doc["content"] for doc in results]


@node(output_name="response")
async def generate(
    retrieved_docs: list[str],
    user_input: str,
    history: list,
    system_prompt: str = "You are a helpful assistant.",
) -> str:
    """Generate response using retrieved context and conversation history."""
    # Build the context block
    context = "\n\n---\n\n".join(retrieved_docs)

    # Build messages for the LLM
    messages = [{"role": "system", "content": f"{system_prompt}\n\nContext:\n{context}"}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_input})

    return await llm.chat(messages)


# The RAG pipeline as a composable unit
rag_pipeline = Graph(
    [embed_query, retrieve, generate],
    name="rag",
)


# ═══════════════════════════════════════════════════════════════
# CONVERSATION LOOP — Wraps the RAG pipeline
# ═══════════════════════════════════════════════════════════════

@node(output_name="history")
def accumulate_history(history: list, user_input: str, response: str) -> list:
    """Update conversation history with the new exchange."""
    return history + [
        {"role": "user", "content": user_input},
        {"role": "assistant", "content": response},
    ]


@route(targets=["rag", END])
def should_continue(history: list, response: str) -> str:
    """
    Decide if the conversation should continue.

    In a real system, this might:
    - Check for explicit end signals ("goodbye", "thanks, that's all")
    - Enforce a maximum turn limit
    - Detect conversation completion
    """
    # Check for end signals in the last response
    end_signals = ["goodbye", "have a great day", "is there anything else"]
    if any(signal in response.lower() for signal in end_signals):
        return END

    # Limit conversation length
    if len(history) >= 20:  # 10 exchanges
        return END

    return "rag"  # Continue the conversation


# Compose the full conversation system
conversation = Graph(
    [
        rag_pipeline.as_node(),  # DAG nested in cycle
        accumulate_history,
        should_continue,
    ],
    name="multi_turn_rag",
)


# ═══════════════════════════════════════════════════════════════
# RUNNING THE CONVERSATION
# ═══════════════════════════════════════════════════════════════

async def chat_session():
    """Interactive chat session."""
    runner = AsyncRunner()

    # Initial state
    state = {
        "history": [],
        "system_prompt": "You are a helpful coding assistant with access to documentation.",
    }

    print("Chat started. Type 'quit' to exit.\n")

    while True:
        user_input = input("You: ").strip()
        if user_input.lower() == "quit":
            break

        # Run one turn
        result = await runner.run(
            conversation,
            {**state, "user_input": user_input},
        )

        print(f"Assistant: {result['response']}\n")

        # Update state for next turn
        state["history"] = result["history"]

        # Check if conversation ended naturally
        if result.get("_terminated"):
            print("(Conversation ended)")
            break


# Run the chat
# asyncio.run(chat_session())
```

## Key Design Decisions

### 1. Context-Aware Embedding

The query embedding includes recent conversation history:

```python
contextualized_query = f"{recent_context}\n\nCurrent question: {user_input}"
```

This helps retrieval understand follow-up questions like "Tell me more about that" or "What about the second point?"

### 2. Adaptive Retrieval

Adjust retrieval strategy based on conversation state:

```python
k = 3 if len(history) == 0 else 5
```

Initial questions get fewer documents (broad context). Follow-ups get more (specific details).

### 3. Clean History Management

The `accumulate_history` node handles state updates:

```python
return history + [
    {"role": "user", "content": user_input},
    {"role": "assistant", "content": response},
]
```

No mutation. No side effects. Just pure data transformation.

### 4. Flexible Termination

The routing logic can terminate based on multiple conditions:

```python
@route(targets=["rag", END])
def should_continue(history: list, response: str) -> str:
    # Content-based termination
    if any(signal in response.lower() for signal in end_signals):
        return END
    # Length-based termination
    if len(history) >= 20:
        return END
    return "rag"
```

## Testing the System

Test the RAG pipeline independently:

```python
async def test_rag_pipeline():
    runner = AsyncRunner()

    result = await runner.run(rag_pipeline, {
        "user_input": "How do I create a graph?",
        "history": [],
    })

    assert "response" in result
    assert len(result["retrieved_docs"]) > 0
```

Test the full conversation:

```python
async def test_multi_turn():
    runner = AsyncRunner()

    # Simulate a conversation
    result = await runner.run(conversation, {
        "user_input": "What is hypergraph?",
        "history": [],
    })

    # Continue the conversation
    result = await runner.run(conversation, {
        "user_input": "How do I install it?",
        "history": result["history"],
    })

    assert len(result["history"]) == 4  # 2 exchanges
```

## Extending the Pattern

### Add Streaming

```python
@node(output_name="response")
async def generate(retrieved_docs: list[str], user_input: str, history: list) -> str:
    # ... same setup ...

    response_chunks = []
    async for chunk in llm.stream(messages):
        print(chunk, end="", flush=True)  # Stream to user
        response_chunks.append(chunk)

    return "".join(response_chunks)
```

### Add Memory Summarization

For long conversations, summarize older history:

```python
@node(output_name="compressed_history")
def compress_history(history: list) -> list:
    if len(history) <= 10:
        return history

    # Keep recent messages, summarize older ones
    recent = history[-6:]
    older = history[:-6]

    summary = llm.summarize(older)
    return [{"role": "system", "content": f"Previous conversation summary: {summary}"}] + recent
```

### Add Citation Tracking

```python
@node(output_name=("response", "citations"))
async def generate_with_citations(retrieved_docs: list[str], ...) -> tuple[str, list]:
    response = await llm.chat(...)

    # Extract which docs were actually used
    citations = [doc for doc in retrieved_docs if doc[:50] in response]

    return response, citations
```

## What's Next?

- [Evaluation Harness](evaluation-harness.md) — Test this conversation system at scale
- [Hierarchical Composition](../03-patterns/04-hierarchical.md) — More nesting patterns
