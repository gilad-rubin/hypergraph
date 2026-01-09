# hypergraph

A graph-based workflow framework for building composable, maintainable execution pipelines.

Hypergraph treats your node outputs as the graph's state. No separate state schema needed. This design eliminates reducers, reducer conflicts, and explicit state management, while maintaining full durability through checkpointing.

## Installation

Not yet published. Currently in design phase with core node types implemented.

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

## Philosophy: Outputs ARE State

In hypergraph, there is no separate "state" to define. Your node outputs **are** the state of the graph.

```python
@node(output_name="response")
def get_response(messages: list, user_input: str) -> str:
    """Get LLM response for the conversation."""
    return llm.chat(messages + [{"role": "user", "content": user_input}])

@node(output_name="messages")
def update_messages(messages: list, user_input: str, response: str) -> list:
    """Append user message and response to conversation history."""
    return messages + [
        {"role": "user", "content": user_input},
        {"role": "assistant", "content": response},
    ]
```

The outputs (`response` and `messages`) flow between nodes and form the graph's state. No explicit state schema. No reducers. Just pure functions connected via their inputs and outputs.

**See [Philosophy](docs/philosophy.md)** for deeper explanation and comparison with other frameworks like LangGraph.

## What's Implemented

Currently implemented:
- **HyperNode** - Base class for all node types with rename functionality
- **FunctionNode** - Wraps Python functions (sync, async, sync generator, async generator)
- **@node** - Decorator for creating FunctionNode instances
- **Rename capabilities** - Transform inputs and outputs with `with_inputs()`, `with_outputs()`, `with_name()`

Coming soon:
- Graph composition and wiring
- GateNode, RouteNode, BranchNode (routing gates)
- TypeRouteNode (type-based routing)
- InterruptNode (human-in-the-loop)
- Runners (SyncRunner, AsyncRunner)
- Checkpointing and durability
- Observability and events

## Documentation

- [Getting Started](docs/getting-started.md) - Core concepts and creating your first node
- [API Reference: Nodes](docs/api/nodes.md) - Complete FunctionNode and HyperNode documentation
- [Philosophy](docs/philosophy.md) - Deep dive into "Outputs ARE State" design

## Design Goals

1. **Explicit over implicit** - No magic. Configuration flows through names and types.
2. **Full durability** - All outputs checkpointed when persistence is enabled.
3. **Composable** - Graphs nest as nodes in outer graphs.
4. **Observable** - Event stream for all execution.
5. **Simple default** - No state schema, no reducers, just outputs flowing between nodes.

## Project Status

This is **design phase** software. The node types are implemented and thoroughly tested. The specification documents are reviewed and final. The execution layer (runners, checkpointing, events) is in active development.

For the complete specification, see `specs/reviewed/` in the repository.
