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

### Stale bytecode after a branch switch

Git does not delete directories, and `__pycache__/` is ignored — so switching
away from a branch that added a subpackage leaves an empty-looking
`src/hypergraph/<pkg>/` behind, kept alive by its stale bytecode. Python then
imports that directory as a PEP 420 namespace package: `import hypergraph.<pkg>`
**succeeds** with `__file__ = None` and no contents.

This makes `pytest.importorskip("hypergraph.<pkg>")` an unreliable probe for a
branch-only subpackage — it does not skip. The failure surfaces much later as
`ImportError: cannot import name 'X' from 'hypergraph.<pkg>' (unknown location)`,
or never, if the probe only imports the package.

Prefer a per-branch worktree: a fresh worktree has no leftover directory, which
is why the guidance above is the real mitigation. If you do switch in place:

```bash
find . -name __pycache__ -type d -prune -exec rm -rf {} +
```

`tests/test_package_layout.py` fails if any directory under `src/hypergraph/`
lacks an `__init__.py`, so a leftover is caught here rather than in a lying probe.

## Notebooks

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

# Generated sync runner template (see "The sync template is generated" below)
uv run python scripts/gen_sync.py          # regenerate after editing template_async.py
uv run python scripts/gen_sync.py --check  # what CI's lint job runs
```

### The sync template is generated

`src/hypergraph/runners/_shared/template_sync.py` is **generated** from
`template_async.py` by `scripts/gen_sync.py`. Do not hand-edit it: change the
async template, run the generator, and commit both files. A pre-commit hook and
CI's lint job both run `--check` and fail with the offending hunk if they
disagree — so drift is caught at commit time, not one CI round-trip later.

The transform is `async def` -> `def`, `await x` -> `x`, a rename table for the
names that genuinely differ (the checkpointer's sync half, `AsyncRunTeardown` ->
`RunTeardown`, the `_async` / `_sync` hook pairs), and three markers for the
places where the two halves are not the same code at all:

| marker | meaning |
|---|---|
| `# sync:skip: <reason>` | drop this one line from the sync file |
| `# sync:skip-start: <reason>` … `# sync:skip-end` | drop this region |
| `# sync:only-start: <reason>` … `# sync:only-end` | emit this commented block as live sync code |

Every marker must carry a reason; the generator refuses one that does not. It
also refuses a marker that appears inside a string literal (where it is prose,
not an instruction), a region marker trailing code, and any transform whose
result fails to compile — which is what catches a `# sync:skip:` suffix left on a
statement `ruff format` has wrapped across several lines, *when* dropping that
one line leaves unbalanced Python, and what catches an `await` the rewriter left
in a plain `def` (that one *parses*; only compiling it says `'await' outside
async function`). A drop that happens to leave valid but different code is not
caught, so prefer a region whenever the statement spans more than one line. It
also refuses a rename-table name written as a keyword
argument (`f(checkpointer=x)`), because it rewrites names and not the signatures
they bind to — pass such an argument positionally. The refusal fires even inside a
`sync:skip` region, since names are rewritten before markers are applied.

Finally, it refuses an f-string whose replacement fields hold anything the
transform would act on — a rename-table name, an `await`, a renamed literal, or a
marker (`f"cp={checkpointer!r}"`). The transform reads tokens, and what a token
*is* inside an f-string changed in 3.12 (PEP 701: before, the whole f-string is
one opaque token; after, its interior is tokenized), so the same template would
generate different sync files on different interpreters. Rather than resolve that
one way or the other, it is refused on all of them: assign the value to a local
first, or, for prose, use a plain string. An f-string's *literal* part is never
rewritten, so `f"an async run awaits {x}"` is fine. Like the keyword-argument
refusal, this one also fires inside a `sync:skip` region (the transform runs
before markers are applied), so an async-only line gets the same treatment.

**The constraint the whole design rests on: a sync run must never require an
event loop.** See
[ADR 0009](../docs/adr/0009-a-sync-run-must-never-require-an-event-loop.md) for
why that rules out a blocking facade over the async engine: `asyncio.run` inside
a running loop breaks in Jupyter, ContextVars do not cross a portal thread
cleanly, and sync's fail-fast sequential `map` is user-visible behavior a shared
concurrent engine would change. So wherever the mechanical transform would
produce loop-dependent code — the concurrent `map` fan-out, the backpressured
`map_iter` worker pool, the shared concurrency limiter, the background
checkpoint-error sink — the sync side is marked, not translated. A new marker is
a claim that the two halves genuinely differ; prefer deleting the difference over
adding one.

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
