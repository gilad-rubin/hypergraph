# How to Rename and Adapt Nodes

Reuse the same function in different contexts by renaming inputs, outputs, and the node itself.

## Why Rename?

The same logic often applies in different contexts with different naming conventions:

```python
# Same embedding function, different contexts
embed_query = embed.with_inputs(text="query")
embed_document = embed.with_inputs(text="document")

# Same validation, different pipelines
validate_order = validate.with_outputs(result="order_valid")
validate_user = validate.with_outputs(result="user_valid")
```

## Renaming Inputs

Use `.with_inputs()` to rename input parameters:

```python
from hypergraph import node

@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return embedder.encode(text)

# Original takes "text"
print(embed.inputs)  # ('text',)

# Adapted to take "query"
query_embed = embed.with_inputs(text="query")
print(query_embed.inputs)  # ('query',)

# Adapted to take "document"
doc_embed = embed.with_inputs(text="document")
print(doc_embed.inputs)  # ('document',)
```

**Important**: The original node is unchanged. `.with_inputs()` returns a new node.

## Renaming Outputs

Use `.with_outputs()` to rename output names:

```python
@node(output_name="result")
def process(data: str) -> str:
    return data.upper()

# Original produces "result"
print(process.outputs)  # ('result',)

# Adapted to produce "processed_text"
text_processor = process.with_outputs(result="processed_text")
print(text_processor.outputs)  # ('processed_text',)
```

## Renaming the Node

Use `.with_name()` to give the node a new name:

```python
@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return embedder.encode(text)

print(embed.name)  # 'embed'

query_embedder = embed.with_name("query_embedder")
print(query_embedder.name)  # 'query_embedder'
```

## Chaining Renames

All rename methods return new instances, so you can chain them:

```python
@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return embedder.encode(text)

query_embed = (
    embed
    .with_name("embed_query")
    .with_inputs(text="query")
    .with_outputs(embedding="query_embedding")
)

print(query_embed.name)     # 'embed_query'
print(query_embed.inputs)   # ('query',)
print(query_embed.outputs)  # ('query_embedding',)
```

## Multiple Inputs/Outputs

Rename multiple at once:

```python
@node(output_name=("mean", "std"))
def statistics(data: list, weights: list) -> tuple[float, float]:
    # ...
    return mean_val, std_val

# Rename both inputs
adapted = statistics.with_inputs(data="values", weights="importance")
print(adapted.inputs)  # ('values', 'importance')

# Rename both outputs
adapted = statistics.with_outputs(mean="average", std="deviation")
print(adapted.outputs)  # ('average', 'deviation')
```

## Use Case: Same Function in Multiple Roles

```python
from hypergraph import Graph, node

@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return embedder.encode(text)

# Create specialized versions
embed_query = (
    embed
    .with_name("embed_query")
    .with_inputs(text="query")
    .with_outputs(embedding="query_vec")
)

embed_doc = (
    embed
    .with_name("embed_doc")
    .with_inputs(text="document")
    .with_outputs(embedding="doc_vec")
)

@node(output_name="similarity")
def compare(query_vec: list[float], doc_vec: list[float]) -> float:
    return cosine_similarity(query_vec, doc_vec)

# Both embedding variants feed into compare
similarity_pipeline = Graph([embed_query, embed_doc, compare])
print(similarity_pipeline.inputs.required)  # ('query', 'document')
```

## Use Case: Adapting Graphs

Graphs can be renamed too when used as nodes:

```python
# Original RAG pipeline
rag = Graph([embed, retrieve, generate], name="rag")
print(rag.inputs.required)  # ('text', 'query')

# Adapt for search context
search_rag = (
    rag.as_node()
    .with_name("search_rag")
    .with_inputs(text="search_query", query="search_query")
)

# Adapt for chat context
chat_rag = (
    rag.as_node()
    .with_name("chat_rag")
    .with_inputs(text="user_message", query="user_message")
)
```

## Error Handling

If you try to rename a non-existent input/output, you get a clear error:

```python
@node(output_name="result")
def process(x: int) -> int:
    return x * 2

# Try to rename non-existent input
process.with_inputs(y="new_name")
# RenameError: 'y' not found. Current inputs: ('x',)
```

If you renamed and try to use the old name:

```python
renamed = process.with_inputs(x="input")
renamed.with_inputs(x="different")
# RenameError: 'x' was renamed to 'input'. Current inputs: ('input',)
```

## Testing Renamed Nodes

The underlying function is the same:

```python
@node(output_name="result")
def double(x: int) -> int:
    return x * 2

renamed = double.with_inputs(x="value").with_outputs(result="doubled")

# Both call the same function
assert double.func(5) == 10
assert renamed.func(5) == 10  # Same underlying function

# But in a graph, they wire differently
g1 = Graph([double])
print(g1.inputs.required)  # ('x',)

g2 = Graph([renamed])
print(g2.inputs.required)  # ('value',)
```

## What's Next?

- [Batch Processing](batch-processing.md) — Process multiple inputs
- [Hierarchical Composition](../03-patterns/04-hierarchical.md) — Adapt graphs for different contexts
