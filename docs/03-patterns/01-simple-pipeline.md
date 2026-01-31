# Simple Pipeline (DAG)

The simplest hypergraph workflow: a linear chain of functions where data flows in one direction.

## When to Use

- ETL pipelines (extract → transform → load)
- Single-pass ML inference (preprocess → predict → postprocess)
- Data transformations (clean → enrich → validate → save)

Straightforward data flow — functions execute in a single pass.

## Basic Pattern

```python
from hypergraph import Graph, node, SyncRunner

@node(output_name="cleaned")
def clean(raw_data: str) -> str:
    """Remove whitespace and normalize."""
    return raw_data.strip().lower()

@node(output_name="features")
def extract_features(cleaned: str) -> dict:
    """Extract features from cleaned text."""
    return {
        "length": len(cleaned),
        "word_count": len(cleaned.split()),
        "has_numbers": any(c.isdigit() for c in cleaned),
    }

@node(output_name="result")
def classify(features: dict) -> str:
    """Classify based on features."""
    if features["word_count"] > 100:
        return "long_form"
    return "short_form"

# Build the pipeline
pipeline = Graph([clean, extract_features, classify])

# Run it
runner = SyncRunner()
result = runner.run(pipeline, {"raw_data": "  Hello World  "})

print(result["cleaned"])   # "hello world"
print(result["features"])  # {"length": 11, "word_count": 2, "has_numbers": False}
print(result["result"])    # "short_form"
```

## How Edges Are Inferred

The magic: **output names match input parameters**.

```text
clean(raw_data) → "cleaned"
                      ↓
extract_features(cleaned) → "features"
                                ↓
classify(features) → "result"
```

- `clean` produces `"cleaned"`
- `extract_features` takes `cleaned` as a parameter → edge created
- `extract_features` produces `"features"`
- `classify` takes `features` as a parameter → edge created

Consistent naming is all it takes — edges are inferred automatically.

## Inspecting the Graph

```python
# What inputs does the pipeline need?
print(pipeline.inputs.required)  # ('raw_data',)

# What outputs does it produce?
print(pipeline.outputs)  # ('cleaned', 'features', 'result')

# Is it a DAG?
print(pipeline.has_cycles)  # False
```

## Multiple Inputs

Nodes can have multiple inputs:

```python
@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return embedder.encode(text)

@node(output_name="docs")
def retrieve(embedding: list[float], top_k: int = 5) -> list[str]:
    return vector_db.search(embedding, k=top_k)

@node(output_name="answer")
def generate(docs: list[str], query: str) -> str:
    context = "\n".join(docs)
    return llm.generate(f"Context: {context}\n\nQuestion: {query}")

pipeline = Graph([embed, retrieve, generate])
print(pipeline.inputs.required)  # ('text', 'query')
print(pipeline.inputs.optional)  # ('top_k',)
```

**Note**: `top_k` has a default value, so it's optional. `text` and `query` are required.

## Parallel Branches

Independent nodes run in parallel (with `AsyncRunner`):

```python
@node(output_name="sentiment")
async def analyze_sentiment(text: str) -> float:
    return await sentiment_model.predict(text)

@node(output_name="topics")
async def extract_topics(text: str) -> list[str]:
    return await topic_model.predict(text)

@node(output_name="summary")
async def summarize(text: str, sentiment: float, topics: list) -> dict:
    return {
        "text": text[:100],
        "sentiment": sentiment,
        "topics": topics,
    }

pipeline = Graph([analyze_sentiment, extract_topics, summarize])

# sentiment and topics run in parallel (both depend only on text)
# summarize waits for both to complete
runner = AsyncRunner()
result = await runner.run(pipeline, {"text": "..."}, max_concurrency=10)
```

## Binding Values

Pre-fill some inputs for reuse:

```python
# General pipeline
pipeline = Graph([embed, retrieve, generate])

# Specialized for FAQ queries
faq_pipeline = pipeline.bind(top_k=10)
print(faq_pipeline.inputs.required)  # ('text', 'query')
print(faq_pipeline.inputs.bound)     # {'top_k': 10}

# Even more specialized
support_pipeline = faq_pipeline.bind(query="How do I reset my password?")
print(support_pipeline.inputs.required)  # ('text',)
```

## Type Validation

Catch type errors at build time:

```python
@node(output_name="count")
def count_words(text: str) -> int:
    return len(text.split())

@node(output_name="result")
def process(count: str) -> str:  # Bug: expects str, but count_words returns int
    return count.upper()

# Catch the error immediately
Graph([count_words, process], strict_types=True)
# GraphConfigError: Type mismatch on edge 'count_words' → 'process'
#   Output type: int
#   Input type:  str
```

## What's Next?

When you need conditional logic:
- [Branching](02-routing.md) — Take different paths based on data
- [Agentic Loops](03-agentic-loops.md) — Iterate until a condition is met

When you need composition:
- [Hierarchical Composition](04-hierarchical.md) — Nest pipelines as nodes
