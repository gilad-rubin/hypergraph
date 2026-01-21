# Project State

## Current Position

Phase: PIVOT — Revised approach after analysis
Branch: `fix-viz-edge-routing-v2`
Status: Planning revised approach
Last activity: 2026-01-21 — Analyzed three branches, identified path forward

Progress: Replanning

## Project Reference

See: .planning/PROJECT.md (updated 2026-01-21)

**Core value:** Pure functions connect automatically with build-time validation
**Current focus:** Fix visualization edge routing

## Critical Discovery (2026-01-21)

### Three Branches Analysis

| Branch | complex_rag | Nested graphs | Code quality |
|--------|-------------|---------------|--------------|
| **b111b07** (good commit) | ✅ Good | ❌ Bad | Original |
| **add-js-viz** | ❌ Ruined | ✅ Good | Original |
| **fix-viz-edge-routing** (GSD) | ❌ Bad | ❌ Bad | ✅ Refactored (NetworkX) |

### Root Cause Analysis

**add-js-viz fixed nested graphs by:**
- Python computes `innerTargetsHierarchy` — explicit mapping of where edges should route
- JavaScript reroutes edges after layout using this explicit info
- Height-based separation in `createRowConstraints`
- Reduced spacing: `spaceY: 50` (was 140), `layerSpaceY: 60` (was 120)

**add-js-viz broke complex_rag because:**
- The spacing changes (height-based + reduced values) made nodes closer
- Original edge routing algorithm wasn't designed for tighter spacing
- Edges go over nodes when vertical space is reduced

**GSD refactoring:**
- Clean NetworkX-only renderer (good)
- JavaScript builds hierarchy via `buildHierarchy()` (inference approach)
- `resolveEdgeTargets()` tries to find entry/exit nodes dynamically
- But inference doesn't match what Python explicitly computed
- Missing the `innerTargetsHierarchy` that add-js-viz relies on

### Path Forward

**Strategy:** Keep GSD's refactored code + port add-js-viz's nested graph fixes WITHOUT the spacing changes

1. Keep NetworkX-only renderer from GSD
2. Add back `innerTargetsHierarchy` computation to renderer
3. Port the innerTargets rerouting logic to JS
4. DO NOT take the spacing changes (keep original `spaceY: 140`)

### Reference Commits

- `b111b07` — Known-good for complex_rag
- `add-js-viz` branch — Has nested graph fixes (but with spacing changes that break complex_rag)
- `fix-viz-edge-routing` — Has GSD refactoring work (NetworkX-only, test infrastructure)

## Preserved Work from GSD

### Worth Keeping
- NetworkX-only renderer (`to_viz_graph()`, renderer consumes `nx.DiGraph`)
- Traversal utilities (`traversal.py`)
- Coordinate utilities (`coordinates.py`)
- Test infrastructure (Playwright, Shapely, visual regression)
- Blocking detection fix (`i <= target.row`, skip target node)

### Needs Revision
- `buildHierarchy()` / `resolveEdgeTargets()` — Replace with explicit innerTargets from Python
- Two-phase layout approach — May need adjustment

## Previous GSD Decisions (for reference)

| Decision | Phase | Context |
|----------|-------|---------|
| Renderer operates on pure NetworkX DiGraph | 01-02 | Eliminates domain dependencies, reads from attributes |
| Include target row in blocking detection | 03-02 | Changed i < target.row to i <= target.row |
| Skip target node in blocking checks | 03-02 | Target not considered blocking obstacle or in bounds |
| Use Shapely for geometric verification | 04-01 | Industry standard intersection detection |
| Use Pillow for screenshot comparison | 04-02 | Pixel-by-pixel visual regression testing |

## Blockers

(None)

## Session Continuity

Last session: 2026-01-21
Stopped at: Analysis complete, ready to implement revised approach
Resume file: None

---
*State initialized: 2026-01-21*
*Last updated: 2026-01-21 (pivot after branch analysis)*
