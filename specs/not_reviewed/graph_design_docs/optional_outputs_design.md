# Optional Inputs, Default Values, and Bound Values

> **Status**: Design Document
> **Author**: hypergraph Team
> **Date**: December 2025
> **Type**: Core Semantics

---

## Table of Contents

1. [Overview](#overview)
2. [Value Sources & Priority](#value-sources--priority)
3. [Optional vs Required Inputs](#optional-vs-required-inputs)
4. [Edge Connections Cancel Optionality](#edge-connections-cancel-optionality)
5. [The `.bind()` Method](#the-bind-method)
6. [Interaction with Graph Execution](#interaction-with-graph-execution)
7. [API Reference](#api-reference)
8. [Examples](#examples)
9. [Edge Cases](#edge-cases)

---

## Overview

hypergraph supports multiple ways to provide values to node inputs:

1. **Function defaults** — Default values in the Python function signature
2. **Bound values** — Values set via `.bind()` on a Graph/Pipeline
3. **Edge connections** — Values produced by upstream nodes
4. **Runtime inputs** — Values provided when calling `.run()` or `.map()`

Understanding how these interact is crucial for building flexible, reusable graphs.

---

## Value Sources & Priority

### Priority Order (Highest to Lowest)

| Priority | Source | Description |
|----------|--------|-------------|
| 1 | **Edge connections** | Values produced by upstream nodes — **immutable, cannot be overridden** |
| 2 | **Runtime inputs** | Values passed to `.run(inputs={...})` — only for root args |
| 3 | **Bound values** | Values set via `.bind()` — only for root args |
| 4 | **Function defaults** | Default values in the function signature |

> **Note**: Edge connections are not "higher priority" in the sense of overriding — they are **exclusive**. Attempting to provide a value (via runtime or bind) for an edge-connected parameter is an error.

### Resolution Rules

```python
def resolve_input(param_name, node, graph, runtime_inputs, bound_values):
    """Pseudo-code for input resolution."""

    # 1. Check if this is an edge-connected parameter
    if param_name in graph.output_to_node:
        # Edge-connected: value MUST come from upstream node
        # Runtime inputs and bound values are forbidden for this param
        return graph.state[param_name]

    # 2. Runtime inputs (highest priority for root args)
    if param_name in runtime_inputs:
        return runtime_inputs[param_name]  # Explicit None is valid

    # 3. Bound values
    if param_name in bound_values:
        return bound_values[param_name]

    # 4. Function defaults
    if param_has_default(node.func, param_name):
        return get_default(node.func, param_name)

    # No value available - ERROR
    raise MissingInputError(f"Required input '{param_name}' not provided")


def validate_inputs_at_runtime(graph, runtime_inputs):
    """Validate before execution starts."""

    # Check for forbidden overrides of edge-connected values
    edge_connected = set(graph.output_to_node.keys())
    forbidden = set(runtime_inputs.keys()) & edge_connected
    if forbidden:
        for param in forbidden:
            producer = graph.output_to_node[param].name
            raise ValueError(f"Cannot provide '{param}': produced by '{producer}'")


def validate_bind(graph, bound_values):
    """Validate at bind time."""

    edge_connected = set(graph.output_to_node.keys())
    forbidden = set(bound_values.keys()) & edge_connected
    if forbidden:
        for param in forbidden:
            producer = graph.output_to_node[param].name
            raise ValueError(f"Cannot bind '{param}': produced by '{producer}'")
```

---

## Optional vs Required Inputs

### Definition

An input is **optional** if it can be omitted at runtime without causing an error.

An input is **optional** when ALL of these conditions are met:
1. No edge connects to it (no upstream node produces this value)
2. It has either a function default OR a bound value

### Function Defaults

Parameters with default values in the function signature are potentially optional:

```python
@node(output_name="result")
def process(data: str, threshold: float = 0.5, verbose: bool = False) -> str:
    # data is REQUIRED (no default)
    # threshold is OPTIONAL (default: 0.5)
    # verbose is OPTIONAL (default: False)
    ...
```

### Bound Values

Values set via `.bind()` make parameters optional:

```python
@node(output_name="result")
def process(data: str, model: Model) -> str:
    ...

# Before bind: both 'data' and 'model' are REQUIRED
graph = Graph(nodes=[process])

# After bind: only 'data' is REQUIRED
graph.bind(model=expensive_model)
```

### Combined Sources

When both defaults and bindings exist, binding takes precedence:

```python
@node(output_name="result")
def process(data: str, threshold: float = 0.5) -> str:
    ...

graph = Graph(nodes=[process]).bind(threshold=0.8)

# Effective value: 0.8 (bound value overrides default)
result = graph.run(inputs={"data": "test"})

# Runtime override: 0.9 (highest priority)
result = graph.run(inputs={"data": "test", "threshold": 0.9})
```

---

## Edge Connections Cancel Optionality

**This is the critical rule**: When an upstream node produces a value that matches a parameter name, that parameter is NO LONGER optional — it becomes a required dependency.

### Why Edges Cancel Optionality

Consider this graph:

```python
@node(output_name="config")
def load_config() -> dict:
    return {"threshold": 0.7}

@node(output_name="result")
def process(data: str, config: dict = {"threshold": 0.5}) -> str:
    # Uses config["threshold"]
    ...

graph = Graph(nodes=[load_config, process])
```

Even though `process` has a default for `config`, the edge from `load_config` means:
- `process` MUST wait for `load_config` to complete
- The value from `load_config` will be used, NOT the default
- If `load_config` fails, `process` cannot proceed

### The Logic

```
For each node parameter:
  IF an upstream node produces a value with matching name:
    → Parameter is REQUIRED (edge dependency)
    → Default value is IGNORED
    → Bound value is IGNORED (for this parameter)
  ELSE:
    → Parameter may be optional (if default or bound exists)
```

### Practical Example

```python
@node(output_name="embedding")
def embed(text: str, model: Model) -> list[float]:
    return model.embed(text)

@node(output_name="result")
def search(embedding: list[float], top_k: int = 10) -> list[str]:
    # embedding: REQUIRED (comes from embed node via edge)
    # top_k: OPTIONAL (has default, no edge produces "top_k")
    ...

graph = Graph(nodes=[embed, search])

# Valid: top_k uses default (10)
result = graph.run(inputs={"text": "query", "model": my_model})

# Valid: top_k overridden at runtime
result = graph.run(inputs={"text": "query", "model": my_model, "top_k": 5})

# Invalid: 'model' is required, 'text' is required
result = graph.run(inputs={"text": "query"})  # Error: missing 'model'
```

---

## The `.bind()` Method

### Purpose

`.bind()` sets default values for graph inputs, similar to `functools.partial`:

```python
# These are equivalent conceptually:
partial_func = functools.partial(process, model=my_model)
bound_graph = graph.bind(model=my_model)
```

### API

```python
class Graph:
    def bind(self, **inputs: Any) -> "Graph":
        """Bind input values to graph parameters.

        Bound inputs are used as defaults in run()/map() calls and can be
        overridden by passing inputs= arguments. Multiple bind() calls merge.

        Args:
            **inputs: Input parameters to bind

        Returns:
            Self for method chaining
        """

    def unbind(self, *keys: str) -> "Graph":
        """Remove specific bindings, or all if no keys specified."""

    @property
    def bound_inputs(self) -> Dict[str, Any]:
        """Get currently bound input values (returns a copy)."""

    @property
    def unfulfilled_args(self) -> tuple:
        """Get parameter names NOT yet satisfied by bindings.

        These are the parameters that MUST be provided at runtime.
        """
```

### Method Chaining

`.bind()` returns `self` for fluent API usage:

```python
graph = (
    Graph(nodes=[embed, retrieve, generate])
    .bind(model=embedding_model)
    .bind(top_k=10, temperature=0.7)
)
```

### Merging Behavior

Multiple `.bind()` calls merge (later calls override earlier):

```python
graph.bind(a=1, b=2)
graph.bind(b=3, c=4)
# Result: bound_inputs = {"a": 1, "b": 3, "c": 4}
```

---

## Interaction with Graph Execution

### Input Validation

At runtime, the graph validates that all **required** inputs are provided:

```python
def validate_inputs(graph, runtime_inputs):
    """Validate all required inputs are available."""

    # Required = root_args - bound_inputs - params_with_defaults
    required = set(graph.root_args) - set(graph.bound_inputs.keys())

    # For params with function defaults, they're optional
    for param in required.copy():
        if param_has_default_in_any_node(graph, param):
            required.discard(param)

    # Check all required params are in runtime_inputs
    missing = required - set(runtime_inputs.keys())
    if missing:
        raise ValueError(f"Missing required inputs: {missing}")
```

### Execution Flow

```
1. Merge inputs: runtime_inputs | bound_inputs
2. For each node in execution order:
   a. Collect inputs from:
      - Merged inputs (for root args)
      - Upstream node outputs (for edge-connected params)
      - Function defaults (for optional params not otherwise provided)
   b. Execute node
   c. Store outputs in state
```

---

## API Reference

### Graph Properties

| Property | Type | Description |
|----------|------|-------------|
| `root_args` | `tuple[str, ...]` | All input parameters (including optional) |
| `bound_inputs` | `dict[str, Any]` | Currently bound values |
| `unfulfilled_args` | `tuple[str, ...]` | Parameters not yet bound (may still have defaults) |

### Introspection

```python
# Check what a graph needs
graph = Graph(nodes=[process]).bind(model=my_model)

print(graph.root_args)         # ('data', 'model', 'threshold')
print(graph.bound_inputs)      # {'model': <Model>}
print(graph.unfulfilled_args)  # ('data', 'threshold')

# Note: 'threshold' is in unfulfilled_args but may have a default
```

---

## Examples

### Example 1: Simple Optional Parameter

```python
@node(output_name="result")
def greet(name: str, greeting: str = "Hello") -> str:
    return f"{greeting}, {name}!"

graph = Graph(nodes=[greet])

# Uses default greeting
result = graph.run(inputs={"name": "Alice"})
# → {"result": "Hello, Alice!"}

# Overrides default
result = graph.run(inputs={"name": "Alice", "greeting": "Hi"})
# → {"result": "Hi, Alice!"}
```

### Example 2: Binding Expensive Resources

```python
@node(output_name="embedding")
def embed(text: str, model: EmbeddingModel) -> list[float]:
    return model.embed(text)

# Model is expensive to create, bind it once
graph = Graph(nodes=[embed]).bind(model=load_model())

# Now only 'text' is required
result = graph.run(inputs={"text": "Hello world"})

# Can still override if needed
result = graph.run(inputs={"text": "Hello", "model": different_model})
```

### Example 3: Edge Cancels Default

```python
@node(output_name="config")
def load_config(env: str) -> dict:
    return {"threshold": 0.7 if env == "prod" else 0.5}

@node(output_name="result")
def process(data: str, config: dict = {"threshold": 0.5}) -> str:
    # Default is IGNORED because load_config produces 'config'
    return f"Threshold: {config['threshold']}"

graph = Graph(nodes=[load_config, process])

# config comes from load_config, not from default
result = graph.run(inputs={"env": "prod", "data": "test"})
# → {"result": "Threshold: 0.7"}
```

### Example 4: Nested Graph with Binding

```python
# Inner graph with bound values
inner = Graph(nodes=[embed, retrieve]).bind(model=embedding_model, top_k=10)

# Inner is fully bound except for 'query'
outer = Graph(nodes=[
    inner.as_node(input_mapping={"query": "text"}),
    generate,
])

# Only need to provide 'text' and generate's inputs
result = outer.run(inputs={"text": "What is RAG?"})
```

---

## Edge Cases

### 1. Same Parameter Name, Different Sources

**Scenario**: Parameter appears in multiple nodes with different default values.

```python
@node(output_name="a")
def node_a(threshold: float = 0.5) -> float:
    return threshold

@node(output_name="b")
def node_b(threshold: float = 0.8) -> float:
    return threshold

graph = Graph(nodes=[node_a, node_b])
```

**Resolution**: Each node uses its own default. `threshold` is a single root arg that feeds both nodes:

```python
# If threshold not provided:
# - node_a uses 0.5 (its default)
# - node_b uses 0.8 (its default)

# If threshold IS provided at runtime:
# - Both nodes receive the runtime value
result = graph.run(inputs={"threshold": 0.6})  # Both use 0.6
```

**Edge case question**: What if user provides `threshold` at runtime? Should both nodes get it, or should defaults still apply?

**Answer**: Runtime inputs always win. Both nodes receive the runtime value.

### 2. Bound Value for Edge-Connected Parameter

**Scenario**: User binds a value that is also produced by an edge.

```python
@node(output_name="config")
def load_config() -> dict:
    return {"a": 1}

@node(output_name="result")
def process(config: dict) -> str:
    return str(config)

graph = Graph(nodes=[load_config, process]).bind(config={"b": 2})
```

**Resolution**: **Error**. The framework raises an error when attempting to bind a value that is produced by an edge:

```python
# Raises: ValueError("Cannot bind 'config': it is produced by node 'load_config'")
```

**Rationale**: Binding an edge-connected value would be silently ignored, leading to confusion. Failing fast helps users understand the graph structure.

### 3. Optional Output Leading to Optional Downstream

**Scenario**: An upstream node might not run (e.g., in a branch), leaving downstream optional.

```python
@branch(when_true="enrich", when_false="skip")
def should_enrich(data: dict) -> bool:
    return data.get("needs_enrichment", False)

@node(output_name="enriched")
def enrich(data: dict) -> dict:
    return {**data, "enriched": True}

@node(output_name="skip_marker")
def skip(data: dict) -> dict:
    return data

@node(output_name="result")
def finalize(data: dict, enriched: dict = None) -> dict:
    # enriched may or may not exist depending on branch
    if enriched:
        return enriched
    return data
```

**Resolution**: In branch scenarios, the framework handles this via gate-based execution. The default value provides a fallback when the producing branch isn't taken.

### 4. Runtime Input Overriding Edge Value

**Scenario**: User provides runtime input for a value that's also produced by an edge.

```python
@node(output_name="config")
def load_config() -> dict:
    return {"threshold": 0.5}

@node(output_name="result")
def process(config: dict) -> str:
    return str(config)

graph = Graph(nodes=[load_config, process])
result = graph.run(inputs={"config": {"threshold": 0.9}})
```

**Resolution**: **Error**. The framework raises an error when runtime inputs attempt to override edge-connected values:

```python
# Raises: ValueError("Cannot provide 'config' as input: it is produced by node 'load_config'")
```

**Rationale**: Edge connections define the graph structure — overriding them at runtime breaks the dataflow execution model. If users need this flexibility, they should:
- Not include the producing node in the graph
- Use a different parameter name
- Restructure the graph to accept the value as a true root input

### 5. None as a Valid Value

**Scenario**: Distinguishing between "not provided" and "explicitly None".

```python
@node(output_name="result")
def process(data: str, context: str | None = "default") -> str:
    if context is None:
        return f"No context: {data}"
    return f"{context}: {data}"
```

**The problem**: How do we distinguish these calls?

```python
# Case 1: User wants the default ("default")
graph.run(inputs={"data": "test"})
# Expected: "default: test"

# Case 2: User explicitly wants None
graph.run(inputs={"data": "test", "context": None})
# Expected: "No context: test"
```

Using `dict.get("context")` returns `None` in both cases — we can't tell if the key was missing or explicitly set to `None`.

**Resolution**: Use a sentinel value internally to distinguish "not provided" from "explicitly None":

```python
_NOT_PROVIDED = object()  # Unique sentinel, not equal to anything

def resolve_input(param, runtime_inputs, bound, default):
    # Check if key exists in runtime_inputs (even if value is None)
    value = runtime_inputs.get(param, _NOT_PROVIDED)
    if value is not _NOT_PROVIDED:
        return value  # Key was provided (could be None, that's intentional)

    # Key wasn't provided — check bound values
    value = bound.get(param, _NOT_PROVIDED)
    if value is not _NOT_PROVIDED:
        return value

    # Fall back to function default
    return default
```

**Alternative approach**: Use `param in runtime_inputs` check:

```python
def resolve_input(param, runtime_inputs, bound, default):
    if param in runtime_inputs:
        return runtime_inputs[param]  # Explicit None preserved
    if param in bound:
        return bound[param]
    return default
```

**Key insight**: `None` is a valid, intentional value. The framework must respect explicit `None` values provided by users.

### 6. Binding in Nested Graphs

**Scenario**: Inner graph has bindings, outer graph also binds same parameter.

```python
inner = Graph(nodes=[process]).bind(threshold=0.5)
outer = Graph(nodes=[inner.as_node()]).bind(threshold=0.8)
```

**Question**: Which binding wins?

**Resolution**: Follow the same priority rules:
1. Outer runtime inputs
2. Outer bindings
3. Inner bindings (via input_mapping)
4. Inner function defaults

The outer binding (0.8) wins if `threshold` is mapped through.

### 7. Dynamic Default Values

**Scenario**: Default value that depends on other parameters.

```python
@node(output_name="result")
def process(data: str, limit: int = len(data)):  # Invalid Python!
    ...
```

**Resolution**: Python doesn't support this directly. Use `None` as sentinel:

```python
@node(output_name="result")
def process(data: str, limit: int | None = None) -> str:
    if limit is None:
        limit = len(data)
    return data[:limit]
```

---

## Summary

### Value Priority

| Priority | Source | Behavior |
|----------|--------|----------|
| 1 | **Runtime input** | Highest priority for root args (Error if edge-connected) |
| 2 | **Edge connection** | Required dependency, cannot be overridden |
| 3 | **Bound value** | Makes root args optional (Error if edge-connected) |
| 4 | **Function default** | Fallback for unbound root args |

### Key Rules

1. **Edge connections are immutable**: You cannot bind or provide runtime values for edge-connected parameters. This is an error.

2. **Edge connections cancel optionality**: A parameter with an edge is REQUIRED, regardless of defaults or bindings.

3. **Runtime inputs override bound/default**: For root args (no edge), runtime values take precedence.

4. **Explicit None is preserved**: `graph.run(inputs={"x": None})` passes `None`, not the default.

### Error Conditions

| Action | Condition | Error |
|--------|-----------|-------|
| `.bind(x=val)` | `x` is produced by a node | `ValueError: Cannot bind 'x': produced by 'node_name'` |
| `.run(inputs={"x": val})` | `x` is produced by a node | `ValueError: Cannot provide 'x': produced by 'node_name'` |
| `.run(inputs={})` | Required `x` missing | `ValueError: Missing required input: 'x'` |
