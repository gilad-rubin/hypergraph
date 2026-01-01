# HyperNodes Source Tree Analysis

**Generated:** 2025-12-22  
**Project:** hypernodes v0.4.8  
**Type:** Python Library (Monolith)

---

## Project Structure Overview

```
hypernodes/
├── src/hypernodes/              # Main package source code (ENTRY POINT)
│   ├── __init__.py              # Package exports (API surface)
│   ├── pipeline.py              # Pipeline class - DAG orchestration
│   ├── node.py                  # @node decorator - function wrapping
│   ├── cache.py                 # Content-addressable caching system
│   ├── engines.py               # Engine protocol definitions
│   ├── sequential_engine.py     # Default sequential execution engine
│   ├── callbacks.py             # Callback protocol and dispatcher
│   ├── graph_builder.py         # DAG construction from nodes
│   ├── orchestrator.py          # Execution lifecycle management
│   ├── node_execution.py        # Single node execution logic
│   ├── branch.py                # @branch decorator - conditional execution
│   ├── dual_node.py             # Dual singular/batch implementations
│   ├── pipeline_node.py         # Nested pipeline wrapper
│   ├── hypernode.py             # HyperNode protocol (structural typing)
│   ├── map_planner.py           # Map operation planning (zip/product)
│   ├── batch_adapter.py         # Batch processing adapters
│   ├── protocols.py             # Protocol definitions
│   ├── decorators.py            # Decorator utilities
│   ├── exceptions.py            # Custom exceptions
│   │
│   ├── integrations/            # Pluggable execution backends
│   │   ├── daft/                # Daft distributed engine
│   │   │   ├── engine.py        # DaftEngine facade
│   │   │   ├── operations.py    # Modular Daft operations
│   │   │   └── codegen.py       # Code generation for Daft scripts
│   │   └── dask/                # Dask parallel engine
│   │       └── engine.py        # DaskEngine implementation
│   │
│   ├── viz/                     # Visualization system
│   │   ├── ui_handler.py        # Backend state manager + serialization
│   │   ├── graph_walker.py      # Graph traversal for visualization
│   │   ├── structures.py        # Data classes (nodes, edges)
│   │   ├── visualization_engine.py  # Pluggable renderer registry
│   │   ├── graphviz/            # Static SVG visualization
│   │   │   ├── renderer.py      # Graphviz rendering engine
│   │   │   └── style.py         # Styling utilities
│   │   ├── js/                  # Interactive HTML/React Flow viz
│   │   │   ├── html_generator.py  # HTML generation with embedded React
│   │   │   └── renderer.py      # React Flow data transformation
│   │   ├── assets/              # Bundled JS/CSS (NO CDN dependencies)
│   │   │   ├── react*.js        # React 18.2.0
│   │   │   ├── reactflow.*      # React Flow 11.10.1
│   │   │   ├── elk.bundled.js   # ELK layout 0.8.2
│   │   │   ├── state_utils.js   # Client-side state transformations
│   │   │   └── tailwind.min.css # Pre-built Tailwind CSS
│   │   ├── state_simulator.py   # Python state simulator (testing)
│   │   ├── debug.py             # Debug utilities
│   │   └── utils.py             # Visualization helpers
│   │
│   ├── telemetry/               # Observability and progress tracking
│   │   ├── progress.py          # ProgressCallback (tqdm + rich)
│   │   ├── tracing.py           # TelemetryCallback (Logfire)
│   │   ├── waterfall.py         # Waterfall diagram generation
│   │   └── environment.py       # Environment detection (CLI/Jupyter)
│   │
│   └── old/                     # Deprecated code (excluded from builds)
│
├── tests/                       # Test suite (pytest + playwright)
│   ├── test_execution.py        # Core execution tests
│   ├── test_map.py              # Map operation tests
│   ├── test_caching.py          # Cache behavior tests
│   ├── test_callbacks.py        # Callback system tests
│   ├── test_nested_pipelines.py # Nested pipeline tests
│   ├── test_branch.py           # Branch node tests
│   ├── test_dual_node.py        # Dual node tests
│   ├── test_bind.py             # Input binding tests
│   ├── test_stateful.py         # Stateful parameter tests
│   ├── test_daft_*.py           # Daft engine tests (10+ files)
│   ├── test_progress.py         # Progress tracking tests
│   ├── viz/                     # Visualization tests (27 files)
│   │   ├── test_graph_walker.py
│   │   ├── test_ui_handler.py
│   │   ├── test_edge_alignment_playwright.py
│   │   └── test_branch_visualization.py
│   └── old/                     # Deprecated tests
│
├── docs/                        # Documentation (MDX format)
│   ├── introduction.mdx         # Project introduction
│   ├── get-started/             # Installation & quickstart
│   ├── core/                    # Core concepts (nodes, pipelines, caching)
│   ├── composition/             # Nested pipelines, branch nodes, dual nodes
│   ├── engines/                 # Sequential, Daft, Dask engines
│   ├── observability/           # Visualization, callbacks, tracing
│   └── scaling/                 # Map operations, stateful objects
│
├── guides/                      # Design and implementation guides
│   ├── graph_design_philosophy.md
│   ├── graph_implementation_guide.md
│   └── HyperNodes Pipeline System Specification.../
│
├── examples/                    # Example scripts
│   ├── daft_backend_example.py
│   ├── dask_engine_example.py
│   └── fluent_api_example.py
│
├── notebooks/                   # Jupyter notebooks for demos
│   ├── guide.ipynb              # Main guide
│   ├── engines.ipynb            # Engine comparison
│   ├── telemetry_examples.ipynb
│   └── visualization_showcase.ipynb
│
├── scripts/                     # Utility scripts (186 Python files)
│
├── build/                       # Build infrastructure
│   └── tailwind/                # Tailwind CSS compilation
│
├── dist/                        # Distribution packages
│   ├── hypernodes-0.4.8-py3-none-any.whl
│   └── hypernodes-0.4.8.tar.gz
│
├── pyproject.toml               # Package configuration (hatchling)
├── uv.lock                      # Locked dependencies
├── README.md                    # Project README
├── CHANGELOG.md                 # Version history
└── LICENSE                      # MIT License
```

---

## Critical Directories Explained

### `/src/hypernodes/` - Core Library
The main package implementing the pipeline system. Zero external dependencies for core functionality.

**Key architectural components:**
- **DAG Construction**: `graph_builder.py` analyzes node dependencies
- **Execution**: `sequential_engine.py`, `orchestrator.py`, `node_execution.py`
- **Composition**: `branch.py`, `dual_node.py`, `pipeline_node.py`
- **Caching**: `cache.py` implements content-addressable storage
- **API Surface**: `__init__.py` exports public API

### `/src/hypernodes/integrations/` - Pluggable Backends
Optional execution engines for distributed/parallel processing.

- **Daft**: Distributed DataFrame execution with auto-batching
- **Dask**: Parallel map operations using Dask Bag

### `/src/hypernodes/viz/` - Visualization System
Comprehensive visualization with both static (Graphviz) and interactive (React Flow) rendering.

**Architecture:**
- `ui_handler.py` - Backend state management
- `graph_walker.py` - Graph traversal
- `graphviz/` - Static SVG generation
- `js/` - Interactive HTML/React generation
- `assets/` - Bundled JS/CSS (fully offline)

### `/src/hypernodes/telemetry/` - Observability
Progress tracking and distributed tracing.

- `progress.py` - tqdm/rich progress bars (auto-detects CLI vs Jupyter)
- `tracing.py` - Logfire integration for distributed tracing
- `waterfall.py` - Execution waterfall diagrams

### `/tests/` - Test Suite
Comprehensive test coverage with pytest and Playwright.

**Test categories:**
- Core: execution, mapping, caching, callbacks
- Composition: nesting, branching, binding
- Engines: Daft/Dask integration tests
- Visualization: 27 files testing rendering, layout, interactions

### `/docs/` - Documentation
MDX-based documentation for rich formatting.

**Structure:**
- Getting started guides
- Core concepts with examples
- Scaling and optimization
- Observability tools

### `/guides/` - Implementation Guides
Internal design documents and specifications.

---

## Entry Points

**Main Package:** `src/hypernodes/__init__.py`

Exports:
- `Pipeline` - Main pipeline class
- `node` - Node decorator
- `branch` - Branch decorator
- `SeqEngine` - Default sequential engine
- `DiskCache` - File-based cache
- `ProgressCallback` - Progress tracking

**CLI Tools:** None (library-only package)

**Test Runner:** `pytest` (via `tests/` directory)

---

## Integration Points

**None** (single cohesive library)

This is a monolithic package with no separate client/server or multi-part architecture.

---

## Build and Distribution

**Build System:** Hatchling (PEP 517)  
**Package Manager:** uv  
**Distribution:** PyPI (`pip install hypernodes`)

**Artifacts:**
- Wheel: `hypernodes-0.4.8-py3-none-any.whl`
- Source: `hypernodes-0.4.8.tar.gz`

**Excluded from builds:**
- `src/hypernodes/old/` - Deprecated code
- Test files and development tools

---

**Note:** This analysis was generated using pattern matching (Quick Scan). For detailed code-level analysis, run a Deep or Exhaustive scan.

