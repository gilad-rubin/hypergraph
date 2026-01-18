# Single-Pass RAG Pipeline

A simple Retrieval-Augmented Generation pipeline. Query comes in, documents are retrieved, answer is generated.

## When to Use

- Question-answering over documents
- Knowledge base search
- Single-turn information retrieval

For multi-turn conversations with follow-up questions, see [Multi-Turn RAG](multi-turn-rag.md).

## The Pipeline

```
query → embed → retrieve → generate → answer
```

## Complete Implementation

```python
from hypergraph import Graph, node, AsyncRunner
from anthropic import Anthropic
from openai import OpenAI

# Initialize clients
anthropic = Anthropic()
openai = OpenAI()

# ═══════════════════════════════════════════════════════════════
# EMBEDDING
# ═══════════════════════════════════════════════════════════════

@node(output_name="embedding")
async def embed(query: str) -> list[float]:
    """
    Embed the query for vector search.
    Uses OpenAI's embedding model.
    """
    response = openai.embeddings.create(
        model="text-embedding-3-large",
        input=query,
    )
    return response.data[0].embedding


# ═══════════════════════════════════════════════════════════════
# RETRIEVAL
# ═══════════════════════════════════════════════════════════════

@node(output_name="docs")
async def retrieve(embedding: list[float], top_k: int = 5) -> list[dict]:
    """
    Search the vector database for relevant documents.
    Returns documents with content and metadata.
    """
    results = await vector_db.search(
        vector=embedding,
        limit=top_k,
        include_metadata=True,
    )

    return [
        {
            "content": r["content"],
            "source": r["metadata"].get("source", "unknown"),
            "score": r["score"],
        }
        for r in results
    ]


# ═══════════════════════════════════════════════════════════════
# GENERATION
# ═══════════════════════════════════════════════════════════════

@node(output_name="answer")
def generate(docs: list[dict], query: str) -> str:
    """
    Generate an answer using Claude Sonnet 4.5.
    Cites sources from retrieved documents.
    """
    # Format context with source attribution
    context_parts = []
    for i, doc in enumerate(docs, 1):
        context_parts.append(f"[{i}] {doc['source']}:\n{doc['content']}")

    context = "\n\n".join(context_parts)

    message = anthropic.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=2048,
        system="""You are a helpful assistant that answers questions based on the provided context.
Always cite your sources using [1], [2], etc.
If the context doesn't contain the answer, say so clearly.""",
        messages=[{
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion: {query}",
        }],
    )

    return message.content[0].text


# ═══════════════════════════════════════════════════════════════
# COMPOSE THE PIPELINE
# ═══════════════════════════════════════════════════════════════

rag_pipeline = Graph([embed, retrieve, generate], name="rag")

# Check what inputs are needed
print(rag_pipeline.inputs.required)  # ('query',)
print(rag_pipeline.inputs.optional)  # ('top_k',)


# ═══════════════════════════════════════════════════════════════
# RUN THE PIPELINE
# ═══════════════════════════════════════════════════════════════

async def main():
    runner = AsyncRunner()

    result = await runner.run(rag_pipeline, {
        "query": "How do I create a graph in hypergraph?",
        "top_k": 5,
    })

    print(f"Answer:\n{result['answer']}")
    print(f"\nRetrieved {len(result['docs'])} documents")


# asyncio.run(main())
```

## With Streaming

Stream the generation while retrieval happens first:

```python
@node(output_name="answer")
def generate_streaming(docs: list[dict], query: str) -> str:
    """Generate with streaming output."""

    context_parts = [f"[{i}] {doc['source']}:\n{doc['content']}"
                     for i, doc in enumerate(docs, 1)]
    context = "\n\n".join(context_parts)

    chunks = []
    print("Answer: ", end="")

    with anthropic.messages.stream(
        model="claude-sonnet-4-5-20250929",
        max_tokens=2048,
        system="Answer based on context. Cite sources.",
        messages=[{
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion: {query}",
        }],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            chunks.append(text)

    print("\n")
    return "".join(chunks)
```

## With Reranking

Add a reranking step for better relevance:

```python
@node(output_name="docs")
async def retrieve(embedding: list[float], top_k: int = 20) -> list[dict]:
    """Retrieve more candidates for reranking."""
    results = await vector_db.search(vector=embedding, limit=top_k)
    return [{"content": r["content"], "source": r["metadata"]["source"]}
            for r in results]

@node(output_name="reranked_docs")
def rerank(docs: list[dict], query: str, top_k: int = 5) -> list[dict]:
    """Rerank documents using a cross-encoder."""
    scores = reranker.score(query, [d["content"] for d in docs])
    ranked = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
    return [doc for doc, _ in ranked[:top_k]]

@node(output_name="answer")
def generate(reranked_docs: list[dict], query: str) -> str:
    # Uses reranked_docs instead of docs
    ...

rag_with_rerank = Graph([embed, retrieve, rerank, generate])
```

## With Query Expansion

Expand the query for better retrieval:

```python
@node(output_name="expanded_query")
def expand_query(query: str) -> str:
    """Expand query with related terms."""

    response = openai.responses.create(
        model="gpt-5.2",
        input=f"Expand this search query with related terms:\n{query}",
        instructions="Return only the expanded query, no explanation.",
    )

    return response.output_text

@node(output_name="embedding")
async def embed(expanded_query: str) -> list[float]:
    """Embed the expanded query."""
    response = openai.embeddings.create(
        model="text-embedding-3-large",
        input=expanded_query,
    )
    return response.data[0].embedding

rag_with_expansion = Graph([expand_query, embed, retrieve, generate])
```

## Testing

```python
import pytest
from hypergraph import AsyncRunner

@pytest.fixture
def runner():
    return AsyncRunner()

@pytest.mark.asyncio
async def test_rag_pipeline(runner):
    result = await runner.run(rag_pipeline, {
        "query": "What is hypergraph?",
    })

    assert "answer" in result
    assert len(result["answer"]) > 50
    assert len(result["docs"]) > 0

def test_embed():
    """Test embedding in isolation."""
    import asyncio
    embedding = asyncio.run(embed.func("test query"))

    assert isinstance(embedding, list)
    assert len(embedding) > 0

def test_generate():
    """Test generation with mock docs."""
    docs = [{"content": "Hypergraph is a workflow framework.", "source": "docs"}]

    answer = generate.func(docs, "What is hypergraph?")

    assert "workflow" in answer.lower() or "framework" in answer.lower()
```

## What's Next?

- [Multi-Turn RAG](multi-turn-rag.md) — Add conversation loops
- [Hierarchical Composition](../03-patterns/04-hierarchical.md) — Nest this pipeline in larger workflows
