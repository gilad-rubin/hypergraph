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

`uv sync --group dev` is the default local setup for contributors. It includes the optional tooling used by this repo's broader test surface, including SQLite checkpointing, notebook kernels, Rich progress, and OTel processors.

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

# CI-equivalent (run before PR) — see "CI parity" below for required setup
uv run pytest -W error -W 'ignore::pytest.PytestUnraisableExceptionWarning'

# Lint & format
uv run ruff check src/ tests/              # lint
uv run ruff format src/ tests/             # format
uv run pre-commit run --all-files          # all hooks

# Viz tests
uv run pytest tests/viz/                   # requires Playwright
```

### CI parity — install ALL extras before the gate

CI's test job installs `daft` and runs `playwright install chromium`, then parses
`pytest.xml` and **fails on any skipped test or warning**. A "passing" local run
that shows `2394 passed, 110 skipped` will fail in CI because each skip — daft
tests, viz tests, and any conditional skip — counts.

Before pushing a behavior change that touches runners, viz, examples, or
public input/output addressing, run the full CI gate locally:

```bash
uv sync --group dev --extra daft
uv run playwright install chromium
uv run pytest -W error -W 'ignore::pytest.PytestUnraisableExceptionWarning'
```

The skip-zero / warning-zero invariant catches bugs that the default `uv run pytest`
silently skips past: e.g. test migrations that miss daft- or playwright-only
fixtures, and `UserWarning`s emitted by code paths only some optional extras
exercise.

### The OpenTelemetry floor job

`pyproject.toml` declares `opentelemetry-api`/`opentelemetry-sdk` `>=1.24.0`, because 1.24.0
is the first release whose `Span` has `add_link` — the call the collapsed-lineage spans make
(`src/hypergraph/events/otel.py`). Every other CI job resolves the *newest* OpenTelemetry, so
the `otel-floor` job is the only thing that exercises the declared floor. Reproduce it locally
before changing anything under `src/hypergraph/events/otel.py` or the `otel` extra:

```bash
uv sync --group dev --python 3.10
uv pip install --python .venv/bin/python \
  opentelemetry-api==1.24.0 opentelemetry-sdk==1.24.0 opentelemetry-exporter-otlp-proto-http==1.24.0
uv run --no-sync --python 3.10 pytest tests/test_run_log \
  -W error -W 'ignore::pytest.PytestUnraisableExceptionWarning'

uv sync --group dev --extra daft   # restore the newest OpenTelemetry when you are done
```

Three things about that recipe are load-bearing:

- **`uv pip install` + `--no-sync`, not `uv run --with`.** The `--with` overlay loses to the
  project environment for a package the project already has, so
  `uv run --with opentelemetry-sdk==1.24.0 pytest ...` reports a green run against the newest
  SDK and proves nothing. `--no-sync` then keeps `uv run` from restoring the newest versions.
- **Pin the OTLP/HTTP exporter too.** It constrains the SDK to its own minor, so leaving it at
  the newest version either breaks the install or skips the wire tests.
- **Python 3.10.** `opentelemetry-proto==1.24.0` requires `protobuf<5`, which emits a
  `DeprecationWarning` on Python 3.12+ that `-W error` turns into a collection error.

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
