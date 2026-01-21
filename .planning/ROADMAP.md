# Roadmap: Hypergraph v1.1

**Milestone:** v1.1 Fix Visualization Edge Routing
**Created:** 2026-01-21
**Phases:** 1

## Overview

Single focused phase to fix edge routing regression and implement unified algorithm for nested graphs.

## Phase 1: Fix Edge Routing

**Goal:** Restore correct edge routing for all graph types with a unified algorithm.

**Requirements covered:** EDGE-01, EDGE-02, EDGE-03, EDGE-04, TEST-01, TEST-02, TEST-03, TEST-04

**Approach:**
1. Revert viz code to known-good commit (`b111b075`)
2. Analyze subsequent commits to understand intended nested graph fixes
3. Design unified edge routing algorithm that handles arbitrary nesting depth
4. Implement the algorithm (replace special-cased logic)
5. Verify all test cases pass

**Success criteria:**
1. `complex_rag` renders with no edges crossing nodes
2. Collapsed nested graph shows edges connecting flush to node boundary
3. Expanded nested graph routes edges to correct inner nodes
4. Double nested graph (2+ levels) routes edges correctly at all levels
5. No code duplication for different nesting depths

**Verification approach:**
- Python scripts that extract node/edge coordinates from rendered output
- Automated geometric tests: edge paths don't intersect node bounding boxes
- Browser automation (Playwright) for screenshot comparison if needed
- Subagents run verification tests and report pass/fail objectively

**Key files:**
- `src/hypergraph/viz/renderer.py`
- `src/hypergraph/viz/html_generator.py`
- `src/hypergraph/viz/assets/constraint-layout.js`
- `src/hypergraph/viz/assets/layout.js`
- `tests/viz/test_renderer.py`
- `tests/viz/test_edge_routing.py`

**Risks:**
- Edge cases in deeply nested graphs may require iteration
- JavaScript layout engine changes need careful testing

## Milestone Summary

| Phase | Name | Requirements | Success Criteria |
|-------|------|--------------|------------------|
| 1 | Fix Edge Routing | EDGE-01 to EDGE-04, TEST-01 to TEST-04 | 5 criteria |

**Total:** 8 requirements, 1 phase

---
*Roadmap created: 2026-01-21*
