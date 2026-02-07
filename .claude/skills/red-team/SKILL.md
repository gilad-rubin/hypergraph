---
name: red-team
description: Stress-test the hypergraph framework by mapping capabilities against facets, spawning agents that build real-world scenarios for each combination to find bugs, inconsistencies, and feature gaps.
---

# Red-Team — Scenario-Based Stress Testing

Map **capabilities** (framework features) against **facets** (cross-cutting perspectives), then spawn agents that build real-world test scenarios for each combination.

Unlike the existing capabilities matrix (combinatorial flag testing), this is **scenario-based** — "can I actually build a RAG pipeline with caching and async?" or "does sync→async require zero code changes?"

## Triggers
- `/red-team` — full matrix
- `/red-team <capability>` — narrow to one capability across all facets

## Workflow

### Phase 0 — Setup

```bash
mkdir -p tests/red_team && touch tests/red_team/__init__.py
```

### Phase 1 — Discovery (single Explore agent)

Spawn one read-only Explore agent that:

1. Reads source code (`src/hypergraph/`), existing tests, docs, and README
2. Reads the capabilities matrix (`tests/capabilities/matrix.py`) to understand existing coverage
3. Produces two lists:

**Capabilities** (framework features to probe):
- Examples: `Runner` (sync/async parity), `Routing` (route/ifelse/END), `Cycles`, `Nesting` (GraphNode), `InterruptNode`, `Caching`, `emit/wait_for`, `Streaming`, `bind/select/rename`, `map_over`, `TypeValidation`, `Events`

**Facets** (cross-cutting concerns/perspectives):
- Examples: `zero-code-change` (sync↔async without changes), `error-propagation` (exceptions across boundaries), `empty-inputs` (None, [], {}), `real-world-pattern` (RAG, ETL, multi-agent), `composability` (combine 3+ features), `state-isolation` (no leaks between runs), `scale` (many nodes, deep nesting), `determinism` (same input → same output)

4. Outputs a **matrix** as JSON:

```json
{
  "capabilities": [{"id": "C1", "name": "Runner", "description": "..."}],
  "facets": [{"id": "F1", "name": "zero-code-change", "description": "..."}],
  "cells": [
    {
      "capability": "C1",
      "facet": "F1",
      "scenario": "Run same graph sync and async with identical results",
      "test_filename": "test_rt_runner_zero_change.py",
      "search_hint": "async sync parity DAG framework",
      "priority": "high"
    }
  ]
}
```

Only include cells where the combination is **meaningful** — not every capability × facet makes sense. Target ~15-25 cells.

If `/red-team <capability>` was used, filter to only that capability's cells.

### Phase 2 — Triage (coordinator)

1. Parse the matrix from the discovery agent
2. Group cells into batches of ~3-4 by related capability (each agent gets a focused theme)
3. Create tasks via TaskCreate for each batch
4. Create a team and spawn up to 6 general-purpose attack agents in parallel, each handling one batch

### Phase 3 — Attack (parallel agents, up to 6)

Each agent receives a batch of matrix cells and for each cell:

1. **Research** (for cells with `search_hint`): Use Perplexity/web search to find real-world workflow patterns that match the scenario — RAG pipelines, ETL flows, multi-agent orchestration, data processing DAGs, multi-turn chat loops
2. **Design**: Create realistic test scenarios inspired by the research + agent knowledge
3. **Implement**: Write tests in `tests/red_team/<test_filename>` following project conventions:
   - Import from public API (`from hypergraph import ...`)
   - Class-based organization, descriptive docstrings
   - 3-5 tests per cell (scenario escalation: basic → combined → stress)
4. **Run**: `uv run pytest tests/red_team/<file> -v`
5. **Classify** each test result:
   - `PASS` — framework handles this correctly
   - `FAIL_BUG` — framework bug (unexpected behavior/error)
   - `FAIL_MISSING` — feature gap (reasonable expectation the framework doesn't meet)
   - `FAIL_INCONSISTENCY` — works one way but not another (e.g., sync works, async doesn't)

### Phase 4 — Consolidation

After all attack agents complete:

1. Run `uv run pytest tests/red_team/ -v --tb=short` to confirm all results
2. Separate tests:
   - **Passing tests** → keep as regression suite
   - **Failing tests (bugs)** → mark with `@pytest.mark.xfail(reason="RT: ...")` so the suite passes, note in report
   - **Test errors** → fix or remove
3. Generate consolidated report (markdown) with:
   - Matrix overview (capability × facet grid showing pass/fail/skip)
   - Bug list sorted by severity
   - Inconsistencies found
   - Feature gap suggestions
   - Recommendations

### Phase 5 — Cleanup

1. Commit: `git add tests/red_team/ && git commit -m "test: red-team scenario tests"`
2. Shut down team, present report
3. Ask user: "Found N bugs and M feature gaps. Want me to fix the bugs?"

## Safety Rules

- Tests in `tests/red_team/` only — **never modify source code or existing tests**
- Each agent writes separate files (no conflicts)
- Always use `uv run pytest` for running tests
- Read existing code and tests for context, but don't change them
