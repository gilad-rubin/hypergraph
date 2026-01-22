---
title: "Fix Visualization Layout and Edge Routing"
created: 2026-01-22
poured:
  - hypergraph-mol-060c
  - hypergraph-mol-5qun
  - hypergraph-mol-tbk1
  - hypergraph-mol-976t
  - hypergraph-mol-vf54
  - hypergraph-mol-iji1
  - hypergraph-mol-6zd7
  - hypergraph-mol-32r6
  - hypergraph-mol-qjqv
  - hypergraph-mol-6itj
  - hypergraph-mol-85js
  - hypergraph-mol-snag
  - hypergraph-mol-mrvq
  - hypergraph-mol-x10a
  - hypergraph-mol-c7lc
  - hypergraph-mol-6pbd
  - hypergraph-mol-uzfl
  - hypergraph-mol-kmud
  - hypergraph-mol-hpdv
  - hypergraph-mol-xxwm
  - hypergraph-mol-biqc
  - hypergraph-mol-gugy
  - hypergraph-mol-wnir
  - hypergraph-mol-yida
  - hypergraph-mol-ekho
  - hypergraph-mol-rjo6
  - hypergraph-mol-agfc
  - hypergraph-mol-qz1j
  - hypergraph-mol-jvwd
  - hypergraph-mol-py76
  - hypergraph-mol-mwpv
  - hypergraph-mol-euws
iteration: 2
auto_discovery: false
auto_learnings: false
---
<project_specification>
<project_name>Fix Visualization Layout and Edge Routing</project_name>

  <overview>
    Comprehensive fix for multiple hypergraph visualization bugs using strict TDD:
    **Every implementation task is preceded by a failing test task.**

    **Problem 1 - Interactive Collapse Edge Routing**: When a user renders a graph
    expanded and clicks to collapse a nested container, edges that were connected to
    internal nodes disappear instead of re-routing back to the container boundary.

    **Problem 2 - Graph Clipping**: Large graphs get cut off at viewport boundaries.
    The complex_rag graph (19 nodes) shows nodes cut off on the left edge.

    **Problem 3 - Edges Crossing Nodes**: Many edges cross through nodes instead of
    routing around them. Browser analysis found 80+ edge-node crossing violations.

    **Problem 4 - Wide Arc Edges**: Some edges have extremely wide horizontal spans
    (up to 561px) creating sweeping arcs that go far outside the graph area.

    **TDD Approach**:
    - Each fix is preceded by a failing test that defines the expected behavior
    - Tests use Playwright for browser automation
    - Existing failing tests: `TestInteractiveCollapseEdgeRouting` (2 of 3 failing)
  </overview>

  <context>
    <existing_patterns>
      - Edge re-routing uses `routingData` passed from Python with deepest mappings
      - `param_to_consumer` maps parameter names to their deepest consumer nodes
      - `output_to_producer` maps output names to their deepest producer nodes
      - Expand re-routing works by checking if deepest target is now visible
      - Layout filters hidden nodes at line 194: `nodes.filter(n => !n.hidden)`
      - Cross-boundary edges handled in Step 4.5 (layout.js lines 787-898)
      - Layout uses constraint-based algorithm (kiwi.js/Cassowary) in `constraint-layout.js`
      - Viewport centering in `fitWithFixedPadding()` function in `app.js`
      - Shadow offset (10px) is subtracted from visible node heights
    </existing_patterns>
    <integration_points>
      - Python: `renderer.py` generates routing data in `meta` object
      - JavaScript: `app.js` passes `routingData` to `useLayout()`, handles centering
      - JavaScript: `layout.js` uses `routingData` for edge re-routing
      - JavaScript: `constraint-layout.js` does node positioning and edge routing
      - Tests: `test_interactive_expand.py` has collapse tests (currently failing)
      - Tests: `test_edge_connections.py` validates edge endpoints
    </integration_points>
    <new_technologies>
      - No new technologies needed
    </new_technologies>
    <conventions>
      - TDD: RED (failing test) → GREEN (make pass) → REFACTOR (clean up)
      - Edge routing uses `actualSource`/`actualTarget` in edge data for debugging
      - Tests use Playwright with `extract_edge_routing()` helper
      - Debug data exposed via `window.__hypergraphVizDebug` for test extraction
    </conventions>
  </context>

  <tasks>
    <!-- =========================================================== -->
    <!-- PHASE 1: COLLAPSE EDGE ROUTING (Priority 0-1)               -->
    <!-- Existing failing tests: 2 of 3 in TestInteractiveCollapseEdgeRouting -->
    <!-- =========================================================== -->

    <task id="verify-collapse-tests-fail" priority="0" category="infrastructure">
      <title>RED: Verify Existing Collapse Tests Fail</title>
      <description>
        Before implementing fixes, verify the existing collapse tests fail as expected.
        This confirms we have proper failing tests to drive the implementation.

        Expected failing tests:
        - `test_input_edge_routes_to_container_after_collapse`
        - `test_interactive_collapse_edge_targets_match_static`
      </description>
      <steps>
        - Run: `pytest tests/viz/test_interactive_expand.py::TestInteractiveCollapseEdgeRouting -v`
        - Verify exactly 2 tests fail with expected error messages
        - Document the exact failure messages for reference
      </steps>
      <test_steps>
        1. Run the test command
        2. Confirm 2 failures, 1 pass
        3. Verify failure messages mention "edge is MISSING" or "targets container"
      </test_steps>
      <review></review>
    </task>

    <task id="test-node-to-parent-map" priority="0" category="infrastructure">
      <title>RED: Add Failing Test for node_to_parent Map</title>
      <description>
        Create a unit test that verifies the Python renderer generates a `node_to_parent`
        map. This map is required for the collapse fix but doesn't exist yet.

        Test should verify:
        - `node_to_parent` key exists in `meta` dict
        - Contains correct mappings: `{'clean_text': 'preprocess', ...}`
      </description>
      <steps>
        - Add test in `tests/viz/test_renderer.py`
        - Test `render_graph()` returns meta with `node_to_parent`
        - Test map contains correct parent relationships
        - Run test - should FAIL (map doesn't exist yet)
      </steps>
      <test_steps>
        1. Create test `test_render_graph_includes_node_to_parent_map`
        2. Run test - verify it fails with KeyError or missing key
      </test_steps>
      <review></review>
    </task>

    <task id="implement-node-to-parent-map" priority="0" category="functional">
      <title>GREEN: Implement node_to_parent Map in Python Renderer</title>
      <description>
        Create a `node_to_parent` map in Python that maps each node ID to its parent
        container ID. This enables JavaScript to find visible ancestors when collapsing.
      </description>
      <steps>
        - Add `_build_node_to_parent_map(flat_graph)` function in renderer.py
        - Call it alongside existing deepest map builders
        - Add `node_to_parent` to the `meta` dict returned by `render_graph()`
      </steps>
      <test_steps>
        1. Run the test from previous task - should now PASS
        2. Verify map contains: `{'clean_text': 'preprocess', 'normalize_text': 'preprocess'}`
      </test_steps>
      <review></review>
    </task>

    <task id="test-node-to-parent-in-js" priority="0" category="infrastructure">
      <title>RED: Add Failing Test for node_to_parent in JavaScript</title>
      <description>
        Create a test that verifies `node_to_parent` is passed to JavaScript and
        accessible via the debug API. This is needed before implementing the JS side.
      </description>
      <steps>
        - Add Playwright test that checks `window.__hypergraphVizDebug.routingData.node_to_parent`
        - Test should verify the map is present and has expected keys
        - Run test - should FAIL (JS doesn't receive the map yet)
      </steps>
      <test_steps>
        1. Create test `test_node_to_parent_exposed_in_debug_api`
        2. Run test - verify it fails (property is undefined)
      </test_steps>
      <review></review>
    </task>

    <task id="implement-node-to-parent-in-js" priority="0" category="functional">
      <title>GREEN: Pass node_to_parent to JavaScript Layout</title>
      <description>
        Update `app.js` to include `node_to_parent` in the `routingData` object
        that's passed to `useLayout()` and exposed in debug API.
      </description>
      <steps>
        - In app.js `routingData` useMemo, add `node_to_parent` from `initialData.meta`
        - Add to debug data export for test visibility
      </steps>
      <test_steps>
        1. Run test from previous task - should now PASS
        2. Verify in browser console: `window.__hypergraphVizDebug.routingData.node_to_parent`
      </test_steps>
      <review></review>
    </task>

    <task id="implement-collapse-fix" priority="1" category="functional">
      <title>GREEN: Implement Collapse Edge Routing Fix</title>
      <description>
        Implement the core fix: add `findVisibleAncestor()` helper and update edge
        routing to use it when the actual consumer/producer is hidden.

        This task makes the existing failing collapse tests pass.
      </description>
      <steps>
        - Extract `nodeToParent` from `routingData` in `performRecursiveLayout()`
        - Add `findVisibleAncestor(nodeId)` helper that walks up parent chain
        - In INPUT node edge handling, add else branch for hidden consumer
        - In data edge producer handling, add else branch for hidden producer
        - Call `findVisibleAncestor()` to find visible parent when node is hidden
      </steps>
      <test_steps>
        1. Run: `pytest tests/viz/test_interactive_expand.py::TestInteractiveCollapseEdgeRouting -v`
        2. All 3 tests should now PASS
      </test_steps>
      <review></review>
    </task>

    <task id="test-multi-level-collapse" priority="1" category="infrastructure">
      <title>RED: Add Failing Tests for Multi-Level Collapse</title>
      <description>
        Add tests for collapsing the `outer` graph (2 levels of nesting) to ensure
        the fix works for deeper nesting, not just single-level.

        Test scenarios:
        - outer at depth=2, collapse inner → edges route to inner container
        - outer at depth=1, collapse middle → edges route to middle container
      </description>
      <steps>
        - Add `TestOuterInteractiveCollapse` class in test_interactive_expand.py
        - Import `make_outer` from conftest
        - Add `test_outer_collapse_inner_routes_to_container`
        - Add `test_outer_collapse_middle_routes_to_container`
        - Run tests - should FAIL if fix doesn't handle multi-level
      </steps>
      <test_steps>
        1. Create new test class with 2 tests
        2. Run tests - verify they fail or pass (depending on if fix already handles it)
        3. If they pass, great! If not, identify what's missing
      </test_steps>
      <review></review>
    </task>

    <task id="fix-multi-level-collapse" priority="1" category="functional">
      <title>GREEN: Fix Multi-Level Collapse (if needed)</title>
      <description>
        If the multi-level collapse tests fail, extend the fix to handle deeper nesting.
        The `findVisibleAncestor()` helper should already handle this by walking up
        the parent chain, but verify and fix if needed.
      </description>
      <steps>
        - Analyze failing tests to understand what's missing
        - Ensure `node_to_parent` map includes all levels of nesting
        - Verify `findVisibleAncestor()` walks up multiple levels
        - Fix any issues found
      </steps>
      <test_steps>
        1. Run multi-level collapse tests - should now PASS
        2. Run all collapse tests - all should pass
      </test_steps>
      <review></review>
    </task>

    <!-- =========================================================== -->
    <!-- PHASE 2: GRAPH CLIPPING (Priority 2)                        -->
    <!-- No existing tests - must add failing tests first            -->
    <!-- =========================================================== -->

    <task id="test-clipping" priority="2" category="infrastructure">
      <title>RED: Add Failing Test for Graph Clipping</title>
      <description>
        Create a test that verifies all nodes in a large graph are visible within
        the viewport bounds. This test should fail for the current clipping bug.

        Test criteria:
        - All nodes have x >= 0 (not cut off on left)
        - All nodes have y >= 0 (not cut off on top)
        - Node bounds are within viewport
      </description>
      <steps>
        - Create `tests/viz/test_viewport_clipping.py`
        - Add test `test_large_graph_all_nodes_visible`
        - Use complex_rag graph or create a graph with 15+ nodes
        - Extract node positions and verify all are within viewport
        - Run test - should FAIL (nodes are clipped)
      </steps>
      <test_steps>
        1. Create test file and test function
        2. Run test - verify it fails with nodes having negative positions
        3. Document which nodes are clipped
      </test_steps>
      <review></review>
    </task>

    <task id="diagnose-clipping" priority="2" category="infrastructure">
      <title>Diagnose Root Cause of Graph Clipping</title>
      <description>
        Investigate why the viewport centering algorithm positions content
        outside the visible area for large graphs.

        Key areas to investigate:
        1. How bounds are calculated from node positions
        2. How initial viewport position is computed in `fitWithFixedPadding()`
        3. Whether edge waypoints are included in bounds
      </description>
      <steps>
        - Add debug logging to fitWithFixedPadding() showing bounds calculation
        - Compare bounds for small (3-5 nodes) vs large (15+ nodes) graphs
        - Check if minX/minY are correctly calculated from ALL nodes
        - Identify the root cause
      </steps>
      <test_steps>
        1. Generate HTML for large graph
        2. Check console for bounds info
        3. Document findings
      </test_steps>
      <review></review>
    </task>

    <task id="fix-clipping" priority="2" category="functional">
      <title>GREEN: Fix Viewport Centering for Large Graphs</title>
      <description>
        Implement a fix for the viewport centering that ensures all graph
        content is visible.
      </description>
      <steps>
        - Modify fitWithFixedPadding() to calculate true content bounds
        - Add safety check: ensure minX >= PADDING_LEFT after corrections
        - Add safety check: ensure minY >= PADDING_TOP after corrections
        - Test with complex_rag and other large graphs
      </steps>
      <test_steps>
        1. Run clipping test from earlier - should now PASS
        2. Verify smaller graphs still render correctly
      </test_steps>
      <review></review>
    </task>

    <!-- =========================================================== -->
    <!-- PHASE 3: EDGE CROSSING (Priority 3)                         -->
    <!-- No existing tests - must add failing tests first            -->
    <!-- =========================================================== -->

    <task id="test-edge-crossing" priority="3" category="infrastructure">
      <title>RED: Add Failing Test for Edge-Node Crossings</title>
      <description>
        Create a test that detects when edges cross through unrelated nodes.
        This test should fail for the current edge crossing bug.

        Test criteria:
        - Edge bounding box should not overlap non-source/non-target nodes
        - Edges connecting A→B should not pass through node C
      </description>
      <steps>
        - Add test `test_no_edge_node_crossings` in test_edge_connections.py
        - For each edge, check if path intersects any non-connected nodes
        - Use edge path waypoints and node bounds to detect crossings
        - Run test - should FAIL (edges cross through nodes)
      </steps>
      <test_steps>
        1. Create test function
        2. Run test - verify it detects crossing violations
        3. Document which edges cross which nodes
      </test_steps>
      <review></review>
    </task>

    <task id="diagnose-edge-crossing" priority="3" category="infrastructure">
      <title>Diagnose Edge-Node Crossing Issues</title>
      <description>
        Investigate why edges cross through nodes. The routing algorithm has
        collision avoidance but it's not working for many edges.

        Key areas:
        1. How blocking rows are detected in routing function
        2. How corridors are calculated for edge paths
        3. Why some edges have very wide horizontal spans (500+ px)
      </description>
      <steps>
        - Add debug logging to routing() function showing blocked rows
        - Check if blocking detection only checks rows BETWEEN source/target
        - Identify patterns in which edges cross nodes
      </steps>
      <test_steps>
        1. Render complex_rag graph with debug logging
        2. Trace routing decision for a crossing edge
        3. Document findings
      </test_steps>
      <review></review>
    </task>

    <task id="fix-edge-crossing" priority="3" category="functional">
      <title>GREEN: Fix Edge Routing to Avoid Node Crossings</title>
      <description>
        Improve the edge routing algorithm to avoid crossing through nodes.
      </description>
      <steps>
        - Update blocking detection to include nodes in same row as target
        - Increase clearance margin when calculating corridors
        - Add post-routing validation to detect crossings
        - Ensure fix doesn't break nested graph edge routing
      </steps>
      <test_steps>
        1. Run edge crossing test - should now PASS
        2. Verify edges still connect correct nodes
        3. Run existing viz tests - all pass
      </test_steps>
      <review></review>
    </task>

    <!-- =========================================================== -->
    <!-- PHASE 4: VALIDATION AND CLEANUP (Priority 4)                -->
    <!-- =========================================================== -->

    <task id="run-all-tests" priority="4" category="functional">
      <title>Verify All Tests Pass and No Regressions</title>
      <description>
        Run the full test suite to ensure all fixes work together without regressions.
      </description>
      <steps>
        - Run all TestInteractiveCollapseEdgeRouting tests
        - Run all TestInteractiveExpandEdgeRouting tests (regression check)
        - Run full viz test suite: `pytest tests/viz/ -v`
      </steps>
      <test_steps>
        1. `pytest tests/viz/test_interactive_expand.py -v` - ALL tests pass
        2. `pytest tests/viz/ -v` - No regressions
      </test_steps>
      <review></review>
    </task>

    <task id="cleanup-debug-logging" priority="4" category="infrastructure">
      <title>REFACTOR: Remove Debug Logging</title>
      <description>
        Remove any debug console.log statements added during development.
        Keep the code clean and production-ready.
      </description>
      <steps>
        - Review layout.js for any debug console.log statements
        - Review app.js for debug logging
        - Review constraint-layout.js for debug logging
        - Remove or guard with `if (debugMode)` flag
      </steps>
      <test_steps>
        1. Run tests - still pass
        2. Visual inspection of browser console - no debug spam
      </test_steps>
      <review></review>
    </task>

  </tasks>

  <success_criteria>
    - All TestInteractiveCollapseEdgeRouting tests pass (workflow and outer graphs)
    - All TestInteractiveExpandEdgeRouting tests still pass (no regression)
    - Large graphs render with all nodes visible (no clipping)
    - No edges cross through unrelated nodes
    - All existing visualization tests pass
    - Interactive collapse in browser shows edges properly reconnecting to container
    - Code is clean, DRY, follows existing patterns
  </success_criteria>

</project_specification>
