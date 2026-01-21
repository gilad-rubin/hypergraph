# Roadmap: Hypergraph v1.1

**Milestone:** v1.1 Fix Visualization Edge Routing
**Created:** 2026-01-21
**Updated:** 2026-01-21 after research
**Phases:** 4

## Overview

Refactor visualization code to add missing abstractions, then fix edge routing. The "refactor first" approach prevents future regressions by eliminating the code smells that caused the original regression.

## Phase 1: Add Core Abstractions

**Goal:** Eliminate manual depth tracking and coordinate arithmetic with reusable abstractions.

**Requirements covered:** REFAC-01, REFAC-02

**Approach:**
1. Create `traverse_to_leaves(node, predicate)` — recursive traversal that handles depth automatically
2. Create `CoordinateSpace` class — explicit transforms between layout, parent-relative, absolute, and React Flow spaces
3. Add characterization tests to document current behavior before refactoring
4. Refactor existing code to use new abstractions

**Success criteria:**
1. No manual `remaining_depth` or `depth` parameter passing in viz code
2. All coordinate transforms go through `CoordinateSpace` methods
3. Existing tests still pass (behavior unchanged)

**Key files:**
- `src/hypergraph/viz/renderer.py` — hierarchy traversal
- `src/hypergraph/viz/assets/layout.js` — coordinate transforms

---

## Phase 2: Unify Edge Routing Logic

**Goal:** Single source of truth for edge routing decisions (eliminate Python/JS duplication).

**Requirements covered:** REFAC-03

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
- `src/hypergraph/viz/renderer.py` — remove routing logic
- `src/hypergraph/viz/assets/layout.js` — owns all routing decisions

---

## Phase 3: Fix Edge Routing Bugs

**Goal:** Using new abstractions, fix all edge routing issues.

**Requirements covered:** EDGE-01, EDGE-02, EDGE-03, EDGE-04

**Approach:**
1. Fix edges going over nodes (regression from recent commits)
2. Fix gap between edges and collapsed nested graph boundaries
3. Fix deeply nested graph edge routing (2+ levels)
4. Verify unified algorithm works for arbitrary nesting depth

**Success criteria:**
1. `complex_rag` renders correctly (no edges over nodes)
2. Collapsed nested graphs have edges flush to boundary
3. Double-nested graphs route edges to correct inner nodes
4. Adding triple-nesting works without code changes

**Key files:**
- `src/hypergraph/viz/assets/constraint-layout.js` — edge routing
- `src/hypergraph/viz/assets/layout.js` — node positioning

---

## Phase 4: Verification & Testing

**Goal:** Automated verification that edge routing is correct.

**Requirements covered:** VERIFY-01, VERIFY-02, VERIFY-03, TEST-01, TEST-02, TEST-03, TEST-04

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
- `tests/viz/conftest.py` — Playwright fixtures

---

## Milestone Summary

| Phase | Name | Requirements | Success Criteria |
|-------|------|--------------|------------------|
| 1 | Add Core Abstractions | REFAC-01, REFAC-02 | 3 criteria |
| 2 | Unify Edge Routing Logic | REFAC-03 | 3 criteria |
| 3 | Fix Edge Routing Bugs | EDGE-01 to EDGE-04 | 4 criteria |
| 4 | Verification & Testing | VERIFY-01 to VERIFY-03, TEST-01 to TEST-04 | 3 criteria |

**Total:** 14 requirements, 4 phases

**Phase ordering rationale:**
- Phase 1 establishes abstractions needed for clean fixes
- Phase 2 eliminates duplication so fixes propagate
- Phase 3 fixes bugs using new abstractions
- Phase 4 verifies and prevents future regressions

---
*Roadmap created: 2026-01-21*
*Updated: 2026-01-21 after research*
