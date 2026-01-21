# Technology Stack: React Flow Edge Routing with Nested Nodes

**Project:** Hypergraph Visualization
**Research Date:** 2026-01-21
**Focus:** Edge routing with parent-child (nested/compound) nodes
**Confidence:** HIGH

## Executive Summary

React Flow uses **relative positioning for child nodes** but **absolute coordinates for edge routing**. Custom edges receive `sourceX`, `sourceY`, `targetX`, `targetY` in absolute viewport coordinates, regardless of node nesting. The key challenge: when nodes are nested, you must manually track and convert between relative (child position) and absolute (edge coordinate) spaces.

**The core issue**: React Flow v11.11.0+ renamed `parentNode` → `parentId` and enforces relative child positioning, but edges always route in absolute coordinates. Your layout engine must maintain both coordinate systems and transform between them correctly.

## React Flow Version

**Current in Hypergraph**: Unknown (bundled UMD build in `reactflow.umd.js`)
**Latest stable**: v12.3.3 (October 2024)
**Breaking changes**: v12.0.0 (July 2024) finalized `parentNode` → `parentId` rename

**Recommendation**: Verify bundled version. If pre-v12, upgrade to v12.3+ for better parent-child handling and bug fixes.

## Core APIs for Nested Node Edge Routing

### 1. Parent-Child Node Structure

```javascript
// Parent node (normal positioning)
{
  id: 'parent-1',
  type: 'group',  // or custom type
  position: { x: 100, y: 100 },  // Absolute viewport coordinates
  style: { width: 400, height: 300 }
}

// Child node (relative positioning)
{
  id: 'child-1',
  parentId: 'parent-1',  // Links to parent (was parentNode pre-v12)
  position: { x: 10, y: 50 },  // RELATIVE to parent's top-left corner
  extent: 'parent'  // Optional: constrains dragging within parent bounds
}
```

**Critical rules**:
- Child `position` is **relative** to parent's top-left corner
- `position: { x: 0, y: 0 }` = top-left corner of parent
- Parent nodes **must appear before children** in nodes array
- Moving parent automatically moves all children

### 2. Edge Coordinate System

```javascript
// Custom edge component receives these props
function CustomEdge({
  sourceX,      // Absolute X coordinate in viewport
  sourceY,      // Absolute Y coordinate in viewport
  targetX,      // Absolute X coordinate in viewport
  targetY,      // Absolute Y coordinate in viewport
  sourcePosition,  // 'top' | 'bottom' | 'left' | 'right'
  targetPosition,
  // ... other props
}) {
  // Edge coordinates are ALWAYS absolute, even when nodes are nested
  const [path] = getBezierPath({
    sourceX, sourceY, targetX, targetY,
    sourcePosition, targetPosition
  });

  return <BaseEdge path={path} />;
}
```

**Key points**:
- Edge coordinates (`sourceX/Y`, `targetX/Y`) are **always absolute** viewport coordinates
- React Flow calculates these from node positions + handle positions
- For nested nodes, React Flow internally converts relative → absolute

### 3. The Absolute Positioning Problem

**GitHub Issue #3393**: "Allow for absolute positioning in sub flows"
- **Status**: Labeled "next major" (not yet implemented as of late 2024)
- **Problem**: Child nodes MUST use relative coordinates, but data often comes in absolute coordinates
- **Impact**: Complex transformations needed when:
  - Backend provides absolute positions
  - Dynamically determining parent-child relationships
  - Resizing parent nodes (breaks relative positions)

**Current workarounds**:
1. **Manual calculation** (recursive): Traverse parent hierarchy to accumulate absolute positions, then convert to relative
2. **Flat structure**: Avoid `parentId` entirely, handle grouping visually only

### 4. Edge Rendering Behavior with Nested Nodes

```javascript
// Default rendering layers (back to front):
// 1. Edges (below nodes)
// 2. Nodes
// 3. Edges connected to nodes WITH parents (above nodes)

// To customize edge z-index:
const defaultEdgeOptions = {
  zIndex: 1  // Higher = rendered on top
};

<ReactFlow
  nodes={nodes}
  edges={edges}
  defaultEdgeOptions={defaultEdgeOptions}
/>
```

**Important**: Edges connected to nested nodes render **above** normal nodes by default (prevents parent container from obscuring edges).

## Hypergraph Implementation Analysis

Based on code review of `layout.js` and `renderer.py`:

### Current Architecture

**Layout Engine** (`layout.js`):
- Uses `ConstraintLayout.graph()` for positioning (custom constraint solver)
- Returns node positions in **layout coordinate space** (with internal padding ~50px)
- Converts to React Flow format with coordinate transformations

**Nested Graph Handling** (`performRecursiveLayout`):
- Layouts children first (deepest-first)
- Converts child positions: layout space → parent-relative
- Edge points transformed: layout space → absolute viewport coordinates

### The Regression (commit b111b075)

**What changed**:
```javascript
// BEFORE (worked correctly):
var boundsMinX = childResult.size.min.x;
var boundsMinY = childResult.size.min.y;
var childX = n.x - w / 2 - boundsMinX;
var childY = n.y - h / 2 - boundsMinY;

// AFTER (broke edge routing):
var layoutPadding = 50;  // Assumed constant
var childX = n.x - w / 2 - layoutPadding + GRAPH_PADDING;
var childY = n.y - h / 2 - layoutPadding + GRAPH_PADDING;
```

**Why it broke**:
- Assumption: layout padding is constant (50px)
- Reality: `ConstraintLayout.graph()` normalizes using `size.min`, which varies per graph
- Edge points use same transform, so mismatch = edges don't connect to nodes

**What's needed**:
- Use actual `childResult.size.min.x/y` (the layout's origin offset)
- OR understand ConstraintLayout's coordinate system better
- Consistent transform for both nodes and edge points

### Edge Point Transformation

```javascript
// Current (lines 562-568 in layout.js):
childResult.edges.forEach(function(e) {
  var offsetPoints = (e.points || []).map(function(pt) {
    return {
      x: pt.x - layoutPadding + absOffsetX,
      y: pt.y - layoutPadding + absOffsetY
    };
  });
  // ...
});
```

**Critical**: Edge point transform MUST match node position transform exactly.

If nodes use `size.min`, edges must too:
```javascript
x: pt.x - childResult.size.min.x + absOffsetX
```

If nodes use constant `layoutPadding`, edges must match:
```javascript
x: pt.x - layoutPadding + absOffsetX
```

Mismatch = edges offset from nodes.

## React Flow Path Utilities

For custom edge rendering with waypoints (like your B-spline edges):

```javascript
import { getBezierPath, getStraightPath, getSmoothStepPath } from 'reactflow';

// Simple curved edge
const [path] = getBezierPath({
  sourceX, sourceY, targetX, targetY,
  sourcePosition, targetPosition
});

// Edge with custom waypoints (for complex routing)
// React Flow doesn't provide this - you build the SVG path manually
function buildCustomPath(points) {
  if (points.length === 0) return '';

  let path = `M ${points[0].x},${points[0].y}`;
  for (let i = 1; i < points.length; i++) {
    path += ` L ${points[i].x},${points[i].y}`;
  }
  return path;
}
```

**Your implementation**: Uses B-spline interpolation (not React Flow's built-in paths)
- Edge points calculated by ConstraintLayout
- Points are in layout coordinate space
- Must transform to absolute viewport coordinates for React Flow

## Recommended Patterns

### Pattern 1: Maintain Dual Coordinate Systems

```javascript
// Track both coordinate systems
const nodePositions = new Map();  // Absolute positions for edge routing

// When positioning child nodes:
const relativeX = absoluteX - parentAbsX - parentPadding;
const relativeY = absoluteY - parentAbsY - parentPadding - headerHeight;

// Store both
nodePositions.set(childId, { x: absoluteX, y: absoluteY });  // For edges
reactFlowNode.position = { x: relativeX, y: relativeY };     // For React Flow
```

**Used in**: `performRecursiveLayout()` - stores absolute positions in `nodePositions` Map for edge routing while setting relative positions on nodes.

### Pattern 2: Consistent Transform Functions

```javascript
function layoutToViewport(layoutPos, layoutOrigin, viewportOrigin) {
  return {
    x: layoutPos.x - layoutOrigin.x + viewportOrigin.x,
    y: layoutPos.y - layoutOrigin.y + viewportOrigin.y
  };
}

// Use for BOTH nodes and edges
const childPos = layoutToViewport(layoutNode, childResult.size.min, parentTopLeft);
const edgePoint = layoutToViewport(layoutPoint, childResult.size.min, parentTopLeft);
```

**Key**: Same function for nodes and edges guarantees consistency.

### Pattern 3: Validate Coordinate Transforms

```javascript
// After layout, verify edges connect correctly
function validateEdgeConnections(nodes, edges, nodePositions) {
  edges.forEach(edge => {
    const sourceNode = nodes.find(n => n.id === edge.source);
    const targetNode = nodes.find(n => n.id === edge.target);

    const sourceAbs = nodePositions.get(edge.source);
    const targetAbs = nodePositions.get(edge.target);

    // Edge points should match node absolute positions (within tolerance)
    const firstPoint = edge.data.points[0];
    const lastPoint = edge.data.points[edge.data.points.length - 1];

    console.assert(
      Math.abs(firstPoint.x - (sourceAbs.x + sourceNode.width/2)) < 1,
      'Edge start mismatch'
    );
    // ... similar for target
  });
}
```

## Debugging Edge Routing Issues

### Common Symptoms & Causes

| Symptom | Likely Cause | Check |
|---------|-------------|-------|
| Edges don't connect to nodes | Coordinate transform mismatch | Node and edge using different origin offsets |
| Edges offset by constant amount | Wrong padding value | Using `layoutPadding` instead of `size.min` |
| Edges correct for root, wrong for nested | Not accounting for parent offset | Missing `parentPos.x/y` in absolute calculation |
| Edges jump on expansion | Expansion state not triggering re-layout | `expansionState` not in `useEffect` dependencies |

### Debug Workflow

1. **Log coordinate spaces**:
```javascript
console.log('Layout space:', { x: n.x, y: n.y });
console.log('Size.min:', childResult.size.min);
console.log('Parent abs:', parentPos);
console.log('Relative:', { x: relativeX, y: relativeY });
console.log('Absolute:', nodePositions.get(n.id));
```

2. **Verify transforms match**:
- Node transform: `layoutX - originX + offsetX`
- Edge transform: `layoutX - originX + offsetX` (must be identical)

3. **Visual inspection**:
- Render debug markers at edge points
- Render debug boxes at node boundaries
- Compare absolute positions with React Flow's calculated handle positions

## Alternative Approaches

### Option 1: Flat Layout + Visual Grouping

**Pros**:
- Avoids React Flow's parent-child complexity entirely
- All nodes use absolute positioning (simpler)
- No coordinate transform bugs

**Cons**:
- Can't use React Flow's built-in parent dragging
- Can't use `extent: 'parent'` for bounds
- Must manually implement all grouping behavior

**When to use**: If parent-child relationships are purely visual (no interaction needed)

### Option 2: Post-Layout Transform

**Pros**:
- Layout engine works in absolute space (simpler)
- Transform to relative at the end
- Single source of truth (absolute)

**Cons**:
- Still need parent hierarchy for transform
- Expansion/collapse requires full re-layout

**When to use**: Layout engine doesn't support hierarchical layout

### Option 3: Custom Node Renderer with Absolute Children

**Pros**:
- Parent node renders children internally (portal or direct rendering)
- Complete control over child positioning
- Can use absolute positioning within parent

**Cons**:
- Bypasses React Flow's node system
- Edges may not connect correctly (React Flow expects nodes in array)
- Complex to maintain

**When to use**: Extreme customization needs, willing to reimplement React Flow features

## Specific Recommendations for Hypergraph

Based on code analysis and the regression:

### 1. Fix the Coordinate Transform

**Problem**: `layoutPadding` constant doesn't match actual layout origin

**Solution**:
```javascript
// Use childResult.size.min (the actual layout origin)
var originX = childResult.size.min.x || 0;
var originY = childResult.size.min.y || 0;

// Transform nodes
var childX = n.x - w / 2 - originX;
var childY = n.y - h / 2 - originY;

// Transform edges (MUST match)
var offsetPoints = (e.points || []).map(function(pt) {
  return {
    x: pt.x - originX + absOffsetX,
    y: pt.y - originY + absOffsetY
  };
});
```

### 2. Add Coordinate Validation

```javascript
// After layout, verify consistency
if (debugMode) {
  const nodePos = nodePositions.get(n.id);
  const expectedX = absOffsetX + childX;
  const expectedY = absOffsetY + childY;

  if (Math.abs(nodePos.x - expectedX) > 0.1) {
    console.warn('Node position mismatch:', n.id, nodePos.x, expectedX);
  }
}
```

### 3. Document Coordinate Spaces

Add to `CLAUDE.md` or code comments:

```
Coordinate Spaces in Nested Layout:

1. Layout Space: ConstraintLayout.graph() output
   - Origin: (size.min.x, size.min.y)
   - Nodes: (x, y) are center coordinates
   - Edges: points[] are path waypoints

2. Parent-Relative Space: React Flow child positions
   - Origin: (0, 0) = parent's top-left corner
   - Nodes: position.{x,y} = top-left corner
   - Used by: React Flow's parent-child system

3. Absolute Viewport Space: Edge routing coordinates
   - Origin: (0, 0) = viewport top-left
   - Nodes: tracked in nodePositions Map
   - Edges: data.points[] for custom edge rendering
   - Used by: Custom edge components, validation

Transform:
  Layout → Absolute: abs = layout - size.min + parentAbs + padding
  Layout → Relative: rel = layout - size.min + GRAPH_PADDING
```

### 4. Verify React Flow Version

```javascript
// Check bundled version
console.log('React Flow version:', ReactFlow.version);  // If exposed

// Or check bundle metadata
// Search reactflow.umd.js for version string
```

If pre-v12: Consider upgrading for parent-child improvements and active support.

## Sources

### Official React Flow Documentation
- [Sub Flows - React Flow](https://reactflow.dev/learn/layouting/sub-flows) - Parent-child relationships, relative positioning
- [Custom Edges - React Flow](https://reactflow.dev/learn/customization/custom-edges) - Edge component props, coordinate system
- [Edge API Reference](https://reactflow.dev/api-reference/types/edge) - Source/target properties
- [Node API Reference](https://reactflow.dev/api-reference/types/node) - parentId, position, extent properties
- [Migrate to React Flow 12](https://reactflow.dev/learn/troubleshooting/migrate-to-v12) - parentNode → parentId change
- [React Flow 12 Release](https://reactflow.dev/whats-new/2024-07-09) - July 2024 release notes

### GitHub Issues & Discussions
- [Allow for absolute positioning in sub flows #3393](https://github.com/wbkd/react-flow/issues/3393) - Absolute positioning limitation and workarounds
- [Parent Child Relation Example](https://reactflow.dev/examples/grouping/parent-child-relation) - Working example

### Third-Party Solutions
- [react-flow-automated-layout](https://github.com/Jalez/react-flow-automated-layout) - Automated layout for nested nodes with enhanced edge routing
- [react-flow-smart-edge](https://github.com/tisoap/react-flow-smart-edge) - Collision-avoiding edges (doesn't solve nested node transforms)

## Confidence Assessment

| Aspect | Level | Reasoning |
|--------|-------|-----------|
| React Flow API | HIGH | Official documentation verified, consistent patterns |
| Parent-child positioning | HIGH | Clear from docs and examples |
| Edge coordinate system | HIGH | Custom edge props well-documented |
| Absolute positioning limitation | HIGH | GitHub issue with clear status |
| Hypergraph regression cause | MEDIUM | Code analysis suggests transform mismatch, needs verification |
| Fix approach | MEDIUM | Theory sound, needs testing to confirm |

## Open Questions

1. **Exact React Flow version in bundle**: Need to extract from UMD build or check build process
2. **ConstraintLayout coordinate system**: Is `size.min` always the origin? Need to verify assumption
3. **Edge calculation in ConstraintLayout**: Are edge points guaranteed to be in same space as nodes?
4. **Regression verification**: Does reverting to `size.min` actually fix the issue?

## Next Steps for Fix

1. Identify exact cause: Add logging to compare `layoutPadding` vs `size.min` values
2. Test hypothesis: Revert edge transform to use `size.min` (match node transform)
3. Validate: Check edge connections visually and via automated tests
4. Document: Update CLAUDE.md with coordinate space explanation
5. Consider upgrade: Evaluate upgrading to React Flow v12.3+ for better support
