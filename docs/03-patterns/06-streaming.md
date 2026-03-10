# Streaming

Stream LLM responses token-by-token so users see output as it's generated, not after the full response completes.

## When to Use

- **Chat interfaces** — Show responses as they're generated
- **Long-form content** — Don't make users wait for full generation
- **Stoppable nodes** — Let users cancel long-running generation

## Basic Pattern: NodeContext

Use `NodeContext` to stream tokens and support cooperative stop:

```python
from hypergraph import Graph, node, AsyncRunner, NodeContext
from anthropic import Anthropic

client = Anthropic()

@node(output_name="response")
async def stream_response(messages: list, ctx: NodeContext, system: str = "") -> str:
    """Stream tokens from Claude with stop support."""
    response = ""

    with client.messages.stream(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        system=system,
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            if ctx.stop_requested:
                break
            response += text
            ctx.stream(text)  # emit StreamingChunkEvent for live UI

    return response


graph = Graph([stream_response])
runner = AsyncRunner()

result = await runner.run(graph, {
    "messages": [{"role": "user", "content": "Explain quantum computing"}],
    "system": "You are a helpful physics tutor.",
})
```

`ctx.stream(chunk)` emits a `StreamingChunkEvent` — a side-channel for live UI preview. It does not affect the return value. The node controls its own output type.

`ctx.stop_requested` is a cooperative stop signal. The node checks it and decides when to break. See [NodeContext API](../06-api-reference/nodes.md#nodecontext) for details.

Adding `ctx: NodeContext` is optional. Nodes without it work exactly as before — the framework detects the type hint and injects it automatically (same pattern as FastAPI's `Request`).

## Streaming with OpenAI

```python
from openai import OpenAI

client = OpenAI()

@node(output_name="response")
async def stream_openai(prompt: str, ctx: NodeContext, instructions: str = "") -> str:
    """Stream tokens from GPT-5.2 with stop support."""
    response = ""

    stream = client.responses.create(
        model="gpt-5.2",
        input=prompt,
        instructions=instructions,
        stream=True,
    )

    for part in stream:
        if ctx.stop_requested:
            break
        if part.output_text:
            response += part.output_text
            ctx.stream(part.output_text)

    return response
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
async def generate(docs: list[str], query: str, ctx: NodeContext) -> str:
    """Stream the generation step with stop support."""
    context = "\n\n---\n\n".join(docs)
    response = ""

    with client.messages.stream(
        model="claude-sonnet-4-5-20250929",
        max_tokens=2048,
        system=f"Answer based on this context:\n{context}",
        messages=[{"role": "user", "content": query}],
    ) as stream:
        for text in stream.text_stream:
            if ctx.stop_requested:
                break
            response += text
            ctx.stream(text)

    return response


rag_pipeline = Graph([retrieve, generate])

runner = AsyncRunner()
result = await runner.run(rag_pipeline, {"query": "How do I use hypergraph?"})
```

## Consuming Streaming Events

`ctx.stream()` emits `StreamingChunkEvent`s through the event system. Consume them with an event processor or via `.iter()`:

```python
from hypergraph import TypedEventProcessor, StreamingChunkEvent

# Option 1: Event processor (works with run())
class StreamToWebSocket(TypedEventProcessor):
    def on_streaming_chunk(self, event: StreamingChunkEvent):
        websocket.send(event.chunk)

result = await runner.run(graph, values, event_processors=[StreamToWebSocket()])

# Option 2: .iter() for interactive sessions (Phase 2)
async with runner.iter(graph, workflow_id="chat-1", **values) as handle:
    async for event in handle:
        match event:
            case StreamingChunkEvent(chunk=chunk):
                send_to_client(chunk)
```

## Multi-Turn Streaming with Stop

Stream responses in a conversation loop with stop support:

```python
from hypergraph import route, END, NodeContext

@node(output_name="response")
async def stream_turn(messages: list, user_input: str, ctx: NodeContext) -> str:
    """Stream one conversation turn, stoppable."""
    full_messages = messages + [{"role": "user", "content": user_input}]
    response = ""

    with client.messages.stream(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        messages=full_messages,
    ) as stream:
        for text in stream.text_stream:
            if ctx.stop_requested:
                break
            response += text
            ctx.stream(text)

    return response

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

To stop mid-stream from another coroutine or endpoint:

```python
runner.stop(workflow_id, info={"kind": "user_stop"})
```

## Error Handling in Streams

Handle streaming errors gracefully:

```python
@node(output_name="response")
async def safe_stream(prompt: str, ctx: NodeContext) -> str:
    """Stream with error handling."""
    response = ""

    try:
        with client.messages.stream(
            model="claude-sonnet-4-5-20250929",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                if ctx.stop_requested:
                    break
                response += text
                ctx.stream(text)

        return response

    except Exception as e:
        if response:
            return response + f"\n\n[Error: {e}]"
        raise
```

## Testing Streaming Nodes

Nodes with `NodeContext` are testable as plain Python — pass a mock:

```python
from unittest.mock import MagicMock

@pytest.mark.asyncio
async def test_stream_response():
    ctx = MagicMock(spec=NodeContext)
    ctx.stop_requested = False

    result = await stream_response(
        messages=[{"role": "user", "content": "Say hello"}],
        ctx=ctx,
    )
    assert len(result) > 0
    assert ctx.stream.called  # chunks were emitted

@pytest.mark.asyncio
async def test_stream_stops():
    ctx = MagicMock(spec=NodeContext)
    ctx.stop_requested = True

    result = await stream_response(
        messages=[{"role": "user", "content": "Write a novel"}],
        ctx=ctx,
    )
    assert result == ""  # stopped immediately
```

No framework setup needed — the function is a plain async function.

## What's Next?

- [NodeContext API](../06-api-reference/nodes.md#nodecontext) — Full API reference
- [Multi-Turn RAG](../04-real-world/multi-turn-rag.md) — Streaming in conversations
- [Integrate with LLMs](../05-how-to/integrate-with-llms.md) — OpenAI and Anthropic patterns
