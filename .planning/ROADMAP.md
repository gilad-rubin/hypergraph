# Roadmap: Hypergraph v1.1

**Milestone:** v1.1 Fix Visualization Edge Routing
**Created:** 2026-01-21
**Updated:** 2026-01-21 after planning Phase 4
**Phases:** 4

## Overview

Refactor visualization code to add missing abstractions, then fix edge routing. The "refactor first" approach prevents future regressions by eliminating the code smells that caused the original regression.

## Reference Codebase

The hypergraph viz was derived from **hypernodes**. Use as reference for understanding original design:

```
/Users/giladrubin/python_workspace/hypernodes/src/hypernodes/viz/
├── js/
│   ├── html_generator.py  (112KB - embedded JS)
│   └── renderer.py
├── assets/
│   ├── state_utils.js
│   └── theme_utils.js
└── graph_walker.py
```

## Phase 1: Add Core Abstractions (COMPLETE)

**Goal:** Decouple viz from hypergraph types and add reusable abstractions for hierarchy and coordinates.

**Requirements covered:** REFAC-01, REFAC-02, REFAC-03, REFAC-04

**Plans:** 3 plans (complete)

Plans:
- [x] 01-01-PLAN.md — Foundation abstractions (to_viz_graph, traversal, coordinates)
- [x] 01-02-PLAN.md — Refactor renderer to consume NetworkX only
- [x] 01-03-PLAN.md — Characterization tests for safety net

**Approach:**
1. **Decouple viz from hypergraph types** — renderer takes NetworkX graph only, not `Graph` object
   - Add `Graph.to_viz_graph()` that returns flattened NetworkX with all viz-needed attrs
   - Include in node attrs: `node_type`, `inputs`, `outputs`, `input_types`, `output_types`, `defaults`, `parent`, `branch_data`
   - Include in graph attrs: `input_spec` (required, optional, **bound** — used for viz logic)
   - Renderer reads attrs only, no `isinstance()` checks
2. Create `traverse_to_leaves(node, predicate)` — recursive traversal that handles depth automatically
3. Create `CoordinateSpace` class — explicit transforms between layout, parent-relative, absolute, and React Flow spaces
4. Add characterization tests to document current behavior before refactoring

**Success criteria:**
1. `render_graph()` takes `nx.DiGraph`, not `Graph`
2. No `isinstance(hypernode, GraphNode)` in viz code
3. InputSpec (including bound params) accessible from NetworkX graph attrs
4. No manual `remaining_depth` or `depth` parameter passing
5. All coordinate transforms go through `CoordinateSpace` methods
6. Existing tests still pass (behavior unchanged)

**Key files:**
- `src/hypergraph/graph/core.py` — add `to_viz_graph()` method
- `src/hypergraph/viz/renderer.py` — change to consume NetworkX only
- `src/hypergraph/viz/assets/layout.js` — coordinate transforms
- `src/hypergraph/viz/assets/constraint-layout.js` — edge routing coords
- `src/hypergraph/viz/assets/components.js` — React components
- `src/hypergraph/viz/assets/state_utils.js` — state management
- `src/hypergraph/viz/html_generator.py` — HTML/JS generation

**Reference:** Compare with `hypernodes/src/hypernodes/viz/js/renderer.py` for original design

---

## Phase 2: Unify Edge Routing Logic (COMPLETE)

**Goal:** Single source of truth for edge routing decisions (eliminate Python/JS duplication).

**Requirements covered:** REFAC-05

**Plans:** 2 plans (complete)

Plans:
- [x] 02-01-PLAN.md — Add hierarchy building and edge target resolution to JavaScript
- [x] 02-02-PLAN.md — Integrate edge resolution with rendering and verify dynamic expand/collapse

**Approach:**
1. Decide ownership: JavaScript handles all hierarchy (Python provides full graph structure)
2. Remove `_find_deepest_consumers` / `_find_deepest_producers` from Python
3. JavaScript builds hierarchy from flat node list and makes all routing decisions
4. Edge data carries logical IDs; JavaScript resolves to visual IDs at render time

**Success criteria:**
1. Edge routing logic exists in ONE place (JavaScript)
2. Python renderer doesn't compute `innerTargets` — just provides graph structure
3. Dynamic expand/collapse works without re-rendering from Python

**Key files:**
- `src/hypergraph/viz/renderer.py` — remove routing logic, just provide graph structure
- `src/hypergraph/viz/assets/layout.js` — owns all routing decisions
- `src/hypergraph/viz/assets/constraint-layout.js` — edge path calculation
- `src/hypergraph/viz/html_generator.py` — JS embedding and generation

**Reference:** Compare with `hypernodes/src/hypernodes/viz/js/html_generator.py` for original approach

---

## Phase 3: Fix Edge Routing Bugs (COMPLETE)

**Goal:** Using new abstractions, fix all edge routing issues.

**Requirements covered:** EDGE-01, EDGE-02, EDGE-03, EDGE-04

**Plans:** 2 plans (complete)

Plans:
- [x] 03-01-PLAN.md — Coordinate transformation system and absolute position tracking
- [x] 03-02-PLAN.md — Fix edge routing algorithm using absolute coordinates

**Approach:**
1. Create explicit CoordinateTransform functions to eliminate inline arithmetic
2. Track absolute positions for all nodes regardless of nesting depth
3. Fix target row blocking detection (include target row, skip target node)
4. Verify unified algorithm works for arbitrary nesting depth

**Success criteria:**
1. `complex_rag` renders correctly (no edges over nodes)
2. Collapsed nested graphs have edges flush to boundary
3. Double-nested graphs route edges to correct inner nodes
4. Adding triple-nesting works without code changes

**Key files:**
- `src/hypergraph/viz/assets/constraint-layout.js` — edge routing algorithm
- `src/hypergraph/viz/assets/layout.js` — node positioning and hierarchy
- `src/hypergraph/viz/assets/components.js` — React Flow node components
- `src/hypergraph/viz/assets/app.js` — main application logic
- `src/hypergraph/viz/html_generator.py` — coordinate calculations, centering

**Reference:** `hypernodes/src/hypernodes/viz/assets/` for original JS implementations

---

## Phase 4: Verification & Testing

**Goal:** Automated verification that edge routing is correct.

**Requirements covered:** VERIFY-01, VERIFY-02, VERIFY-03, TEST-01, TEST-02, TEST-03, TEST-04

**Plans:** 3 plans

Plans:
- [ ] 04-01-PLAN.md — Geometric verification tests with Playwright and Shapely
- [ ] 04-02-PLAN.md — Visual regression tests with baseline screenshots
- [ ] 04-03-PLAN.md — CI workflow for automated testing

**Approach:**
1. Create Python script to extract coordinates from rendered output
2. Implement geometric tests: edge paths vs node bounding boxes
3. Add Playwright-based visual regression tests
4. Run all verification against test cases

**Success criteria:**
1. Automated script can detect edge-over-node violations
2. All 4 test cases (complex_rag, collapsed, expanded, double-nested) pass
3. CI catches regressions via screenshot comparison

**Key files:**
- `tests/viz/test_edge_routing.py` — automated verification
- `tests/viz/test_visual_regression.py` — screenshot comparison
- `tests/viz/conftest.py` — Playwright fixtures
- `.github/workflows/viz-tests.yml` — CI workflow

---

## Milestone Summary

| Phase | Name | Requirements | Success Criteria |
|-------|------|--------------|------------------|
| 1 | Add Core Abstractions | REFAC-01 to REFAC-04 | 6 criteria |
| 2 | Unify Edge Routing Logic | REFAC-05 | 3 criteria |
| 3 | Fix Edge Routing Bugs | EDGE-01 to EDGE-04 | 4 criteria |
| 4 | Verification & Testing | VERIFY-01 to VERIFY-03, TEST-01 to TEST-04 | 3 criteria |

**Total:** 16 requirements, 4 phases

**Phase ordering rationale:**
- Phase 1 establishes abstractions needed for clean fixes
- Phase 2 eliminates duplication so fixes propagate
- Phase 3 fixes bugs using new abstractions
- Phase 4 verifies and prevents future regressions

---
*Roadmap created: 2026-01-21*
*Updated: 2026-01-21 after planning Phase 4*
