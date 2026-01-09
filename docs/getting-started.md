# Getting Started with Hypergraph

This guide introduces the core concepts and walks you through creating your first nodes.

## Core Concepts

### Nodes

A **node** is a function wrapped as a graph component. Nodes have:

- **Inputs** - Parameter names from the function signature
- **Outputs** - Named values produced by the function
- **A name** - Identifier for the node (defaults to function name)

```python
from hypergraph import node

@node(output_name="result")
def add(x: int, y: int) -> int:
    return x + y

# Properties
print(add.name)      # "add"
print(add.inputs)    # ("x", "y")
print(add.outputs)   # ("result",)
```

### Outputs ARE State

Unlike other frameworks, hypergraph doesn't require a separate state schema. **Your node outputs form the graph's state.**

When you run a graph, outputs flow from one node to the next:

```python
@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return model.embed(text)

@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]:
    return db.search(embedding)

# State flow:
# embed → produces "embedding" → retrieve consumes "embedding" → produces "docs"
```

No state schema. No reducers. No conflicts. Just outputs flowing.

## Creating Your First Node

### Simple Function

```python
from hypergraph import node

@node(output_name="doubled")
def double(x: int) -> int:
    """Double the input."""
    return x * 2

# Call directly
result = double(5)
print(result)  # 10

# Access properties
print(double.inputs)   # ("x",)
print(double.outputs)  # ("doubled",)
print(double.is_async)  # False
print(double.is_generator)  # False
```

### Side-Effect Only Nodes

If a function doesn't return a value, omit `output_name`:

```python
@node  # No output_name - side-effect only
def log(message: str) -> None:
    print(f"[LOG] {message}")

print(log.outputs)  # ()
```

Warning: If you accidentally have a return annotation without `output_name`, hypergraph warns you:

```python
@node  # Missing output_name!
def fetch_data(url: str) -> dict:
    return requests.get(url).json()

# Warning: Function 'fetch_data' has return type '<class 'dict'>' but no output_name.
# If you want to capture the return value, use @node(output_name='...')
```

### Multiple Outputs

Functions can produce multiple outputs:

```python
@node(output_name=("mean", "std"))
def statistics(data: list) -> tuple[float, float]:
    """Calculate mean and standard deviation."""
    mean = sum(data) / len(data)
    std = (sum((x - mean) ** 2 for x in data) / len(data)) ** 0.5
    return mean, std

print(statistics.outputs)  # ("mean", "std")
```

The return value must be a tuple matching the number of output names. Unpacking is automatic.

## Working with Nodes

### Renaming Inputs and Outputs

Use `with_inputs()` and `with_outputs()` to rename without creating new functions:

```python
@node(output_name="result")
def process(text: str) -> str:
    return text.upper()

# Rename to fit your graph's naming convention
adapted = process.with_inputs(text="raw_input").with_outputs(result="processed")

print(adapted.inputs)   # ("raw_input",)
print(adapted.outputs)  # ("processed",)

# Original unchanged
print(process.inputs)   # ("text",)
print(process.outputs)  # ("result",)
```

### Renaming the Node

```python
preprocessor = process.with_name("string_preprocessor")
print(preprocessor.name)  # "string_preprocessor"
```

### Chaining Renames

All rename methods return new instances:

```python
custom = (
    process
    .with_name("preprocessor")
    .with_inputs(text="raw")
    .with_outputs(result="cleaned")
)

print(custom.name)     # "preprocessor"
print(custom.inputs)   # ("raw",)
print(custom.outputs)  # ("cleaned",)
```

## Execution Modes

Hypergraph supports four execution modes, auto-detected from the function signature:

### 1. Synchronous Function (Default)

```python
@node(output_name="result")
def sync_process(x: int) -> int:
    return x * 2

print(sync_process.is_async)      # False
print(sync_process.is_generator)  # False
```

### 2. Asynchronous Function

```python
import httpx

@node(output_name="data")
async def fetch(url: str) -> dict:
    async with httpx.AsyncClient() as client:
        return (await client.get(url)).json()

print(fetch.is_async)      # True
print(fetch.is_generator)  # False
```

### 3. Synchronous Generator

```python
from typing import Iterator

@node(output_name="chunks")
def chunk_text(text: str, size: int = 100) -> Iterator[str]:
    """Yield text in chunks."""
    for i in range(0, len(text), size):
        yield text[i:i+size]

print(chunk_text.is_async)      # False
print(chunk_text.is_generator)  # True
```

### 4. Asynchronous Generator

```python
from typing import AsyncIterator

@node(output_name="tokens")
async def stream_llm(prompt: str) -> AsyncIterator[str]:
    """Stream LLM response tokens."""
    async for chunk in openai.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        stream=True,
    ):
        yield chunk.choices[0].delta.content or ""

print(stream_llm.is_async)      # True
print(stream_llm.is_generator)  # True
```

## Function Node Properties

Every node created with `@node` has these properties:

### name
The public identifier for this node (defaults to function name).

```python
@node(output_name="result")
def process(x): pass

print(process.name)  # "process"
print(process.with_name("custom").name)  # "custom"
```

### inputs
Tuple of input parameter names from the function signature.

```python
@node(output_name="result")
def add(x: int, y: int) -> int: pass

print(add.inputs)  # ("x", "y")
```

### outputs
Tuple of output names (empty if no output_name).

```python
@node(output_name="sum")
def add(x: int, y: int) -> int: pass

print(add.outputs)  # ("sum",)
```

### func
Direct reference to the underlying Python function.

```python
@node(output_name="result")
def double(x: int) -> int:
    return x * 2

result = double.func(5)  # Call directly
print(result)  # 10
```

### cache
Whether this node's results are cached (default: False).

```python
@node(output_name="result", cache=True)
def expensive(x: int) -> int:
    return x ** 100

print(expensive.cache)  # True
```

### is_async
True if the function is async or async generator.

```python
@node(output_name="data")
async def fetch(url: str) -> dict: pass

print(fetch.is_async)  # True
```

### is_generator
True if the function yields values (sync or async generator).

```python
@node(output_name="items")
def produce(n: int):
    for i in range(n):
        yield i

print(produce.is_generator)  # True
```

### definition_hash
SHA256 hash of the function's source code (for cache invalidation).

```python
@node(output_name="result")
def process(x: int) -> int:
    return x * 2

print(len(process.definition_hash))  # 64 (hex string)
```

## Error Handling: RenameError

When you try to rename a name that doesn't exist, you get a helpful error:

```python
@node(output_name="result")
def process(x: int) -> int: pass

# Try to rename non-existent input
process.with_inputs(y="renamed")
# RenameError: 'y' not found. Current inputs: ('x',)
```

If you renamed and then try to use the old name:

```python
renamed = process.with_inputs(x="input")
renamed.with_inputs(x="different")  # ERROR - x was already renamed to "input"

# Error message shows history:
# RenameError: 'x' was renamed to 'input'. Current inputs: ('input',)
```

## Next Steps

- Explore [API Reference: Nodes](api/nodes.md) for complete documentation
- Read [Philosophy](philosophy.md) to understand "Outputs ARE State"
- When graphs are available, learn how to compose nodes into workflows

## Common Patterns

### Creating Multiple Variants

Reuse a function in different contexts:

```python
@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return model.embed(text)

# For search pipeline
search_embed = embed.with_name("search_embedding")

# For chat pipeline
chat_embed = embed.with_name("chat_embedding")

print(search_embed.name)  # "search_embedding"
print(chat_embed.name)    # "chat_embedding"
```

### Reconfiguring an Existing Node

Pass a FunctionNode to `FunctionNode()` to create a fresh node with new configuration. Only the underlying function is extracted - all other settings are discarded:

```python
from hypergraph import node, FunctionNode

@node(output_name="original_output", cache=True)
def process(x: int) -> int:
    return x * 2

# Create new node with different config (extracts just the function)
reconfigured = FunctionNode(
    process,  # Pass the FunctionNode directly
    name="new_name",
    output_name="new_output",
    cache=False,
)

print(reconfigured.name)     # "new_name"
print(reconfigured.outputs)  # ("new_output",)
print(reconfigured.cache)    # False

# Original unchanged
print(process.name)     # "process"
print(process.outputs)  # ("original_output",)
print(process.cache)    # True

# The underlying function is the same
print(reconfigured.func is process.func)  # True
```

This is useful when you want to completely reconfigure a node rather than just rename parts of it.

### Conditional Output Names

Choose output names at runtime:

```python
def create_processor(mode: str):
    @node
    def process(x: int) -> int:
        if mode == "double":
            return x * 2
        else:
            return x + 1

    if mode == "double":
        return process.with_outputs(process="doubled")
    else:
        return process.with_outputs(process="incremented")

processor = create_processor("double")
print(processor.outputs)  # ("doubled",)
```
