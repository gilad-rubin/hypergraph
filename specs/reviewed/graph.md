# Graph API Specification

**Pure structure definition of a computation graph.** No execution logic, just nodes and their relationships.

---

## Overview

`Graph` is a pure structure definition. It has no execution logic, no state, no cache - just structure and validation.

**See also:**
- [Node Types](node-types.md) - All node types including GraphNode
- [Execution Types](execution-types.md) - Runtime state and results
- [Runners API](runners.md) - Execution guide
- [State Model](state-model.md) - "Outputs ARE state" philosophy
- [Durable Execution](durable-execution.md) - Checkpointing and persistence

---

## Constructor

```python
class Graph:
    """Graph structure definition."""

    def __init__(
        self,
        nodes: list[HyperNode],
        *,
        name: str | None = None,
        strict_types: bool = False,
        complete_on_stop: bool = False,
    ):
        """
        Create a graph from nodes.

        Args:
            nodes: List of HyperNode objects
            name: Optional graph name. Used as default for as_node() when
                  nesting this graph. If not set here, must be provided
                  when calling as_node(name='...')
            strict_types: Validate type annotations between connected nodes (default: False)
            complete_on_stop: Behavior when a stop signal is received during execution.

                True:
                  - Streaming nodes: save partial output, mark step COMPLETED with truncated=True
                  - Regular nodes being stopped: mark step STOPPED (no output possible)
                  - Continue executing remaining nodes in the graph

                False (default):
                  - All stopped nodes: mark step STOPPED (no output)
                  - Stop graph immediately, skip remaining nodes

                This setting can be overridden when using as_node(complete_on_stop=...).
                See durable-execution.md for patterns and examples.

        Example:
            # Basic graph
            graph = Graph(nodes=[embed, retrieve, generate])

            # Named graph for nesting
            rag = Graph(nodes=[embed, retrieve, generate], name="rag_pipeline")
            outer = Graph(nodes=[preprocess, rag.as_node(), postprocess])

            # Graph that saves partial streaming output on stop
            chat = Graph(
                nodes=[generate, accumulate],
                name="chat",
                complete_on_stop=True,  # Partial responses saved to history
            )
        """
        self._nodes = {n.name: n for n in nodes}
        self.name = name
        self.complete_on_stop = complete_on_stop
        self._nx_graph = self._build_graph(nodes)
        self._bound = {}
        self._validate()  # Build-time validation
```

---

## Properties

**Ordering:** All tuple/frozenset properties maintain deterministic order based on node list order (the order nodes were passed to the constructor). This ensures consistent behavior across runs.

### Structure Properties

```python
@property
def nodes(self) -> dict[str, HyperNode]:
    """Map of node name → node object. Use .keys() for node names."""

@property
def nx_graph(self) -> nx.DiGraph:
    """Underlying NetworkX graph for visualization/analysis.

    Node attributes:
        - 'hypernode': The HyperNode/RouteNode/BranchNode object
        - 'is_gate': True for route/branch nodes
        - 'node_type': 'node' | 'route' | 'branch' | 'interrupt'

    Edge attributes:
        - 'edge_type': 'data' | 'control'
        - 'value_name': For data edges, the value being passed
        - 'condition': For control edges, the gate condition
    """
```

### Graph Feature Checks

These use short-circuit evaluation - no list building needed. Used by runners for compatibility validation.

```python
@property
def has_cycles(self) -> bool:
    """True if this graph level contains cycles.

    Note: Only checks current level, not nested GraphNodes.
    Implementation: not nx.is_directed_acyclic_graph(self._nx_graph)
    """

@property
def has_gates(self) -> bool:
    """True if graph contains routing gates (RouteNode, BranchNode, TypeRouteNode).

    Implementation: any(isinstance(n, GateNode) for n in self._nodes.values())
    """

@property
def has_interrupts(self) -> bool:
    """True if graph contains InterruptNodes.

    Implementation: any(isinstance(n, InterruptNode) for n in self._nodes.values())
    """

@property
def has_async_nodes(self) -> bool:
    """True if any FunctionNode is async.

    Implementation: any(isinstance(n, FunctionNode) and n.is_async for n in self._nodes.values())
    """
```

### Detailed Node Lists

When you need the actual nodes (not just boolean checks):

```python
@property
def interrupt_nodes(self) -> list[InterruptNode]:
    """All interrupt nodes."""
```

### Input/Output Properties

```python
@property
def inputs(self) -> InputSpec:
    """Input parameter specification. See InputSpec below for details."""

@property
def outputs(self) -> tuple[str, ...]:
    """All output names produced by nodes, in node order."""

@property
def leaf_outputs(self) -> tuple[str, ...]:
    """Outputs from leaf nodes (nodes with no downstream consumers)."""
```

---

## Methods

### bind()

**Purpose**: Set default values for graph inputs, similar to `functools.partial`.

```python
def bind(self, **values: Any) -> Graph:
    """
    Bind default values for graph inputs.

    Bound values are used when:
    - Parameter has no incoming edge, AND
    - No runtime input provided

    Args:
        **values: Parameter name → value mappings

    Returns:
        New Graph instance with bound values.
        Original graph is not modified (immutable operation).

    Raises:
        ValueError: If attempting to bind a value produced by an edge

    Example:
        graph = Graph(nodes=[process])
        bound = graph.bind(temperature=0.7, max_tokens=1000)

        # These are equivalent:
        runner.run(bound, values={"query": "hello"})
        runner.run(graph, values={"query": "hello", "temperature": 0.7, "max_tokens": 1000})
    """
```

#### Value Resolution Order

When determining what value to use for a parameter:

```
1. Edge value (if upstream executed)  - output from upstream node flows forward
2. Input value                         - from merged inputs (checkpoint + runtime)
3. Bound value (via .bind())           - default set on graph
4. Function default                    - default in function signature
```

**Key insight:** When you provide a `workflow_id`, checkpoint state is merged with your inputs *before* execution. Inputs win on conflicts. This means during execution, there's no separate "checkpoint value" — it's already part of the merged inputs.

This hierarchy enables powerful patterns:
- **Skip nodes** by providing their outputs as inputs
- **Continuation state** loads automatically from checkpoint
- **Inputs override checkpoint** (explicit values always win)
- **Bound values initialize seeds** (like empty message lists)

See [State Model](state-model.md#value-resolution-hierarchy) for detailed examples.

#### Binding Examples

**Basic binding:**

```python
@node(output_name="result")
def process(query: str, model: str, temperature: float) -> str:
    return llm.invoke(query, model=model, temperature=temperature)

graph = Graph(nodes=[process])

# Bind common defaults
bound = graph.bind(model="gpt-4", temperature=0.7)

# Now only query is required
result = runner.run(bound, values={"query": "hello"})

# Can still override at runtime
result = runner.run(bound, values={
    "query": "hello",
    "temperature": 0.9  # Overrides bound value
})
```

**Method chaining:**

```python
# .bind() returns self for chaining
graph = (
    Graph(nodes=[embed, retrieve, generate])
    .bind(model="text-embedding-ada-002")
    .bind(top_k=10, temperature=0.7)  # Multiple calls merge
)

# Equivalent to:
graph.bind(model="text-embedding-ada-002", top_k=10, temperature=0.7)
```

**Error: binding edge-connected values:**

```python
@node(output_name="config")
def load_config() -> dict:
    return {"threshold": 0.7}

@node(output_name="result")
def process(data: str, config: dict) -> str:
    return transform(data, config)

graph = Graph(nodes=[load_config, process])

# ❌ Error: config is produced by an edge
graph.bind(config={"threshold": 0.5})
# → ValueError: Cannot bind 'config': it is produced by node 'load_config'
```

**Why?** Edge connections define the graph structure. Binding edge-connected values would silently override the graph's dataflow.

#### Properties After Binding

```python
# Example: how properties change after binding
graph = Graph(nodes=[process])

# Before binding
graph.inputs.all       # frozenset({"query", "model", "temperature"})
graph.inputs.required  # frozenset({"query", "model", "temperature"})  (assuming no defaults)
graph.inputs.optional  # frozenset()
graph.inputs.bound     # {}

# After binding
bound = graph.bind(model="gpt-4", temperature=0.7)

bound.inputs.all       # frozenset({"query", "model", "temperature"})  (unchanged)
bound.inputs.required  # frozenset({"query"})  (model & temperature now have fallbacks)
bound.inputs.optional  # frozenset({"model", "temperature"})  (have bound values)
bound.inputs.bound     # {"model": "gpt-4", "temperature": 0.7}
```

### unbind()

```python
def unbind(self, *keys: str) -> Graph:
    """Remove specific bindings."""
```

**Example:**

```python
bound = graph.bind(a=1, b=2, c=3)
bound.unbind("b")        # Removes b, keeps a and c
bound.unbind("a", "c")   # Removes a and c, keeps b
bound.unbind()           # Removes all bindings
```

### as_node()

```python
def as_node(
    self,
    *,
    name: str | None = None,
    runner: BaseRunner | None = None,
    complete_on_stop: bool | None = None,
) -> GraphNode:
    """
    Wrap graph as a node for composition.

    Returns a NEW GraphNode instance. Does NOT modify this Graph.

    Name Resolution (in GraphNode):
        1. Use `name` parameter if provided
        2. Otherwise use `graph.name` (from Graph constructor)
        3. If neither exists, raise ValueError

    Args:
        name: Override node name (default: use graph.name)
        runner: Runner for nested execution (default: inherit from parent)
        complete_on_stop: Override graph's complete_on_stop setting.
            If None (default), inherits from Graph(..., complete_on_stop=).
            See Graph constructor for behavior details.

    Returns:
        GraphNode with graph's leaf_outputs as default outputs.
        Use .with_outputs() to override output names.
        Use .map_over() to configure iteration.

    Raises:
        ValueError: If name not provided and graph has no name.

    Example:
        # Simple wrapping (uses graph.name if set)
        node = graph.as_node()

        # Override name
        node = graph.as_node(name="custom")

        # Override complete_on_stop for this nested usage
        node = graph.as_node(complete_on_stop=True)

        # Configure outputs and iteration via chaining
        node = (
            graph.as_node()
            .with_outputs(docs="retrieved_docs")
            .map_over("query")
        )
    """
    return GraphNode(self, name=name, runner=runner, complete_on_stop=complete_on_stop)
```

**Name resolution examples:**

```python
# Option 1: Name from Graph constructor
named_graph = Graph(nodes=[double, add_ten], name="math_ops")
node1 = named_graph.as_node()  # Uses graph.name
assert node1.name == "math_ops"

# Option 2: Override with as_node(name=...)
node2 = named_graph.as_node(name="custom_name")  # Overrides graph.name
assert node2.name == "custom_name"

# Option 3: Provide name on as_node when Graph has no name
anonymous_graph = Graph(nodes=[double, add_ten])  # No name
node3 = anonymous_graph.as_node(name="required_name")  # Must provide
assert node3.name == "required_name"

# Error case: Neither Graph nor as_node has a name
try:
    anonymous_graph.as_node()  # No name anywhere → raises
except ValueError as e:
    assert "GraphNode requires a name" in str(e)
```

### visualize()

```python
def visualize(self, **kwargs):
    """Generate visual representation of graph."""
```

---

## InputSpec

### Purpose

**Structured specification of graph inputs.** Returned by `Graph.inputs`, provides all information about what inputs a graph accepts.

### Class Definition

```python
@dataclass(frozen=True)
class InputSpec:
    """Specification of graph input parameters."""

    required: frozenset[str]
    """Must provide: no incoming edge, not bound, no default value."""

    optional: frozenset[str]
    """Has fallback: bound (highest priority) OR has default value."""

    seeds: frozenset[str]
    """Cycle initialization: params with self/cycle edge that need initial values."""

    bound: dict[str, Any]
    """Currently bound values from .bind(). Takes priority over defaults."""

    @property
    def all(self) -> frozenset[str]:
        """All input names (required + optional)."""
        return self.required | self.optional
```

### Priority Rules

When determining if an input is required or optional:

1. **Bound values** (highest priority): If `name in bound`, it's optional
2. **Default values**: If function parameter has a default, it's optional
3. **Otherwise**: It's required

### Example

```python
@node(output_name="result")
def process(x: int, config: dict = None) -> int:
    return x * 2

graph = Graph(nodes=[process])

# Before binding
assert graph.inputs.required == frozenset({"x"})
assert graph.inputs.optional == frozenset({"config"})  # Has default
assert graph.inputs.all == frozenset({"x", "config"})
assert graph.inputs.bound == {}

# After binding
bound_graph = graph.bind(x=5)
assert bound_graph.inputs.required == frozenset()      # x is now bound
assert bound_graph.inputs.optional == frozenset({"x", "config"})
assert bound_graph.inputs.bound == {"x": 5}

# With cycles
@node(output_name="count")
def counter(count: int) -> int:
    return count + 1

cyclic = Graph(nodes=[counter])  # count feeds back to itself
assert cyclic.inputs.seeds == frozenset({"count"})  # Needs initial value
```

---

## Build-Time Validation

Validation happens at graph construction time (`Graph.__init__`). The graph validates itself before any execution.

### 1. Route Target Validation

```python
def _validate_route_targets(self):
    """All @route/@branch targets must exist or be END."""
    for gate in self.gates:
        targets = gate.targets if isinstance(gate, RouteNode) else [gate.when_true, gate.when_false]
        for target in targets:
            if target is END:
                continue
            if target not in self.node_names:
                closest = find_closest_match(target, self.node_names)
                raise GraphConfigError(
                    f"@route target '{target}' doesn't exist\n\n"
                    f"  → {gate.name}() declares target '{target}'\n"
                    f"  → No node named '{target}' in this graph\n"
                    f"  → Available nodes: {sorted(self.node_names)}\n"
                    + (f"\nDid you mean '{closest}'?" if closest else "")
                )
```

### 2. Parallel Producer Validation

```python
def _validate_no_conflicts(self):
    """Multiple producers of same output must be mutually exclusive."""
    for output in self.outputs:
        producers = self._producers_of(output)
        if len(producers) > 1:
            # Check if all pairs are mutually exclusive
            for i, p1 in enumerate(producers):
                for p2 in producers[i+1:]:
                    if not self._mutually_exclusive(p1, p2):
                        raise GraphConfigError(
                            f"Multiple nodes produce '{output}'\n\n"
                            f"  → {p1} creates '{output}'\n"
                            f"  → {p2} creates '{output}'\n\n"
                            f"The problem: If both run, which value should we use?\n\n"
                            f"How to fix:\n"
                            f"  Option A: Rename one output to avoid conflict\n"
                            f"  Option B: Add @branch to make them mutually exclusive\n"
                        )
```

### 3. Cycle Termination Validation

```python
def _validate_cycle_termination(self):
    """Every cycle must have a path to termination."""
    for cycle in self.cycles:
        can_terminate = False

        # Check for route with END
        for node_name in cycle:
            node = self.nodes[node_name]
            if isinstance(node, RouteNode) and END in node.targets:
                can_terminate = True
                break

        # Check for path to leaf
        if not can_terminate:
            for node_name in cycle:
                for leaf in self.leaf_nodes:
                    if nx.has_path(self.nx_graph, node_name, leaf):
                        can_terminate = True
                        break

        if not can_terminate:
            raise GraphConfigError(
                f"Cycle has no termination path\n\n"
                f"  → Cycle: {' → '.join(cycle)}\n"
                f"  → No @route returns END\n"
                f"  → No path to a leaf node\n\n"
                f"How to fix:\n"
                f"  Add a @route that can return END to break the cycle"
            )
```

### 4. Deadlock Detection

```python
def _validate_no_deadlock(self):
    """Cycles must have valid starting inputs."""
    for cycle in self.cycles:
        can_start = False

        for node_name in cycle:
            node = self.nodes[node_name]
            # Can this node start from external inputs?
            external_deps = [
                p for p in node.parameters
                if not self._is_produced_in_cycle(p, cycle)
            ]

            # If all external deps can be satisfied, cycle can start
            if all(self._can_satisfy(d) for d in external_deps):
                can_start = True
                break

        if not can_start:
            raise GraphConfigError(
                f"Cycle cannot start - deadlock detected\n\n"
                f"  → Cycle: {' → '.join(cycle)}\n"
                f"  → Every node depends on another node in the cycle\n"
                f"  → No external entry point\n\n"
                f"How to fix:\n"
                f"  Ensure at least one node can start from external inputs"
            )
```

### 5. GraphNode Map + Interrupts Validation

```python
def _validate_graphnode_map_over(self):
    """GraphNodes with map_over cannot wrap graphs containing interrupts."""
    for node in self._nodes.values():
        if isinstance(node, GraphNode) and node._map_over:
            # Use shared validation function from runners module
            validate_map_compatible(
                node.graph,
                context=f"GraphNode '{node.name}' has map_over={node._map_over}"
            )
```

This validation uses the shared `validate_map_compatible()` function (see [runners-api-reference.md](runners-api-reference.md#validate_map_compatible)) to ensure GraphNodes configured with `.map_over()` don't wrap graphs containing interrupts.

**Example error:**

```python
# Inner graph with interrupt
inner = Graph(nodes=[
    process_node,
    InterruptNode("approval", "draft", "decision"),
    finalize_node,
], name="workflow")

# ❌ Error at Graph() construction time
outer = Graph(nodes=[
    inner.as_node().map_over("data"),  # Can't map over graph with interrupts
])
# → GraphConfigError: GraphNode 'workflow' has map_over=['data'],
#                     but the graph contains interrupts.
#
#   The problem: map runs the graph multiple times in batch,
#   but interrupts pause for human input - these don't mix.
#
#   Interrupts found: ['approval']
#
#   How to fix:
#     Use runner.run() in a loop instead of map
```

### 6. Node Name Validation

```python
def _validate_node_names(self):
    """Node and output names cannot contain '/'."""
    for node in self._nodes.values():
        # Check node name
        if "/" in node.name:
            raise GraphConfigError(
                f"Invalid node name: '{node.name}'\n\n"
                f"  → Names cannot contain '/'\n\n"
                f"The problem:\n"
                f"  The '/' character is reserved as the path separator for\n"
                f"  nested graphs. When you access nested outputs like:\n\n"
                f"    result['rag_pipeline/embedding']\n"
                f"    runner.run(..., select=['outer/inner/*'])\n\n"
                f"  The '/' tells hypergraph to navigate into nested RunResults.\n"
                f"  If node names contained '/', paths would be ambiguous:\n\n"
                f"    'rag/pipeline/embedding' - Is this:\n"
                f"      • 'rag' → 'pipeline' → 'embedding' (3 levels)?\n"
                f"      • 'rag/pipeline' → 'embedding' (2 levels)?\n\n"
                f"How to fix:\n"
                f"  Use underscores or hyphens instead:\n"
                f"  • 'rag_pipeline' ✓\n"
                f"  • 'rag-pipeline' ✓\n"
                f"  • 'rag/pipeline' ✗"
            )

        # Check output name
        if hasattr(node, 'output_name') and "/" in node.output_name:
            raise GraphConfigError(
                f"Invalid output name: '{node.output_name}' (from node '{node.name}')\n\n"
                f"  → Output names cannot contain '/'\n"
                f"  → See above for why '/' is reserved"
            )
```

### 7. Namespace Collision Validation

```python
def _validate_no_namespace_collision(self):
    """Output names cannot match GraphNode names (would create ambiguous paths)."""
    graphnode_names = {
        node.name for node in self._nodes.values()
        if isinstance(node, GraphNode)
    }

    for node in self._nodes.values():
        output_name = getattr(node, 'output_name', node.name)
        if output_name in graphnode_names and node.name != output_name:
            raise GraphConfigError(
                f"Namespace collision: output '{output_name}' matches GraphNode name\n\n"
                f"  → Node '{node.name}' produces output '{output_name}'\n"
                f"  → GraphNode '{output_name}' exists in this graph\n\n"
                f"The problem:\n"
                f"  RunResult.values stores both regular outputs and nested\n"
                f"  RunResults from GraphNodes in the same dict:\n\n"
                f"    result.values = {{\n"
                f"      'answer': '...',           # Regular output\n"
                f"      'rag_pipeline': RunResult  # Nested graph\n"
                f"    }}\n\n"
                f"  If an output name equals a GraphNode name, result['{output_name}']\n"
                f"  is ambiguous - is it the output value or the nested RunResult?\n\n"
                f"How to fix:\n"
                f"  Rename one to avoid the collision:\n"
                f"  • Change the output name: @node(output_name='...')\n"
                f"  • Change the GraphNode name: graph.as_node(name='...')"
            )
```

**Example error:**

```python
@node(output_name="summary")
def summarize(text: str) -> str:
    return text[:100]

# Nested graph also named "summary"
inner = Graph(nodes=[...], name="summary")

# ❌ Error at Graph() construction time
outer = Graph(nodes=[summarize, inner.as_node()])
# → GraphConfigError: Namespace collision: output 'summary' matches GraphNode name
#
#   → Node 'summarize' produces output 'summary'
#   → GraphNode 'summary' exists in this graph
#
#   The problem:
#     RunResult.values stores both regular outputs and nested
#     RunResults from GraphNodes in the same dict...
```

---

## Usage Examples

### Basic Graph

```python
@node(output_name="embedded")
def embed(text: str) -> list[float]:
    return model.encode(text)

@node(output_name="result")
def classify(embedded: list[float]) -> str:
    return classifier.predict(embedded)

graph = Graph(nodes=[embed, classify])
# Edges inferred: text → embed → classify
```

### Cyclic Graph

```python
@node(output_name="response")
def generate(messages: list) -> str:
    return llm.chat(messages)

@node(output_name="messages")
def accumulate(messages: list, response: str) -> list:
    return messages + [{"role": "assistant", "content": response}]

@route(targets=["generate", END])
def check_done(messages: list) -> str:
    return END if is_complete(messages) else "generate"

graph = Graph(nodes=[generate, accumulate, check_done])
# Cycle: generate → accumulate → check_done → generate
```

### Nested Graphs

```python
inner = Graph(nodes=[step1, step2, step3], name="inner")
outer = Graph(nodes=[
    preprocess,
    inner.as_node(),
    postprocess,
])
```

---

## Nested Graph Results

### Graph Names Are Mandatory for Nesting

When nesting graphs, the nested graph **must** have a name to be addressable in results. You can provide the name either:

1. **When constructing the Graph** (recommended):
```python
rag = Graph(nodes=[...], name="rag_pipeline")
outer = Graph(nodes=[preprocess, rag.as_node(), postprocess])
```

2. **When calling .as_node()**:
```python
rag = Graph(nodes=[...])
outer = Graph(nodes=[preprocess, rag.as_node(name="rag_pipeline"), postprocess])
```

**If you provide both, `.as_node(name=...)` overrides `Graph(..., name=...)`.**

### Result Structure

Results from nested graphs are returned as nested `RunResult` objects. This preserves the full execution context (status, pause info) for each subgraph:

```python
result = await runner.run(outer_graph, values={...})

# Direct values
result["answer"]                      # value
result["cleaned"]                     # value

# Nested graphs (by name) → RunResult objects
result["rag_pipeline"]                # RunResult (with its own status, pause, etc.)
result["rag_pipeline"]["embedding"]   # value inside nested result
result["rag_pipeline"]["inner"]       # another nested RunResult
```

Both output values and nested graph names share the same namespace:

```python
result.values = {
    "answer": "...",                  # output value
    "rag_pipeline": RunResult(...),   # nested graph by name
}
```

### Why Nested RunResult?

Using `RunResult` (not a simpler value container) for nested graphs enables:

1. **Per-subgraph status:** Know if a subgraph completed, paused, or is running
2. **Pause propagation:** When a nested graph pauses, you can identify which one
3. **Consistent access:** Same dict-like API at every level

```python
# Example: Nested graph paused
result = await runner.run(outer_graph, values={...})

if result.status == RunStatus.PAUSED:
    # Find which nested graph paused
    if result["rag_pipeline"].status == RunStatus.PAUSED:
        print(f"RAG paused: {result['rag_pipeline'].pause}")
```

### Filtering with select Parameter

Use the `select` parameter in `.run()` to filter what's included in the result:

```python
# Default: everything accessible
result = runner.run(graph, values={...})
result.keys()  # ["answer", "cleaned", "rag_pipeline", "other_graph"]

# Filtered: specific outputs only
result = runner.run(graph, values={...}, select=["answer"])
result.keys()  # ["answer"]

# With patterns
result = runner.run(
    graph,
    inputs={...},
    select=["answer", "rag_pipeline/*"]
)
```

### Pattern Syntax

| Pattern | Meaning |
|---------|---------|
| `"answer"` | Specific output |
| `"rag_pipeline"` | Nested graph as RunResult |
| `"rag_pipeline/*"` | All direct outputs from rag_pipeline |
| `"rag_pipeline/**"` | All outputs recursively |
| `"**/embedding"` | Any "embedding" at any depth |
| `"*/docs"` | "docs" from any direct child graph |

### Examples

**Full access (default):**

```python
result = runner.run(graph, values={...})
result["rag_pipeline"]["embedding"]  # accessible
```

**Only top-level values:**

```python
result = runner.run(graph, values={...}, select=["answer", "cleaned"])
result["rag_pipeline"]  # KeyError - not selected
```

**Specific nested outputs:**

```python
result = runner.run(
    graph,
    inputs={...},
    select=["answer", "rag_pipeline/embedding"]
)
result["rag_pipeline"]["embedding"]  # accessible
result["rag_pipeline"]["docs"]       # KeyError - not selected
```

**Everything from a nested graph:**

```python
result = runner.run(graph, values={...}, select=["rag_pipeline/**"])
# All outputs from rag_pipeline and its nested graphs
```

### RunResult for Nested Graphs

Nested graphs return `RunResult` objects, which provide:
- `values`: Dict of output values (and nested `RunResult` for deeper nesting)
- `status`: Execution status (`COMPLETED`, `PAUSED`, `STOPPED`, `FAILED`)
- `pause`: Pause info if the nested graph paused

```python
@dataclass
class RunResult:
    values: dict[str, Any | "RunResult"]  # Supports nesting
    status: RunStatus
    workflow_id: str | None
    run_id: str
    pause: PauseInfo | None = None

    def __getitem__(self, key: str) -> Any | "RunResult":
        return self.values[key]
```

**See [Execution Types](execution-types.md)** for the complete `RunResult` definition, `RunStatus`, and pause handling.

### Nested Graph Example

```python
# Inner RAG pipeline
@node(output_name="embedding")
def embed(query: str) -> list[float]:
    return model.embed(query)

@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]:
    return vector_db.search(embedding)

@node(output_name="response")
def generate(docs: list[str]) -> str:
    return llm.generate(docs)

rag_pipeline = Graph(
    nodes=[embed, retrieve, generate],
    name="rag_pipeline"  # Name required for nesting
)

# Outer pipeline
@node(output_name="cleaned")
def clean(query: str) -> str:
    return query.strip().lower()

outer = Graph(nodes=[
    clean,
    rag_pipeline.as_node(name="rag"),  # Nested
])

# Execute
result = await runner.run(outer, values={"query": "  What is RAG?  "})

# Access results
result["cleaned"]                  # "what is rag?"
result["response"]                 # Final answer
result["rag"]                      # RunResult from nested pipeline
result["rag"]["embedding"]         # Embedding from nested
result["rag"]["docs"]              # Retrieved docs from nested
result["rag"]["response"]          # Same as result["response"]
result["rag"].status               # RunStatus.COMPLETED

# Filtered execution
result = await runner.run(
    outer,
    inputs={"query": "  What is RAG?  "},
    select=["response", "rag/embedding"]  # Only these
)
result["response"]          # Available
result["rag"]["embedding"]  # Available
result["rag"]["docs"]       # KeyError - not selected
```

---

## Type Hierarchy

For the complete node type hierarchy (`HyperNode`, `FunctionNode`, `GateNode`, etc.), see [Node Types - Type Hierarchy Summary](node-types.md#type-hierarchy-summary).

```
Graph (structure definition)
├── InputSpec (input parameter specification, returned by .inputs)
└── GraphState (runtime values - INTERNAL)
    └── Holds ALL values during execution
    └── Used by runners, not user-facing

RunResult (user-facing result)
├── values: dict[str, Any | RunResult]  # Nested graphs → nested RunResult
├── status: RunStatus
├── pause: PauseInfo | None
└── workflow_id, run_id
```

**See [Execution Types](execution-types.md)** for complete type definitions including:
- `RunStatus`, `PauseReason` enums
- `RunResult` with nested support and pause handling
- `Workflow`, `Step`, `StepResult` for persistence
- `GraphState` (internal runtime state)

**Composition pattern:**

GraphNode enables nested composition - it's just another HyperNode:

```python
# Inner graph
inner = Graph(nodes=[embed_node, retrieve_node], name="rag")

# Wrap as node
rag_node = inner.as_node()  # Returns GraphNode

# Use in outer graph
outer = Graph(nodes=[preprocess, rag_node, postprocess])

# GraphNode supports all HyperNode methods:
adapted = (
    inner.as_node()
    .with_name("custom_rag")
    .with_inputs(query="user_question")
    .with_outputs(docs="retrieved_docs")
    .map_over("user_question")
)
```
