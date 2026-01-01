# Unified Graph Execution Model

> **Status**: Design Document  
> **Date**: December 2024  
> **Type**: Implementation & Architecture Guide  
> **Summary**: Concrete architecture for hypernodes using explicit graph modeling with NetworkX, supporting DAGs, branching, cycles, and nested graphs with a single unified execution algorithm.

---

## Related Documents

- **[Design Philosophy](graph_design_philosophy.md)** — Motivation, principles, problem statements, and why this approach was chosen
- **[Edge Cases](graph_edge_cases.md)** — Conflict resolution, parallel producers, mutual exclusivity, and checkpointing edge cases

---

## Table of Contents

1. [Design Goals](#design-goals)
2. [Core Abstractions](#core-abstractions)
3. [Graph Model](#graph-model)
4. [Gates: Branch and Router](#gates-branch-and-router)
5. [Graph State](#graph-state)
6. [Execution Algorithm](#execution-algorithm)
7. [Nested Graphs](#nested-graphs)
8. [Caching](#caching)
9. [Checkpointing](#checkpointing)
10. [Validation](#validation)
11. [API Reference](#api-reference)
12. [Examples](#examples)
13. [Migration from Pipeline](#migration-from-pipeline)
14. [File Structure](#file-structure)

---

## Design Goals

1. **Single execution algorithm** - One algorithm handles DAGs, branching, and cyclic graphs
2. **Explicit graph modeling** - Use NetworkX for graph operations, leverage standard algorithms
3. **Minimal proprietary logic** - Only custom code where no standard algorithm exists
4. **Fail fast** - Static validation at graph build time, not runtime surprises
5. **Pure functions** - Nodes are portable, no framework coupling
6. **Implicit edges** - Dependencies inferred from function signatures
7. **Hierarchical composition** - Graphs nest inside graphs naturally

---

## Core Abstractions

### Node

A node wraps a Python function with metadata:

```python
@node(output_name="result")
def process(x: int, y: str) -> float:
    return float(x) * len(y)
```

**Properties**:
- `name`: Function name (default) or custom
- `output_name`: String or tuple of output names
- `root_args`: Input parameter names (from signature)
- `code_hash`: SHA256 of function source (for caching)
- `cache`: Whether to cache output (default: True)

### Graph

The main user-facing class:

```python
graph = Graph(nodes=[node1, node2, gate1])
result = graph.run(inputs={"x": 5})
```

### Gate

Controls execution flow. Two types:

1. **Branch** - Boolean, exactly 2 paths
2. **Router/Gate** - Enum-based, N paths, can open multiple

### Transition

Result of a gate decision - which node(s) to activate next.

---

## Graph Model

### ExecutionGraph

Single NetworkX DiGraph with node and edge attributes:

```python
class ExecutionGraph:
    """Graph representation for execution."""

    def __init__(self, nodes: List[HyperNode]):
        self._graph = nx.DiGraph()
        self._nodes = {n.name: n for n in nodes}
        self._build(nodes)
```

### Node Attributes

```python
G.add_node(node.name,
    hypernode=node,      # The actual node object
    is_gate=False,       # Is this a gate node?
    is_branch=False,     # Is this a branch node?
)
```

### Edge Types

Two edge types distinguished by `edge_type` attribute:

**Data edges** - Dependency flow:
```python
G.add_edge(source_node, dest_node,
    edge_type="data",
    value="x"  # The value name being passed
)
```

**Control edges** - Gate routing:
```python
G.add_edge(gate_node, target_node,
    edge_type="control",
    decision="CONTINUE"  # Enum member name
)
```

### Computed Properties

All derived from graph structure, cached lazily:

```python
@cached_property
def source_to_node(self) -> Dict[str, str]:
    """Value name -> source node name."""

@cached_property
def root_inputs(self) -> List[str]:
    """External inputs (values with no source in graph)."""

@cached_property
def has_cycles(self) -> bool:
    """Check for cycles at this level or any nested level."""
```

### Standard Graph Operations

Use NetworkX directly:

| Operation | NetworkX Function |
|-----------|-------------------|
| Cycle detection | `nx.is_directed_acyclic_graph(G)` |
| Topological sort | `nx.topological_sort(G)` |
| Find ancestors | `nx.ancestors(G, node)` |
| Find descendants | `nx.descendants(G, node)` |
| Reachability | `nx.has_path(G, u, v)` |
| SCCs (for cycles) | `nx.strongly_connected_components(G)` |

---

## Gates: Branch and Router

### Branch (Boolean Gate)

For simple true/false decisions, use `Literal` types for string-based targets:

```python
from typing import Literal

@branch(when_true="handle_valid", when_false="handle_error")
def check(data: dict) -> bool:
    return data.get("valid")
```

**Properties**:
- Returns `bool`
- Exactly 2 targets declared in decorator as strings
- Targets are validated at graph initialization (must match node names)
- Clean syntax for common case

### Router/Gate (Literal-based)

For multi-way routing, use `Literal` types with optional descriptions:

```python
from typing import Literal
from hypernodes import gate, END

# Simple: just target strings
AgentAction = Literal["research", "retrieve", "respond", END]

@gate
def agent_decide(state: dict) -> AgentAction:
    if state.get("ready"):
        return "respond"
    return "research"
```

**With descriptions** (for visualization and documentation):

```python
from typing import Literal
from hypernodes import gate, END

# Tuple format: (target, description)
AgentAction = Literal[
    "research",                                    # No description
    ("retrieve", "Fetch from vector store"),      # With description
    ("respond", "Generate final response"),       # With description
    END,                                           # Termination
]

@gate
def agent_decide(state: dict) -> AgentAction:
    if state.get("ready"):
        return "respond"  # Return just the target string
    return "research"
```

**Target values can be**:
- `"node_name"` - Just the target node name (string)
- `("node_name", "description")` - Target with description for visualization
- `END` - Special sentinel for termination

### Multiple Gate Activation

Gates can open multiple paths simultaneously:

```python
@gate
def parallel_decide(state: dict) -> AgentAction | list[AgentAction]:
    if state.get("needs_both"):
        return ["research", "retrieve"]  # Both activate
    return "respond"
```

**Constraint**: Targets that can be activated together must not produce the same output (validated at build time).

### Gate Implementation

```python
from typing import Literal, get_args, get_origin, Union

@dataclass
class GateNode:
    func: Callable
    routes_literal: type  # The Literal type from return annotation

    @cached_property
    def targets(self) -> Set[str]:
        """Extract target node names from Literal type."""
        return {self._extract_target(v) for v in get_args(self.routes_literal)}

    @cached_property
    def descriptions(self) -> Dict[str, str]:
        """Extract descriptions from tuple values."""
        result = {}
        for value in get_args(self.routes_literal):
            target, desc = self._extract_route(value)
            if desc:
                result[target] = desc
        return result

    def _extract_route(self, value) -> Tuple[str, str]:
        """Extract (target, description) from Literal value."""
        if isinstance(value, tuple):
            return value[0], value[1] if len(value) > 1 else ""
        return value, ""

    def _extract_target(self, value) -> str:
        """Extract target string from value (handles tuples)."""
        return value[0] if isinstance(value, tuple) else value

    def __call__(self, **inputs) -> Set[str]:
        """Execute gate, return set of activated target names."""
        result = self.func(**inputs)

        # Normalize to list
        decisions = result if isinstance(result, list) else [result]

        # Validate targets exist in Literal type
        valid_targets = self.targets
        for target in decisions:
            if target not in valid_targets and target != END:
                raise ValueError(f"Invalid route '{target}'. Valid: {valid_targets}")

        return set(decisions)

def gate(func: Callable) -> GateNode:
    """Decorator that creates GateNode from function with Literal return type."""
    hints = get_type_hints(func)
    return_hint = hints.get('return')

    # Handle Union types (Route | list[Route])
    origin = get_origin(return_hint)
    if origin is Union:
        args = get_args(return_hint)
        # Find the Literal type
        literal_type = next(
            a for a in args 
            if get_origin(a) is Literal or (hasattr(a, '__origin__') and a.__origin__ is Literal)
        )
    else:
        literal_type = return_hint

    if get_origin(literal_type) is not Literal:
        raise TypeError(f"Gate return type must be a Literal type")

    return GateNode(func=func, routes_literal=literal_type)
```

**Validation at Graph initialization:**

```python
class Graph:
    def __init__(self, nodes: list[HyperNode]):
        self.nodes = nodes
        self._node_names = {n.name for n in nodes}
        self._validate_gate_targets()
    
    def _validate_gate_targets(self):
        """Validate all gate/branch targets reference existing nodes."""
        for node in self.nodes:
            if isinstance(node, GateNode):
                for target in node.targets:
                    if target != END and target not in self._node_names:
                        raise ValueError(
                            f"Gate '{node.name}' references unknown node '{target}'. "
                            f"Available: {self._node_names}"
                        )
            elif isinstance(node, BranchNode):
                for target in [node.when_true, node.when_false]:
                    if target != END and target not in self._node_names:
                        raise ValueError(
                            f"Branch '{node.name}' references unknown node '{target}'. "
                            f"Available: {self._node_names}"
                        )
```

---

## Graph State

### GraphState

Tracks execution state with versioning for cycle support:

```python
@dataclass
class VersionedValue:
    data: Any
    version: int

class GraphState:
    """Tracks execution state."""

    def __init__(self):
        self.values: Dict[str, VersionedValue] = {}
        self.executed: Dict[str, Dict[str, int]] = {}  # node -> {input: version_used}
        self.active_gates: Set[str] = set()  # Currently activated targets

    def write(self, name: str, data: Any) -> int:
        """Write value, return new version."""
        current = self.values.get(name)
        new_version = (current.version + 1) if current else 1
        self.values[name] = VersionedValue(data, new_version)
        return new_version

    def read(self, name: str) -> Any:
        """Read value data."""
        return self.values[name].data

    def get_version(self, name: str) -> int:
        """Get current version (0 if not exists)."""
        return self.values.get(name, VersionedValue(None, 0)).version

    def has_value(self, name: str) -> bool:
        return name in self.values

    def record_execution(self, node_name: str, input_versions: Dict[str, int]):
        """Record that node executed with given input versions."""
        self.executed[node_name] = input_versions

    def has_executed(self, node_name: str) -> bool:
        return node_name in self.executed
```

### Versioning

Every value has a version number:
- Starts at 1 when first written
- Increments on each update
- Used for staleness detection in cyclic graphs

For acyclic graphs, versions stay at 1 (each node runs once).

---

## Execution Algorithm

### Single Algorithm for All Graph Types

The same algorithm handles DAGs, branching, and cycles:

```python
class GraphEngine:
    """Executes any graph structure."""

    def run(
        self,
        graph: ExecutionGraph,
        inputs: Dict[str, Any],
        output_names: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Execute graph."""
        state = GraphState()
        node_signatures: Dict[str, str] = {}

        # Initialize inputs
        for name, data in inputs.items():
            state.write(name, data)

        # Main execution loop
        while True:
            ready = find_ready_nodes(graph, state)

            if not ready:
                break

            check_write_conflicts(ready, graph)

            for node_name in ready:
                terminated = self._execute_node(
                    node_name, graph, state, node_signatures
                )
                if terminated:
                    return state.get_outputs(output_names)

        return state.get_outputs(output_names)
```

### Node Readiness

A node is ready when ALL conditions are met:

```python
def is_node_ready(node_name: str, graph: ExecutionGraph, state: GraphState) -> bool:
    node = graph.get_node(node_name)

    # 1. All inputs available
    if not inputs_available(node, state):
        return False

    # 2. Node is stale (needs execution)
    if not is_stale(node_name, node, graph, state):
        return False

    # 3. Gate satisfied (not blocked by routing)
    if not gate_satisfied(node_name, state):
        return False

    return True
```

### Input Availability

```python
def inputs_available(node: HyperNode, state: GraphState) -> bool:
    """Check if all inputs have values."""
    return all(state.has_value(p) for p in node.root_args)
```

### Staleness Detection

For cycle support - determines if node needs (re)execution:

```python
def is_stale(
    node_name: str,
    node: HyperNode,
    graph: ExecutionGraph,
    state: GraphState
) -> bool:
    """Check if node needs (re)execution."""
    # Never ran = stale
    if not state.has_executed(node_name):
        return True

    used_versions = state.executed[node_name]

    for param in node.root_args:
        # Sole source rule: skip self-produced values
        if is_sole_source(param, node_name, graph):
            continue

        current = state.get_version(param)
        used = used_versions.get(param, 0)

        if current > used:
            return True  # Input changed since last run

    return False
```

### Sole Source Rule

Prevents infinite loops for accumulator patterns:

```python
def is_sole_source(value: str, node_name: str, graph: ExecutionGraph) -> bool:
    """Check if node is the only source of this value."""
    return graph.source_to_node.get(value) == node_name
```

Example: `add_response` both reads and writes `messages`. Without this rule, it would re-trigger from its own output.

### Gate Satisfaction

```python
def gate_satisfied(node_name: str, state: GraphState) -> bool:
    """Check if node's gate requirements are met."""
    if not state.active_gates:
        return True  # No gates active = all nodes can run
    return node_name in state.active_gates
```

### Finding Ready Nodes

```python
def find_ready_nodes(graph: ExecutionGraph, state: GraphState) -> Set[str]:
    """Find all nodes ready to execute."""
    return {
        node_name
        for node_name in graph._nodes
        if is_node_ready(node_name, graph, state)
    }
```

### Write Conflict Detection

```python
def check_write_conflicts(ready: Set[str], graph: ExecutionGraph):
    """Raise error if multiple nodes would write same value."""
    outputs = collect_outputs_from_nodes(ready, graph)

    for value, sources in outputs.items():
        if len(sources) > 1:
            raise ConflictError(
                f"Value '{value}' has multiple sources ready: {sources}"
            )
```

### Node Execution

```python
def _execute_node(
    self,
    node_name: str,
    graph: ExecutionGraph,
    state: GraphState,
    node_signatures: Dict[str, str],
) -> bool:
    """Execute single node. Returns True if END reached."""
    node = graph.get_node(node_name)

    # Gather inputs
    node_inputs = {p: state.read(p) for p in node.root_args}
    input_versions = {p: state.get_version(p) for p in node.root_args}

    # Execute (handles caching, callbacks internally)
    result, signature = execute_node(
        node, node_inputs, self.cache, self.callbacks, node_signatures
    )

    # Update state
    update_state_after_execution(
        node, result, signature, state, node_signatures, input_versions
    )

    # Handle gates
    return handle_gate_if_needed(node, result, state)

def handle_gate_if_needed(node: HyperNode, result: Any, state: GraphState) -> bool:
    """Handle gate/branch decisions. Returns True if END reached."""
    if isinstance(node, GateNode):
        targets = result  # GateNode.__call__ returns Set[HyperNode]

        if END in targets:
            return True

        state.active_gates = {t.name for t in targets}
        return False

    if isinstance(node, BranchNode):
        target = node.true_target if result else node.false_target
        state.active_gates = {target.name}
        return False

    # Regular node - clear gates after execution if this node was gated
    if node.name in state.active_gates:
        state.active_gates.discard(node.name)

    return False
```

---

## Nested Graphs

### GraphNode

Wraps a graph to behave as a node:

```python
inner = Graph(nodes=[clean, tokenize])
outer = Graph(nodes=[fetch, inner.as_node(), process])
```

### Hierarchical Model

Nested graphs are opaque nodes with a `_subgraph` attribute:

```python
class GraphNode:
    """Wraps a Graph to behave as a node."""

    def __init__(
        self,
        graph: Graph,
        input_mapping: Optional[Dict[str, str]] = None,
        output_mapping: Optional[Dict[str, str]] = None,
        map_over: Optional[Union[str, List[str]]] = None,
        map_mode: str = "zip"
    ):
        self.graph = graph
        self._subgraph = graph._execution_graph
        self.input_mapping = input_mapping or {}
        self.output_mapping = output_mapping or {}
        self.map_over = map_over
        self.map_mode = map_mode

    @property
    def root_args(self) -> Tuple[str, ...]:
        """Outer parameter names this node needs."""
        inner_inputs = self._subgraph.root_inputs
        # Reverse map: inner -> outer
        reverse_map = {v: k for k, v in self.input_mapping.items()}
        return tuple(reverse_map.get(i, i) for i in inner_inputs)

    @property
    def output_name(self) -> Tuple[str, ...]:
        """Outer output names this node produces."""
        inner_outputs = list(self._subgraph.source_to_node.keys())
        return tuple(self.output_mapping.get(o, o) for o in inner_outputs)

    def __call__(self, **inputs) -> Dict[str, Any]:
        """Execute inner graph."""
        # Apply input mapping
        mapped = {
            self.input_mapping.get(k, k): v
            for k, v in inputs.items()
        }

        # Handle map_over
        if self.map_over:
            results = self.graph.map(
                inputs=mapped,
                map_over=self._translate_map_over(),
                map_mode=self.map_mode
            )
            return self._collect_mapped_results(results)
        else:
            result = self.graph.run(inputs=mapped)
            return self._apply_output_mapping(result)
```

### Recursive Cycle Detection

```python
@cached_property
def has_cycles(self) -> bool:
    """Check for cycles at this level or any nested level."""
    # Check this graph
    if not nx.is_directed_acyclic_graph(self._graph):
        return True

    # Check nested graphs recursively
    for node in self._nodes.values():
        if hasattr(node, '_subgraph') and node._subgraph.has_cycles:
            return True

    return False
```

### Execution

Nested graphs execute as a unit - the parent treats them as opaque nodes:

```python
def execute_node(node, inputs, cache, callbacks, signatures):
    if isinstance(node, GraphNode):
        # Nested graph executes internally
        result = node(**inputs)
        # Compute aggregate signature for caching
        signature = compute_graph_node_signature(node, inputs, signatures)
        return result, signature
    else:
        # Regular node
        ...
```

---

## Caching

### Signature Computation

Cache keys are deterministic signatures:

```
signature = SHA256(code_hash : inputs_hash : deps_hash)
```

- **code_hash**: SHA256 of function source
- **inputs_hash**: SHA256 of input values
- **deps_hash**: Signatures of upstream nodes (propagates changes)

### Implementation

```python
def compute_node_signature(
    node: HyperNode,
    inputs: Dict[str, Any],
    node_signatures: Dict[str, str]
) -> str:
    """Compute cache signature for a node."""
    code_hash = node.code_hash
    inputs_hash = hash_inputs(inputs)

    # Collect upstream signatures
    deps_signatures = [
        node_signatures[p]
        for p in node.root_args
        if p in node_signatures
    ]
    deps_hash = ":".join(sorted(deps_signatures))

    combined = f"{code_hash}:{inputs_hash}:{deps_hash}"
    return hashlib.sha256(combined.encode()).hexdigest()
```

### Unchanged from Current Design

The caching system works identically for all graph types:
- Same signature formula
- Sequential accumulation of `node_signatures` during execution
- Works with cycles (each re-execution gets new signature due to changed inputs)

---

## Checkpointing

### Default: Save Everything

```python
class Graph:
    def checkpoint(self, state: GraphState) -> Dict[str, Any]:
        """Checkpoint all values."""
        return {
            name: value.data
            for name, value in state.values.items()
        }

    def resume(
        self,
        checkpoint: Dict[str, Any],
        new_inputs: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Resume from checkpoint with optional new inputs."""
        merged = {**checkpoint}
        if new_inputs:
            merged.update(new_inputs)
        return self.run(merged)
```

### Future Optimizations

Later we can add:
- `@node(checkpoint=False)` to skip certain values
- Smart detection of what's actually needed for resume
- Compression/serialization options

---

## Validation

All validation happens at graph build time (fail fast).

### Target Existence

```python
def validate_gate_targets(gate: GateNode, graph: ExecutionGraph):
    """Validate all gate targets exist in graph."""
    for target in gate.targets:
        if target != END and target.name not in graph._nodes:
            raise ConfigError(
                f"Gate '{gate.name}' targets '{target.name}' which is not in graph"
            )
```

### Mutual Exclusivity for Shared Outputs

When multiple nodes produce the same output, validate they're mutually exclusive:

```python
def validate_output_sources(graph: ExecutionGraph):
    """Validate outputs with multiple sources are mutually exclusive."""
    outputs = get_outputs_with_multiple_sources(graph)

    for output, sources in outputs.items():
        for node1, node2 in all_pairs(sources):
            if not are_mutually_exclusive(node1, node2, graph):
                raise ConflictError(
                    f"Output '{output}' produced by both '{node1}' and '{node2}' "
                    f"which aren't in mutually exclusive branches."
                )
```

### Gate Requirements (Transitive)

```python
def get_gate_requirements(node_name: str, graph: ExecutionGraph) -> Set[str]:
    """Get all gates this node requires (transitively)."""
    requirements = set()

    # Direct: control edges pointing to this node
    for pred, _, data in graph._graph.in_edges(node_name, data=True):
        if data.get('edge_type') == 'control':
            requirements.add(f"{pred}:{data.get('decision')}")

    # Transitive: inherit from data dependencies
    for pred, _, data in graph._graph.in_edges(node_name, data=True):
        if data.get('edge_type') == 'data':
            requirements.update(get_gate_requirements(pred, graph))

    return requirements

def are_mutually_exclusive(node1: str, node2: str, graph: ExecutionGraph) -> bool:
    """Check if nodes can never be ready simultaneously."""
    reqs1 = get_gate_requirements(node1, graph)
    reqs2 = get_gate_requirements(node2, graph)

    # Look for opposite decisions from same gate
    for req in reqs1:
        gate, decision = req.rsplit(':', 1)
        # Check if node2 requires different decision from same gate
        for req2 in reqs2:
            gate2, decision2 = req2.rsplit(':', 1)
            if gate == gate2 and decision != decision2:
                return True

    return False
```

### Parallel Gate Activation Conflicts

```python
def validate_parallel_activation(gate: GateNode, graph: ExecutionGraph):
    """Validate targets that can activate together don't share outputs."""
    for t1, t2 in all_pairs(gate.targets):
        if t1 == END or t2 == END:
            continue

        outputs1 = get_node_outputs(graph.get_node(t1.name))
        outputs2 = get_node_outputs(graph.get_node(t2.name))
        shared = set(outputs1) & set(outputs2)

        if shared:
            raise ConfigError(
                f"Gate '{gate.name}' can activate both '{t1.name}' and '{t2.name}' "
                f"but they share outputs: {shared}"
            )
```

---

## API Reference

### Graph

```python
class Graph:
    def __init__(
        self,
        nodes: List[HyperNode],
        cache: Optional[Cache] = None,
        callbacks: Optional[List[Callback]] = None,
        name: Optional[str] = None
    ):
        """Create a graph from nodes."""

    def run(
        self,
        inputs: Dict[str, Any],
        output_names: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Execute graph with given inputs."""

    def map(
        self,
        inputs: Dict[str, Any],
        map_over: Union[str, List[str]],
        map_mode: str = "zip",  # or "product"
        output_names: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """Execute graph for each item in map_over parameters."""

    def as_node(
        self,
        input_mapping: Optional[Dict[str, str]] = None,
        output_mapping: Optional[Dict[str, str]] = None,
        map_over: Optional[Union[str, List[str]]] = None,
        map_mode: str = "zip"
    ) -> GraphNode:
        """Wrap this graph as a node for nesting."""

    def checkpoint(self, state: GraphState) -> Dict[str, Any]:
        """Create checkpoint of current state."""

    def resume(
        self,
        checkpoint: Dict[str, Any],
        new_inputs: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Resume execution from checkpoint."""

    @property
    def root_inputs(self) -> List[str]:
        """External inputs required."""

    @property
    def has_cycles(self) -> bool:
        """Whether graph contains cycles."""
```

### Decorators

```python
from typing import Literal

@node(output_name="result", cache=True)
def my_node(x: int) -> int:
    """Regular computation node."""
    return x * 2

@branch(when_true="handle_valid", when_false="handle_error")
def check(data: dict) -> bool:
    """Boolean branch - two paths (string targets validated at init)."""
    return data.get("valid")

MyDecision = Literal["continue", "fallback", END]

@gate
def decide(state: dict) -> MyDecision:
    """Multi-way gate - N paths, can activate multiple."""
    return "continue"
```

### Special Values

```python
from typing import Literal
from hypernodes import END

# String targets with optional descriptions
Decision = Literal[
    "process",                           # Simple target
    ("review", "Send to human review"),  # With description
    END                                   # Terminates graph execution
]
```

---

## Examples

> **Note**: See [Design Philosophy](graph_design_philosophy.md) for the motivation and principles behind these patterns.

### Example 1: Static DAG (No Loops)

```python
from hypernodes import node, Graph

@node(output_name="result_a")
def process_a(input_a: int) -> int:
    return input_a * 2

@node(output_name="result_b")
def process_b(input_b: int) -> int:
    return input_b * 3

@node(output_name="combined")
def combine(result_a: int, result_b: int) -> int:
    return result_a + result_b

graph = Graph(nodes=[process_a, process_b, combine])
result = graph.run(inputs={"input_a": 5, "input_b": 10})
# result["combined"] = 40  (10 + 30)
```

**Execution trace:**
```
Iteration 1: process_a READY, process_b READY → PARALLEL
Iteration 2: combine READY → run
Done
```

### Example 2: Diamond with Parallel Paths

```python
@node(output_name="a_out")
def node_a(x: int) -> int:
    return x + 1

@node(output_name="b_out")
def node_b(a_out: int) -> int:
    return a_out * 2

@node(output_name="c_out")
def node_c(a_out: int) -> int:
    return a_out * 3

@node(output_name="result")
def node_d(b_out: int, c_out: int) -> int:
    return b_out + c_out

#        ┌──→ B ──┐
#   A ──┤         ├──→ D
#        └──→ C ──┘

graph = Graph(nodes=[node_a, node_b, node_c, node_d])
result = graph.run(inputs={"x": 10})
# x=10 → A:11 → B:22, C:33 (PARALLEL) → D:55
```

### Example 3: RAG Agent with Loop (using @gate)

```python
from typing import Literal
from hypernodes import node, gate, Graph, END

@node(output_name="enriched_q")
def enrich(question: str) -> str:
    return f"Detailed: {question}"

@node(output_name="docs")
def retrieve(enriched_q: str, retriever) -> list[str]:
    return retriever.search(enriched_q)

@node(output_name="response")
def respond(messages: list, docs: list, model) -> str:
    context = "\n".join(docs)
    return model.invoke(messages, context=context)

@node(output_name="messages")
def add_response(messages: list, response: str) -> list:
    return messages + [{"role": "assistant", "content": response}]

RouteDecision = Literal["retrieve", END]

@gate
def route(response: str) -> RouteDecision:
    if "[MORE]" in response:
        return "retrieve"
    return END

rag_agent = Graph(nodes=[enrich, retrieve, respond, add_response, route])

result = rag_agent.run(inputs={
    "question": "What is RAG?",
    "messages": [{"role": "user", "content": "What is RAG?"}],
    "retriever": my_retriever,
    "model": my_llm,
})
```

**Execution trace:**
```
Iteration 1: enrich READY → run
Iteration 2: retrieve READY → run
Iteration 3: respond READY → run
Iteration 4: add_response READY, route READY → PARALLEL
             route decides MORE → gate opens for retrieve
Iteration 5: retrieve READY (gate) → run
Iteration 6: respond READY (docs changed) → run
Iteration 7: add_response READY, route READY → PARALLEL
             route decides DONE → END
```

### Example 4: Early Termination with Interrupt

```python
from hypernodes import node, Graph, Interrupt

@node(output_name="validated")
def validate(input_data: dict) -> dict:
    if not input_data.get("valid"):
        raise Interrupt("Invalid input", data=input_data)
    return input_data

@node(output_name="processed")
def process(validated: dict) -> str:
    return f"Processed: {validated['value']}"

graph = Graph(nodes=[validate, process])

result = graph.run(inputs={"input_data": {"valid": False, "value": "test"}})
# result.interrupted == True
# result.interrupt_reason == "Invalid input"
```

### Example 5: Resume from Checkpoint

```python
# First run - stops mid-execution
result1 = rag_agent.run(
    inputs={...},
    checkpoint_every="gate"  # Save state at gate decisions
)
# User closes app

# Later - resume from checkpoint
result2 = rag_agent.resume(
    checkpoint=load_checkpoint("conv_123"),
    new_inputs={"messages": [..., new_user_message]}
)
# Continues from where it left off
```

### Example 6: Mutually Exclusive Branches with @branch

```python
from hypernodes import node, branch, Graph

@node(output_name="result")
def process_valid(data: dict) -> str:
    return f"Success: {data['value']}"

@node(output_name="result")  # Same output - OK, mutually exclusive
def handle_error(data: dict) -> str:
    return f"Error: {data['error']}"

@branch(when_true="process_valid", when_false="handle_error")
def is_valid(data: dict) -> bool:
    return not data.get("error")

graph = Graph(nodes=[is_valid, process_valid, handle_error])
result = graph.run(inputs={"data": {"value": "test"}})
# result["result"] = "Success: test"
```

### Example 7: Multi-way Routing with @gate

```python
from typing import Literal
from hypernodes import node, gate, Graph, END

# Simple string targets
AgentAction = Literal["research", "retrieve", "respond", END]

@gate
def agent_decide(state: dict) -> AgentAction:
    if state.get("needs_research"):
        return "research"
    if state.get("needs_docs"):
        return "retrieve"
    if state.get("ready"):
        return "respond"
    return END
```

**With descriptions** (for visualization and documentation):

```python
from typing import Literal
from hypernodes import node, gate, Graph, END

# Tuple format for descriptions: (target, description)
AgentAction = Literal[
    ("research", "Gather more information"),
    "retrieve",                              # No description needed
    ("respond", "Generate final answer"),
    END,
]

@gate
def agent_decide(state: dict) -> AgentAction:
    if state.get("needs_research"):
        return "research"  # Return just the target string
    if state.get("needs_docs"):
        return "retrieve"
    if state.get("ready"):
        return "respond"
    return END
```

---

## Migration from Pipeline

### Before (Pipeline)

```python
from hypernodes import Pipeline, node

@node(output_name="result")
def process(x: int) -> int:
    return x * 2

pipeline = Pipeline(nodes=[process])
result = pipeline.run(inputs={"x": 5})
```

### After (Graph)

```python
from hypernodes import Graph, node

@node(output_name="result")
def process(x: int) -> int:
    return x * 2

graph = Graph(nodes=[process])
result = graph.run(inputs={"x": 5})
```

### Key Changes

| Pipeline | Graph |
|----------|-------|
| `Pipeline(nodes=[...])` | `Graph(nodes=[...])` |
| `pipeline.run(...)` | `graph.run(...)` |
| `pipeline.map(...)` | `graph.map(...)` |
| `pipeline.as_node(...)` | `graph.as_node(...)` |
| `PipelineNode` | `GraphNode` |
| `@branch` decorator | `@branch` (unchanged) |
| N/A | `@gate` for multi-way routing |

---

## File Structure

```
src/hypernodes/
├── node.py                 # @node decorator, Node class
├── branch.py               # @branch decorator, BranchNode class
├── gate.py                 # @gate decorator, GateNode class
├── graph.py                # Graph class (main user API)
│
├── graph/
│   ├── __init__.py
│   ├── execution_graph.py  # ExecutionGraph (NetworkX wrapper)
│   ├── state.py            # GraphState, VersionedValue
│   ├── ops.py              # Pure functions: is_stale, is_ready, etc.
│   └── validation.py       # Build-time validation
│
├── execution/
│   ├── __init__.py
│   ├── engine.py           # GraphEngine
│   ├── node_execution.py   # execute_node (with caching, callbacks)
│   └── signatures.py       # Signature computation
│
├── graph_node.py           # GraphNode (nested graphs)
├── map_planner.py          # MapPlanner (unchanged)
├── cache.py                # Caching (unchanged)
├── callbacks.py            # Callbacks (unchanged)
│
└── telemetry/              # (unchanged)
    ├── progress.py
    └── tracing.py
```

---

## Summary

### What Uses Standard Algorithms

| Operation | Algorithm | NetworkX |
|-----------|-----------|----------|
| Graph construction | Adjacency list | `nx.DiGraph` |
| Cycle detection | DFS | `nx.is_directed_acyclic_graph()` |
| Topological sort | Kahn's | `nx.topological_sort()` |
| Ancestors | BFS/DFS | `nx.ancestors()` |
| Descendants | BFS/DFS | `nx.descendants()` |
| Reachability | BFS | `nx.has_path()` |

### What's Proprietary

| Component | Reason |
|-----------|--------|
| Staleness detection | Version-based with sole-source rule |
| Gate control | Domain-specific routing semantics |
| Signature computation | Framework-specific caching |
| Checkpoint/resume | State serialization |

### Key Design Decisions

1. **Single `Graph` class** - No separate Pipeline vs Graph
2. **One execution algorithm** - Works for DAG, branching, cyclic
3. **NetworkX internally** - Standard algorithms, explicit graph
4. **Hierarchical nesting** - Nested graphs are opaque nodes
5. **Static validation** - Fail fast at build time
6. **Enum-based gates** - Type-safe, discoverable, DRY
7. **Tuple descriptions** - Optional, minimal syntax

---

> **See also**: [Design Philosophy](graph_design_philosophy.md) for the motivation, problem statements, and principles behind these design decisions.
