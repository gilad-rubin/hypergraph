# Hypergraph

A graph-based workflow framework for building composable, maintainable execution pipelines.

Hypergraph supports **DAGs, cycles, runtime conditional branches, and multi-turn interactions** - all while maintaining pure, portable functions.

## Core Idea: Outputs ARE State

In hypergraph, there is no separate "state" to define. Your node outputs form the graph's state. No state schema. No reducers. No conflicts. Just outputs flowing between pure functions.

## Quick Start

```python
from hypergraph import node

@node(output_name="doubled")
def double(x: int) -> int:
    """Double the input value."""
    return x * 2

# Create and call the node
result = double(5)
print(result)  # Output: 10

# Access node properties
print(double.inputs)   # ('x',)
print(double.outputs)  # ('doubled',)
print(double.name)     # 'double'
```

## What's Implemented

Currently implemented:
- **HyperNode** - Base class for all node types with rename functionality
- **FunctionNode** - Wraps Python functions (sync, async, sync generator, async generator)
- **@node** - Decorator for creating FunctionNode instances
- **Rename capabilities** - Transform inputs and outputs with `with_inputs()`, `with_outputs()`, `with_name()`

Coming soon:
- Graph composition and wiring
- Routing nodes (GateNode, RouteNode, BranchNode)
- Runners (SyncRunner, AsyncRunner)
- Checkpointing and durability
- Observability and events

## Documentation

- [Getting Started](getting-started.md) - Core concepts and creating your first node
- [Philosophy](philosophy.md) - Why hypergraph exists and design principles
- [API Reference: Nodes](api/nodes.md) - Complete FunctionNode and HyperNode documentation

## Design Goals

1. **Outputs ARE state** - No separate state schema needed
2. **Pure functions** - Nodes are testable without the framework
3. **Explicit over implicit** - No magic defaults
4. **Full durability** - All outputs checkpointed when persistence is enabled
5. **Composable** - Graphs nest as nodes in outer graphs
