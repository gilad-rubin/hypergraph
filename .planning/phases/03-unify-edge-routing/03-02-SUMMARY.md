---
phase: 03-unify-edge-routing
plan: 02
subsystem: viz
tags: [javascript, edge-routing, blocking-detection, absolute-coordinates, nested-graphs]

# Dependency graph
requires:
  - phase: 03-unify-edge-routing
    plan: 01
    provides: CoordinateTransform and absolutePositions tracking
provides:
  - Fixed target row blocking detection in constraint-layout.js
  - Absolute position storage in edge data (_sourceAbsPos, _targetAbsPos)
affects: [04-use-absolute-positions]

# Tech tracking
tech-stack:
  added: []
  patterns: [absolute-coordinate-edge-routing, inclusive-blocking-detection]

key-files:
  created: []
  modified:
    - src/hypergraph/viz/assets/constraint-layout.js
    - src/hypergraph/viz/assets/layout.js

key-decisions:
  - "Include target row in blocking detection (i <= target.row)"
  - "Skip target node itself in blocking checks and bounds calculation"
  - "Store absolute positions in edge data for routing algorithm"

patterns-established:
  - "Blocking detection: check rows from source.row+1 through target.row inclusive"
  - "Edge data augmentation: _sourceAbsPos/_targetAbsPos from absolutePositions Map"

# Metrics
duration: 1min
completed: 2026-01-21
---

# Phase 3 Plan 02: Fix Edge Routing Algorithm

**Fixed critical blocking detection bug (missed target row nodes) and added absolute coordinate storage to edge data**

## Performance

- **Duration:** 1 min
- **Started:** 2026-01-21T14:37:03Z
- **Completed:** 2026-01-21T14:38:03Z
- **Tasks:** 3
- **Files modified:** 2

## Accomplishments
- Fixed blocking detection to include target row (changed `i < target.row` to `i <= target.row`)
- Added target node skip logic in both blocking detection and bounds calculation
- Stored absolute positions in edge data for both root and child edges
- All 52 visualization tests pass

## Task Commits

Each task was committed atomically:

1. **Task 1: Fix target row blocking detection** - `092365f` (fix)
2. **Task 2: Store absolute positions in edge data** - `52ee7a2` (feat)
3. **Task 3: Run comprehensive viz tests** - `685729e` (test)

## Files Created/Modified
- `src/hypergraph/viz/assets/constraint-layout.js` - Fixed blocking detection loop to include target row, added target node skip
- `src/hypergraph/viz/assets/layout.js` - Added _sourceAbsPos/_targetAbsPos to edge data for routing

## Decisions Made

**Blocking detection fix:**
- Changed loop condition from `i < target.row` to `i <= target.row` to detect nodes in target's row
- Added `if (node === target) continue;` to skip target node itself in two places:
  - Blocking detection loop (don't consider target as blocking obstacle)
  - Node bounds calculation (don't include target in corridor bounds)

**Absolute position storage:**
- Store `_sourceAbsPos` and `_targetAbsPos` in edge data during layout
- Pull from `absolutePositions.get(e.source/target)`
- Apply to both root edges and child edges (nested graphs)
- Enables future edge routing to use absolute coordinates instead of parent-relative

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None. Both fixes were straightforward:
- The blocking detection bug was identified in research phase
- Absolute position storage was a simple data augmentation

## Next Phase Readiness

- Critical blocking detection bug fixed (edges won't go over nodes in target row)
- Edge data now contains absolute positions for routing algorithm
- All existing tests pass (backwards compatible)
- Ready for Phase 4: Use absolute positions in edge routing algorithm

---
*Phase: 03-unify-edge-routing*
*Completed: 2026-01-21*
