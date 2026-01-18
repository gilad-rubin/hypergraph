# Streaming

Stream LLM responses token-by-token using async generators. Users see output as it's generated, not after the full response completes.

## When to Use

- **Chat interfaces** — Show responses as they're generated
- **Long-form content** — Don't make users wait for full generation
- **Real-time feedback** — Stream progress indicators

## Basic Pattern

Use async generators to yield tokens as they arrive:

```python
from hypergraph import Graph, node, AsyncRunner
from anthropic import Anthropic

client = Anthropic()

@node(output_name="response")
async def stream_response(messages: list, system: str = "") -> str:
    """Stream tokens from Claude, yielding as they arrive."""

    chunks = []

    with client.messages.stream(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        system=system,
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)  # Stream to user
            chunks.append(text)

    print()  # Newline after streaming
    return "".join(chunks)


graph = Graph([stream_response])
runner = AsyncRunner()

result = await runner.run(graph, {
    "messages": [{"role": "user", "content": "Explain quantum computing"}],
    "system": "You are a helpful physics tutor.",
})
```

## Streaming with OpenAI

```python
from openai import OpenAI

client = OpenAI()

@node(output_name="response")
async def stream_openai(prompt: str, instructions: str = "") -> str:
    """Stream tokens from GPT-5.2 using the Responses API."""

    stream = client.responses.create(
        model="gpt-5.2",
        input=prompt,
        instructions=instructions,
        stream=True,
    )

    chunks = []
    for part in stream:
        if part.output_text:
            print(part.output_text, end="", flush=True)
            chunks.append(part.output_text)

    print()
    return "".join(chunks)
```

## Async Generator Nodes

For true async iteration, use async generators:

```python
from typing import AsyncIterator

@node(output_name="tokens")
async def generate_tokens(prompt: str) -> AsyncIterator[str]:
    """Yield tokens one at a time."""

    with client.messages.stream(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            yield text


# Check node properties
print(generate_tokens.is_async)      # True
print(generate_tokens.is_generator)  # True
```

## Streaming in RAG Pipelines

Combine retrieval (fast) with streaming generation:

```python
@node(output_name="docs")
async def retrieve(query: str) -> list[str]:
    """Fast retrieval - no need to stream."""
    embedding = await embedder.embed(query)
    return await vector_db.search(embedding, k=5)

@node(output_name="response")
async def generate(docs: list[str], query: str) -> str:
    """Stream the generation step."""

    context = "\n\n---\n\n".join(docs)

    with client.messages.stream(
        model="claude-sonnet-4-5-20250929",
        max_tokens=2048,
        system=f"Answer based on this context:\n{context}",
        messages=[{"role": "user", "content": query}],
    ) as stream:
        chunks = []
        for text in stream.text_stream:
            print(text, end="", flush=True)
            chunks.append(text)

    print()
    return "".join(chunks)


rag_pipeline = Graph([retrieve, generate])

runner = AsyncRunner()
result = await runner.run(rag_pipeline, {"query": "How do I use hypergraph?"})
```

## Streaming with Callbacks

Pass a callback for custom handling:

```python
from typing import Callable

@node(output_name="response")
async def generate_with_callback(
    prompt: str,
    on_token: Callable[[str], None] | None = None,
) -> str:
    """Stream tokens with optional callback."""

    chunks = []

    with client.messages.stream(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            if on_token:
                on_token(text)
            chunks.append(text)

    return "".join(chunks)


# Usage with callback
def handle_token(token: str):
    # Send to websocket, update UI, etc.
    websocket.send(token)

result = await runner.run(graph, {
    "prompt": "Write a story",
    "on_token": handle_token,
})
```

## Multi-Turn Streaming

Stream responses in a conversation loop:

```python
from hypergraph import route, END

@node(output_name="response")
async def stream_turn(messages: list, user_input: str) -> str:
    """Stream one conversation turn."""

    full_messages = messages + [{"role": "user", "content": user_input}]

    chunks = []
    print("Assistant: ", end="")

    with client.messages.stream(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        messages=full_messages,
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            chunks.append(text)

    print("\n")
    return "".join(chunks)

@node(output_name="messages")
def update_history(messages: list, user_input: str, response: str) -> list:
    return messages + [
        {"role": "user", "content": user_input},
        {"role": "assistant", "content": response},
    ]

@route(targets=["stream_turn", END])
def should_continue(messages: list) -> str:
    if len(messages) >= 20:
        return END
    return "stream_turn"

streaming_chat = Graph([stream_turn, update_history, should_continue])
```

## Error Handling in Streams

Handle streaming errors gracefully:

```python
@node(output_name="response")
async def safe_stream(prompt: str) -> str:
    """Stream with error handling."""

    chunks = []

    try:
        with client.messages.stream(
            model="claude-sonnet-4-5-20250929",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
                chunks.append(text)

        print()
        return "".join(chunks)

    except Exception as e:
        # Return partial response if available
        if chunks:
            return "".join(chunks) + f"\n\n[Error: {e}]"
        raise
```

## Testing Streaming Nodes

Test the accumulated output:

```python
@pytest.mark.asyncio
async def test_streaming():
    runner = AsyncRunner()

    result = await runner.run(stream_response, {
        "messages": [{"role": "user", "content": "Say hello"}],
    })

    assert "response" in result
    assert len(result["response"]) > 0
```

## What's Next?

- [Multi-Turn RAG](../04-real-world/multi-turn-rag.md) — Streaming in conversations
- [Integrate with LLMs](../05-how-to/integrate-with-llms.md) — OpenAI and Anthropic patterns
