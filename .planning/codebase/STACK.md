# Technology Stack

**Analysis Date:** 2026-01-16

## Languages

**Primary:**
- Python 3.12 - Core implementation language (specified in `.python-version`)
- Supports Python 3.10, 3.11, 3.12, 3.13 (per `pyproject.toml` classifiers)

**Secondary:**
- Markdown - Design specifications in `specs/reviewed/`

## Runtime

**Environment:**
- Python 3.10+ (requires-python = ">=3.10")
- macOS (Darwin) development platform

**Package Manager:**
- uv - Modern Python package manager
- Lockfile: `uv.lock` (present, ~966KB)
- Build backend: Hatchling

## Frameworks

**Core:**
- NetworkX >= 3.2 - Graph data structure and algorithms (only production dependency)

**Testing:**
- pytest >= 8.4.2 - Test runner
- pytest-asyncio >= 1.3.0 - Async test support
- playwright >= 1.56.0 - Browser automation (dev dependency)

**Build/Dev:**
- Hatchling - Build backend for packaging
- uv - Package management and virtual environment

## Key Dependencies

**Critical:**
- `networkx >= 3.2` - Provides the underlying directed graph structure (`nx.DiGraph`) for representing workflow graphs. Used for edge inference, cycle detection, and topological operations.

**Optional Dependency Groups:**

| Group | Dependencies | Purpose |
|-------|-------------|---------|
| `batch` | pyarrow >= 14.0.0 | Batch processing support |
| `daft` | daft >= 0.6.11 | Distributed data processing |
| `notebook` | notebook >= 7.5.0, ipykernel < 7, ipywidgets, nbformat | Jupyter support |
| `telemetry` | logfire >= 2.0.0, tqdm >= 4.67.1, rich >= 13.0.0 | Observability and progress |
| `modal` | modal >= 0.64.0, cloudpickle >= 3.0.0 | Serverless execution |
| `examples` | colbert-ai, daft, model2vec, pandas, pydantic, etc. | Example notebooks/scripts |
| `all` | Combines batch, daft, notebook, telemetry | Full install |

## Configuration

**Environment:**
- No `.env` file required for core functionality
- Python version pinned via `.python-version` file
- No runtime configuration needed for basic usage

**Build:**
- `pyproject.toml` - Package configuration, dependencies, build settings
- `uv.lock` - Dependency lockfile
- `.python-version` - Python version for uv

**Package Structure:**
```toml
[tool.hatch.build.targets.wheel]
packages = ["src/hypergraph"]
```

## Platform Requirements

**Development:**
- Python 3.10+
- uv package manager
- Git for version control

**Production:**
- Pure Python - no native extensions required
- NetworkX as only runtime dependency
- Cross-platform (macOS, Linux, Windows)

**Project Status:**
- Design phase - not yet published to PyPI
- Core node types implemented
- Execution layer (runners, checkpointing) in specification/design

## Module Structure

**Installed Package:** `hypergraph`

**Import Example:**
```python
from hypergraph import node, Graph, FunctionNode, GraphNode, HyperNode
from hypergraph import InputSpec, GraphConfigError, RenameError
```

---

*Stack analysis: 2026-01-16*
