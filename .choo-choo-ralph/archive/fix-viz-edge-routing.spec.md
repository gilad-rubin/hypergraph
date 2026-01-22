---
title: "Fix Visualization Edge Routing"
created: 2026-01-22
poured:
  - hypergraph-mol-6gm
  - hypergraph-mol-kg6p
  - hypergraph-mol-a4f
  - hypergraph-mol-wr1v
  - hypergraph-mol-wfc
  - hypergraph-mol-c4u
iteration: 4
auto_discovery: false
auto_learnings: false
---
<project_specification>
<project_name>Fix Visualization Edge Routing</project_name>

  <overview>
    Fix critical edge routing bugs in the hypergraph visualization system using TDD.

    **TDD Approach**: Write failing tests FIRST, verify they fail for the right reason,
    then implement minimal fixes to make them pass.

    Two major issues identified through browser-based investigation:

    1. **Edge-to-Shadow Gap**: Edges connect to shadow/glow boundaries instead of
       visible node boundaries, creating 6-14px visual gaps
    2. **Interactive Expand Broken**: Clicking to expand nested graphs produces
       completely different (broken) edge routing vs static depth=N rendering
  </overview>

  <context>
    <existing_patterns>
      - Python layer generates explicit VizInstructions (instructions.py)
      - JavaScript layer renders via React Flow (constraint-layout.js)
      - Edge routing uses stem points at node top/bottom centers
      - Debug overlay exposes node/edge geometry via window.__hypergraphVizDebug
      - Playwright tests validate geometry with tight tolerances
      - Existing test pattern in test_edge_connections.py uses extract_debug_data()
    </existing_patterns>
    <integration_points>
      - Node bounds: wrapper includes shadow, inner excludes shadow
      - Edge start/end: currently uses wrapper bounds (wrong)
      - Expand click handler: single click on container toggles expansion
      - Edge re-routing: must update edges when expansion state changes
    </integration_points>
    <new_technologies>
      - No new technologies needed
    </new_technologies>
    <conventions>
      - Tests should be programmatic Python (pytest + Playwright)
      - Use extract_debug_data() for geometry validation
      - Edge gap tests use 0px tolerance for connections
      - TDD: RED (failing test) → GREEN (minimal fix) → REFACTOR
    </conventions>
  </context>

  <tasks>
    <!-- ============================================================ -->
    <!-- PHASE 1: RED - Write Failing Tests                           -->
    <!-- ============================================================ -->

    <task id="test-shadow-gap" priority="0" category="functional">
      <title>RED: Write Failing Test for Edge-to-Shadow Gap</title>
      <description>
        Write a test that measures the gap between edge start/end points and
        the VISIBLE node boundaries (excluding shadows). This test MUST FAIL
        with the current implementation because edges connect to shadow boundaries.

        **WHY EXISTING TESTS PASS BUT ARE WRONG:**
        The existing tests in test_edge_connections.py use `window.__hypergraphVizDebug.nodes`
        which reports the React Flow WRAPPER bounds (includes shadow). So tests show 0px gap
        but that's comparing edge position to wrapper position - both are wrong by the same amount!

        **This test must do differently:**
        1. Query the INNER DOM element directly (`.group.rounded-lg` inside the node wrapper)
        2. Use `element.getBoundingClientRect()` to get true visible bounds
        3. Compare edge Y coordinates to these INNER bounds
        4. This will reveal the 6-14px gaps that currently exist

        **Expected failure**: Test will show 6-14px gaps where we expect 0px.

        **Evidence from browser inspection:**
        - input_text: inner bottom=189px, edge starts at 195px → 6px gap
        - clean_text: inner bottom=357px, edge starts at 371px → 14px gap
        - normalize: inner bottom=497px, edge starts at 511px → 14px gap
      </description>
      <steps>
        - Create test in tests/viz/test_edge_shadow_gap.py
        - Use Playwright page.evaluate() to query inner elements directly
        - Get inner element bounds via getBoundingClientRect() for each node
        - Get edge path start/end Y coordinates from SVG
        - Assert: edge_start_y == source_inner_bottom (0px tolerance)
        - Assert: edge_end_y == target_inner_top (0px tolerance)
        - Test the workflow graph at depth=1 (shows INPUT, FUNCTION nodes)
      </steps>
      <test_steps>
        1. Run the new test: `pytest tests/viz/test_edge_shadow_gap.py -v`
        2. Verify test FAILS (this is expected - RED phase)
        3. Verify failure message shows gaps like "Gap at source: 6px" or "14px"
        4. Verify failure is NOT because nodes/edges are missing (test setup bug)
        5. Document the exact gap values in the test output
      </test_steps>
      <review></review>
    </task>

    <task id="test-interactive-expand" priority="0" category="functional">
      <title>RED: Write Failing Test for Interactive Expand/Collapse</title>
      <description>
        Write a test that compares edge routing between interactive expand and
        static depth rendering. This test MUST FAIL because interactive expand
        currently produces broken edge routing.

        **The key bug:**
        When you click to expand a nested graph interactively, edges should route
        to the INTERNAL nodes (clean_text, normalize). Instead, they incorrectly
        stay connected to the CONTAINER node (preprocess).

        **What to compare:**
        1. **Edge target IDs** - Most critical! After expanding, edges should target
           internal nodes (clean_text), not the container (preprocess)
        2. **Edge source IDs** - After expanding, edges should source from internal
           nodes (normalize), not the container
        3. **Edge paths (Y coordinates)** - Secondary validation, helps diagnose issues

        **Evidence from browser inspection:**

        Static depth=1 (CORRECT behavior):
        - Edge: input_text → clean_text (target is INTERNAL node)
        - Edge: normalize → analyze (source is INTERNAL node)

        Interactive expand (BROKEN behavior):
        - Edge: input_text → preprocess (target is CONTAINER - wrong!)
        - Edge: preprocess → analyze (source is CONTAINER - wrong!)
        - Some edges go backwards: Y 400→286

        **Test invariant**: After click-expand, edge targets must match what
        visualize(depth=N) produces. Same node IDs, same routing.
      </description>
      <steps>
        - Create test in tests/viz/test_interactive_expand.py
        - Render workflow graph at depth=0 (preprocess collapsed)
        - Capture initial edge data: source_id, target_id for each edge
        - Click on preprocess node to expand it
        - Wait for layout to stabilize
        - Capture expanded edge data: source_id, target_id for each edge
        - Render FRESH workflow graph at static depth=1
        - Capture static edge data: source_id, target_id for each edge
        - Assert: interactive edge targets == static edge targets
        - Assert: interactive edge sources == static edge sources
      </steps>
      <test_steps>
        1. Run the new test: `pytest tests/viz/test_interactive_expand.py -v`
        2. Verify test FAILS (this is expected - RED phase)
        3. Verify failure shows edge target mismatch:
           - Interactive: target="preprocess", Static: target="clean_text"
        4. Verify failure is NOT because click didn't work (expansion state should change)
        5. Document the exact edge differences in test output
      </test_steps>
      <review></review>
    </task>

    <!-- ============================================================ -->
    <!-- PHASE 2: GREEN - Implement Minimal Fixes                     -->
    <!-- ============================================================ -->

    <task id="fix-shadow-gap" priority="1" category="functional">
      <title>GREEN: Fix Edge-to-Shadow Gap</title>
      <description>
        Implement the minimal fix to make test-shadow-gap pass.

        **Root cause**: The layout/routing code uses React Flow node wrapper
        dimensions which include the CSS shadow/glow effect.

        **Fix approach** (choose simplest that works):
        1. Calculate node bounds excluding shadow in JavaScript
        2. Adjust edge stem calculations to offset by shadow size
        3. Modify shadow CSS to use outline or filter instead of box-shadow
      </description>
      <steps>
        - Identify where node bounds are calculated for edge routing
        - Modify to use inner visible bounds, not wrapper bounds
        - Run test-shadow-gap - must PASS
        - Run all existing tests - must still PASS
      </steps>
      <test_steps>
        1. Run `pytest tests/viz/test_edge_shadow_gap.py -v` - PASS
        2. Run `pytest tests/viz/ -v` - all PASS
      </test_steps>
      <review></review>
    </task>

    <task id="fix-interactive-expand" priority="1" category="functional">
      <title>GREEN: Fix Interactive Expand/Collapse Edge Routing</title>
      <description>
        Implement the minimal fix to make test-interactive-expand pass.

        **Root cause**: The interactive expand handler toggles node visibility
        but doesn't properly regenerate edge routing to internal nodes.

        **Fix approach**:
        - Find expand/collapse handler in JavaScript
        - When expansion state changes, regenerate edges using same logic
          as static depth rendering
        - Ensure edges route to actualSource/actualTarget when expanded
      </description>
      <steps>
        - Find the expand/collapse click handler
        - Trace edge generation on expansion state change
        - Fix: regenerate edges with proper routing to internal nodes
        - Run test-interactive-expand - must PASS
        - Run all existing tests - must still PASS
      </steps>
      <test_steps>
        1. Run `pytest tests/viz/test_interactive_expand.py -v` - PASS
        2. Run `pytest tests/viz/ -v` - all PASS
      </test_steps>
      <review></review>
    </task>

    <!-- ============================================================ -->
    <!-- PHASE 3: REFACTOR - Clean Up and Document                    -->
    <!-- ============================================================ -->

    <task id="refactor-edge-tests" priority="2" category="functional">
      <title>REFACTOR: Consolidate Edge Connection Tests</title>
      <description>
        After fixes pass, refactor tests for maintainability:
        - Merge shadow gap tests into test_edge_connections.py if appropriate
        - Extract common Playwright helpers
        - Ensure test names clearly describe what they verify
      </description>
      <steps>
        - Review test organization
        - Extract common measurement helpers
        - Ensure all edge cases are covered
        - All tests remain GREEN after refactor
      </steps>
      <test_steps>
        1. Run `pytest tests/viz/ -v` - all PASS
        2. No duplicate test logic
      </test_steps>
      <review></review>
    </task>

    <task id="document-findings" priority="3" category="documentation">
      <title>Document Bug Findings and Fixes in CLAUDE.md</title>
      <description>
        Add documentation about the edge gap and interactive expand bugs
        to viz/CLAUDE.md for future reference.
      </description>
      <steps>
        - Document shadow gap issue with measurements and fix
        - Document interactive expand issue with fix approach
        - Add section on how to debug these issues using dev-browser
      </steps>
      <test_steps>
        1. CLAUDE.md updated with new sections
        2. Documentation is clear and actionable
      </test_steps>
      <review></review>
    </task>

  </tasks>

  <success_criteria>
    - RED: Failing tests written for both bugs
    - GREEN: Both tests pass with minimal fixes
    - REFACTOR: Tests organized and documented
    - All existing tests still pass
  </success_criteria>

</project_specification>
