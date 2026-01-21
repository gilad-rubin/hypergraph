# Codebase Structure

**Analysis Date:** 2026-01-21

## Directory Layout

```
hypergraph/
├── src/hypergraph/                 # Main package
│   ├── __init__.py                 # Public API exports
│   ├── _typing.py                  # Type aliases and utilities
│   ├── _utils.py                   # Internal helpers (ensure_tuple, hash_definition)
│   ├── exceptions.py               # Runtime exceptions
│   ├── graph/                      # Graph definition and validation
│   │   ├── __init__.py
│   │   ├── core.py                 # Graph class, edge inference
│   │   ├── input_spec.py           # InputSpec dataclass and computation
│   │   └── validation.py           # Build-time validation
│   ├── nodes/                      # Node types and decorators
│   │   ├── __init__.py             # Exports @node, @route, @ifelse, END
│   │   ├── base.py                 # HyperNode abstract base class
│   │   ├── function.py             # FunctionNode and @node decorator
│   │   ├── graph_node.py           # GraphNode and Graph.as_node()
│   │   ├── gate.py                 # GateNode, RouteNode, IfElseNode, @route/@ifelse
│   │   └── _rename.py              # Rename API implementation
│   ├── runners/                    # Execution engines
│   │   ├── __init__.py             # Exports SyncRunner, AsyncRunner, BaseRunner
│   │   ├── base.py                 # BaseRunner abstract interface
│   │   ├── sync/                   # Synchronous execution
│   │   │   ├── __init__.py
│   │   │   ├── runner.py           # SyncRunner class
│   │   │   ├── superstep.py        # run_superstep_sync()
│   │   │   └── executors/          # Node type executors
│   │   │       ├── __init__.py
│   │   │       ├── function_node.py
│   │   │       ├── graph_node.py
│   │   │       ├── ifelse_node.py
│   │   │       └── route_node.py
│   │   ├── async_/                 # Asynchronous execution
│   │   │   ├── __init__.py
│   │   │   ├── runner.py           # AsyncRunner class
│   │   │   ├── superstep.py        # run_superstep_async()
│   │   │   └── executors/          # Node type executors (async versions)
│   │   │       ├── __init__.py
│   │   │       ├── function_node.py
│   │   │       ├── graph_node.py
│   │   │       ├── ifelse_node.py
│   │   │       └── route_node.py
│   │   └── _shared/                # Shared runner utilities
│   │       ├── __init__.py
│   │       ├── helpers.py          # get_ready_nodes, collect_inputs, etc.
│   │       ├── protocols.py        # NodeExecutor protocol
│   │       ├── types.py            # GraphState, RunResult, RunStatus
│   │       ├── validation.py       # Input/runner validation
│   │       └── routing_validation.py
│   └── viz/                        # Visualization
│       ├── __init__.py
│       ├── renderer.py             # Graph to React Flow transformation
│       ├── html_generator.py       # HTML/JavaScript output with layout
│       ├── layout_estimator.py     # Node dimension calculation
│       ├── widget.py               # Jupyter widget integration
│       ├── styles/                 # Styling components
│       │   ├── __init__.py
│       │   └── nodes.py            # Node style definitions
│       └── assets/                 # Generated JavaScript
│           ├── __init__.py
│           ├── constraint-layout.js     # Edge routing and layout
│           ├── layout.js                # Recursive layout for nesting
│           └── components.js            # React Flow components
├── tests/                          # Test suite (comprehensive)
│   ├── test_graph.py               # Graph construction and validation
│   ├── test_nodes_*.py             # Node type tests
│   ├── test_runners/               # Runner-specific tests
│   │   ├── test_sync_runner.py
│   │   ├── test_async_runner.py
│   │   ├── test_routing.py
│   │   ├── test_execution.py
│   │   └── ...
│   ├── test_integration.py         # End-to-end scenarios
│   ├── capabilities/               # Pairwise capability matrix tests
│   │   ├── matrix.py
│   │   ├── builders.py
│   │   └── test_matrix.py
│   └── viz/                        # Visualization tests
│       ├── test_renderer.py
│       ├── test_html_generator.py
│       └── test_edge_routing.py
├── docs/                           # User documentation
│   ├── getting-started.md
│   ├── guides/                     # How-to guides (routing, etc.)
│   ├── api/                        # API reference
│   └── comparison.md
├── examples/                       # Example scripts
├── scripts/                        # Utility scripts (build, test, etc.)
├── pyproject.toml                  # Project metadata and dependencies
└── README.md                       # Project overview
```

## Directory Purposes

**src/hypergraph/:**
- Purpose: All production code
- Contains: Package modules organized by concern (graph, nodes, runners, viz)
- Key files: `__init__.py` exports public API

**src/hypergraph/graph/:**
- Purpose: Graph definition and validation
- Contains: Graph class, edge inference, input spec computation, build-time validation
- Key files: `core.py` (Graph class), `input_spec.py` (InputSpec), `validation.py` (validation)

**src/hypergraph/nodes/:**
- Purpose: Node types and their decorators
- Contains: HyperNode base class, FunctionNode, GraphNode, GateNode (RouteNode, IfElseNode), rename API
- Key files: `base.py` (HyperNode), `function.py` (@node decorator), `gate.py` (@route/@ifelse)

**src/hypergraph/runners/:**
- Purpose: Execution engines (sync vs async)
- Contains: BaseRunner interface, SyncRunner, AsyncRunner, shared helpers, node executors
- Key files: `base.py` (interface), `sync/runner.py`, `async_/runner.py`, `_shared/helpers.py`

**src/hypergraph/runners/sync/:**
- Purpose: Synchronous execution (no concurrency, sequential simulation)
- Contains: SyncRunner, superstep logic, type-specific executors
- Key files: `runner.py` (main loop), `superstep.py` (execute ready nodes), `executors/` (node-specific logic)

**src/hypergraph/runners/async_/:**
- Purpose: Asynchronous execution (concurrent supersteps)
- Contains: AsyncRunner, async superstep logic, async node executors
- Key files: `runner.py` (main loop), `superstep.py` (concurrent execution)

**src/hypergraph/runners/_shared/:**
- Purpose: Shared utilities for both sync and async runners
- Contains: Common helpers, protocols, type definitions, validation functions
- Key files: `helpers.py` (get_ready_nodes, input collection), `types.py` (GraphState, RunResult), `protocols.py` (NodeExecutor)

**src/hypergraph/viz/:**
- Purpose: Visualization (HTML/React Flow output)
- Contains: Graph to visualization transformation, layout engine, styling
- Key files: `renderer.py` (Graph to JSON), `html_generator.py` (HTML/JS output), `assets/` (JavaScript)

**tests/:**
- Purpose: Comprehensive test coverage
- Contains: Unit tests, integration tests, capability matrix tests, visualization tests
- Key files: `test_graph.py` (500+ lines), `test_runners/` (runner tests), `capabilities/matrix.py` (pairwise tests)

## Key File Locations

**Entry Points:**
- `src/hypergraph/__init__.py`: Public API (exports Graph, @node, @route, SyncRunner, etc.)
- User creates: `@node` decorated functions, then `Graph([node1, node2, ...])`
- User runs: `runner.run(graph, values)`

**Configuration:**
- `pyproject.toml`: Package metadata, dependencies, build config
- `.python-version`: Python version constraint (3.10+)

**Core Logic:**
- `src/hypergraph/graph/core.py`: Graph construction and edge inference
- `src/hypergraph/runners/sync/runner.py`: Main execution loop
- `src/hypergraph/runners/_shared/helpers.py`: Readiness determination, state management

**Testing:**
- `tests/test_graph.py`: Graph behavior and validation (1200+ lines)
- `tests/test_runners/test_sync_runner.py`: Runner execution tests
- `tests/capabilities/matrix.py`: Pairwise capability combinations
- `tests/viz/test_renderer.py`: Visualization rendering

## Naming Conventions

**Files:**
- Test files: `test_*.py` (pytest discovers these)
- Private modules: `_*.py` prefix (e.g., `_typing.py`, `_utils.py`, `_rename.py`)
- Executors: `{type_name}.py` e.g., `function_node.py`, `graph_node.py`
- JavaScript: `*.js` (constraint-layout.js, layout.js, components.js)

**Directories:**
- Layer directories: lowercase (graph, nodes, runners)
- Runner variants: `sync/`, `async_/` (suffix for async to avoid keyword conflict)
- Executors grouped by runner: `runners/{sync,async_}/executors/`
- Shared code: `_shared/` prefix

**Classes:**
- Nodes: `{Type}Node` suffix (FunctionNode, GraphNode, RouteNode, IfElseNode)
- Runners: `{Strategy}Runner` (SyncRunner, AsyncRunner)
- Errors: `{Cause}Error` (GraphConfigError, MissingInputError, InfiniteLoopError)

**Functions:**
- Decorators: `@node`, `@route`, `@ifelse`
- Private helpers: `_leading_underscore` (e.g., `_resolve_outputs`, `_get_ready_nodes`)
- Public functions: lowercase with underscore (collect_inputs_for_node)

## Where to Add New Code

**New Feature (e.g., add checkpointing):**
- Primary code: `src/hypergraph/runners/_shared/` (shared checkpoint logic)
- Sync integration: `src/hypergraph/runners/sync/runner.py` (hook into run loop)
- Async integration: `src/hypergraph/runners/async_/runner.py` (async checkpoint logic)
- Tests: `tests/test_runners/test_checkpointing.py`

**New Node Type (e.g., LLMNode for LLM calls):**
- Implementation: `src/hypergraph/nodes/llm.py` (extends HyperNode)
- Export: Add to `src/hypergraph/nodes/__init__.py`
- Executors: `src/hypergraph/runners/sync/executors/llm_node.py`, `src/hypergraph/runners/async_/executors/llm_node.py`
- Register executor: Add to `_executors` dict in `SyncRunner.__init__()`, `AsyncRunner.__init__()`
- Tests: `tests/test_nodes_llm.py`

**New Visualization Feature (e.g., custom styling):**
- Styling: `src/hypergraph/viz/styles/nodes.py` (add new style class)
- Rendering: Update `src/hypergraph/viz/renderer.py` if new node data needed
- HTML generation: `src/hypergraph/viz/html_generator.py` (if rendering changes needed)
- JavaScript: `src/hypergraph/viz/assets/layout.js` or `constraint-layout.js` (if layout changes)
- Tests: `tests/viz/test_renderer.py`

**Utilities:**
- Shared helpers: `src/hypergraph/_utils.py`
- Type aliases: `src/hypergraph/_typing.py`
- Runner helpers: `src/hypergraph/runners/_shared/helpers.py`

## Special Directories

**src/hypergraph/runners/sync/executors/:**
- Purpose: Sync execution strategy for each node type
- Generated: No (hand-written)
- Committed: Yes
- Pattern: Each file has one class implementing NodeExecutor protocol

**src/hypergraph/runners/async_/executors/:**
- Purpose: Async execution strategy for each node type
- Generated: No (hand-written)
- Committed: Yes
- Pattern: Same structure as sync, but returns awaitables

**src/hypergraph/viz/assets/:**
- Purpose: Generated JavaScript from Python string templates
- Generated: Yes (at runtime when viz module imports)
- Committed: No (rebuilt on each import)
- Pattern: HTML is generated with embedded JavaScript

**tests/capabilities/:**
- Purpose: Pairwise capability matrix testing
- Generated: No (hand-written)
- Committed: Yes
- Pattern: `matrix.py` defines dimensions, `builders.py` constructs test cases, `test_matrix.py` runs ~21 tests locally, ~8K in CI

---

*Structure analysis: 2026-01-21*
