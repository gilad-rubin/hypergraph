# Architecture

**Analysis Date:** 2026-01-16

## Pattern Overview

**Overall:** Layered Architecture with Immutable Domain Model

**Key Characteristics:**
- Pure structure definition (no execution logic in core)
- Immutable nodes and graphs (all mutations return new instances)
- Automatic edge inference from input/output name matching
- Hierarchical composition (graphs nest as nodes)
- Separation of structure (Graph/Node) from execution (Runners - not yet implemented)

## Layers

**Public API Layer:**
- Purpose: Expose user-facing classes and decorators
- Location: `src/hypergraph/__init__.py`
- Contains: Re-exports of Graph, node decorator, FunctionNode, GraphNode, HyperNode, InputSpec, errors
- Depends on: Graph module, Nodes subpackage
- Used by: End users importing from `hypergraph`

**Graph Layer:**
- Purpose: Define computation graph structure and validation
- Location: `src/hypergraph/graph.py`
- Contains: `Graph` class, `InputSpec` dataclass, `GraphConfigError`
- Depends on: Nodes layer, NetworkX, typing utilities
- Used by: Users constructing graphs, GraphNode for nesting

**Nodes Layer:**
- Purpose: Define all node types that can exist in a graph
- Location: `src/hypergraph/nodes/`
- Contains:
  - `base.py` - `HyperNode` abstract base class
  - `function.py` - `FunctionNode` and `@node` decorator
  - `graph_node.py` - `GraphNode` for nested graphs
  - `_rename.py` - Rename tracking utilities
- Depends on: Utils layer, typing utilities
- Used by: Graph layer, users creating nodes

**Utilities Layer:**
- Purpose: Shared helper functions
- Location: `src/hypergraph/_utils.py`, `src/hypergraph/_typing.py`
- Contains:
  - `_utils.py` - `ensure_tuple()`, `hash_definition()`
  - `_typing.py` - Type compatibility checking for strict mode
- Depends on: Python stdlib only
- Used by: Nodes layer, Graph layer

## Data Flow

**Graph Construction Flow:**

1. User creates `FunctionNode` instances via `@node` decorator or constructor
2. User passes nodes list to `Graph()` constructor
3. Graph builds internal NetworkX DiGraph representation
4. Graph infers edges by matching output names to input parameter names
5. Graph runs validations (duplicates, types if strict, namespace collisions)
6. Graph computes `InputSpec` (required/optional/seed inputs)

**Edge Inference Flow:**

1. Each node declares `outputs` tuple (what it produces)
2. Each node declares `inputs` tuple (what it consumes)
3. Graph creates mapping: `output_name -> source_node_name`
4. For each node's input, if an output exists with that name, create edge
5. Edges carry metadata: `edge_type="data"`, `value_name=<param_name>`

**Hierarchical Composition Flow:**

1. Inner graph defined: `inner = Graph([...], name="inner")`
2. Inner graph wrapped as node: `gn = inner.as_node()` returns `GraphNode`
3. GraphNode exposes inner graph's inputs/outputs as its own
4. GraphNode placed in outer graph: `outer = Graph([..., gn, ...])`
5. Edge inference treats GraphNode like any other node

## Key Abstractions

**HyperNode (Abstract Base):**
- Purpose: Unified interface for all node types
- Examples: `src/hypergraph/nodes/base.py`
- Pattern: Template Method - defines interface, subclasses implement
- Key attributes: `name`, `inputs`, `outputs`, `_rename_history`
- Universal capabilities: `definition_hash`, `is_async`, `is_generator`, `cache`, `has_default_for()`, `get_default_for()`, `get_input_type()`, `get_output_type()`
- Immutable pattern: `with_name()`, `with_inputs()`, `with_outputs()` return new instances

**FunctionNode:**
- Purpose: Wrap Python functions as graph nodes
- Examples: `src/hypergraph/nodes/function.py`
- Pattern: Adapter - adapts any callable to HyperNode interface
- Created via `@node` decorator or `FunctionNode()` constructor
- Extracts inputs from function signature, outputs from decorator argument
- Auto-detects async/generator execution modes

**GraphNode:**
- Purpose: Enable hierarchical graph composition
- Examples: `src/hypergraph/nodes/graph_node.py`
- Pattern: Composite - treats nested graph as single node
- Created via `Graph.as_node()`
- Delegates `definition_hash` and type queries to inner graph

**Graph:**
- Purpose: Container for nodes with automatic edge inference
- Examples: `src/hypergraph/graph.py`
- Pattern: Builder (construction) + Facade (for NetworkX)
- Properties expose computed views: `inputs`, `outputs`, `leaf_outputs`
- `bind()` and `unbind()` return new graphs (immutable)

**InputSpec:**
- Purpose: Categorize graph inputs for execution planning
- Examples: `src/hypergraph/graph.py` (line 17-36)
- Pattern: Value Object (frozen dataclass)
- Categories: `required` (must provide), `optional` (has default/bound), `seeds` (cycle initial values)

## Entry Points

**Package Entry:**
- Location: `src/hypergraph/__init__.py`
- Triggers: `from hypergraph import ...`
- Responsibilities: Re-export public API classes and decorators

**Node Creation:**
- Location: `src/hypergraph/nodes/function.py` (line 351-401)
- Triggers: `@node` decorator on functions
- Responsibilities: Create FunctionNode wrapping the function

**Graph Creation:**
- Location: `src/hypergraph/graph.py` (line 78-100)
- Triggers: `Graph([...])` constructor
- Responsibilities: Build graph structure, infer edges, validate, compute InputSpec

**Composition:**
- Location: `src/hypergraph/graph.py` (line 577-591)
- Triggers: `graph.as_node()`
- Responsibilities: Wrap Graph as GraphNode for nesting

## Error Handling

**Strategy:** Fail-fast validation at construction time

**Patterns:**
- `GraphConfigError` for all graph construction issues
- `RenameError` for invalid rename operations
- `ValueError` for invalid bind operations
- Error messages include context and "How to fix" guidance
- No error handling for execution (runners not implemented yet)

**Validation Points:**
1. Duplicate node names (in `_build_nodes_dict`)
2. Duplicate output names (in `_create_output_source_mapping`)
3. Invalid identifiers for names
4. Namespace collision (GraphNode name vs output name)
5. Inconsistent defaults across nodes sharing inputs
6. Type mismatches (only when `strict_types=True`)

## Cross-Cutting Concerns

**Logging:** Not implemented (no logging in current codebase)

**Validation:** Performed at Graph construction via `_validate()` method chain

**Authentication:** Not applicable (library, not service)

**Type Checking:** Optional via `strict_types=True` parameter, uses `_typing.py` module for compatibility checks

**Hashing:** `definition_hash` property on all nodes and graphs for caching/change detection

---

*Architecture analysis: 2026-01-16*
