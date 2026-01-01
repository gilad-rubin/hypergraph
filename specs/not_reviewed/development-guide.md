# HyperNodes Development Guide

**Project:** hypernodes v0.4.8  
**Type:** Python Library  
**Generated:** 2025-12-22

---

## Prerequisites

**Required:**
- Python >= 3.10 (tested on 3.10, 3.11, 3.12, 3.13)
- uv (recommended) or pip

**Recommended Development Tools:**
- pytest >= 8.4.2
- playwright >= 1.56.0 (for visualization tests)

---

## Installation

### For Users

```bash
# Install from PyPI
pip install hypernodes

# Or with uv (recommended)
uv add hypernodes
```

### For Contributors

```bash
# Clone the repository
git clone https://github.com/gilad-rubin/hypernodes
cd hypernodes

# Install with all development dependencies
uv sync

# Install with specific optional features
uv add hypernodes[all]           # All features
uv add hypernodes[daft]           # Daft distributed engine
uv add hypernodes[viz]            # Visualization support
uv add hypernodes[telemetry]      # Tracing and progress
uv add hypernodes[notebook]       # Jupyter support
uv add hypernodes[modal]          # Modal cloud integration
uv add hypernodes[examples]       # Example dependencies
```

---

## Project Structure

```
hypernodes/
├── src/hypernodes/       # Main package source
├── tests/                # Test suite (pytest)
├── docs/                 # Documentation (MDX)
├── guides/               # Design documents
├── examples/             # Example scripts
├── notebooks/            # Jupyter notebooks
├── scripts/              # Utility scripts
└── pyproject.toml        # Package configuration
```

---

## Development Workflow

### Running the Code

The library is meant to be imported, not run directly. See `examples/` and `notebooks/` for usage examples.

```python
from hypernodes import Pipeline, node, SeqEngine, DiskCache

@node(output_name="result")
def process(x: int) -> int:
    return x * 2

engine = SeqEngine(cache=DiskCache(path=".cache"))
pipeline = Pipeline(nodes=[process], engine=engine)
result = pipeline.run(inputs={"x": 5})
```

### Running Tests

```bash
# Run all tests
uv run pytest

# Run specific test file
uv run pytest tests/test_execution.py

# Run with coverage
uv run pytest --cov=hypernodes

# Run visualization tests (requires Playwright)
uv run pytest tests/viz/

# Run Daft engine tests
uv run pytest tests/test_daft_*.py
```

**Test Organization:**
- `tests/test_*.py` - Core functionality tests
- `tests/viz/` - Visualization system tests (27 files)
- `tests/old/` - Deprecated tests (not run by default)

### Code Quality

**Linting & Formatting:**
The project uses standard Python tooling (specific tools not specified in pyproject.toml).

```bash
# Run tests with pytest
uv run pytest

# Check for issues
uv run pytest -v
```

### Building the Package

```bash
# Build distribution packages
uv build

# Output: dist/hypernodes-0.4.8-py3-none-any.whl
#         dist/hypernodes-0.4.8.tar.gz
```

**Build Configuration:**
- **Build Backend:** Hatchling (PEP 517)
- **Package Source:** `src/hypernodes/`
- **Excluded:** `src/hypernodes/old/` (deprecated code)

---

## Optional Feature Groups

The library uses optional dependencies for different use cases:

| Feature Group | Install | Purpose |
|---------------|---------|---------|
| `daft` | `uv add hypernodes[daft]` | Distributed execution with Daft (>=0.6.11) |
| `dask` | Included in examples | Parallel map operations with Dask |
| `viz` | `uv add hypernodes[viz]` | Graphviz static visualization (>=0.20) |
| `notebook` | `uv add hypernodes[notebook]` | Jupyter support with IPyWidgets (>=8.1.7) |
| `telemetry` | `uv add hypernodes[telemetry]` | Logfire tracing + tqdm/rich progress |
| `modal` | `uv add hypernodes[modal]` | Modal serverless integration (>=0.64.0) |
| `batch` | `uv add hypernodes[batch]` | PyArrow batch processing (>=14.0.0) |
| `all` | `uv add hypernodes[all]` | All optional features |
| `examples` | `uv add hypernodes[examples]` | Dependencies for example scripts |

---

## Environment Setup

**No special environment variables required** for core functionality.

**Optional:**
- `LOG_HYPERNODES_CACHE=1` - Enable cache hit/miss logging
- Standard Python environment variables apply

---

## Running Examples

```bash
# Run example scripts
uv run python examples/daft_backend_example.py
uv run python examples/dask_engine_example.py
uv run python examples/fluent_api_example.py

# Launch Jupyter notebooks
uv run jupyter notebook notebooks/guide.ipynb
```

---

## Development Scripts

The `scripts/` directory contains 186 utility Python files for various development tasks.

**Common patterns:**
- Test reproduction scripts
- Benchmark scripts
- Visualization test scripts
- Development utilities

---

## Testing Strategy

**Test Framework:** pytest with asyncio support

**Test Configuration (`pyproject.toml`):**
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
norecursedirs = ["old", ".*", "build", "dist", "*.egg"]
```

**Test Categories:**
1. **Core Execution** - Basic pipeline functionality
2. **Map Operations** - Collection processing
3. **Caching** - Cache behavior and invalidation
4. **Callbacks** - Lifecycle hooks
5. **Nested Pipelines** - Composition and nesting
6. **Branch Nodes** - Conditional execution
7. **Engines** - Daft, Dask integration
8. **Visualization** - Rendering and layout (includes Playwright browser tests)
9. **Stateful Objects** - Stateful parameter handling
10. **Binding** - Input binding and defaults

**Playwright Setup** (for visualization tests):
```bash
# Install Playwright browsers
playwright install chromium
```

---

## Common Development Tasks

### Add a New Node Type

1. Create new decorator in `src/hypernodes/decorators.py` or separate file
2. Implement node class following `HyperNode` protocol
3. Update `src/hypernodes/__init__.py` exports
4. Add tests in `tests/test_<feature>.py`
5. Update documentation in `docs/`

### Add a New Engine

1. Create new file in `src/hypernodes/integrations/<engine_name>/`
2. Implement `Engine` protocol from `protocols.py`
3. Add integration tests in `tests/test_<engine>_*.py`
4. Update `pyproject.toml` optional dependencies
5. Document in `docs/engines/`

### Add Visualization Features

1. Update graph walker in `src/hypernodes/viz/graph_walker.py`
2. Modify renderers in `viz/graphviz/` or `viz/js/`
3. Add tests in `tests/viz/`
4. Rebuild Tailwind CSS if needed: `build/tailwind/rebuild.sh`

---

## Continuous Integration

**No CI/CD configuration found** in the repository.

Typical workflow would include:
- Run pytest on multiple Python versions
- Check code quality (linting, formatting)
- Build distribution packages
- Publish to PyPI on release tags

---

## Release Process

**Based on project structure:**

1. Update version in `pyproject.toml`
2. Update `CHANGELOG.md`
3. Build packages: `uv build`
4. Test distribution: `uv run pytest`
5. Publish to PyPI: `uv publish` (or twine)
6. Tag release in git
7. Update documentation

**Current Version:** 0.4.8  
**Status:** Alpha (Development Status :: 3 - Alpha)

---

## Resources

**Documentation:**
- `docs/` - Complete MDX documentation
- `guides/` - Design and implementation guides
- `README.md` - Quick start and overview

**Examples:**
- `examples/` - Standalone example scripts
- `notebooks/` - Interactive Jupyter notebooks

**Support:**
- GitHub Issues: https://github.com/gilad-rubin/hypernodes/issues
- Repository: https://github.com/gilad-rubin/hypernodes

---

## Troubleshooting

### Import Errors
- Ensure Python >= 3.10
- Check optional dependencies are installed for features you're using

### Test Failures
- Run `uv sync` to update dependencies
- For Playwright tests: `playwright install chromium`
- Check that deprecated tests in `tests/old/` are excluded

### Visualization Issues
- Graphviz visualization requires `graphviz` package
- Interactive visualization works offline (assets bundled)
- Jupyter support requires `ipywidgets` >= 8.1.7

---

**Last Updated:** 2025-12-22

