# Contributing

Setup, commands, and workflow for hypergraph development.

## Prerequisites

- Python 3.10+ (3.12 recommended)
- [uv](https://docs.astral.sh/uv/) package manager

## Setup

```bash
# Clone and install
git clone https://github.com/gilad-rubin/hypergraph.git
cd hypergraph
uv sync --group dev

# Install pre-commit hooks
uv run pre-commit install

# Playwright (only needed for viz tests)
uv run playwright install chromium
```

## Commands

```bash
# Tests
uv run pytest                              # default (parallel, fast)
uv run pytest tests/test_specific.py       # single file
uv run pytest -k "test_name"               # single test
uv run pytest -m slow                      # slow tests
uv run pytest -m full_matrix               # full capability matrix

# Lint & format
uv run ruff check src/ tests/              # lint
uv run ruff format src/ tests/             # format
uv run pre-commit run --all-files          # all hooks

# Viz tests
uv run pytest tests/viz/                   # requires Playwright
```

## Workflow

1. Create a feature branch from `master`
2. Implement with TDD: write failing test first, then make it pass
3. Run `uv run pytest` after each logical step
4. Commit with conventional commits: `feat(graph): add X`, `fix(runners): handle Y`
5. Push and create PR

## PR Expectations

- Conventional commit title (e.g., `feat(graph): add strict type validation`)
- Tests pass (`uv run pytest`)
- Lint clean (`uv run ruff check src/ tests/`)
- Review checklist satisfied (see [dev/REVIEW-CHECKLIST.md](REVIEW-CHECKLIST.md))

## Project Structure

```
src/hypergraph/    # Library source
tests/             # Test suite (70+ files)
docs/              # User-facing documentation
dev/               # Internal development guidance (you are here)
specs/             # Design specifications
scripts/           # Utility scripts
examples/          # Example usage
```
