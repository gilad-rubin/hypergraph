---
phase: 03-unify-edge-routing
plan: 01
subsystem: viz
tags: [javascript, coordinate-transform, layout, edge-routing, nested-graphs]

# Dependency graph
requires:
  - phase: 02-unify-edge-routing
    provides: JavaScript edge resolution with hierarchy building
provides:
  - CoordinateTransform utility with 4 coordinate spaces
  - Absolute position tracking in performRecursiveLayout
  - Parent-to-child coordinate conversion functions
affects: [04-use-absolute-positions]

# Tech tracking
tech-stack:
  added: []
  patterns: [coordinate-space-separation, layout-to-absolute-transform]

key-files:
  created: []
  modified:
    - src/hypergraph/viz/assets/layout.js

key-decisions:
  - "Use frozen 4-space coordinate model: Layout, Parent-Relative, Absolute, React Flow"
  - "Track both parent-relative (for React Flow) and absolute (for edge routing) positions"
  - "CoordinateTransform owns all space conversions with explicit functions"

patterns-established:
  - "CoordinateTransform.layoutToParentRelative(): center-based to top-left conversion"
  - "CoordinateTransform.parentRelativeToAbsolute(): child-to-viewport transformation"
  - "CoordinateTransform.getAbsolutePosition(): recursive parent traversal for absolute coords"

# Metrics
duration: 2min
completed: 2026-01-21
---

# Phase 3 Plan 01: Coordinate Transformation System

**CoordinateTransform utility with 4-space model (Layout/Parent-Relative/Absolute/React Flow) and absolute position tracking in performRecursiveLayout**

## Performance

- **Duration:** 2 min
- **Started:** 2026-01-21T14:29:56Z
- **Completed:** 2026-01-21T14:32:37Z
- **Tasks:** 3
- **Files modified:** 1

## Accomplishments
- Created CoordinateTransform object with explicit space conversion functions
- Added absolutePositions Map tracking to performRecursiveLayout
- Integrated transformations for root and child nodes without changing React Flow positions
- All 52 existing visualization tests pass without modification

## Task Commits

Each task was committed atomically:

1. **Task 1: Add CoordinateTransform object** - `002bfad` (feat)
2. **Task 2: Track absolute positions in performRecursiveLayout** - `1e62092` (feat)
3. **Task 3: Verify existing tests still pass** - No commit (verification only)

## Files Created/Modified
- `src/hypergraph/viz/assets/layout.js` - Added CoordinateTransform object and absolutePositions tracking

## Decisions Made

**Coordinate space definitions:**
- Layout Space: Centers with 50px padding (constraint solver output)
- Parent-Relative Space: Top-left relative to parent content area (React Flow)
- Absolute Viewport Space: Top-left relative to viewport (edge routing)
- React Flow Space: DOM coordinates with zoom/pan (not yet used)

**Implementation approach:**
- Keep existing React Flow positions unchanged (parent-relative)
- Add separate absolutePositions Map for edge routing
- Use CoordinateTransform functions explicitly rather than inline math
- Return absolutePositions in performRecursiveLayout result

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None. Changes were purely additive - new tracking alongside existing position logic.

## Next Phase Readiness

- CoordinateTransform functions ready for edge routing
- absolutePositions Map available in layout result
- React Flow rendering unchanged (backwards compatible)
- Ready for Phase 3 Plan 02: Use absolute positions in edge routing

---
*Phase: 03-unify-edge-routing*
*Completed: 2026-01-21*
