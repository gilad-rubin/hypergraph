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
- When evaluating designs, optimize first for conceptual cleanliness and API coherence, not estimated implementation effort.
- Treat effort estimates with skepticism in design conversations. Agent implementation cost is often much lower than human intuition suggests.
- Do not argue against a design mainly because it seems like "too much work" or would take humans a long time; this repo is intentionally optimized for fast iteration with agents.
- Backward compatibility is not a default constraint here. Hypergraph is currently a solo-user project, so prefer the best design unless compatibility is explicitly requested.

## Design Conversation Preferences

When doing design work (exploring options, writing specs, proposing APIs):

- **Start from user goals.** Begin design docs and design discussions with the concrete goals the API must satisfy before proposing shapes or mechanisms.
- **Code examples over prose.** Show user-facing code first, explain rationale after. A concrete `runner.run(...)` call communicates more than a paragraph about execution semantics.
- **Start from the real use case.** Don't design in the abstract. Start with "user presses stop in a chat app" and work backward to the API, not the other way around.
- **Clean design over local minima.** Explore the best end-state API first, then discuss phasing. Do not prematurely narrow the design around "smallest change" reasoning.
- **Build on existing patterns.** Before proposing new machinery, check if an existing feature (checkpointer, interrupt resume, event dispatch) already handles the case. Layer, don't duplicate.
- **Simple top-level API, detail at lower levels.** `result.stopped` (boolean) for app control flow. Detailed metadata (which node, why, user-provided info) on events and step records. Don't force every consumer to destructure a dataclass for a yes/no question.
- **Framework owns its own state.** If the framework needs a registry (active signals, handles), the framework manages it. Never leak internal bookkeeping to the app as dicts the user must maintain and clean up.
- **Watch for naming collisions.** New concepts shouldn't shadow existing ones. If `InterruptNode` already exists, naming something `InterruptionInfo` creates confusion. Flag conflicts early.
- **Enforce constraints explicitly.** If "one active run per workflow_id" is an invariant, validate it with an error — don't hope users follow the convention.
- **Separate "what happened" from "what's next."** Status answers "what should the app do now?" Stop/failure info answers "what happened during this run?" Don't overload one field with both meanings.
- **Question the framing before diving in.** If a feature is being designed around `.iter()` but the real pattern is checkpointer-based resume, say so. The right framing avoids wasted design work.
- **Do not over-weight backward compatibility.** If the current API shape is getting in the way, say so plainly and propose the cleaner replacement.
- **Keep the main design surface user-facing.** In the main body, show only the proposed user-facing APIs and how each one satisfies the goals. Put internal mechanics and implementation notes in an addendum.
- **Show the main journeys before the full matrix.** Lead with the 2-4 primary user journeys the feature is really about before expanding into broader scenario coverage.
- **Make runner setup explicit in examples.** In design docs, do not use a floating `runner` variable without first showing whether it is a `SyncRunner` or `AsyncRunner` and how it was constructed.
- **Make checkpoint requirements explicit in examples.** If an example uses `workflow_id`, `fork_from`, or `retry_from`, show a checkpointer in that example unless the point of the example is that Hypergraph should error without one.
- **Show scenario coverage explicitly.** When working on a design doc, include a concrete scenario matrix for the important combinations. Cover sync/async, success/failure, stop/pause, inspect on/off, checkpoint on/off, nested graphs, and map when relevant. For broad features, aim for at least 10 scenarios. For each scenario, show the user-facing code, what the user sees visually, what they get in code, and which goals it serves.
- **Design the plain error surface too.** If the feature affects debugging or failures, do not stop at widgets or structured objects. Also specify how plain raised errors and returned error fields should identify the failing graph/subgraph/node/item and suggest an immediate next step for fixing or rerunning.
- **Call out dilemmas explicitly.** If there are real design forks or non-obvious tradeoffs, surface them in a dedicated dilemmas/open-questions section instead of mixing them into the primary API story.

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
