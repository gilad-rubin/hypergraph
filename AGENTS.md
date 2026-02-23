# Hypergraph

Python workflow orchestration framework (alpha, solo dev). One set of primitives for DAGs, branches, loops, and nested hierarchies.

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
    core.py            #   Graph class (build pipeline, bind/select/unbind)
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
| Documentation guidelines | [docs/CLAUDE.md](docs/CLAUDE.md) |
| Design specs | `specs/reviewed/`, `specs/not_reviewed/` |

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
