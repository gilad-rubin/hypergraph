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

# Install pre-commit and pre-push hooks
uv run pre-commit install
uv run pre-commit install --hook-type pre-push

# Notebook diff/cleanup integration
uv run nbstripout --install --attributes .gitattributes
uv run nbdime config-git --enable

# Playwright (only needed for viz tests)
uv run playwright install chromium
```

`uv sync --group dev` is the default local setup for contributors. It includes the optional tooling used by this repo's broader test surface, including CLI, SQLite checkpointing, notebook kernels, Rich progress, and OTel processors.

## Worktrees

When working in a Git worktree, treat each worktree as owning its own `.venv`.

```bash
cd /path/to/worktree
uv sync --group dev
```

Guidelines:

- If `.venv` is missing, run `uv sync --group dev`
- If `pyproject.toml` or `uv.lock` is newer than `.venv`, run `uv sync --group dev` again
- Do not reuse another worktree's virtualenv
- Prefer `uv run ...` so commands execute against the local worktree environment

The notebook setup does two separate jobs:

- `nbstripout` removes notebook outputs and transient metadata from the Git-tracked version so HTML-heavy cells do not explode diffs.
- `nbdime` gives `.ipynb` files notebook-aware diffs and merges instead of raw JSON diffs.

## Commands

```bash
# Tests
uv run pytest                              # default (parallel, fast)
uv run pytest tests/test_specific.py       # single file
uv run pytest -k "test_name"              # single test
uv run pytest -m slow                      # slow tests
uv run pytest -m full_matrix               # full capability matrix

# CI-equivalent (run before PR)
uv run pytest -W error -W 'ignore::pytest.PytestUnraisableExceptionWarning'

# Lint & format
uv run ruff check src/ tests/              # lint
uv run ruff format src/ tests/             # format
uv run pre-commit run --all-files          # all hooks

# Viz tests
uv run pytest tests/viz/                   # requires Playwright
```

## Workflow

1. Create a feature branch from `master`
2. If using a fresh worktree, bootstrap it with `uv sync --group dev`
3. Implement with TDD: write failing test first, then make it pass
4. Run `uv run pytest` after each logical step
5. Commit with conventional commits: `feat(graph): add X`, `fix(runners): handle Y`
6. Push and create PR

## PR Expectations

- Conventional commit title (e.g., `feat(graph): add strict type validation`)
- CI-equivalent tests pass: `uv run pytest -W error -W 'ignore::pytest.PytestUnraisableExceptionWarning'`
- Lint clean (`uv run ruff check src/ tests/`)
- Review checklist satisfied (see [dev/REVIEW-CHECKLIST.md](REVIEW-CHECKLIST.md))

## Project Structure

```
src/hypergraph/    # Library source
tests/             # Test suite (70+ files)
docs/              # User-facing documentation
dev/               # Internal development guidance (you are here)
specs/             # Design specifications
scripts/           # Utility scripts and runnable examples
```
