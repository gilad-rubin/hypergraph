# Roadmap: Hypergraph v1.1

**Milestone:** v1.1 Fix Visualization Edge Routing
**Created:** 2026-01-21
**Updated:** 2026-01-21 (PIVOT — revised approach after branch analysis)
**Phases:** 5 (original 4 + 1 integration phase)

## Overview

Fix edge routing to work correctly for BOTH complex flat graphs (like `complex_rag`) AND nested graphs.

**Key insight:** Two separate branches solved different parts of the problem:
- `b111b07` — complex_rag works, nested graphs broken
- `add-js-viz` — nested graphs work, complex_rag broken (due to spacing changes)
- `fix-viz-edge-routing` (GSD) — clean refactoring, but neither works

**Strategy:** Combine the best of both approaches:
1. Keep GSD's NetworkX-only refactoring
2. Port add-js-viz's nested graph fixes (innerTargetsHierarchy)
3. Do NOT port add-js-viz's spacing changes (they break complex_rag)

## Reference Branches

| Branch | complex_rag | Nested | Code | Key Changes |
|--------|-------------|--------|------|-------------|
| `b111b07` | ✅ | ❌ | Original | Known-good baseline |
| `add-js-viz` | ❌ | ✅ | Original | innerTargets + spacing changes |
| `fix-viz-edge-routing` | ❌ | ❌ | ✅ Refactored | NetworkX-only, buildHierarchy |

---

## Phase 1-4: Original GSD Work (COMPLETE but needs integration)

Phases 1-4 completed significant refactoring work that should be preserved:

**Phase 1 (Complete):** NetworkX-only renderer, `to_viz_graph()`, traversal utilities
**Phase 2 (Complete):** JavaScript hierarchy building (`buildHierarchy`, `resolveEdgeTargets`)
**Phase 3 (Complete):** Blocking detection fix (`i <= target.row`, skip target)
**Phase 4 (Complete):** Test infrastructure (Playwright, Shapely, visual regression)

**Problem:** The JavaScript inference approach (`resolveEdgeTargets`) doesn't produce the same results as Python's explicit `innerTargetsHierarchy` computation. This is why nested graphs still don't work.

---

## Phase 5: Integration — Port innerTargets to Refactored Code

**Goal:** Make both complex_rag AND nested graphs work with the refactored codebase.

**Status:** Planning

### Step 5.1: Verify Hypothesis

Before porting, confirm that spacing is the issue:

```bash
# On add-js-viz branch:
# 1. Revert spacing changes (restore spaceY: 140, remove height-based separation)
# 2. Test complex_rag — should work now
# 3. Test nested graphs — should still work (innerTargets logic is separate)
```

If this works, we know:
- Spacing changes broke complex_rag
- innerTargets logic is what fixes nested graphs
- We can port innerTargets without spacing changes

### Step 5.2: Port innerTargetsHierarchy to NetworkX Renderer

Add `innerTargetsHierarchy` computation back to the refactored renderer:

**File:** `src/hypergraph/viz/renderer.py`

The add-js-viz branch computes this in Python:
- `_find_deepest_in_hierarchy()` — traverse hierarchy to find leaf nodes
- Edges get `innerTargets` and `innerTargetsHierarchy` in their data
- This tells JavaScript exactly where to route edges

Port this logic to work with the NetworkX-based renderer.

### Step 5.3: Port innerTargets Rerouting to JavaScript

Add the edge rerouting logic from add-js-viz:

**File:** `src/hypergraph/viz/assets/layout.js`

Key functions from add-js-viz:
- `rootEdgesToReroute` array — collect edges that need rerouting
- Post-layout rerouting — after children are positioned, reroute edges to inner nodes
- `findVisibleTarget()` — traverse hierarchy based on expansion state

This can potentially replace or work alongside the GSD `resolveEdgeTargets` approach.

### Step 5.4: Verify All Graph Types

Test all scenarios:
1. `complex_rag` — flat graph with multiple edges
2. Nested collapsed — edges to collapsed pipeline boundary
3. Nested expanded — edges to inner nodes
4. Double-nested — edges through multiple levels

Use existing test infrastructure from Phase 4.

### Success Criteria

1. `complex_rag` renders correctly (no edges over nodes)
2. Nested collapsed graphs connect edges flush to boundary
3. Nested expanded graphs route edges to correct inner nodes
4. Double-nested graphs work without special-casing
5. All existing tests pass
6. NetworkX-only renderer architecture preserved

---

## Key Files

**Python (renderer):**
- `src/hypergraph/viz/renderer.py` — add innerTargetsHierarchy computation

**JavaScript (layout):**
- `src/hypergraph/viz/assets/layout.js` — add edge rerouting logic
- `src/hypergraph/viz/assets/constraint-layout.js` — keep original spacing

**Tests:**
- `tests/viz/test_edge_routing.py` — geometric verification
- `tests/viz/test_visual_regression.py` — screenshot comparison

---

## Reference: add-js-viz Key Changes

For porting, these are the relevant changes from add-js-viz:

**renderer.py additions:**
```python
def _find_deepest_in_hierarchy(hierarchy, depth):
    """Find deepest nodes in hierarchy up to given depth."""
    ...

# In edge creation:
edge_data = {
    "innerTargets": inner_targets,
    "innerTargetsHierarchy": inner_targets_hierarchy,
    "innerSources": inner_sources,
    "innerSourcesHierarchy": inner_sources_hierarchy,
}
```

**layout.js additions:**
```javascript
var rootEdgesToReroute = [];
// ... collect edges with innerTargets ...

// After positioning children:
rootEdgesToReroute.forEach(function(edgeInfo) {
    // Reroute to actual inner node positions
});
```

**constraint-layout.js changes to AVOID:**
```javascript
// DON'T port these:
spaceY: 50,  // Keep 140
layerSpaceY: 60,  // Keep 120
// Height-based separation — don't port
```

---

## Milestone Summary

| Phase | Name | Status | Outcome |
|-------|------|--------|---------|
| 1 | Add Core Abstractions | ✅ Complete | NetworkX-only renderer |
| 2 | Unify Edge Routing Logic | ✅ Complete | buildHierarchy (needs revision) |
| 3 | Fix Edge Routing Bugs | ✅ Complete | Blocking detection fix |
| 4 | Verification & Testing | ✅ Complete | Test infrastructure |
| 5 | Integration | 🔄 Planning | Port innerTargets approach |

---
*Roadmap created: 2026-01-21*
*Updated: 2026-01-21 (pivot after branch analysis)*
