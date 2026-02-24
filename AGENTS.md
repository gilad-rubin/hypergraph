# Hypergraph

Python workflow orchestration framework (alpha, solo dev). One set of primitives for DAGs, branches, loops, and nested hierarchies.

## Information Architecture

This repo uses a three-layer progressive disclosure pattern to keep agent context lean:

| Layer | What | When Loaded |
|-------|------|-------------|
| **L1: Index** | This file (AGENTS.md) — module map, commands, deep-dive links | Every session (auto-loaded) |
| **L2: Domain guides** | `dev/` directory, subdomain docs (`docs/AGENTS.md`, `src/hypergraph/viz/AGENTS.md`) | Read when the task touches that domain |
| **L3: Skills & specs** | `.claude/skills/`, `specs/` — detailed workflows, reviewed designs | Activated by triggers or explicit request |

**Convention**: Every directory with its own AGENTS.md also has a `CLAUDE.md` symlink pointing to it (`CLAUDE.md → AGENTS.md`). This ensures both Claude Code and Codex find the same instructions.

**Rule**: Don't dump L2/L3 content into context upfront. Read it when the task requires it.

## Module Map

```
src/hypergraph/
  __init__.py          # Public API (decorators, types, runners, events, cache)
  _typing.py           # Internal type utilities
  _utils.py            # Internal helpers
  cache.py             # CacheBackend, InMemoryCache, DiskCache
  exceptions.py        # MissingInputError, InfiniteLoopError, IncompatibleRunnerError, ExecutionError

  nodes/               # Node types and decorators
    base.py            #   HyperNode (abstract), END sentinel
    function.py        #   FunctionNode, @node
    gate.py            #   GateNode, IfElseNode, RouteNode, @ifelse, @route
    graph_node.py      #   GraphNode (.as_node(), map_over)
    interrupt.py       #   InterruptNode, @interrupt
    _callable.py       #   Internal callable introspection
    _rename.py         #   Internal rename/copy machinery

  graph/               # Graph construction and validation
    core.py            #   Graph class (build pipeline, bind/select/unbind/with_entrypoint)
    input_spec.py      #   InputSpec (required/optional/entrypoint classification)
    validation.py      #   Build-time validation checks
    _conflict.py       #   Name conflict resolution
    _helpers.py        #   Graph construction helpers

  runners/             # Execution engines
    base.py            #   BaseRunner (shared interface)
    _shared/           #   Common utilities (caching, events, gate execution, routing, templates)
    sync/              #   SyncRunner + per-node-type executors
    async_/            #   AsyncRunner + per-node-type executors

  events/              # Observability (decoupled from execution)
    types.py           #   Event dataclasses (NodeStart, NodeEnd, RouteDecision, etc.)
    dispatcher.py      #   EventDispatcher
    processor.py       #   EventProcessor, AsyncEventProcessor, TypedEventProcessor
    rich_progress.py   #   RichProgressProcessor

  viz/                 # Graph visualization (HTML, Mermaid)
    renderer/          #   Edge/node precomputation, scope resolution
    html/              #   HTML generation, size estimation
    styles/            #   Node styling
    assets/            #   JS/CSS (React, ReactFlow, dagre, tailwind)
```

## Key Commands

```bash
# Run tests (parallel by default via xdist)
uv run pytest

# Include slow tests
uv run pytest -m slow

# Full capability matrix (CI only, ~8K tests)
uv run pytest -m full_matrix

# Lint + format
uv run ruff check src/ tests/
uv run ruff format src/ tests/

# All pre-commit hooks
uv run pre-commit run --all-files
```

## Commit Style

Conventional commits with scopes: `feat(graph):`, `fix(runners):`, `test(viz):`, `docs:`, `refactor(nodes):`, etc.

## Deep Dives

| Topic | Location |
|-------|----------|
| Architecture & boundaries | [dev/ARCHITECTURE.md](dev/ARCHITECTURE.md) |
| Design principles | [dev/CORE-BELIEFS.md](dev/CORE-BELIEFS.md) |
| Code conventions | [dev/CODE-CONVENTIONS.md](dev/CODE-CONVENTIONS.md) |
| Testing guide | [dev/TESTING-GUIDE.md](dev/TESTING-GUIDE.md) |
| Review checklist | [dev/REVIEW-CHECKLIST.md](dev/REVIEW-CHECKLIST.md) |
| Setup & workflow | [dev/CONTRIBUTING.md](dev/CONTRIBUTING.md) |
| Documentation guidelines | [docs/AGENTS.md](docs/AGENTS.md) |
| Visualization system | [src/hypergraph/viz/AGENTS.md](src/hypergraph/viz/AGENTS.md) |
| Design specs | `specs/reviewed/`, `specs/not_reviewed/` |

## Hooks (Automatic)

These run without agent intervention via `.claude/settings.json`:

| Event | What It Does |
|-------|--------------|
| **PostToolUse (Write\|Edit)** | Auto-runs `ruff check --fix` + `ruff format` on any `.py` file after edits |
| **SessionStart** | Entire: begins session tracking, captures agent type and start time |
| **UserPromptSubmit** | Entire: records each prompt for the session transcript |
| **PreToolUse (Task)** | Entire: tracks subagent spawn (nested session hierarchy) |
| **PostToolUse (Task)** | Entire: captures subagent results and token usage |
| **PostToolUse (TodoWrite)** | Entire: records task list changes |
| **SessionEnd / Stop** | Entire: finalizes session, stores transcript + token metrics + line attribution |

The auto-format hook means agents never need to manually run ruff — code is always formatted after every edit.

### Entire (Session Tracking)

[Entire](https://docs.entire.io) captures agent sessions as Git-native checkpoints. On every commit, it stores the full transcript, files touched, token usage, and agent-vs-human line attribution. Strategy: `manual-commit` (checkpoints only on explicit commits, no extra commits on your branch).

```bash
entire status                    # Show active sessions
entire explain                   # Browse checkpoints on current branch
entire explain --commit HEAD     # See AI reasoning behind a specific commit
entire rewind                    # Interactive rewind to any checkpoint
```

**Note**: If `uv run pre-commit install` overwrites git hooks, re-run `entire enable` to restore.

## Skills

| Skill | Trigger | What It Does |
|-------|---------|--------------|
| `/feature` | New feature implementation | Doer+critic team workflow: plan, implement (TDD), docs, PR |
| `/review-pr` | PR feedback loop | Fetch all reviewer comments, triage, fix with TDD, iterate |
| `/debug-viz` | Viz bugs | Debug missing edges, scope issues, layout problems |
| `/red-team` | Stress testing | Map capabilities x facets, spawn attack agents |
| `/test-matrix-analysis` | Coverage gaps | N-dimensional test matrix, gap analysis |
| `/update-docs` | Sync docs with code | Detect changes, update docs/, README |
| `/code-smells` | Design review | Surface code smells, SOLID violations, flat-code issues |

## Guardrails

- **Branch protection**: `master` requires PR review + all CI checks before merge
- **Auto-format**: ruff runs on every file edit (hook), so lint errors are fixed automatically
- **Pre-commit**: ruff check + ruff format + nbstripout run on every commit
- **CI**: lint + test matrix (Python 3.10-3.13) on every push/PR to master
- **Build-time validation**: Graph() catches structural errors at construction, not runtime
- **Tests**: `uv run pytest` must pass before any PR — the `/feature` skill enforces this
