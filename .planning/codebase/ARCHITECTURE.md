# Architecture

**Analysis Date:** 2026-01-21

## Pattern Overview

**Overall:** Superstep-based execution engine with automatic edge inference and hierarchical graph composition.

**Key Characteristics:**
- Pure function-based nodes with automatic type inference from signatures
- Automatic edge creation from output/input name matching
- Superstep execution model: all "ready" nodes execute in parallel (sync sequential simulation)
- Unified algorithm handles DAGs, branching (via gates), and cyclic graphs (agentic loops)
- Hierarchical composition: graphs nest as nodes, enabling test-first component assembly

## Layers

**Graph Definition Layer:**
- Purpose: Describe computation structure (nodes and connections)
- Location: `src/hypergraph/graph/core.py`, `src/hypergraph/nodes/`
- Contains: HyperNode subclasses (FunctionNode, GraphNode, GateNode), Graph class, InputSpec
- Depends on: Nothing (pure data structures)
- Used by: Runners, validation, visualization

**Node Layer:**
- Purpose: Wrap functions, compose graphs, and define routing logic
- Location: `src/hypergraph/nodes/`
- Contains:
  - `base.py`: HyperNode abstract base with common interface
  - `function.py`: FunctionNode wraps Python functions (sync/async/generator)
  - `graph_node.py`: GraphNode wraps Graph for hierarchical composition
  - `gate.py`: RouteNode and IfElseNode for control flow
  - `_rename.py`: API for renaming inputs/outputs
- Depends on: Utilities only
- Used by: Graph construction, runners

**Graph Layer:**
- Purpose: Assemble nodes into executable graphs with automatic edge inference
- Location: `src/hypergraph/graph/`
- Contains:
  - `core.py`: Graph class - builds NetworkX DiGraph, validates structure
  - `input_spec.py`: InputSpec dataclass - categorizes parameters (required/optional/seeds)
  - `validation.py`: Build-time validation - checks duplicate names, routing targets, type compatibility
- Depends on: Nodes, NetworkX
- Used by: Runners

**Execution Layer:**
- Purpose: Run graphs with different strategies (sync vs async, with/without concurrency)
- Location: `src/hypergraph/runners/`
- Contains:
  - `base.py`: BaseRunner abstract interface
  - `sync/runner.py`: SyncRunner - sequential execution
  - `async_/runner.py`: AsyncRunner - concurrent execution
  - `_shared/`: Common helpers for all runners
  - `_shared/types.py`: GraphState (tracking values/versions), RunResult
  - `_shared/helpers.py`: get_ready_nodes, initialize_state, collect_inputs
  - `sync/executors/`, `async_/executors/`: Type-specific node executors
- Depends on: Graph, Nodes, NetworkX
- Used by: End users, visualization

**Visualization Layer:**
- Purpose: Render graphs to interactive React Flow visualizations
- Location: `src/hypergraph/viz/`
- Contains:
  - `renderer.py`: Transform Graph to React Flow node/edge format with hierarchy trees
  - `html_generator.py`: Build HTML/JavaScript with layout and centering logic
  - `layout_estimator.py`: Calculate node dimensions for layout
  - `assets/`: JavaScript layout engine, constraint solver, edge routing
- Depends on: Graph, Nodes
- Used by: Jupyter widgets, web displays

## Data Flow

**Graph Construction:**

1. User writes functions with `@node` decorator specifying output names
2. User builds Graph from list of nodes
3. Graph._build_graph() creates output→source mapping by scanning node outputs
4. Graph._add_data_edges() matches inputs to outputs, creating edges automatically
5. Graph.validate() checks for duplicates, routing target validity, type compatibility

**Graph Execution (SyncRunner):**

1. `runner.run(graph, values)` validates inputs against graph.inputs
2. `initialize_state()` creates GraphState with input values
3. Loop until no ready nodes:
   - `get_ready_nodes()` finds nodes whose inputs are all satisfied and not stale
   - `run_superstep_sync()` executes ready nodes sequentially:
     - For each node: `collect_inputs_for_node()` gathers inputs from state
     - `execute_node()` calls type-specific executor (FunctionNodeExecutor, RouteNodeExecutor, etc.)
     - Update state with outputs, record input versions for staleness tracking
4. Return RunResult with leaf outputs (from terminal nodes)

**State Management:**

- `GraphState.values`: Current value for each named output
- `GraphState.versions`: Version counter for each value (increments when value changes)
- `GraphState.node_executions`: History of node executions with input versions (for staleness detection in cyclic graphs)
- `GraphState.routing_decisions`: Decisions made by gate nodes (RouteNode, IfElseNode)

**Readiness Determination:**

A node is ready to execute when:
1. All inputs have values (from edges, graph.bound, or defaults via has_default_for())
2. Inputs are not stale (input versions match last execution versions, OR node hasn't executed yet)
3. Node is activated by gates (if controlled by GateNode, gate decision includes this node)

**Gate Routing:**

- RouteNode returns target node name(s) to activate
- IfElseNode returns target based on boolean decision
- State tracks routing decision, used to determine which nodes are activated
- END sentinel stops execution (special target meaning "halt here")

**Cyclic Graph Execution:**

- Cycle edges loop back to earlier nodes
- Staleness detection prevents infinite loops: if no inputs changed since last execution, node won't re-execute
- Default max_iterations=1000 prevents runaway loops

## Key Abstractions

**HyperNode:**
- Purpose: Common interface for all executable entities
- Examples: `src/hypergraph/nodes/base.py`, `src/hypergraph/nodes/function.py`, `src/hypergraph/nodes/gate.py`, `src/hypergraph/nodes/graph_node.py`
- Pattern: Abstract base class with properties (name, inputs, outputs) and optional capabilities (is_async, is_generator, cache, has_default_for)

**Graph:**
- Purpose: Pure structure definition - what nodes exist and how they connect
- Examples: `src/hypergraph/graph/core.py`
- Pattern: Immutable after construction; provides nodes dict, NetworkX DiGraph, input spec computation

**InputSpec:**
- Purpose: Describe what parameters a graph needs at runtime
- Examples: `src/hypergraph/graph/input_spec.py`
- Pattern: Categorizes parameters as required (must provide), optional (has default/bound), or seeds (for cycles)

**NodeExecutor Protocol:**
- Purpose: Standardize how different node types are executed
- Examples: `src/hypergraph/runners/_shared/protocols.py`, `src/hypergraph/runners/sync/executors/`
- Pattern: Callable interface: `(node, state, inputs) -> outputs_dict`

**GraphState:**
- Purpose: Runtime state tracking values, versions, routing decisions
- Examples: `src/hypergraph/runners/_shared/types.py`
- Pattern: Mutable container updated during execution; enables staleness detection for cycles

## Entry Points

**Graph Construction:**
- Location: User code (e.g., `Graph([node1, node2])`)
- Triggers: When orchestrating a workflow
- Responsibilities: Define nodes and assemble them; validation happens here

**Execution:**
- Location: `runner.run(graph, values)`
- Triggers: When executing a constructed graph
- Responsibilities: Validate inputs, initialize state, run supersteps until completion

**Visualization:**
- Location: `Graph.to_react_flow_json()` or via `hypergraph.viz` module
- Triggers: When rendering in Jupyter or web interface
- Responsibilities: Transform graph to React Flow format with hierarchy trees for dynamic routing

## Error Handling

**Strategy:** Build-time validation with fast-fail, runtime errors capture in RunResult.

**Patterns:**

- **Graph validation** (at construction): GraphConfigError for duplicate names, unknown routing targets, type mismatches
- **Input validation** (before execution): MissingInputError if required inputs not provided
- **Runner compatibility** (before execution): IncompatibleRunnerError if runner can't handle graph features (e.g., SyncRunner with async nodes)
- **Execution failure** (during run): RunResult.error contains exception, status=FAILED
- **Infinite loops** (during cyclic execution): InfiniteLoopError if max_iterations exceeded

## Cross-Cutting Concerns

**Logging:**
- Approach: print statements and Python logging module
- When used: Debug tracing in runner execution, edge routing validation

**Validation:**
- Approach: Dedicated validation module (`src/hypergraph/graph/validation.py`, `src/hypergraph/runners/_shared/validation.py`)
- When: At graph construction time (fast-fail) and before execution

**Authentication:**
- Approach: Not applicable - hypergraph has no built-in auth; user functions may require credentials

**Type Safety:**
- Approach: Optional strict_types flag at Graph construction
- When: Set strict_types=True to validate type hints match between connected nodes at build time
- Where: `src/hypergraph/graph/core.py` calls _validate_types() if strict_types=True

---

*Architecture analysis: 2026-01-21*
