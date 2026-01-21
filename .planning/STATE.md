# Project State

## Current Position

Phase: 2 of 4 (Unify Edge Routing Logic)
Plan: 0 of 2 complete
Status: Phase 2 planned, ready to execute
Last activity: 2026-01-21 — Created Phase 2 plans

Progress: █████░░░░░░░ 25% (3/12 plans complete)

## Project Reference

See: .planning/PROJECT.md (updated 2026-01-21)

**Core value:** Pure functions connect automatically with build-time validation
**Current focus:** Fix visualization edge routing

## Accumulated Context

### Decisions Made

| Decision | Phase | Context |
|----------|-------|---------|
| Revert viz to commit `b111b075` as starting point | Research | Known working state before nested graph issues |
| Re-implement nested graph fixes with unified algorithm | Roadmap | Avoid per-graph-type conditionals |
| Use duck typing for node classification | 01-01 | Avoid import dependencies, use hasattr checks |
| Frozen dataclasses for coordinates | 01-01 | Ensure immutability during transformations |
| Recursive flattening to single NetworkX graph | 01-01 | Parent references instead of separate graph objects |
| Renderer operates on pure NetworkX DiGraph | 01-02 | Eliminates domain dependencies, reads from attributes |
| Conversion at widget boundary | 01-02 | graph.to_viz_graph() before render_graph() |
| Use characterization tests before refactoring | 01-03 | Document current behavior for safety net |
| Assert on structure not positions | 01-03 | Node types, edges, hierarchy - not coordinates |
| JavaScript owns edge routing | 02-planning | Single source of truth, Python provides flat structure |

### Technical Notes

- Known-good commit: `b111b075a6385d23ce0e3a85b8d55662a8fcd9d0`
- Test to validate: `complex_rag` in `test_viz_layout`
- Problem: Edge routing breaks with nested graphs, edges go over nodes
- Node classification: hasattr('graph') -> PIPELINE, hasattr('targets') -> BRANCH
- Coordinate spaces: local -> parent -> absolute -> viewport
- Current renderer behavior documented: 29 characterization tests as refactoring baseline
- Branch nodes store 'targets' list (not when_true/when_false), depth>0 expands all pipelines
- Phase 2 approach: JavaScript builds hierarchy from flat nodes, resolves edge targets dynamically

### Blockers

(None)

## Session Continuity

Last session: 2026-01-21
Stopped at: Created Phase 2 plans (02-01, 02-02)
Resume file: None

---
*State initialized: 2026-01-21*
*Last updated: 2026-01-21*
