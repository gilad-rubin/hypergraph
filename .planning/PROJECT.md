# Hypergraph

## What This Is

A unified Python framework for workflow orchestration — DAG pipelines, agentic workflows, and everything in between. Automatic edge inference from output/input names, hierarchical graph composition, sync/async runners, and React Flow visualization.

## Core Value

Pure functions connect automatically. Write `@node` functions with named outputs, hypergraph wires them together. Build-time validation catches errors before runtime.

## Current Milestone: v1.1 Fix Visualization Edge Routing

**Goal:** Restore correct edge routing for complex and nested graphs by reverting to known-good state and re-implementing nested graph fixes properly.

**Target features:**
- Fix edge routing regression (edges going over nodes)
- Fix collapsed nested graph edge gap
- Fix deeply nested graph edge routing (2+ levels)
- Unified algorithm that works regardless of nesting depth

## Requirements

### Validated

<!-- Shipped and working. -->

- `@node` decorator for wrapping functions (sync, async, generators)
- `Graph` construction with automatic edge inference
- `InputSpec` categorization (required, optional, bound, internal)
- Rename API (`.with_inputs()`, `.with_outputs()`, `.with_name()`)
- Hierarchical composition (`.as_node()`, `.map_over()`)
- Build-time validation with helpful error messages
- `SyncRunner` for sequential execution
- `AsyncRunner` with concurrency control (`max_concurrency`)
- Batch processing with `runner.map()` (zip and product modes)
- `@route` for conditional routing with `END` sentinel
- `@ifelse` for binary boolean routing
- Cyclic graphs for agentic loops
- React Flow visualization (basic rendering)

### Active

<!-- Current scope. Building toward these. -->

- [ ] Edge routing works for complex graphs (no edges over nodes)
- [ ] Collapsed nested graphs connect edges flush to node boundary
- [ ] Deeply nested graphs (2+ levels) route edges correctly
- [ ] Single unified edge routing algorithm for all nesting depths

### Out of Scope

<!-- Explicit boundaries. -->

- Checkpointing and durability — future milestone
- Event streaming (`.iter()`) — future milestone
- `InterruptNode` for human-in-the-loop — future milestone
- Observability hooks — future milestone

## Context

**Regression source:** Edge routing broke after commit `b111b075a6385d23ce0e3a85b8d55662a8fcd9d0`. That commit works correctly for `complex_rag` in `test_viz_layout`. Subsequent commits attempted to fix nested graph edge routing but introduced regressions.

**Strategy:** Revert visualization code to known-good state (`b111b075`), understand what the subsequent fixes were trying to achieve, and re-implement them with a unified approach that handles arbitrary nesting depth.

**Code smell indicator:** The fix for single-level nesting didn't generalize to double nesting — suggests duplicated or special-cased logic that needs to be replaced with a recursive/general algorithm.

## Constraints

- **Compatibility**: Must work with existing graph API (no breaking changes)
- **Test coverage**: `test_viz_layout` tests must pass, especially `complex_rag`
- **Browser support**: React Flow visualization in modern browsers

## Key Decisions

<!-- Decisions that constrain future work. -->

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Revert to b111b075 for viz | Known working state for complex graphs | — Pending |
| Re-implement nested fixes | Original fixes had code duplication | — Pending |

---
*Last updated: 2026-01-21 after milestone initialization*
