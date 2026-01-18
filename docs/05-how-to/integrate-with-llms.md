# How to Integrate with LLMs

Patterns for using OpenAI, Anthropic, and other LLM providers with hypergraph.

## Anthropic Claude

### Setup

```bash
pip install anthropic
```

```python
import os
from anthropic import Anthropic

client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
```

### Basic Message

```python
from hypergraph import node

@node(output_name="response")
def generate(prompt: str, system: str = "") -> str:
    """Generate a response using Claude Sonnet 4.5."""

    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )

    return message.content[0].text
```

### Streaming

```python
@node(output_name="response")
def stream_claude(prompt: str, system: str = "") -> str:
    """Stream response from Claude."""

    chunks = []

    with client.messages.stream(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            chunks.append(text)

    print()
    return "".join(chunks)
```

### Multi-Turn Conversation

```python
@node(output_name="response")
def chat(messages: list, system: str = "") -> str:
    """Multi-turn chat with Claude."""

    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        system=system,
        messages=messages,  # List of {"role": "user/assistant", "content": "..."}
    )

    return message.content[0].text
```

### Model Options

| Model | Use Case |
|-------|----------|
| `claude-opus-4-5-20251101` | Complex reasoning, analysis, coding |
| `claude-sonnet-4-5-20250929` | Balanced performance and cost |
| `claude-haiku-4-5` | Fast, cost-efficient for simple tasks |

---

## OpenAI GPT

### Setup

```bash
pip install openai
```

```python
import os
from openai import OpenAI

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
```

### Basic Response (Responses API)

```python
@node(output_name="response")
def generate(prompt: str, instructions: str = "") -> str:
    """Generate a response using GPT-5.2."""

    response = client.responses.create(
        model="gpt-5.2",
        input=prompt,
        instructions=instructions,
    )

    return response.output_text
```

### Streaming

```python
@node(output_name="response")
def stream_gpt(prompt: str, instructions: str = "") -> str:
    """Stream response from GPT-5.2."""

    chunks = []

    stream = client.responses.create(
        model="gpt-5.2",
        input=prompt,
        instructions=instructions,
        stream=True,
    )

    for part in stream:
        if part.output_text:
            print(part.output_text, end="", flush=True)
            chunks.append(part.output_text)

    print()
    return "".join(chunks)
```

### Multi-Turn with State

The Responses API supports stateful conversations:

```python
@node(output_name=("response", "response_id"))
def chat_turn(prompt: str, previous_response_id: str | None = None) -> tuple[str, str]:
    """Single turn in a stateful conversation."""

    response = client.responses.create(
        model="gpt-5.2",
        input=prompt,
        previous_response_id=previous_response_id,
        store=True,  # Enable state storage
    )

    return response.output_text, response.id
```

### With Tools

```python
@node(output_name="response")
def generate_with_tools(prompt: str) -> str:
    """Use GPT-5.2 with built-in tools."""

    response = client.responses.create(
        model="gpt-5.2",
        input=prompt,
        tools=[
            {"type": "web_search"},
            {"type": "code_interpreter", "container": {"type": "auto"}},
        ],
    )

    return response.output_text
```

### Model Options

| Model | Use Case |
|-------|----------|
| `gpt-5.2` | Latest, best for coding and agentic tasks |
| `gpt-5-mini` | Faster, cost-efficient |
| `o3` | Reasoning model for complex problems |

---

## RAG Pattern

Combine retrieval with LLM generation:

```python
from hypergraph import Graph, node, AsyncRunner

@node(output_name="embedding")
async def embed(query: str) -> list[float]:
    """Embed query for retrieval."""
    response = client.embeddings.create(
        model="text-embedding-3-large",
        input=query,
    )
    return response.data[0].embedding

@node(output_name="docs")
async def retrieve(embedding: list[float]) -> list[str]:
    """Search vector database."""
    results = await vector_db.search(embedding, k=5)
    return [doc["content"] for doc in results]

@node(output_name="answer")
def generate(docs: list[str], query: str) -> str:
    """Generate answer using retrieved context."""

    context = "\n\n---\n\n".join(docs)

    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=2048,
        system=f"Answer based on this context:\n\n{context}",
        messages=[{"role": "user", "content": query}],
    )

    return message.content[0].text


rag_pipeline = Graph([embed, retrieve, generate])
```

---

## Structured Outputs

### With Anthropic

```python
from pydantic import BaseModel

class Analysis(BaseModel):
    sentiment: str
    confidence: float
    topics: list[str]

@node(output_name="analysis")
def analyze(text: str) -> Analysis:
    """Extract structured data from text."""

    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"Analyze this text and return JSON:\n\n{text}",
        }],
    )

    # Parse the JSON response
    import json
    data = json.loads(message.content[0].text)
    return Analysis(**data)
```

### With OpenAI

```python
@node(output_name="analysis")
def analyze(text: str) -> dict:
    """Extract structured data using GPT-5.2."""

    response = client.responses.create(
        model="gpt-5.2",
        input=f"Analyze this text:\n\n{text}",
        text={"format": {"type": "json_object"}},
    )

    import json
    return json.loads(response.output_text)
```

---

## Error Handling

```python
from anthropic import APIError, RateLimitError

@node(output_name="response")
def safe_generate(prompt: str, max_retries: int = 3) -> str:
    """Generate with retry logic."""

    for attempt in range(max_retries):
        try:
            message = client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text

        except RateLimitError:
            if attempt < max_retries - 1:
                import time
                time.sleep(2 ** attempt)  # Exponential backoff
                continue
            raise

        except APIError as e:
            raise RuntimeError(f"API error: {e}")
```

---

## Testing LLM Nodes

Mock the client for unit tests:

```python
from unittest.mock import MagicMock, patch

def test_generate():
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="Test response")]

    with patch.object(client.messages, "create", return_value=mock_response):
        result = generate.func("Test prompt")
        assert result == "Test response"
```

Or use dependency injection:

```python
@node(output_name="response")
def generate(prompt: str, client: Anthropic | None = None) -> str:
    """Generate with injectable client."""
    client = client or Anthropic()

    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    return message.content[0].text


# In tests:
def test_generate():
    mock_client = MagicMock()
    mock_client.messages.create.return_value.content = [MagicMock(text="Mocked")]

    result = generate.func("Test", client=mock_client)
    assert result == "Mocked"
```

## What's Next?

- [Streaming](../03-patterns/06-streaming.md) — Token-by-token streaming patterns
- [Multi-Turn RAG](../04-real-world/multi-turn-rag.md) — Conversational RAG example
