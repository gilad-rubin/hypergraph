# Hypergraph Agent Guide (L1 Index)

Purpose: keep this file high-signal because it is injected into every agent session.

## Project Why

Hypergraph is a Python workflow orchestration framework (alpha, solo dev) with one primitive model for DAGs, branches, loops, and nested graphs.

Core intent:
- Keep user API small and clear; absorb complexity internally.
- Treat nested graphs as first-class everywhere (execution, checkpointing, debugging, CLI, observability).
- Catch structural errors at graph build time when possible, not late at runtime.

## Project What

Primary code areas (coarse map only):
- `src/hypergraph/nodes/`: node primitives and decorators.
- `src/hypergraph/graph/`: graph construction, binding, validation, input specs.
- `src/hypergraph/runners/`: sync/async execution engines and shared run-state utilities.
- `src/hypergraph/events/`: event types/processors, progress, and OTel integration.
- `src/hypergraph/checkpointers/`: persistent run history and SQLite implementation.
- `src/hypergraph/cli/`: run/inspect graphs from terminal.
- `src/hypergraph/viz/`: renderer + HTML/Mermaid visualization pipeline.

## Project How

Use `uv` for all Python tooling in this repo.

Worktree bootstrap:
- Treat each worktree as owning its own `.venv`.
- On first use of a fresh worktree, if `.venv` is missing or older than `pyproject.toml` / `uv.lock`, run `uv sync --group dev`.
- Do not reuse another worktree's virtualenv.
- Prefer `uv run ...` after bootstrap so commands execute against the local worktree environment.

Core commands:
```bash
uv run pytest                        # fast local (parallel, xdist)
uv run pytest -m slow
uv run pytest -m full_matrix
uv run pre-commit run --all-files
```

CI-equivalent local check (run before PR):
```bash
uv run pytest -W error -W 'ignore::pytest.PytestUnraisableExceptionWarning'
```
This matches what CI runs: all warnings as errors, except GC-triggered unraisable exceptions from `__del__` cleanup (sockets, event loops) which are non-deterministic.

Change validation expectations:
- Run focused tests for touched modules first, then broader suites as needed.
- Before PR, run the CI-equivalent command above — `uv run pytest` alone does NOT catch warning-as-error failures that CI enforces.
- Auto-format/lint hook runs after Python edits (`ruff check --fix` + `ruff format`), so avoid redundant manual formatting loops.
- For async tests that allocate long-lived resources (for example `SqliteCheckpointer` / `aiosqlite`), make teardown explicit. Prefer async fixtures that `await ...close()` rather than relying on event-loop shutdown to clean up worker threads.

## Working Rules For Agents

- Prefer minimal, targeted changes over broad refactors.
- Preserve sync/async behavioral parity when modifying runners.
- Keep nested-graph behavior consistent with flat-graph behavior.
- If public API changes, update tests and relevant docs in the same task.
- Use conventional commits with scopes, e.g. `feat(graph): ...`, `fix(runners): ...`.

## Design Conversation Preferences

When doing design work (exploring options, writing specs, proposing APIs):

- **Code examples over prose.** Show user-facing code first, explain rationale after. A concrete `runner.run(...)` call communicates more than a paragraph about execution semantics.
- **Start from the real use case.** Don't design in the abstract. Start with "user presses stop in a chat app" and work backward to the API, not the other way around.
- **Build on existing patterns.** Before proposing new machinery, check if an existing feature (checkpointer, interrupt resume, event dispatch) already handles the case. Layer, don't duplicate.
- **Simple top-level API, detail at lower levels.** `result.stopped` (boolean) for app control flow. Detailed metadata (which node, why, user-provided info) on events and step records. Don't force every consumer to destructure a dataclass for a yes/no question.
- **Framework owns its own state.** If the framework needs a registry (active signals, handles), the framework manages it. Never leak internal bookkeeping to the app as dicts the user must maintain and clean up.
- **Watch for naming collisions.** New concepts shouldn't shadow existing ones. If `InterruptNode` already exists, naming something `InterruptionInfo` creates confusion. Flag conflicts early.
- **Enforce constraints explicitly.** If "one active run per workflow_id" is an invariant, validate it with an error — don't hope users follow the convention.
- **Separate "what happened" from "what's next."** Status answers "what should the app do now?" Stop/failure info answers "what happened during this run?" Don't overload one field with both meanings.
- **Question the framing before diving in.** If a feature is being designed around `.iter()` but the real pattern is checkpointer-based resume, say so. The right framing avoids wasted design work.

## Progressive Disclosure

Load deeper docs only when relevant:
- Architecture: `dev/ARCHITECTURE.md`
- Core principles: `dev/CORE-BELIEFS.md`
- Testing strategy: `dev/TESTING-GUIDE.md`
- Contributing/workflow: `dev/CONTRIBUTING.md`
- Review checklist: `dev/REVIEW-CHECKLIST.md`
- Docs authoring: `docs/AGENTS.md`
- Viz-specific guidance: `src/hypergraph/viz/AGENTS.md`
- Debugging workflows: `docs/05-how-to/debug-workflows.md`
- Widget UX preferences: `dev/WIDGET-PREFERENCES.md`
- Specs: `specs/reviewed/`, `specs/not_reviewed/`

Convention:
- Any directory with `AGENTS.md` should also expose `CLAUDE.md -> AGENTS.md` symlink for tool compatibility.

## Guardrails

- Do not merge to `master` without PR review + passing CI.
- Follow `.github/PULL_REQUEST_TEMPLATE.md` (problem statement, before/after, test plan).
- Keep this file concise; move task/domain detail into linked docs above.
