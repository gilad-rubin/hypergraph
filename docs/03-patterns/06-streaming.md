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

`ctx.stream()` emits `StreamingChunkEvent`s through the event system. Event processors remain useful for a host-owned sink:

```python
from hypergraph import TypedEventProcessor, StreamingChunkEvent

class StreamToWebSocket(TypedEventProcessor):
    def on_streaming_chunk(self, event: StreamingChunkEvent):
        websocket.send(event.chunk)

result = await runner.run(graph, values, event_processors=[StreamToWebSocket()])
```

For hosts that need to pull a run's existing typed event stream, `AsyncRunner.iter()` returns a context-managed handle. It yields lifecycle events and delivered preview chunks in their execution order; after the stream ends, `result()` returns the normal `RunResult` (and raises the run failure by default):

```python
from hypergraph import AsyncRunner, NodeEndEvent, StreamingChunkEvent

runner = AsyncRunner()

async with runner.iter(graph, values) as handle:
    async for event in handle:
        if isinstance(event, StreamingChunkEvent):
            await websocket.send_text(event.chunk)
        elif isinstance(event, NodeEndEvent):
            logger.info("%s finished", event.node_name)

result = await handle.result()
```

The context manager is required. Leaving it early cancels the live run and waits for processors to settle. The handle retains at most **128 events** by default (`buffer_size=` changes that limit). Lifecycle events are never dropped; if a slow consumer fills the buffer, the oldest queued `StreamingChunkEvent` is discarded first. `handle.dropped_chunks` reports the total discarded preview chunks. Preview delivery therefore never backpressures graph execution; hosts that require every chunk should use their own event processor and transport policy.

## Streaming Batch Results with map_iter()

Streaming also applies at the batch level. When you map a graph over many inputs, `runner.map_iter()` yields each item's `RunResult` as it completes — instead of buffering the whole batch into one `MapResult` like `runner.map()` does:

```python
queries = [
    "How do I install hypergraph?",
    "What is a nested graph?",
    "How does caching work?",
]

runner = AsyncRunner()

async for index, result in runner.map_iter(
    rag_pipeline,
    {"query": queries},
    map_over="query",
):
    send_to_client(index, result["response"])
```

- **Incremental** — each `(index, result)` pair is yielded as soon as that item finishes; `index` is the input item's position, so you can correlate results even when they arrive out of order.
- **Bounded memory** — nothing accumulates: the sync form pulls one input item at a time through a lazy generator; the async form buffers completed results in a bounded queue, so a slow consumer pauses production (backpressure).
- **Concurrent (async)** — `AsyncRunner.map_iter()` runs items concurrently and yields in completion order; pass `max_concurrency=` to cap the parallelism. `SyncRunner.map_iter()` runs items one at a time and yields in input order.
- **Error handling** — the default `error_handling="raise"` re-raises when a failed item is reached; `error_handling="continue"` yields the failed `RunResult` and keeps going.

The sync form is a plain generator:

```python
from hypergraph import SyncRunner

runner = SyncRunner()

for index, result in runner.map_iter(rag_pipeline, {"query": queries}, map_over="query"):
    print(f"[{index}] {result['response']}")
```

See [Batch Processing](../05-how-to/batch-processing.md) for the buffering counterpart, `runner.map()`.

### map_iter() Is Not a Background Handle

Choose by what the caller needs:

- `map_iter()` streams completed item results to the consumer and does not
  accumulate a `MapResult`.
- `start_map()` returns a process-local control handle immediately, accumulates
  the settled `MapResult`, and supports cooperative `handle.stop()`.

`map_iter()` streams completed map items; `AsyncRunner.iter()` streams events
from one graph run. Background handles returned by `start_map()` do not expose
an item iterator. If a UI needs both live progress and a final background map
result, attach an event processor to `start_map()` and retrieve the result
after settlement.

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

When the code that serves the Stop action already owns a background handle, no
workflow ID is required:

```python
handle = runner.start_run(streaming_chat, values)
# Later:
handle.stop(info={"kind": "user_stop"})
result = await handle.result(raise_on_failure=False)
```

Both forms set the same cooperative signal. They do not hard-cancel the
framework task; streaming nodes observe `ctx.stop_requested` and return at a
safe boundary.

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
