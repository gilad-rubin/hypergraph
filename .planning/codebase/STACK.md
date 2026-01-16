# Technology Stack

**Analysis Date:** 2026-01-16

## Languages

**Primary:**
- Python 3.12 - All source code, tests, and scripts

**Version Support:**
- Python 3.10, 3.11, 3.12, 3.13 - Per `pyproject.toml` classifiers

## Runtime

**Environment:**
- Python 3.12 (specified in `.python-version`)
- Virtual environment: `.venv/`

**Package Manager:**
- uv - Primary package manager (see `uv run` workflow in `CLAUDE.md`)
- Lockfile: `uv.lock` (present, 965KB)

## Frameworks

**Core:**
- networkx >= 3.2 - Graph data structures and algorithms (`src/hypergraph/graph.py`)

**Testing:**
- pytest >= 8.4.2 - Test runner
- pytest-asyncio >= 1.3.0 - Async test support

**Build:**
- hatchling - Build backend for distribution
- uv - Development workflow

## Key Dependencies

**Critical (Required):**
- networkx >= 3.2 - Core graph operations, cycle detection, DAG validation

**Optional Dependency Groups:**

| Group | Packages | Purpose |
|-------|----------|---------|
| `batch` | pyarrow >= 14.0.0 | Batch processing support |
| `daft` | daft >= 0.6.11 | Dataframe integration |
| `notebook` | notebook, ipykernel, ipywidgets, nbformat | Jupyter support |
| `telemetry` | logfire >= 2.0.0, tqdm, rich | Observability |
| `modal` | modal >= 0.64.0, cloudpickle | Serverless execution |
| `examples` | colbert-ai, model2vec, pandas, pydantic, etc. | Example notebooks |

**Development Dependencies:**
- playwright >= 1.56.0 - Browser automation testing
- pytest >= 8.4.2 - Test framework
- pytest-asyncio >= 1.3.0 - Async test support

## Configuration

**Environment:**
- No `.env` file in project root
- Environment variables loaded via dotenv when needed (per `CLAUDE.md`)
- No required environment variables for core functionality

**Build:**
- `pyproject.toml` - Project metadata, dependencies, build config
- `tool.hatch.build.targets.wheel.packages` = `["src/hypergraph"]`
- Exclusions: `src/hypergraph/old/` excluded from distribution

**Testing:**
- `tool.pytest.ini_options` in `pyproject.toml`
- `asyncio_mode = "auto"` - Automatic async test detection
- `testpaths = ["tests"]`
- `norecursedirs = ["old", ".*", "build", "dist", "*.egg"]`

**Linting/Formatting:**
- Ruff - Linting (`.ruff_cache/` present)
- No explicit config files, likely using defaults

## Documentation

**Platforms:**
- GitBook - Documentation hosting (`.gitbook.yaml`)
- doc-manager - Custom doc management (`.doc-manager.yml`)

**Doc Paths:**
- Root: `docs/`
- README: `docs/README.md`
- Summary: `docs/SUMMARY.md`

## Platform Requirements

**Development:**
- Python 3.10+ (3.12 recommended)
- uv package manager
- macOS, Linux, or Windows

**Production:**
- Pure Python library, no platform-specific requirements
- Target: PyPI distribution as `hypergraph-ai`

## Package Distribution

**Package Name:** hypergraph
**PyPI Name:** hypergraph-ai (per README install instructions)
**Version:** 0.1.0 (Alpha)
**License:** MIT

**Source Layout:**
- `src/hypergraph/` - Main package
- Hatch src-layout pattern

---

*Stack analysis: 2026-01-16*
