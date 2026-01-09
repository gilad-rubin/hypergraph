# Edge Cancels Default - Specification

## The Rule

**If a parameter has an incoming edge, its function default is IGNORED.**

This simple rule eliminates ambiguity in cycles and removes the need for explicit "entrypoint" declarations.

## The Three Cases

### Case 1: Parameter Has Edge → Default Ignored

```python
@node(outputs="b")
def node_a(x: int) -> int:
    return x + 1

@node(outputs="result")
def node_b(b: int, multiplier: int = 2) -> int:  # ← default exists
    return b * multiplier

graph = Graph(nodes=[node_a, node_b])
# `b` has an edge from node_a
# `multiplier` has NO edge

runner.run(graph, inputs={"x": 5})
# node_a runs: b = 6
# node_b runs: result = 6 * 2 = 12  (default used for multiplier)
```

### Case 2: No Edge + Has Default → Use Default

```python
@node(outputs="result")
def format_output(value: int, prefix: str = "Result: ") -> str:
    return f"{prefix}{value}"

# prefix has no edge AND has default → uses "Result: "
```

### Case 3: No Edge + No Default → Required Input

```python
@node(outputs="result")
def process(query: str) -> str:  # query has no default
    return llm.process(query)

# query has no edge AND no default → MUST provide in inputs
runner.run(graph, inputs={"query": "hello"})  # ✓
runner.run(graph, inputs={})  # ✗ MissingInputError
```

## Why This Matters for Cycles

Consider a simple accumulator:

```python
@node(outputs="messages")
def add_response(messages: list, response: str) -> list:
    return messages + [{"role": "assistant", "content": response}]
```

In a cycle, `messages` has an edge from itself (the output feeds back as input).

**Without "edge cancels default":**
- If `messages` had a default `= []`
- Both the edge AND the default would be valid sources
- Ambiguous: which one to use?

**With "edge cancels default":**
- `messages` has an edge → default is ignored
- Must provide `messages` via inputs to start the cycle
- Clear and unambiguous

## Input Determines Where Cycles Start

This rule eliminates the need for explicit "entrypoint" declarations:

```python
@node(outputs="a")
def node_a(b: int) -> int:
    return b + 1

@node(outputs="b") 
def node_b(a: int) -> int:
    return a * 2

graph = Graph(nodes=[node_a, node_b])

# YOU choose where to start by which input you provide:
runner.run(graph, inputs={"a": 5})  # Start from node_b (has a=5)
# node_b runs first: b = 10
# node_a runs: a = 11
# ...continues...

runner.run(graph, inputs={"b": 5})  # Start from node_a (has b=5)
# node_a runs first: a = 6
# node_b runs: b = 12
# ...continues...
```

## Graph Input Categories

The "edge cancels default" rule creates three distinct categories of inputs:

| Category | Definition | Graph Property |
|----------|------------|----------------|
| **required** | No edge, no default, not bound | `graph.required` |
| **optional** | No edge, has default OR bound | `graph.optional` |
| **seeds** | Has edge (cycle), needs initial value | `graph.seeds` |

**At runtime:**
- Fresh run: must provide `required ∪ seeds`
- Resume: must provide only `required` (seeds come from state)

**Error types:**
- Missing `required` → `MissingInputError`
- Missing `seeds` on fresh run → `MissingSeedError`

## Input Resolution Order

When multiple sources could provide a value, this is the precedence:

1. **Edge value** (if available) - Always wins
2. **Runtime input** - For initialization or override
3. **Bound value** (`graph.bind()`) - Only if no edge
4. **Function default** - Only if no edge

```python
@node(outputs="result")
def process(x: int, y: int = 10) -> int:
    return x + y

graph = Graph(nodes=[producer_of_x, process])
bound_graph = graph.bind(y=20)

# x has edge from producer_of_x
# y has no edge, has default=10, bound to 20

runner.run(bound_graph, inputs={"y": 30})
# x comes from edge (producer_of_x output)
# y = 30 (runtime input beats bound beats default)

runner.run(bound_graph, inputs={})
# x comes from edge
# y = 20 (bound beats default)

runner.run(graph, inputs={})
# x comes from edge
# y = 10 (default, since not bound)
```

## Implementation

### Detecting Edges

```python
def has_incoming_edge(param_name: str, node: HyperNode, graph: Graph) -> bool:
    """Check if parameter receives value from another node's output."""
    for other_node in graph.nodes:
        if other_node.outputs == param_name:
            return True
        if isinstance(other_node.outputs, tuple):
            if param_name in other_node.outputs:
                return True
    return False
```

### Resolving Input Values

```python
def resolve_input(
    param_name: str,
    node: HyperNode,
    graph: Graph,
    state: GraphState,
    runtime_inputs: dict,
    bound_values: dict,
) -> Any:
    """
    Resolve the value for a parameter following precedence rules.
    
    Raises:
        MissingInputError: If no value can be resolved
    """
    has_edge = has_incoming_edge(param_name, node, graph)
    
    # 1. Edge value (highest priority)
    if has_edge and param_name in state.values:
        return state.values[param_name]
    
    # 2. Runtime input
    if param_name in runtime_inputs:
        return runtime_inputs[param_name]
    
    # From here, only applies if NO edge
    if has_edge:
        raise MissingInputError(
            f"'{param_name}' needs a value\n\n"
            f"  → {node.name} wants to read '{param_name}', but it hasn't been produced yet.\n"
            f"  → This parameter has an edge, so you must either:\n"
            f"    - Provide it in inputs to start the cycle\n"
            f"    - Ensure the producing node runs first\n"
        )
    
    # 3. Bound value
    if param_name in bound_values:
        return bound_values[param_name]
    
    # 4. Function default
    if has_default(node.func, param_name):
        return get_default(node.func, param_name)
    
    # No value available
    raise MissingInputError(
        f"'{param_name}' is required but not provided\n\n"
        f"  → {node.name} needs '{param_name}' but no value is available.\n"
        f"  → Provide it in inputs: runner.run(graph, inputs={{'{param_name}': value}})\n"
    )
```

### Build-Time Warning

Optionally warn about ignored defaults:

```python
def warn_ignored_defaults(graph: Graph):
    """
    Emit warnings for defaults that will never be used.
    
    This helps catch mistakes where someone adds a default
    expecting it to work, but an edge exists.
    """
    for node in graph.nodes:
        for param in node.parameters:
            if has_incoming_edge(param, node, graph):
                if has_default(node.func, param):
                    warnings.warn(
                        f"{node.name}.{param} has default={get_default(node.func, param)} "
                        f"but also has an incoming edge. The default will be ignored."
                    )
```

## Comparison: With vs Without Entrypoints

| Aspect | Entrypoint Approach | Edge Cancels Default |
|--------|--------------------|--------------------|
| Declaration | Extra concept (`@entrypoint`) | Just provide inputs |
| Flexibility | Fixed at build time | Choose at runtime |
| Clarity | Implicit state initialization | Explicit: you see initial values |
| Validation | Complex ambiguity detection | Simple: run what's ready |
| Learning curve | Another thing to learn | Natural from function semantics |

## Examples

### Multi-Turn RAG

```python
@node(outputs="messages")
def add_user(messages: list, user_input: str) -> list:
    return messages + [{"role": "user", "content": user_input}]

@node(outputs="response")
def generate(messages: list) -> str:
    return llm.chat(messages)

@node(outputs="messages")
def add_assistant(messages: list, response: str) -> list:
    return messages + [{"role": "assistant", "content": response}]

# messages has edge from add_assistant → default would be ignored
# Must initialize via input:
runner.run(graph, inputs={
    "user_input": "What is RAG?",
    "messages": []  # ← Starts the cycle
})
```

### Iteration Counter

```python
@node(outputs="count")
def increment(count: int) -> int:
    return count + 1

@route(targets=["increment", END])
def check_limit(count: int, limit: int = 10) -> str:
    return END if count >= limit else "increment"

# count has edge from increment → must initialize
# limit has NO edge → default 10 is used
runner.run(graph, inputs={"count": 0})  # Runs 10 iterations
runner.run(graph, inputs={"count": 0, "limit": 5})  # Runs 5 iterations
```
