---
phase: 04-verification-testing
plan: 01
subsystem: testing
tags: [playwright, shapely, geometric-testing, browser-automation, edge-routing]

# Dependency graph
requires:
  - phase: 03-unify-edge-routing-logic
    provides: Edge routing implementation in constraint-layout.js
provides:
  - Automated geometric verification tests for edge-node intersections
  - Playwright-based browser testing infrastructure
  - Shapely geometric intersection detection
  - Test fixtures for complex graph structures
affects: [05-fix-implementation, testing]

# Tech tracking
tech-stack:
  added: [shapely, pytest-playwright]
  patterns: [geometric-intersection-testing, playwright-fixtures, DOM-coordinate-extraction]

key-files:
  created:
    - tests/viz/test_edge_routing.py
  modified:
    - tests/viz/conftest.py
    - pyproject.toml

key-decisions:
  - "Use Shapely for geometric intersection detection (industry standard, proven reliable)"
  - "Extract coordinates via JavaScript DOM APIs (actual rendered positions)"
  - "Parse SVG paths with bezier curve sampling (10 samples per cubic bezier segment)"
  - "Extract edge IDs from data-testid attribute (rf__edge-{id} format)"
  - "Mark tests as @pytest.mark.slow (browser automation overhead)"

patterns-established:
  - "Factory fixtures for graph rendering (serve_graph_html, page_with_graph)"
  - "Coordinate extraction via page.evaluate() JavaScript"
  - "Geometric verification with explicit intersection reporting"
  - "Graceful skip if Playwright or Shapely unavailable"

# Metrics
duration: 5min
completed: 2026-01-21
---

# Phase 4 Plan 01: Geometric Verification Tests with Playwright and Shapely Summary

**Automated edge-node intersection detection using Playwright browser automation and Shapely geometric analysis**

## Performance

- **Duration:** 5 min
- **Started:** 2026-01-21T14:51:12Z
- **Completed:** 2026-01-21T14:56:12Z
- **Tasks:** 3
- **Files modified:** 3

## Accomplishments
- Created automated geometric verification tests that detect edge-node intersections
- Set up Playwright browser automation infrastructure for visualization testing
- Implemented coordinate extraction from rendered HTML via JavaScript DOM APIs
- Tests successfully detect 12 edge-node intersections in complex_rag graph

## Task Commits

Each task was committed atomically:

1. **Task 1: Add test dependencies and fixtures** - `3b7ff98` (test)
2. **Task 2 & 3: Create coordinate extraction and tests** - `312a731` (test)

## Files Created/Modified
- `tests/viz/test_edge_routing.py` - Geometric verification tests with Playwright and Shapely
- `tests/viz/conftest.py` - Added complex_rag_graph fixture and Playwright helpers
- `pyproject.toml` - Added shapely and pytest-playwright to dev dependencies

## Decisions Made

1. **Shapely for geometric checks** - Industry standard geometry library with robust intersection detection
2. **DOM coordinate extraction** - Extract actual rendered positions via JavaScript rather than predicted positions
3. **Bezier curve sampling** - Sample cubic bezier curves at 10 points per segment for accurate path representation
4. **Edge ID from data-testid** - React Flow stores edge IDs in data-testid attribute as `rf__edge-{id}`
5. **Graceful skips** - Tests skip automatically if Playwright or Shapely unavailable (not hard dependencies)

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

**Edge ID extraction format discovery**
- Initial attempt used data-id attribute (None)
- Second attempt used className with regex (failed)
- Solution: React Flow uses data-testid with format `rf__edge-{id}`
- Resolved by inspecting actual rendered HTML with Playwright

## Test Results

Tests are working correctly and detecting real issues:

```
test_complex_rag_no_edge_node_intersections: FAILED
  - 12 edge-node intersections detected
  - e___inputs_0___to_chunk crosses search_index
  - e___inputs_1___to_load_data crosses embed_expanded
  - e___inputs_4___to_search_index crosses __inputs_6__ and call_llm
  - e_chunk_fetch_documents crosses __inputs_5__
  - e_normalize_build_index crosses __inputs_5__
  - e_build_index_search_index crosses call_llm
  - e_build_index_search_expanded crosses build_prompt
  - e_embed_query_search_index crosses call_llm
  - e_embed_expanded_search_expanded crosses postprocess
  - e_search_index_merge_results crosses postprocess
```

These failures are EXPECTED and CORRECT - they confirm the tests are detecting the actual bug that Phase 5 will fix.

## Next Phase Readiness

- Test infrastructure complete and validated
- Tests detect the bug (12 intersections in complex_rag)
- Ready for Phase 5 to fix the edge routing implementation
- Tests will verify the fix works when re-run after Phase 5

---
*Phase: 04-verification-testing*
*Completed: 2026-01-21*
