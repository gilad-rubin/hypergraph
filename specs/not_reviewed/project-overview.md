# HyperNodes Project Overview

**Generated:** 2025-12-22  
**Version:** 0.4.8  
**Status:** Alpha

---

## Project Summary

**HyperNodes** is a hierarchical, modular pipeline system for ML/AI workflows. It enables developers to build cache-first data pipelines from decorated Python functions with automatic dependency resolution, intelligent caching, and pluggable execution engines.

**Repository Type:** Monolith (single cohesive codebase)  
**Primary Language:** Python 3.10+  
**License:** MIT  
**Homepage:** https://github.com/gilad-rubin/hypernodes

---

## Quick Reference

### Technology Stack

| Component | Technology |
|-----------|------------|
| **Language** | Python 3.10, 3.11, 3.12, 3.13 |
| **Package Manager** | uv |
| **Build System** | Hatchling (PEP 517) |
| **Core Dependencies** | None (zero-dependency core) |
| **Test Framework** | pytest + playwright |
| **Documentation** | MDX |

### Entry Points

- **Main Package:** `src/hypernodes/__init__.py`
- **Public API:** Pipeline, node, branch, SeqEngine, DiskCache
- **CLI:** None (library-only)

### Architecture Type

**Modular Library with Pluggable Execution Engines**

```
User Code
    ↓
Public API (Pipeline, @node, @branch)
    ↓
Core Execution (GraphBuilder, Orchestrator)
    ↓
Engines (Sequential, Daft, Dask)
    ↓
Storage (DiskCache) + Observability (Callbacks)
```

---

## Repository Structure

**Structure:** Monolith (single cohesive library)

```
hypernodes/
├── src/hypernodes/       # Main package (64 Python files)
│   ├── Core modules      # pipeline, node, cache, engines
│   ├── integrations/     # Daft, Dask execution backends
│   ├── viz/              # Visualization system
│   └── telemetry/        # Progress tracking, tracing
├── tests/                # Test suite (94 test files)
├── docs/                 # Documentation (MDX format)
├── guides/               # Design and implementation guides
├── examples/             # Example scripts
├── notebooks/            # Jupyter notebooks
└── pyproject.toml        # Package configuration
```

**Key Directories:**
- **Core Library:** `src/hypernodes/` - Zero-dependency pipeline system
- **Integrations:** `integrations/` - Optional Daft and Dask engines
- **Visualization:** `viz/` - Graphviz + React Flow renderers
- **Tests:** `tests/` - Comprehensive pytest suite
- **Documentation:** `docs/` + `guides/` - Complete docs and design guides

---

## Key Features

### 1. Think Singular, Scale with Map

Write logic for **one item**, then scale automatically:

```python
@node(output_name="result")
def process(text: str) -> dict:
    return {"clean": text.strip()}

# Single item
pipeline.run(inputs={"text": "hello"})

# Multiple items (automatic parallelization)
pipeline.map(inputs={"text": ["a", "b", "c"]}, map_over="text")
```

### 2. Smart Content-Addressable Caching

**Incremental computation** - only recompute what changed:

- Function code changes → Auto-invalidate
- Input changes → Only affected items re-execute
- Dependency changes → Cascade invalidation
- Per-item signatures in `.map()` → Add one item, only compute one item

### 3. Hierarchical Composition

**Pipelines are nodes** - compose infinitely:

```python
inner = Pipeline(nodes=[clean])
outer = Pipeline(nodes=[inner.as_node(), analyze])
```

### 4. Pluggable Execution Engines

- **SeqEngine:** Sequential execution (default)
- **DaftEngine:** Distributed DataFrame execution
- **DaskEngine:** Parallel map operations

**Same API, different scale:**

```python
# Local
engine = SeqEngine()

# Distributed
engine = DaftEngine(use_batch_udf=True)

pipeline = Pipeline(nodes=[...], engine=engine)
```

### 5. Conditional Execution (Branch Nodes)

```python
@branch(when_true=process_valid, when_false=handle_error)
def is_valid(data: dict) -> bool:
    return data["score"] > 0.5
```

### 6. Rich Visualization

- **Static SVG:** Graphviz rendering
- **Interactive HTML:** React Flow with expand/collapse, bundled assets (fully offline)
- **Debug Mode:** Edge/node validation overlays

### 7. Observability

- **Progress Tracking:** tqdm/rich progress bars (auto-detects environment)
- **Distributed Tracing:** Logfire integration
- **Waterfall Diagrams:** Execution timing visualization

---

## Installation

### For Users

```bash
pip install hypernodes
# or
uv add hypernodes
```

### Optional Features

```bash
# All features
uv add hypernodes[all]

# Specific features
uv add hypernodes[daft]        # Distributed execution
uv add hypernodes[viz]         # Visualization
uv add hypernodes[telemetry]   # Tracing and progress
uv add hypernodes[notebook]    # Jupyter support
```

---

## Quick Start

```python
from hypernodes import Pipeline, node, SeqEngine, DiskCache

# Define nodes
@node(output_name="cleaned")
def clean(text: str) -> str:
    return text.strip().lower()

@node(output_name="word_count")
def count(cleaned: str) -> int:
    return len(cleaned.split())

# Create pipeline with caching
engine = SeqEngine(cache=DiskCache(path=".cache"))
pipeline = Pipeline(nodes=[clean, count], engine=engine)

# Run
result = pipeline.run(inputs={"text": "  Hello World  "})
# {'cleaned': 'hello world', 'word_count': 2}

# Scale with map
results = pipeline.map(
    inputs={"text": ["Hello", "World"]},
    map_over="text"
)
```

---

## Documentation

### Core Documentation

Located in `docs/`:

**Getting Started:**
- `introduction.mdx` - Project overview
- `get-started/installation.mdx` - Installation guide
- `get-started/quickstart.mdx` - Quick start tutorial

**Core Concepts:**
- `core/nodes.mdx` - Node decorator and behavior
- `core/pipelines.mdx` - Pipeline construction and execution
- `core/caching.mdx` - Caching system

**Composition:**
- `composition/nested-pipelines.mdx` - Pipeline nesting
- `composition/branch-nodes.mdx` - Conditional execution
- `composition/dual-nodes.mdx` - Singular/batch implementations

**Scaling:**
- `engines/overview.mdx` - Engine concepts
- `engines/daft.mdx` - Distributed execution with Daft
- `scaling/map-operations.mdx` - Parallel processing

**Observability:**
- `observability/visualization.mdx` - Visualization system
- `observability/progress.mdx` - Progress tracking
- `observability/tracing.mdx` - Distributed tracing

### Design Guides

Located in `guides/`:

- `graph_design_philosophy.md` - Design principles
- `graph_implementation_guide.md` - Implementation details
- `HyperNodes Pipeline System Specification/` - Complete specification

### Examples

Located in `examples/` and `notebooks/`:

**Scripts:**
- `daft_backend_example.py` - Daft engine usage
- `dask_engine_example.py` - Dask engine usage
- `fluent_api_example.py` - API patterns

**Notebooks:**
- `guide.ipynb` - Main guide
- `engines.ipynb` - Engine comparison
- `visualization_showcase.ipynb` - Visualization features
- `telemetry_examples.ipynb` - Observability demos

---

## Development

### Running Tests

```bash
# All tests
uv run pytest

# Specific category
uv run pytest tests/test_execution.py
uv run pytest tests/viz/
uv run pytest tests/test_daft_*.py
```

### Building

```bash
# Build distribution
uv build

# Output: dist/hypernodes-0.4.8-py3-none-any.whl
```

### Test Coverage

**94 test files** organized by feature:

- Core: execution, mapping, caching, callbacks
- Composition: nesting, branching, binding
- Engines: Daft, Dask, benchmarks
- Visualization: 27 files (includes Playwright browser tests)
- Features: stateful parameters, progress tracking

---

## Architecture Highlights

### Design Principles

1. **Cache-First:** Content-addressable caching for reproducible pipelines
2. **Think Singular:** Write for one item, scale automatically
3. **Hierarchical Composition:** Pipelines as nodes
4. **Pluggable Engines:** Swap execution strategy without code changes
5. **Zero Dependencies:** Core has no external dependencies

### Key Architectural Components

**DAG Construction:**
- `graph_builder.py` - Analyzes node dependencies
- `pipeline.py` - Pure definition (no execution state)

**Execution:**
- `sequential_engine.py` - Default sequential execution
- `orchestrator.py` - Lifecycle management
- `node_execution.py` - Single node execution logic

**Caching:**
- `cache.py` - Content-addressable cache
- Signature = hash(code + inputs + dependencies)

**Composition:**
- `branch.py` - Conditional routing
- `dual_node.py` - Singular/batch implementations
- `pipeline_node.py` - Nested pipeline wrapper

**Observability:**
- `callbacks.py` - Lifecycle hooks
- `telemetry/progress.py` - Progress bars
- `telemetry/tracing.py` - Distributed tracing

---

## Extension Points

### Custom Engines

Implement the `Engine` protocol for alternative execution strategies.

### Custom Cache Backends

Extend cache interface for Redis, S3, or other storage.

### Custom Callbacks

Inherit from `PipelineCallback` for custom observability.

### Custom Visualizations

Implement `VisualizationEngine` protocol for custom renderers.

---

## Roadmap

**Current Focus (v0.4.x - Alpha):**
- Core pipeline functionality ✓
- Caching system ✓
- Daft/Dask engines ✓
- Visualization system ✓
- Branch nodes ✓

**Future Directions:**
- Distributed caching (Redis/S3)
- Multi-way branching
- Pipeline optimization passes
- Streaming data support
- Full mypy type checking

---

## Community & Support

**GitHub:** https://github.com/gilad-rubin/hypernodes  
**Issues:** https://github.com/gilad-rubin/hypernodes/issues  
**License:** MIT  
**Author:** Gilad Rubin (me@giladrubin.com)

**Inspiration:**
- [Pipefunc](https://github.com/pipefunc/pipefunc) - Function composition
- [Apache Hamilton](https://github.com/dagworks-inc/hamilton) - Dataflow paradigm

---

## Project Metrics

**Source Code:**
- 64 Python files in `src/hypernodes/`
- 94 test files
- ~10,000+ lines of code

**Documentation:**
- 40+ documentation files
- Complete API reference
- Design guides and specifications

**Test Coverage:**
- Unit tests for all core features
- Integration tests for engines
- Playwright browser tests for visualization

**Distribution:**
- PyPI package (`pip install hypernodes`)
- Wheel and source distributions
- Python 3.10+ support

---

**Last Updated:** 2025-12-22  
**Version:** 0.4.8  
**Status:** Alpha (Development Status :: 3 - Alpha)

