# Project State

## Current Position

Phase: 4 of 4 (Verification & Testing)
Plan: 1 of 2 complete
Status: In progress
Last activity: 2026-01-21 — Completed 04-01-PLAN.md

Progress: ████████░░░░ 67% (8/12 plans complete)

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
| Object reference hierarchy building | 02-01 | O(n) complexity, two-phase map creation then linking |
| Topological entry/exit detection | 02-01 | Identifies edge connection points from sibling edges |
| Recursive resolution with depth limit | 02-01 | Max depth 10 prevents infinite loops in expansion |
| Resolve edges after layout completes | 02-02 | Layout logic stays pure, resolution as final step |
| Store resolved targets as edge properties | 02-02 | _resolvedSource/_resolvedTarget for visual endpoints |
| Python provides logical structure | 02-02 | JavaScript resolves visual targets based on expansion |
| Use frozen 4-space coordinate model | 03-01 | Layout, Parent-Relative, Absolute, React Flow spaces |
| Track both parent-relative and absolute positions | 03-01 | React Flow uses relative, edge routing uses absolute |
| CoordinateTransform owns all space conversions | 03-01 | Explicit functions for each transformation type |
| Include target row in blocking detection | 03-02 | Changed i < target.row to i <= target.row |
| Skip target node in blocking checks | 03-02 | Target not considered blocking obstacle or in bounds |
| Store absolute positions in edge data | 03-02 | _sourceAbsPos/_targetAbsPos for routing algorithm |
| Use Shapely for geometric verification | 04-01 | Industry standard, proven reliable intersection detection |
| Extract coordinates via JavaScript DOM APIs | 04-01 | Actual rendered positions, not predicted |
| Parse SVG paths with bezier sampling | 04-01 | Sample cubic bezier curves at 10 points per segment |
| Extract edge IDs from data-testid | 04-01 | React Flow format: rf__edge-{id} |

### Technical Notes

- Known-good commit: `b111b075a6385d23ce0e3a85b8d55662a8fcd9d0`
- Test to validate: `complex_rag` in `test_viz_layout`
- Problem: Edge routing breaks with nested graphs, edges go over nodes
- Node classification: hasattr('graph') -> PIPELINE, hasattr('targets') -> BRANCH
- Coordinate spaces: local -> parent -> absolute -> viewport
- Current renderer behavior documented: 29 characterization tests as refactoring baseline
- Branch nodes store 'targets' list (not when_true/when_false), depth>0 expands all pipelines
- Phase 2 approach: JavaScript builds hierarchy from flat nodes, resolves edge targets dynamically
- Edge resolution flow: logical edges → layout → resolve visual targets → render
- Resolved edge properties: _resolvedSource, _resolvedTarget, _logicalSource, _logicalTarget
- Coordinate spaces defined: Layout (centers+50px), Parent-Relative (top-left), Absolute (viewport), React Flow (DOM)
- absolutePositions Map available in performRecursiveLayout result for edge routing
- Blocking detection fixed: includes target row, skips target node in both checks and bounds
- Edge data augmented: _sourceAbsPos/_targetAbsPos stored from absolutePositions Map
- Geometric verification tests detect 12 edge-node intersections in complex_rag graph
- Test infrastructure: Playwright browser automation, Shapely geometric analysis
- Edge ID format: data-testid="rf__edge-{id}" in React Flow DOM

### Blockers

(None)

## Session Continuity

Last session: 2026-01-21T14:56:12Z
Stopped at: Completed 04-01-PLAN.md
Resume file: None

---
*State initialized: 2026-01-21*
*Last updated: 2026-01-21T14:56:12Z*
