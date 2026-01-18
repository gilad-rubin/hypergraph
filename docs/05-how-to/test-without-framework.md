# How to Test Without the Framework

Hypergraph nodes are pure functions. Test them directly — no framework setup, no mocking.

## The Core Pattern

Every node has a `.func` attribute that gives you the raw function:

```python
@node(output_name="result")
def process(text: str) -> str:
    return text.upper()

# Test the function directly
def test_process():
    result = process.func("hello")
    assert result == "HELLO"
```

## Why This Works

The `@node` decorator adds metadata (inputs, outputs, name) but doesn't change the function's behavior:

```python
@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

# These are equivalent:
double(5)       # Call through the node wrapper → 10
double.func(5)  # Call the raw function → 10
```

## Testing Patterns

### Unit Test a Single Node

```python
import pytest
from myapp.nodes import embed, retrieve, generate

def test_embed():
    result = embed.func("hello world")

    assert isinstance(result, list)
    assert len(result) == 768  # Embedding dimension
    assert all(isinstance(x, float) for x in result)

def test_retrieve():
    fake_embedding = [0.1] * 768
    docs = retrieve.func(fake_embedding, k=3)

    assert isinstance(docs, list)
    assert len(docs) <= 3

def test_generate():
    docs = ["Document 1", "Document 2"]
    query = "What is the answer?"

    response = generate.func(docs, query)

    assert isinstance(response, str)
    assert len(response) > 0
```

### Test with Mocked Dependencies

```python
from unittest.mock import patch, MagicMock

@node(output_name="response")
def call_llm(prompt: str) -> str:
    return llm_client.generate(prompt)

def test_call_llm_with_mock():
    with patch("myapp.nodes.llm_client") as mock_client:
        mock_client.generate.return_value = "Mocked response"

        result = call_llm.func("test prompt")

        assert result == "Mocked response"
        mock_client.generate.assert_called_once_with("test prompt")
```

### Test Async Nodes

```python
import pytest

@node(output_name="data")
async def fetch(url: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        return response.json()

@pytest.mark.asyncio
async def test_fetch():
    result = await fetch.func("https://api.example.com/data")

    assert "id" in result
```

### Test Multiple Outputs

```python
@node(output_name=("mean", "std"))
def statistics(data: list) -> tuple[float, float]:
    m = sum(data) / len(data)
    s = (sum((x - m) ** 2 for x in data) / len(data)) ** 0.5
    return m, s

def test_statistics():
    mean, std = statistics.func([1, 2, 3, 4, 5])

    assert mean == 3.0
    assert abs(std - 1.414) < 0.01
```

## Testing Graphs

### Test Graph Construction

```python
from hypergraph import Graph

def test_graph_builds():
    graph = Graph([node_a, node_b, node_c])

    assert "node_a" in graph.nodes
    assert graph.inputs.required == ("input_param",)
    assert "output" in graph.outputs

def test_graph_with_strict_types():
    # Should not raise
    graph = Graph([node_a, node_b], strict_types=True)

    assert graph.strict_types is True
```

### Test Graph Execution

```python
from hypergraph import SyncRunner

def test_pipeline_integration():
    graph = Graph([clean, transform, validate])
    runner = SyncRunner()

    result = runner.run(graph, {"raw_data": "test input"})

    assert result.status == RunStatus.COMPLETED
    assert "validated" in result
    assert result["validated"] is True
```

### Test with Fixtures

```python
import pytest
from hypergraph import Graph, SyncRunner

@pytest.fixture
def rag_pipeline():
    return Graph([embed, retrieve, generate])

@pytest.fixture
def runner():
    return SyncRunner()

def test_rag_responds(rag_pipeline, runner):
    result = runner.run(rag_pipeline, {
        "query": "What is Python?",
        "top_k": 3,
    })

    assert "answer" in result
    assert len(result["answer"]) > 50
```

## Testing Routing Logic

```python
from hypergraph import END

@route(targets=["process", END])
def should_continue(score: float) -> str:
    if score >= 0.8:
        return END
    return "process"

def test_routing_continues_on_low_score():
    result = should_continue.func(0.5)
    assert result == "process"

def test_routing_ends_on_high_score():
    result = should_continue.func(0.9)
    assert result is END
```

## Property-Based Testing

Use hypothesis for thorough testing:

```python
from hypothesis import given, strategies as st

@node(output_name="cleaned")
def clean(text: str) -> str:
    return text.strip().lower()

@given(st.text())
def test_clean_always_lowercase(text):
    result = clean.func(text)
    assert result == result.lower()

@given(st.text())
def test_clean_no_leading_trailing_whitespace(text):
    result = clean.func(text)
    assert result == result.strip()
```

## Snapshot Testing

For complex outputs, use snapshot testing:

```python
def test_generate_response(snapshot):
    result = generate.func(
        docs=["Doc 1", "Doc 2"],
        query="Test query",
    )

    # Compare against saved snapshot
    snapshot.assert_match(result, "generate_response.txt")
```

## Benefits

1. **Fast** — No graph construction or runner overhead
2. **Isolated** — Test one function at a time
3. **Simple** — Standard pytest patterns work
4. **Debuggable** — Step through your function directly

## What's Next?

- [Batch Processing](batch-processing.md) — Test batch operations
- [Core Concepts](../02-core-concepts/getting-started.md) — Node fundamentals
