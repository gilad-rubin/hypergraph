# Milestones

## Completed

### v1.0 — Core Framework (Pre-GSD)

**Completed:** Prior to 2026-01-21 (before GSD tracking)
**Phases:** N/A (not tracked)

**Delivered:**
- `@node` decorator for wrapping functions (sync, async, generators)
- `Graph` construction with automatic edge inference
- `InputSpec` categorization (required, optional, bound, internal)
- Rename API (`.with_inputs()`, `.with_outputs()`, `.with_name()`)
- Hierarchical composition (`.as_node()`, `.map_over()`)
- Build-time validation with helpful error messages
- `SyncRunner` for sequential execution
- `AsyncRunner` with concurrency control
- Batch processing with `runner.map()`
- `@route` for conditional routing with `END` sentinel
- `@ifelse` for binary boolean routing
- Cyclic graphs for agentic loops
- React Flow visualization (initial implementation)

## In Progress

### v1.1 — Fix Visualization Edge Routing

**Started:** 2026-01-21
**Phases:** 1 (starting at Phase 1)

**Goal:** Restore correct edge routing for complex and nested graphs.

**Target:**
- Fix edge routing regression (edges going over nodes)
- Fix collapsed nested graph edge gap
- Fix deeply nested graph edge routing
- Unified algorithm for all nesting depths

---
*Milestones initialized: 2026-01-21*
