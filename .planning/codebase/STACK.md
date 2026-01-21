# Technology Stack

**Analysis Date:** 2026-01-21

## Languages

**Primary:**
- Python 3.10+ - Core framework implementation
  - Minimum supported: Python 3.10
  - Tested: Python 3.10, 3.11, 3.12, 3.13
  - Used in: `src/hypergraph/`

**Scripting/Markup:**
- JavaScript - Frontend visualization and layout engine
- HTML/CSS - Jupyter widget rendering and visualization UI

## Runtime

**Environment:**
- CPython (system Python via `uv`)
- Async support via `asyncio` (standard library)

**Package Manager:**
- `uv` - Primary package manager (as defined in pyproject.toml)
- Lockfile: `uv.lock` (UV lock format)
- Traditional pip support also available

## Frameworks

**Core Execution:**
- `networkx>=3.2` - Graph data structures and algorithms
  - Used for: DAG validation, cycle detection, topological sorting
  - Location: `src/hypergraph/graph/core.py`, `src/hypergraph/graph/validation.py`

**Async Execution:**
- `asyncio` (standard library) - Async/await support and concurrent execution
  - Used in: `src/hypergraph/runners/async_/runner.py`
  - Features: Concurrency control via `max_concurrency` parameter

**Testing:**
- `pytest>=8.4.2` - Test runner framework
- `pytest-asyncio>=1.3.0` - Async test support
- `pytest-xdist>=3.5.0` - Parallel test execution
- `allpairspy>=2.5.1` - Pairwise test matrix generation

**Development/Compatibility:**
- `playwright>=1.56.0` - Browser automation (for visualization tests)

## Key Dependencies

**Critical (Required):**
- `networkx>=3.2` - Graph computations (only required dependency)

**Optional by Use Case:**

**Batch Processing:**
- `pyarrow>=14.0.0` - Arrow columnar data for batch operations

**Distributed Computing:**
- `daft>=0.6.11` - Distributed dataframes (Ray-based)
- `modal>=0.64.0` - Serverless compute integration
- `cloudpickle>=3.0.0` - Function serialization for modal

**Visualization & Jupyter:**
- `ipykernel>=6.31.0` - Jupyter kernel support for visualization
- `ipywidgets>=8.1.7` - Interactive widgets framework
- `notebook>=7.5.0` - Jupyter notebook server
- `nbformat>=5.10.4` - Notebook format support

**Telemetry & Observability:**
- `logfire>=2.0.0` - Structured logging and tracing
- `tqdm>=4.67.1` - Progress bar indicators
- `rich>=13.0.0` - Rich console output and formatting

**Examples/Demonstrations:**
- `numpy>=1.21.0` - Numerical operations
- `pandas>=1.3.0` - DataFrames (demonstration purposes)
- `pydantic>=2.12.3` - Data validation (example use)
- `colbert-ai>=0.2.22` - RAG/dense retrieval example
- `model2vec>=0.7.0` - Lightweight embeddings
- `pylate>=1.0.0` - Late binding for examples
- `rank-bm25>=0.2.2` - BM25 ranking
- `diskcache>=5.6.3` - Disk-based caching
- `pytrec-eval>=0.5` - Evaluation metrics

## Configuration

**Environment:**
- Python version: Controlled via `.python-version` or system default
- Virtual environment: `.venv/` (standard structure)
- Configuration: `pyproject.toml` (single source of truth)

**Build Configuration:**
- Build backend: `hatchling`
- Wheel packages: Built from `src/hypergraph`
- Excludes: `src/hypergraph/old/` (deprecated code excluded from distribution)

**Test Configuration:**
- Config file: `pyproject.toml` under `[tool.pytest.ini_options]`
- Test paths: `tests/`
- Async mode: Auto (asyncio_mode = "auto")
- Parallel execution: Enabled by default (`-n auto --dist worksteal`)
- Matrix tests: Excluded by default, run with `pytest -m full_matrix` in CI
- Test markers: `slow`, `full_matrix`

**Development Setup:**
- Dependency groups: `dev` (testing tools)
- Command: `uv run pytest` (ensures consistent environment)

## Platform Requirements

**Development:**
- Python 3.10+ with pip or uv
- Git (for version control)
- Optional: Jupyter/JupyterLab for notebook visualization

**Production:**
- Python 3.10+
- `networkx>=3.2` (only required dependency)
- Optional dependencies as needed for specific features:
  - Modal for serverless execution
  - Daft for distributed processing
  - Jupyter for notebook environments
  - Logfire for observability

**Visualization:**
- Jupyter environment (Lab or Notebook)
- Modern browser with JavaScript support
- For VSCode: ipywidgets extension support

---

*Stack analysis: 2026-01-21*
