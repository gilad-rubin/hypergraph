# Phase 3: Fix Edge Routing Bugs - Research

**Researched:** 2026-01-21
**Domain:** JavaScript edge routing in hierarchical graphs with B-spline curves
**Confidence:** HIGH

## Summary

Phase 3 fixes edge routing bugs in a hierarchical graph visualization system using constraint-based layout with B-spline curves. The system uses React Flow for rendering with nested nodes (sub-flows), and a custom constraint solver (kiwi.js) for positioning. The research reveals that edge routing bugs in hierarchical graphs typically stem from **coordinate space confusion** (4 different coordinate systems), not algorithm deficiencies.

The current codebase has:
1. **Working constraint layout** - Positions nodes correctly with kiwi.js solver
2. **Working B-spline routing** - Routes edges around obstacles using corridor detection
3. **Partial hierarchy support** - `buildHierarchy()` and `resolveEdgeTargets()` exist from Phase 2
4. **Coordinate transformation bugs** - Edges route in wrong coordinate space when nodes are nested

The standard approach for fixing edge routing in nested graphs is:
1. **Define coordinate spaces explicitly** - Create transformation functions, not inline arithmetic
2. **Use absolute coordinates for edge routing** - All edge points in viewport space, regardless of node nesting
3. **Test with bounding box intersection** - Verify edge paths don't intersect node bounding boxes
4. **Visual regression testing** - Playwright screenshots comparing before/after

**Primary recommendation:** Fix coordinate space transformations by creating explicit transformation functions, then verify edge paths don't intersect nodes using geometric intersection tests.

## Standard Stack

The established libraries/tools for edge routing in hierarchical graphs with React Flow:

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| React Flow | 11.11+ | Nested node rendering with parent-child positioning | Industry standard, built-in sub-flow support with relative coordinates |
| kiwi.js | Latest | Constraint solver (Cassowary algorithm) | Fast, well-tested constraint solving for node positioning |
| B-spline curves | Native JS | Smooth edge curves through control points | Standard for graph visualization, curves stay inside control point bounding box |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| Playwright | Latest | Visual regression testing with screenshots | Verify edge routing fixes don't introduce regressions |
| Canvas API | Native | Geometric intersection testing (point-in-polygon, line-rect) | Automated verification that edges don't cross nodes |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| B-spline curves | Bezier curves | Bezier harder to control with multiple waypoints; B-spline more stable |
| Constraint solver | Force-directed layout | Constraint solver gives predictable results; force-directed less consistent |
| React Flow | Cytoscape.js, vis.js | React Flow has best React integration and sub-flow support |

**Installation:**
```bash
# No new dependencies - uses existing React Flow and kiwi.js
# For testing:
npm install --save-dev playwright @playwright/test
```

## Architecture Patterns

### Recommended Approach

```
Coordinate Spaces (4 types):
├── Layout Space - node.x, node.y (centers with 50px padding)
├── Parent-Relative Space - position.x, position.y (top-left relative to parent)
├── Absolute Viewport Space - edge points (top-left relative to viewport)
└── React Flow Space - DOM coordinates (includes zoom/pan transform)

Edge Routing Flow:
1. Layout nodes in Layout Space (constraint solver)
2. Convert to Parent-Relative Space for React Flow rendering
3. Track absolute positions separately for edge routing
4. Route edges in Absolute Viewport Space (ignores nesting)
5. Render edges with absolute coordinates
```

### Pattern 1: Explicit Coordinate Transformations
**What:** Define transformation functions between coordinate spaces instead of inline arithmetic
**When to use:** Any time converting between layout positions, parent-relative positions, or absolute positions

**Example:**
```javascript
// Source: React Flow sub-flows pattern, yFiles coordinate system architecture
const CoordinateTransform = {
  // Layout space (centers) → Parent-relative space (top-left)
  layoutToParentRelative(layoutNode, layoutPadding, graphPadding) {
    return {
      x: layoutNode.x - layoutNode.width/2 - layoutPadding + graphPadding,
      y: layoutNode.y - layoutNode.height/2 - layoutPadding + graphPadding
    };
  },

  // Parent-relative space → Absolute viewport space (for edge routing)
  parentRelativeToAbsolute(childPos, parentAbsPos, headerHeight, graphPadding) {
    return {
      x: parentAbsPos.x + childPos.x + graphPadding,
      y: parentAbsPos.y + childPos.y + headerHeight + graphPadding
    };
  },

  // Get absolute position of node (handles nesting recursively)
  getAbsolutePosition(node, nodePositions, nodeMap) {
    if (!node.parentNode) {
      // Root node - position is already absolute
      return nodePositions.get(node.id);
    }

    const parent = nodeMap.get(node.parentNode);
    const parentAbsPos = this.getAbsolutePosition(parent, nodePositions, nodeMap);
    const relativePos = nodePositions.get(node.id);

    return {
      x: parentAbsPos.x + relativePos.x,
      y: parentAbsPos.y + relativePos.y
    };
  }
};
```

### Pattern 2: B-spline Edge Routing with Obstacle Avoidance
**What:** Route edges using control points that avoid node bounding boxes
**When to use:** Edges that span multiple rows with blocking nodes in between

**Example:**
```javascript
// Source: Current constraint-layout.js with corridor routing
function routeEdge(edge, nodes, spaceX, spaceY) {
  const source = edge.sourceNode;
  const target = edge.targetNode;
  const naturalX = source.x + (target.x - source.x) * 0.5;

  // Find blocking nodes
  const blockedRows = [];
  for (let row = source.row + 1; row < target.row; row++) {
    for (const node of nodes[row]) {
      if (naturalX >= nodeLeft(node) - spaceX &&
          naturalX <= nodeRight(node) + spaceX) {
        blockedRows.push(row);
        break;
      }
    }
  }

  if (blockedRows.length === 0) {
    // Direct path - no waypoints needed
    return [];
  }

  // Calculate corridor position (left or right of blocking nodes)
  const leftCorridorX = Math.min(...blockedRows.map(r => nodeLeft(nodes[r][0]))) - spaceX;
  const rightCorridorX = Math.max(...blockedRows.map(r => nodeRight(nodes[r][0]))) + spaceX;

  const corridorX = Math.abs(naturalX - leftCorridorX) <= Math.abs(naturalX - rightCorridorX)
    ? leftCorridorX
    : rightCorridorX;

  // Add waypoints at corridor entry/exit
  const y1 = nodeTop(nodes[blockedRows[0]][0]) - spaceY;
  const y2 = nodeBottom(nodes[blockedRows[blockedRows.length-1]][0]) + spaceY;

  return [
    { x: corridorX, y: y1 },
    { x: corridorX, y: y2 }
  ];
}
```

### Pattern 3: Edge Target Resolution for Nested Nodes
**What:** Resolve logical edge targets to visual targets based on expansion state
**When to use:** Edges connect to container nodes that may be expanded (showing children) or collapsed

**Example:**
```javascript
// Source: Phase 2 research, layout.js resolveEdgeTargets
function resolveVisualTarget(logicalTargetId, expansionState, hierarchy, maxDepth = 10) {
  if (maxDepth <= 0) return logicalTargetId;

  const node = hierarchy.nodeMap.get(logicalTargetId);
  if (!node) return logicalTargetId;

  // Not a container OR collapsed → use logical ID
  const isPipeline = node.data?.nodeType === 'PIPELINE';
  const isExpanded = expansionState.get(logicalTargetId);

  if (!isPipeline || !isExpanded) {
    return logicalTargetId;
  }

  // Expanded container → route to entry nodes (no incoming edges from siblings)
  if (!node.children || node.children.length === 0) {
    return logicalTargetId;  // Empty container
  }

  const entryNodes = findEntryNodes(node.children, allEdges);
  if (entryNodes.length === 0) return logicalTargetId;

  // Recurse into first entry node (handles multi-level nesting)
  return resolveVisualTarget(entryNodes[0].id, expansionState, hierarchy, maxDepth - 1);
}
```

### Pattern 4: Bounding Box Intersection Testing
**What:** Verify edge paths don't intersect node bounding boxes
**When to use:** Automated testing to catch edge routing regressions

**Example:**
```javascript
// Source: Intersection testing literature, B-spline bounding box properties
function testEdgeNodeIntersection(edgePoints, nodes) {
  // B-spline property: curve stays inside control point bounding box
  const edgeBounds = getBoundingBox(edgePoints);

  for (const node of nodes) {
    const nodeBounds = {
      left: node.x - node.width/2,
      right: node.x + node.width/2,
      top: node.y - node.height/2,
      bottom: node.y + node.height/2
    };

    // Check if bounding boxes intersect
    if (boxesIntersect(edgeBounds, nodeBounds)) {
      // Detailed check - sample points along B-spline
      const samples = sampleBSpline(edgePoints, 20);
      for (const point of samples) {
        if (pointInBox(point, nodeBounds)) {
          return { intersects: true, node: node.id, point };
        }
      }
    }
  }

  return { intersects: false };
}

function boxesIntersect(a, b) {
  return !(a.right < b.left || a.left > b.right ||
           a.bottom < b.top || a.top > b.bottom);
}

function pointInBox(point, box) {
  return point.x >= box.left && point.x <= box.right &&
         point.y >= box.top && point.y <= box.bottom;
}
```

### Anti-Patterns to Avoid

- **Inline coordinate arithmetic** - Formulas like `n.x - w/2 - layoutPadding + graphPadding` scattered throughout code; use named transformation functions instead
- **Mixing coordinate spaces** - Using parent-relative coordinates for edge routing; edges must always use absolute viewport coordinates
- **Manual depth tracking** - Passing `remaining_depth` parameters; use recursive functions that naturally terminate at leaves
- **Special-casing nesting levels** - Different code paths for depth=1 vs depth=2; write unified algorithm that handles arbitrary depth
- **Ignoring target row in collision detection** - Only checking `i < target.row` misses nodes in target's row; check all rows between source and target

## Don't Hand-Roll

Problems that look simple but have existing solutions:

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| B-spline curve generation | Manual interpolation | Existing B-spline library or constraint-layout.js algorithm | B-spline curves have subtle math (knot vectors, basis functions); existing code handles edge cases |
| Bounding box intersection | Custom polygon intersection | Separating Axis Theorem or bounding box overlap test | Academic algorithms are proven correct; easy to get wrong with custom code |
| Coordinate space tracking | Global variables or manual offsets | Explicit transformation functions with clear input/output types | Transformation bugs are the #1 cause of edge routing issues in nested graphs |
| Visual regression testing | Manual screenshot comparison | Playwright toMatchSnapshot() | Built-in pixel diff, threshold tuning, baseline management |

**Key insight:** The hardest bug to debug is "edge goes through node by 2 pixels" - you know the symptom (bad rendering) but the root cause could be in any of 4 coordinate space transformations. Explicit transformation functions let you log input/output at each step.

## Common Pitfalls

### Pitfall 1: Forgetting to Use Absolute Coordinates for Edge Routing
**What goes wrong:** Edges route to wrong positions when nodes are nested
**Why it happens:** React Flow uses parent-relative positions for child nodes, but edge routing must use absolute viewport coordinates
**How to avoid:** Always track absolute positions separately from React Flow positions; convert to absolute before routing edges
**Warning signs:** Edges connect to wrong points on nested nodes; edge positions shift when parent moves

### Pitfall 2: Inconsistent Coordinate Space Conversions
**What goes wrong:** Off-by-one-pixel errors, edges don't connect to node boundaries correctly
**Why it happens:** Converting center→top-left, adding/subtracting padding, adjusting for headers - easy to get formula wrong
**How to avoid:** Create transformation functions with clear names and single implementation; never duplicate the arithmetic
**Warning signs:** Comments like "TODO: why do we need +4 here?", magic number offsets, edge gaps at node boundaries

### Pitfall 3: Checking Only Intermediate Rows for Blocking Nodes
**What goes wrong:** Edges route through nodes in the target row (regression from Phase 2)
**Why it happens:** Loop checks `i < target.row` instead of `i <= target.row`, missing nodes in target's row
**How to avoid:** Include target row in blocking detection, explicitly skip target node itself
**Warning signs:** Edges look correct for most graphs but occasionally go through nodes in bottom row

### Pitfall 4: Using Layout Padding Instead of Graph Padding
**What goes wrong:** Child nodes positioned incorrectly within parent, edges don't align
**Why it happens:** Constraint layout uses 50px padding internally, but parent containers use 40px padding (GRAPH_PADDING)
**How to avoid:** Explicitly convert between layout space (50px padding) and parent space (40px padding) using transformation functions
**Warning signs:** Child nodes have unexpected offset, edges don't reach child node boundaries

### Pitfall 5: Forgetting Parent Nodes Must Precede Children in Array
**What goes wrong:** React Flow doesn't establish parent-child relationship; children positioned absolutely instead of relative
**Why it happens:** React Flow processes nodes in array order; if child appears before parent, parent doesn't exist yet
**How to avoid:** Sort nodes by depth (parents first) before passing to React Flow
**Warning signs:** Dragging parent doesn't move children; children positioned in wrong location

### Pitfall 6: Modifying Edge Points In-Place During Recursion
**What goes wrong:** Edge points corrupted, edges render in wrong positions after expansion/collapse
**Why it happens:** Recursive layout modifies edge.points array, but same edge object used in multiple contexts
**How to avoid:** Create new edge objects with new points arrays; don't mutate input
**Warning signs:** Edges jump to wrong positions after expand/collapse; edge coordinates change unexpectedly

## Code Examples

Verified patterns from current codebase and React Flow documentation:

### Complete Coordinate Transformation System
```javascript
// Source: Synthesized from React Flow patterns, yFiles architecture, current codebase
const LAYOUT_PADDING = 50;   // constraint-layout.js internal padding
const GRAPH_PADDING = 40;    // layout.js parent container padding
const HEADER_HEIGHT = 32;    // layout.js parent container header

// Track absolute positions for edge routing (separate from React Flow positions)
const absolutePositions = new Map();

function layoutRecursive(visibleNodes, edges, expansionState) {
  const nodeGroups = groupNodesByParent(visibleNodes);
  const nodePositions = new Map();  // Parent-relative positions for React Flow

  // Layout children first (deepest to shallowest)
  const layoutOrder = getLayoutOrder(visibleNodes, expansionState);

  layoutOrder.forEach(graphNode => {
    const children = nodeGroups.get(graphNode.id) || [];

    // Run constraint layout on children
    const childResult = ConstraintLayout.graph(children, edges);

    // Convert layout space → parent-relative space
    childResult.nodes.forEach(layoutNode => {
      const relativePos = CoordinateTransform.layoutToParentRelative(
        layoutNode,
        LAYOUT_PADDING,
        GRAPH_PADDING
      );

      // Store parent-relative position (for React Flow rendering)
      nodePositions.set(layoutNode.id, relativePos);

      // Calculate absolute position (for edge routing)
      const parentAbsPos = absolutePositions.get(graphNode.id);
      const absolutePos = CoordinateTransform.parentRelativeToAbsolute(
        relativePos,
        parentAbsPos,
        HEADER_HEIGHT,
        GRAPH_PADDING
      );
      absolutePositions.set(layoutNode.id, absolutePos);
    });
  });

  // Layout root nodes
  const rootResult = ConstraintLayout.graph(rootNodes, edges);

  rootResult.nodes.forEach(layoutNode => {
    const absPos = {
      x: layoutNode.x - layoutNode.width/2,
      y: layoutNode.y - layoutNode.height/2
    };

    nodePositions.set(layoutNode.id, absPos);
    absolutePositions.set(layoutNode.id, absPos);
  });

  // Route edges using absolute positions only
  const routedEdges = edges.map(edge => {
    const sourceAbsPos = absolutePositions.get(edge.source);
    const targetAbsPos = absolutePositions.get(edge.target);

    const waypoints = routeEdge(edge, sourceAbsPos, targetAbsPos, absolutePositions);

    return {
      ...edge,
      data: {
        ...edge.data,
        points: waypoints  // All points in absolute viewport space
      }
    };
  });

  return { nodePositions, absolutePositions, routedEdges };
}
```

### Automated Edge-Node Intersection Testing
```javascript
// Source: B-spline intersection testing literature, Playwright patterns
// Test that edges don't intersect node bounding boxes
async function testEdgeRouting(page, graphName) {
  await page.goto(`/viz/${graphName}`);

  // Wait for layout to complete
  await page.waitForSelector('.react-flow__node');
  await page.waitForTimeout(500);  // Let B-splines settle

  // Extract node positions and edge paths from DOM
  const nodes = await page.$$eval('.react-flow__node', elements =>
    elements.map(el => {
      const rect = el.getBoundingClientRect();
      return {
        id: el.getAttribute('data-id'),
        left: rect.left,
        right: rect.right,
        top: rect.top,
        bottom: rect.bottom
      };
    })
  );

  const edges = await page.$$eval('.react-flow__edge path', paths =>
    paths.map(path => {
      const length = path.getTotalLength();
      const samples = [];
      for (let i = 0; i <= 20; i++) {
        const point = path.getPointAtLength(length * i / 20);
        samples.push({ x: point.x, y: point.y });
      }
      return samples;
    })
  );

  // Check for intersections
  const intersections = [];
  edges.forEach((edgePoints, edgeIdx) => {
    nodes.forEach(node => {
      edgePoints.forEach(point => {
        if (point.x >= node.left && point.x <= node.right &&
            point.y >= node.top && point.y <= node.bottom) {
          intersections.push({
            edge: edgeIdx,
            node: node.id,
            point
          });
        }
      });
    });
  });

  expect(intersections).toHaveLength(0);
}
```

### Visual Regression Testing with Playwright
```javascript
// Source: Playwright visual testing best practices 2026
// tests/viz/edge-routing.spec.js
import { test, expect } from '@playwright/test';

test.describe('Edge Routing Regression Tests', () => {
  test('complex_rag - edges do not cross nodes', async ({ page }) => {
    await page.goto('/viz/complex_rag');
    await page.waitForSelector('.react-flow__node');

    // Take screenshot and compare to baseline
    await expect(page).toHaveScreenshot('complex_rag.png', {
      maxDiffPixels: 100,  // Allow minor rendering differences
      threshold: 0.2
    });

    // Automated intersection test
    await testEdgeRouting(page, 'complex_rag');
  });

  test('nested graph collapsed - edge connects to boundary', async ({ page }) => {
    await page.goto('/viz/nested_graph');

    // Collapse nested graph
    await page.click('[data-testid="collapse-button"]');
    await page.waitForTimeout(300);

    // Verify edge connects to parent node boundary (not inner node)
    const edgePath = await page.$('.react-flow__edge path');
    const edgeEnd = await edgePath.evaluate(path => {
      const length = path.getTotalLength();
      return path.getPointAtLength(length);  // End point
    });

    const parentNode = await page.$('[data-id="parent-graph"]');
    const parentBounds = await parentNode.boundingBox();

    // Edge should end at parent's top boundary (with small margin for stems)
    expect(Math.abs(edgeEnd.y - parentBounds.top)).toBeLessThan(15);
  });

  test('double nested graph - routes to deepest inner node', async ({ page }) => {
    await page.goto('/viz/double_nested');

    // All nodes expanded
    const innerNodeId = 'grandchild-node';
    const innerNode = await page.$(`[data-id="${innerNodeId}"]`);
    const innerBounds = await innerNode.boundingBox();

    // Find edge targeting this node
    const edgePath = await page.$(`[data-id="edge-to-${innerNodeId}"] path`);
    const edgeEnd = await edgePath.evaluate(path => {
      const length = path.getTotalLength();
      return path.getPointAtLength(length);
    });

    // Edge should end at inner node's boundary (not parent or grandparent)
    expect(edgeEnd.x).toBeGreaterThanOrEqual(innerBounds.left - 5);
    expect(edgeEnd.x).toBeLessThanOrEqual(innerBounds.right + 5);
    expect(Math.abs(edgeEnd.y - innerBounds.top)).toBeLessThan(15);
  });
});
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Inline coordinate arithmetic | Explicit transformation functions | Phase 3 (this work) | Eliminates coordinate space bugs, makes transformations testable |
| Manual depth tracking | Recursive traversal with natural termination | Phase 3 (this work) | Handles arbitrary nesting depth without special cases |
| Python computes edge targets | JavaScript resolves at render time | Phase 2 (previous) | Enables dynamic expand/collapse |
| Flat graph only | Hierarchical with sub-flows | Phase 1-2 (previous) | Supports nested graphs |

**Deprecated/outdated:**
- Manual coordinate offsets like `n.x - w/2 - layoutPadding + graphPadding` - replaced by `CoordinateTransform.layoutToParentRelative()`
- Checking only `i < target.row` for blocking - must check `i <= target.row` and skip target node
- Using parent-relative coordinates for edge routing - edges must use absolute viewport coordinates
- Special-case logic for nesting depth - unified algorithm handles all depths

## Open Questions

Things that couldn't be fully resolved:

1. **Should edge routing use control points or sample points for intersection testing?**
   - What we know: B-spline curves stay inside control point bounding box; can sample curve for precise testing
   - What's unclear: Tradeoff between performance (control point box) vs accuracy (sampled points)
   - Recommendation: Start with control point bounding box test; add sampling only if false positives occur

2. **How to handle edge routing when parent is collapsed mid-edge-path?**
   - What we know: Edge source might be inside expanded parent, target outside; or vice versa
   - What's unclear: Should edge route from parent boundary or be hidden?
   - Recommendation: Route from parent boundary (existing `resolveEdgeTargets` handles this)

3. **Should visual regression tests use exact pixel matching or threshold?**
   - What we know: Rendering can vary slightly across browsers/OS; threshold prevents flaky tests
   - What's unclear: What threshold is appropriate (maxDiffPixels, threshold percentage)
   - Recommendation: Start with threshold=0.2, maxDiffPixels=100; tune based on false positive rate

4. **How to test deeply nested graphs (3+ levels) without complex test fixtures?**
   - What we know: Need to verify arbitrary depth works; current tests only go to depth=2
   - What's unclear: Whether to generate test graphs programmatically or maintain manual fixtures
   - Recommendation: Create parameterized test that generates N-level nested graphs automatically

## Sources

### Primary (HIGH confidence)
- [React Flow Sub-Flows documentation](https://reactflow.dev/learn/layouting/sub-flows) - Parent-relative positioning, { x: 0, y: 0 } is parent top-left
- [React Flow Parent-Child example](https://reactflow.dev/examples/grouping/parent-child-relation) - Parent must appear before children in array
- [Playwright Visual Testing 2026](https://www.browserstack.com/guide/playwright-visual-regression-testing) - toHaveScreenshot(), threshold tuning
- [An Overview+Detail Layout for Visualizing Compound Graphs | arXiv 2024](https://arxiv.org/html/2408.04045v1) - Entry/exit ports, edge routing through group boundaries
- Current codebase `CLAUDE.md` - Documents existing architecture, known issues, coordinate systems

### Secondary (MEDIUM confidence)
- [React Flow Issue #3393 - Absolute positioning in sub flows](https://github.com/wbkd/react-flow/issues/3393) - Clarifies relative coordinate requirements
- [Intersection Tests in 2D](https://noonat.github.io/intersect/) - Bounding box overlap, point-in-box algorithms
- [B-spline Wikipedia](https://en.wikipedia.org/wiki/B-spline) - Curve stays inside control point bounding box property
- [yFiles Hierarchical Layout](https://docs.yworks.com/yfiles-html/dguide/layout/hierarchical_layout.html) - Coordinate assignment in hierarchical layouts
- [Playwright Best Practices 2026](https://www.browserstack.com/guide/playwright-best-practices) - Visual testing workflows, assertion strategies

### Tertiary (LOW confidence)
- [Graphviz spline routing issue](https://github.com/ellson/MOTHBALLED-graphviz/issues/1284) - "Unable to reclaim box space" error (indicates coordinate system bug)
- [XYFlow Discussion #2806 - Edge collision avoidance](https://github.com/xyflow/xyflow/discussions/2806) - Community patterns for custom edge routing
- WebSearch results on B-spline intersection - concepts are sound but need verification with actual implementation

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - React Flow, kiwi.js, B-splines are established and documented
- Architecture patterns: HIGH - React Flow docs explicitly document parent-relative positioning, current codebase has working examples
- Pitfalls: HIGH - Directly observed in codebase (CLAUDE.md documents issues, git history shows regression commits)
- Testing approaches: HIGH - Playwright visual testing is industry standard 2026, geometric intersection tests are textbook algorithms

**Research date:** 2026-01-21
**Valid until:** ~90 days (React Flow API stable, core algorithms evergreen, Playwright patterns mature)
