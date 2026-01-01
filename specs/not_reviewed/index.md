# hypergraph Project Documentation Index

**Generated:** 2025-12-22  
**Project:** hypergraph v0.4.8  
**Type:** Python Library (Monolith)  
**Scan Level:** Quick Scan (pattern-based)

---

## 👋 Welcome

This is the **primary AI retrieval source** for the hypergraph project. All documentation generated during the brownfield project scan is indexed here for easy reference during AI-assisted development.

**Purpose:** Use this index when creating PRDs, planning features, or understanding the existing codebase.

---

## Project Overview

- **Type:** Monolith (single cohesive library)
- **Primary Language:** Python 3.10+
- **Architecture:** Modular Library with Pluggable Execution Engines
- **Core Principle:** Cache-first pipeline system for ML/AI workflows

---

## Quick Reference

### Technology Stack

| Component | Technology |
|-----------|------------|
| **Language** | Python 3.10, 3.11, 3.12, 3.13 |
| **Package Manager** | uv |
| **Build System** | Hatchling (PEP 517) |
| **Core Dependencies** | None (zero-dependency core) |
| **Optional Engines** | Daft (distributed), Dask (parallel) |
| **Visualization** | Graphviz + React Flow |
| **Test Framework** | pytest + playwright |

### Architecture Pattern

**Cache-First DAG Execution with Pluggable Engines**

```
User Code (@node decorators)
    ↓
Pipeline (DAG definition)
    ↓
GraphBuilder (dependency resolution)
    ↓
Orchestrator (lifecycle management)
    ↓
Engine (Sequential / Daft / Dask)
    ↓
Cache (DiskCache) + Callbacks (Progress, Tracing)
```

### Entry Point

**Main Package:** `src/hypergraph/__init__.py`

**Public API:**
- `Pipeline` - Main pipeline class
- `node` - Node decorator
- `branch` - Branch decorator
- `SeqEngine` - Sequential execution engine
- `DiskCache` - Filesystem cache

---

## Generated Documentation

All documentation files are located in `_bmad-output/`:

### Core Documentation

1. **[Project Overview](./project-overview.md)**
   - Executive summary, features, quick start
   - Technology stack and architecture highlights
   - Installation and usage examples

2. **[Architecture](./architecture.md)**
   - Complete architectural documentation
   - Component descriptions and responsibilities
   - Data flow, API design, extension points
   - Performance characteristics and scaling

3. **[Source Tree Analysis](./source-tree-analysis.md)**
   - Annotated directory tree with explanations
   - Critical directories and their purposes
   - Entry points and integration points
   - Build and distribution details

4. **[Development Guide](./development-guide.md)**
   - Development workflow and environment setup
   - Running tests and building the package
   - Optional feature groups and dependencies
   - Common development tasks

---

## Existing Documentation

The project has extensive pre-existing documentation:

### User Documentation (`docs/`)

**Getting Started:**
- `docs/introduction.mdx` - Introduction and overview
- `docs/get-started/installation.mdx` - Installation guide
- `docs/get-started/quickstart.mdx` - Quick start tutorial

**Core Concepts:**
- `docs/core/nodes.mdx` - Node decorator and behavior
- `docs/core/pipelines.mdx` - Pipeline construction and execution
- `docs/core/caching.mdx` - Caching system

**Composition:**
- `docs/composition/nested-pipelines.mdx` - Pipeline nesting
- `docs/composition/branch-nodes.mdx` - Conditional execution
- `docs/composition/dual-nodes.mdx` - Singular/batch implementations

**Execution Engines:**
- `docs/engines/overview.mdx` - Engine concepts
- `docs/engines/sequential.mdx` - Sequential engine
- `docs/engines/daft.mdx` - Distributed Daft engine
- `docs/engines/dask.mdx` - Parallel Dask engine

**Observability:**
- `docs/observability/visualization.mdx` - Visualization system
- `docs/observability/progress.mdx` - Progress tracking
- `docs/observability/callbacks.mdx` - Callback system
- `docs/observability/tracing.mdx` - Distributed tracing

**Scaling:**
- `docs/scaling/map-operations.mdx` - Parallel processing
- `docs/scaling/stateful-objects.mdx` - Stateful parameters

**Other:**
- `docs/best-practices.mdx` - Best practices
- `docs/philosophy/overview.mdx` - Design philosophy
- `docs/reproducibility/overview.mdx` - Reproducibility
- `docs/integrations/overview.mdx` - Integration guides

### Design & Implementation Guides (`guides/`)

**Design Documents:**
- `guides/graph_design_philosophy.md` - Design principles
- `guides/graph_implementation_guide.md` - Implementation details
- `guides/graph_execution_api.md` - Execution API design
- `guides/graph_edge_cases.md` - Edge cases and handling

**Specification:**
- `guides/hypergraph Pipeline System Specification.../` - Complete specification with subsections:
  - Core Concepts
  - Caching
  - Backends
  - Nested Pipelines
  - Intelligent Callback System
  - Pipeline Visualization
  - Progress Visualization
  - Tracing & Telemetry
  - Test Cases (organized by phase)

**Feature Guides:**
- `guides/node_chaining.md` - Node chaining
- `guides/node_constructor.md` - Node construction
- `guides/node_renaming.md` - Node renaming
- `guides/nested_graph_results.md` - Nested graph results
- `guides/optional_outputs_design.md` - Optional outputs
- `guides/runner_api_design.md` - Runner API
- `guides/async_execution_design.md` - Async execution

### Project README

- `README.md` - Main project README with examples, features, and installation

### Test Documentation

- `tests/README.md` - Testing documentation
- `src/hypergraph/integrations/dask/README.md` - Dask integration docs

### Changelog

- `CHANGELOG.md` - Version history and release notes

---

## Getting Started

### For AI-Assisted Development

**When planning new features:**

1. **Read architecture overview:**
   - [Project Overview](./project-overview.md) - High-level summary
   - [Architecture](./architecture.md) - Detailed component descriptions

2. **Understand existing code structure:**
   - [Source Tree Analysis](./source-tree-analysis.md) - Directory organization
   - `README.md` - Usage examples and patterns

3. **Review design documents:**
   - `docs/` - User-facing documentation
   - `guides/` - Implementation guides and specifications

4. **Check development workflow:**
   - [Development Guide](./development-guide.md) - Dev setup and testing

### For Implementing Features

**UI-only features:** Not applicable (library package, no UI)

**API-only features:**
- Reference: [Architecture](./architecture.md) - API Design section
- Examples: `examples/` and `notebooks/`

**Core functionality:**
- Reference: [Architecture](./architecture.md) - Core Components section
- Tests: `tests/test_*.py` - Examples of usage patterns

---

## Key Architectural Concepts

### 1. Node System

**Nodes** wrap Python functions with pipeline metadata. Created using the `@node` decorator.

**Key files:**
- `src/hypergraph/node.py` - Node implementation
- `src/hypergraph/decorators.py` - Decorator utilities
- `docs/core/nodes.mdx` - User documentation

### 2. Pipeline DAG

**Pipelines** orchestrate DAGs of nodes with automatic dependency resolution.

**Key files:**
- `src/hypergraph/pipeline.py` - Pipeline class
- `src/hypergraph/graph_builder.py` - DAG construction
- `docs/core/pipelines.mdx` - User documentation

### 3. Caching System

**Content-addressable cache** using deterministic signatures (code + inputs + dependencies).

**Key files:**
- `src/hypergraph/cache.py` - Cache implementation
- `docs/core/caching.mdx` - User documentation
- `guides/hypergraph Pipeline System Specification.../Caching.md` - Design spec

### 4. Execution Engines

**Pluggable execution strategies** - Sequential, Daft (distributed), Dask (parallel).

**Key files:**
- `src/hypergraph/sequential_engine.py` - Default engine
- `src/hypergraph/integrations/daft/engine.py` - Distributed engine
- `src/hypergraph/integrations/dask/engine.py` - Parallel engine
- `docs/engines/` - User documentation

### 5. Composition Patterns

**Hierarchical composition** - Pipelines as nodes, branch nodes for conditionals.

**Key files:**
- `src/hypergraph/branch.py` - Branch nodes
- `src/hypergraph/dual_node.py` - Dual nodes
- `src/hypergraph/pipeline_node.py` - Nested pipelines
- `docs/composition/` - User documentation

### 6. Visualization System

**Dual-mode visualization** - Static (Graphviz) and interactive (React Flow).

**Key files:**
- `src/hypergraph/viz/` - Visualization system
  - `ui_handler.py` - State management
  - `graph_walker.py` - Graph traversal
  - `graphviz/renderer.py` - Static SVG
  - `js/html_generator.py` - Interactive HTML
  - `assets/` - Bundled JS/CSS (offline)
- `docs/observability/visualization.mdx` - User documentation

### 7. Observability

**Progress tracking and tracing** with tqdm, rich, and Logfire.

**Key files:**
- `src/hypergraph/telemetry/progress.py` - Progress bars
- `src/hypergraph/telemetry/tracing.py` - Distributed tracing
- `src/hypergraph/callbacks.py` - Callback protocol
- `docs/observability/` - User documentation

---

## Module Organization

### Core Modules (`src/hypergraph/`)

**DAG & Execution:**
- `pipeline.py` - Pipeline class (DAG definition)
- `node.py` - Node wrapper
- `graph_builder.py` - Dependency resolution
- `sequential_engine.py` - Sequential execution
- `orchestrator.py` - Lifecycle management
- `node_execution.py` - Single node execution

**Composition:**
- `branch.py` - Conditional routing
- `dual_node.py` - Singular/batch implementations
- `pipeline_node.py` - Nested pipeline wrapper

**Caching & Storage:**
- `cache.py` - Content-addressable cache
- `callbacks.py` - Callback protocol

**Utilities:**
- `protocols.py` - Protocol definitions
- `decorators.py` - Decorator utilities
- `exceptions.py` - Custom exceptions
- `hypernode.py` - HyperNode protocol
- `map_planner.py` - Map operation planning
- `batch_adapter.py` - Batch adapters

### Sub-packages

**Integrations (`integrations/`):**
- `daft/` - Distributed DataFrame execution
  - `engine.py` - DaftEngine facade
  - `operations.py` - Modular operations
  - `codegen.py` - Code generation
- `dask/` - Parallel map operations
  - `engine.py` - DaskEngine implementation

**Visualization (`viz/`):**
- `ui_handler.py` - State management
- `graph_walker.py` - Graph traversal
- `structures.py` - Data classes
- `visualization_engine.py` - Renderer registry
- `graphviz/` - Static SVG rendering
- `js/` - Interactive HTML rendering
- `assets/` - Bundled JS/CSS

**Telemetry (`telemetry/`):**
- `progress.py` - Progress bars (tqdm/rich)
- `tracing.py` - Distributed tracing (Logfire)
- `waterfall.py` - Waterfall diagrams
- `environment.py` - Environment detection

---

## Test Organization

### Test Suite (`tests/`)

**94 test files** organized by feature:

**Core Functionality:**
- `test_execution.py` - Basic pipeline execution
- `test_map.py` - Map operations (zip/product)
- `test_caching.py` - Cache behavior and invalidation
- `test_callbacks.py` - Callback system
- `test_construction.py` - Pipeline construction

**Composition:**
- `test_nested_pipelines.py` - Nested pipeline composition
- `test_branch.py` - Branch node execution
- `test_branch_same_output.py` - Same output in exclusive branches
- `test_dual_node.py` - Dual node implementations
- `test_bind.py` - Input binding

**Engines:**
- `test_daft_*.py` - Daft engine tests (10+ files)
  - `test_daft_operations.py`
  - `test_daft_caching.py`
  - `test_daft_codegen.py`
  - `test_daft_optimizations.py`
  - etc.
- `test_benchmarks_engines.py` - Engine benchmarks

**Visualization (`viz/`):**
- 27 test files including:
  - `test_graph_walker.py`
  - `test_ui_handler.py`
  - `test_edge_alignment_playwright.py` - Browser tests
  - `test_branch_visualization.py`
  - `test_collapsed_*.py` - Collapse behavior

**Features:**
- `test_stateful*.py` - Stateful parameter handling
- `test_progress.py` - Progress tracking
- `test_output_mapping.py` - Output mapping
- `test_selective_inputs.py` - Selective input processing

---

## Examples & Notebooks

### Example Scripts (`examples/`)

- `daft_backend_example.py` - Daft distributed engine usage
- `dask_engine_example.py` - Dask parallel engine usage
- `fluent_api_example.py` - API usage patterns
- `modal_backend_test.py` - Modal serverless integration

### Jupyter Notebooks (`notebooks/`)

- `guide.ipynb` - Main guide
- `engines.ipynb` - Engine comparison
- `visualization_showcase.ipynb` - Visualization features
- `telemetry_examples.ipynb` - Observability demos
- `daft_benchmarks.ipynb` - Performance benchmarks
- And 20+ other notebooks for various features

---

## Common Development Patterns

### Creating a Basic Pipeline

```python
from hypergraph import Pipeline, node, SeqEngine, DiskCache

@node(output_name="result")
def process(x: int) -> int:
    return x * 2

engine = SeqEngine(cache=DiskCache(path=".cache"))
pipeline = Pipeline(nodes=[process], engine=engine)
result = pipeline.run(inputs={"x": 5})
```

### Scaling with Map

```python
results = pipeline.map(
    inputs={"x": [1, 2, 3]},
    map_over="x"
)
# [{"result": 2}, {"result": 4}, {"result": 6}]
```

### Nesting Pipelines

```python
inner = Pipeline(nodes=[clean])
outer = Pipeline(nodes=[inner.as_node(), analyze])
```

### Adding Conditional Logic

```python
from hypergraph import branch

@branch(when_true=process_valid, when_false=handle_error)
def is_valid(data: dict) -> bool:
    return data["score"] > 0.5
```

### Binding Default Inputs

```python
pipeline = Pipeline(nodes=[scale]).bind(factor=10)
result = pipeline.run(inputs={"value": 5})  # Uses factor=10
```

---

## Troubleshooting & Common Issues

### Quick Scan Limitations

This documentation was generated using a **Quick Scan** (pattern-based analysis without reading source files).

**For deeper analysis:**
- Run Deep Scan: Reads critical files
- Run Exhaustive Scan: Reads all source files
- Review existing docs in `docs/` and `guides/`

### Finding Specific Information

**API details:** Check `docs/core/` and `docs/composition/`  
**Engine specifics:** Check `docs/engines/` and `src/hypergraph/integrations/`  
**Visualization:** Check `docs/observability/visualization.mdx` and `src/hypergraph/viz/`  
**Design decisions:** Check `guides/graph_design_philosophy.md`

---

## Next Steps for AI-Assisted Development

### Creating a PRD for New Features

1. **Read this index** to understand project structure
2. **Review architecture** to understand extension points
3. **Check existing docs** for related functionality
4. **Reference tests** for usage patterns
5. **Create PRD** with context from this documentation

### Planning Refactoring

1. **Read architecture document** to understand component responsibilities
2. **Review source tree** to see current organization
3. **Check test coverage** for affected areas
4. **Plan changes** with awareness of existing patterns

### Understanding Code Behavior

1. **Start with architecture** for high-level understanding
2. **Check user docs** (`docs/`) for conceptual explanations
3. **Review design guides** (`guides/`) for implementation details
4. **Examine tests** for concrete examples

---

## Project Status

**Version:** 0.4.8  
**Status:** Alpha (Development Status :: 3 - Alpha)  
**License:** MIT  
**Homepage:** https://github.com/gilad-rubin/hypergraph

**Metrics:**
- **Source Files:** 64 Python modules
- **Test Files:** 94 test files
- **Documentation:** 40+ documentation files
- **Examples:** 5 example scripts + 20+ notebooks

---

## Scan Report

**Scan Type:** Initial Scan  
**Scan Level:** Quick (pattern-based, no source file reading)  
**Generated:** 2025-12-22  
**State File:** `_bmad-output/project-scan-report.json`

**Documentation Files Generated:**
1. `project-overview.md` - Executive summary and quick start
2. `architecture.md` - Complete architectural documentation
3. `source-tree-analysis.md` - Annotated directory tree
4. `development-guide.md` - Development workflow
5. `index.md` (this file) - Master index

**Existing Documentation:** 40+ files (docs/, guides/, README.md, CHANGELOG.md)

---

**Last Updated:** 2025-12-22  
**For AI:** Use this index as the primary entry point for understanding the hypergraph codebase. All paths are relative to the project root.

