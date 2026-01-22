---
title: "Fix Edge-to-Node Dimension Mismatch"
created: 2026-01-22
poured:
  - hypergraph-mol-4nre
  - hypergraph-mol-wqzb
  - hypergraph-mol-n3uv
  - hypergraph-mol-d1pk
  - hypergraph-mol-xnez
  - hypergraph-mol-5223
  - hypergraph-mol-0lf6
  - hypergraph-mol-yx0q
  - hypergraph-mol-iwc5
  - hypergraph-mol-4eoo
  - hypergraph-mol-fads
  - hypergraph-mol-lhrm
  - hypergraph-mol-jaya
  - hypergraph-mol-jvbi
  - hypergraph-mol-lh7v
  - hypergraph-mol-sljz
  - hypergraph-mol-lnj6
  - hypergraph-mol-4rze
iteration: 1
auto_discovery: false
auto_learnings: true
---
<project_specification>
<project_name>Fix Edge-to-Node Dimension Mismatch</project_name>

<overview>
Remove the SHADOW_OFFSET hack by fixing the root cause: a mismatch between calculated node dimensions and actual rendered dimensions.

**Key insight**: CSS `box-shadow` does NOT affect `getBoundingClientRect()` or element dimensions. If edges don't connect to visible nodes, the problem is NOT the shadow - it's a dimension/position mismatch somewhere in the layout pipeline.

**Goal**: After this fix, changing any node's shadow class should have ZERO impact on edge routing.
</overview>

<context>
<existing_patterns>
- Node dimensions calculated in `layout.js::calculateDimensions()`
- Edge routing in `constraint-layout.js` uses `nodeBottom()`, `nodeTop()` helpers
- Current hack: `SHADOW_OFFSET = 10` subtracted from node height in 3 places
- Tests use `extract_inner_bounds_and_edge_paths()` to compare edge endpoints to node bounds
</existing_patterns>
<integration_points>
- `assets/layout.js` - calculateDimensions(), edge re-routing (Step 5)
- `assets/constraint-layout.js` - nodeBottom(), nodeVisibleBottom(), edge stem calculations
- `assets/app.js` - SHADOW_OFFSET in debug overlay
- `assets/components.js` - node rendering with shadow classes
</integration_points>
<conventions>
- All edge routing uses px coordinates
- Node positions are center-based (x, y = center of node)
- React Flow wrapper bounds should match our calculated dimensions exactly
</conventions>
</context>

<tasks>
<!-- Phase 1: Diagnosis - understand the REAL root cause -->

<task id="diagnose-dimension-mismatch" priority="0" category="infrastructure">
<title>Create diagnostic test to measure exact dimension mismatch</title>
<description>
Create a Playwright test that renders a simple graph and compares:
1. Our calculated dimensions (from calculateDimensions)
2. React Flow wrapper dimensions (getBoundingClientRect on .react-flow__node)
3. Inner content dimensions (getBoundingClientRect on .group.rounded-lg)
4. Edge endpoint coordinates

This will show us EXACTLY where the mismatch occurs.

**Debugging tools available:**
- `extract_inner_bounds_and_edge_paths(page)` - extracts inner/wrapper bounds and edge paths
- `window.__hypergraphVizDebug` - browser debug API with node/edge data
- See `src/hypergraph/viz/DEBUGGING.md` for full documentation
</description>
<steps>
- Create test file `tests/viz/test_dimension_mismatch.py`
- Render a simple 2-node graph (function node -> function node)
- Extract: calculated dimensions, wrapper bounds, inner bounds, edge endpoints
- Log all measurements with clear labels
- Assert nothing yet - just gather data to understand the mismatch
</steps>
<test_steps>
1. Run `pytest tests/viz/test_dimension_mismatch.py -v -s`
2. Review logged measurements
3. Identify: Is mismatch between calculated vs wrapper? Or wrapper vs inner?
4. Document findings in test file comments
</test_steps>
<review></review>
</task>

<task id="verify-shadow-not-affecting-bounds" priority="0" category="infrastructure">
<title>Verify that box-shadow doesn't affect getBoundingClientRect</title>
<description>
Create a simple HTML test to confirm CSS box-shadow doesn't affect element dimensions.
This validates our assumption that the "shadow offset" is fixing the wrong problem.
</description>
<steps>
- Create test that renders a div with shadow-lg class
- Measure getBoundingClientRect
- Verify width/height match the CSS dimensions exactly
- If they DO match, the shadow is NOT the problem
</steps>
<test_steps>
1. Run the test
2. Confirm box-shadow has no effect on bounds
3. Document: "Shadow is not the root cause"
</test_steps>
<review></review>
</task>

<!-- Phase 2: Fix the root cause -->

<task id="fix-calculated-vs-rendered" priority="1" category="functional">
<title>Fix dimension calculation to match rendered size</title>
<description>
Based on diagnostic findings, fix the mismatch between `calculateDimensions()` output and actual rendered node size.

Possible causes to check:
1. Padding not accounted for in calculation
2. Border width not included
3. React Flow adding internal padding
4. Coordinate system offset (center vs corner based)

**After fix**: Calculated dimensions should EXACTLY match wrapper bounds.
</description>
<steps>
- Review diagnostic test results to identify source of mismatch
- Update calculateDimensions() or fix React Flow setup
- Verify calculated == wrapper bounds for all node types
</steps>
<test_steps>
1. Run diagnostic test - mismatch should be 0px
2. Verify for DATA, INPUT, FUNCTION, PIPELINE node types
3. Check both width and height
</test_steps>
<review></review>
</task>

<task id="remove-shadow-offset" priority="1" category="functional">
<title>Remove SHADOW_OFFSET hack from all files</title>
<description>
With dimensions fixed, remove the SHADOW_OFFSET hack entirely.

Files to update:
- `constraint-layout.js`: Remove SHADOW_OFFSET constant and nodeVisibleBottom()
- `layout.js`: Remove SHADOW_OFFSET from edge re-routing
- `app.js`: Remove SHADOW_OFFSET from debug overlay

Replace `nodeVisibleBottom(node)` with `nodeBottom(node)` everywhere.
</description>
<steps>
- Remove SHADOW_OFFSET from constraint-layout.js
- Replace nodeVisibleBottom() calls with nodeBottom()
- Remove SHADOW_OFFSET from layout.js
- Remove SHADOW_OFFSET from app.js
- Search for any other SHADOW_OFFSET usage
</steps>
<test_steps>
1. Grep for SHADOW_OFFSET - should find 0 results
2. Grep for nodeVisibleBottom - should find 0 results
3. Run all viz tests
</test_steps>
<review></review>
</task>

<!-- Phase 3: Validation -->

<task id="test-edge-connection-all-node-types" priority="1" category="infrastructure">
<title>Add edge connection tests for ALL node types</title>
<description>
Create comprehensive tests that verify edges connect properly to each node type:
- DATA nodes (shadow-sm)
- INPUT nodes (shadow-sm)
- INPUT_GROUP nodes (shadow-sm)
- FUNCTION nodes (shadow-lg)
- PIPELINE collapsed (shadow-lg)
- PIPELINE expanded (no shadow)
- BRANCH nodes (drop-shadow filter)

Each test should verify edge endpoints are within 1px of node bounds (not 5px tolerance).
</description>
<steps>
- Create TestEdgeConnectionByNodeType class
- Test: DATA as source, FUNCTION as target
- Test: FUNCTION as source, DATA as target
- Test: PIPELINE expanded as source
- Test: BRANCH node connections
- All with 1px tolerance (not 5px)
</steps>
<test_steps>
1. Run tests with 1px tolerance
2. All should pass after dimension fix
3. Verify no special handling needed per node type
</test_steps>
<review></review>
</task>

<task id="test-shadow-class-changes" priority="2" category="infrastructure">
<title>Test that changing shadow class doesn't affect edges</title>
<description>
The ultimate validation: changing a node's shadow class should have ZERO effect on edge positioning.

Create a test that:
1. Renders graph with current shadow classes
2. Measures edge endpoints
3. Modifies shadow classes (shadow-none, shadow-sm, shadow-lg, shadow-2xl)
4. Re-measures edge endpoints
5. Asserts: positions are IDENTICAL regardless of shadow

This proves shadows are fully decoupled from edge routing.
</description>
<steps>
- Create test_shadow_decoupled_from_edges()
- Render graph, measure edges
- Inject CSS to override shadow classes
- Re-measure edges
- Assert positions unchanged
</steps>
<test_steps>
1. Edge endpoints identical with shadow-none
2. Edge endpoints identical with shadow-sm
3. Edge endpoints identical with shadow-lg
4. Edge endpoints identical with shadow-2xl
</test_steps>
<review></review>
</task>

<task id="update-documentation" priority="2" category="documentation">
<title>Update CLAUDE.md to remove shadow offset documentation</title>
<description>
Remove the "Shadow Gap Issue" section from CLAUDE.md since it documents a hack that no longer exists.

Replace with a note: "Shadows are purely decorative and do not affect edge routing."
</description>
<steps>
- Remove "Shadow Gap Issue" section from CLAUDE.md
- Remove test tolerance explanations related to shadow variance
- Add note about shadow/edge decoupling
- Update DEBUGGING.md if needed
</steps>
<test_steps>
1. Review CLAUDE.md - no mention of SHADOW_OFFSET
2. Documentation reflects clean architecture
</test_steps>
<review></review>
</task>

<task id="reduce-test-tolerance" priority="2" category="functional">
<title>Reduce edge connection test tolerance from 5px to 1px</title>
<description>
With the root cause fixed, we no longer need 5px tolerance to account for shadow variance.
Reduce to 1px (or 0.5px) for more precise validation.
</description>
<steps>
- Find all test files using tolerance for edge validation
- Reduce tolerance from 5.0 to 1.0
- Run tests to verify they still pass
- If any fail, investigate the specific case
</steps>
<test_steps>
1. All edge tests pass with 1px tolerance
2. No special cases needed
</test_steps>
<review></review>
</task>

</tasks>

<success_criteria>
- SHADOW_OFFSET removed from codebase entirely
- Edge endpoints within 1px of node bounds (not 5px)
- Changing shadow CSS class has ZERO effect on edge positions
- All existing viz tests pass
- Documentation updated to reflect clean architecture
</success_criteria>

</project_specification>
