# Architecture

**Analysis Date:** 2026-01-16

## Pattern Overview

**Overall:** Graph-Based Workflow Framework (Separation of Structure and Execution)

**Key Characteristics:**
- Pure graph structure definition separate from execution logic
- "Outputs ARE state" - no explicit state schema required
- Immutable node transformations via `with_*` methods
- Edge inference from parameter name matching
- Hierarchical composition via `GraphNode`

## Layers

**Graph Definition Layer:**
- Purpose: Define computation structure without execution logic
- Location: `src/hypergraph/graph.py`
- Contains: `Graph`, `InputSpec`, `GraphConfigError`
- Depends on: `nodes/` module for node types, `networkx` for graph representation
- Used by: Runners (execution layer - not yet implemented in src/)

**Node Types Layer:**
- Purpose: Define building blocks for graphs (nodes are the "verbs")
- Location: `src/hypergraph/nodes/`
- Contains: `HyperNode` (ABC), `FunctionNode`, `GraphNode`, rename utilities
- Depends on: Standard library (`inspect`, `hashlib`, `copy`)
- Used by: `Graph` class, user code via decorators

**Utilities Layer:**
- Purpose: Shared helper functions
- Location: `src/hypergraph/_utils.py`
- Contains: `ensure_tuple()`, `hash_definition()`
- Depends on: Standard library only
- Used by: Node types

## Data Flow

**Graph Construction Flow:**

1. User defines functions with `@node` decorator
2. Decorator wraps function in `FunctionNode` with metadata
3. User passes nodes to `Graph([node1, node2, ...])`
4. Graph builds NetworkX DiGraph with edge inference
5. Graph validates structure (duplicate names, outputs, identifiers)
6. Graph computes `InputSpec` (required/optional/seeds)

**Edge Inference:**

```
@node(output_name="embedding")    @node(output_name="docs")
def embed(query): ...      -->    def retrieve(embedding): ...
        |                                  |
        +---- "embedding" matches ---------+
              parameter name
```

Edges are created automatically when a node's output name matches another node's input parameter name.

**Value Resolution Hierarchy (at runtime):**
```
1. Edge value        <- Output from upstream node (if executed)
2. Input value       <- From runner.run(values={...})
3. Bound value       <- From graph.bind(...)
4. Function default  <- From function signature
```

## Key Abstractions

**HyperNode (Abstract Base):**
- Purpose: Minimal interface for all node types
- Examples: `src/hypergraph/nodes/base.py`
- Pattern: Template Method for `with_*` rename operations
- Attributes: `name`, `inputs`, `outputs`, `_rename_history`

**FunctionNode:**
- Purpose: Wrap Python functions as graph nodes
- Examples: `src/hypergraph/nodes/function.py`
- Pattern: Adapter (wraps callable with graph-compatible interface)
- Properties: `func`, `is_async`, `is_generator`, `definition_hash`, `defaults`

**GraphNode:**
- Purpose: Enable graph composition (graph as node)
- Examples: `src/hypergraph/nodes/graph_node.py`
- Pattern: Composite (nested graphs treated uniformly)
- Properties: `graph` (inner graph reference), delegates `definition_hash`

**InputSpec:**
- Purpose: Structured specification of graph inputs
- Examples: `src/hypergraph/graph.py`
- Pattern: Value Object (frozen dataclass)
- Categories: `required`, `optional`, `seeds`, `bound`

**Graph:**
- Purpose: Pure structure definition of computation
- Examples: `src/hypergraph/graph.py`
- Pattern: Builder (incremental construction), Facade (hides NetworkX)
- Key methods: `bind()`, `unbind()`, `as_node()`

## Entry Points

**Package Entry Point:**
- Location: `src/hypergraph/__init__.py`
- Triggers: `import hypergraph` or `from hypergraph import ...`
- Responsibilities: Export public API (`Graph`, `node`, `FunctionNode`, `GraphNode`, `HyperNode`, `InputSpec`, `RenameError`, `GraphConfigError`)

**User Entry Point (Typical Usage):**
- Location: User code
- Pattern:
  ```python
  from hypergraph import node, Graph

  @node(output_name="result")
  def my_func(x): return x * 2

  graph = Graph([my_func])
  ```

**Future Entry Points (Designed but not implemented):**
- Runners: `SyncRunner.run()`, `AsyncRunner.run()`, `DaftRunner.map()`
- See `specs/reviewed/runners.md` for API design

## Error Handling

**Strategy:** Fail-fast validation at construction time

**Patterns:**
- `GraphConfigError`: Raised during `Graph.__init__()` for structural issues
  - Duplicate node names
  - Duplicate output names
  - Invalid identifiers (contain `.` or `/`)
  - Inconsistent defaults across shared parameters
- `RenameError`: Raised during `with_*()` calls for invalid renames
  - Includes rename history for helpful error messages
- `ValueError`: Standard Python for invalid arguments (e.g., `bind()` unknown key)

**Validation Order in Graph.__init__():**
1. `_build_nodes_dict()` - Check for duplicate node names
2. `_create_output_source_mapping()` - Check for duplicate outputs
3. `_validate_graph_names()` - Graph name must not contain reserved chars
4. `_validate_valid_identifiers()` - Node/output names must be Python identifiers
5. `_validate_no_namespace_collision()` - Reserved for GraphNode conflicts
6. `_validate_consistent_defaults()` - Shared params must have consistent defaults

## Cross-Cutting Concerns

**Immutability:**
- All `with_*` methods return new instances
- `Graph.bind()` returns new Graph
- Node attributes are set once in `__init__`
- `_rename_history` is copied on clone to prevent shared mutation

**Hashing:**
- `FunctionNode.definition_hash`: SHA256 of function source code
- `Graph.definition_hash`: Merkle-tree hash of all nodes + edges
- `GraphNode.definition_hash`: Delegates to inner graph
- Used for: Cache invalidation, version detection on workflow resume

**Type Hints:**
- Full type hints throughout codebase
- `TYPE_CHECKING` guards for circular import avoidance
- `TypeVar` for self-referential return types in `with_*` methods

---

*Architecture analysis: 2026-01-16*
