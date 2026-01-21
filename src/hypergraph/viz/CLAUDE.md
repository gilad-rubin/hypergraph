# Visualization System Insights

Documentation of key findings and design decisions for the hypergraph visualization system.

## Edge Routing Architecture

### Stem Structure
- Edges connect to nodes via "stems" - control points that guide the B-spline curve
- **Source stem**: Points at the bottom of source nodes (where edges exit)
- **Target stem**: Points at the top of target nodes (where edges enter)
- Simplified from 3 points to 2 points to reduce vertical segments at node boundaries
- All stem points share the same X coordinate (node center), creating vertical entry/exit

### Key Parameters (`constraint-layout.js` routing config)
- `stemMinSource`: Minimum vertical distance below source node (currently 0)
- `stemMinTarget`: Minimum vertical distance above target node (controls arrow visibility, currently ~6-12px)
- `stemUnit`: Base multiplier for stem spread (affects Y offset variation)
- `stemMax`: Cap on maximum stem spread
- `spaceX/spaceY`: Clearance around nodes for edge routing

## Shoulder Waypoints (Fan-out Effect)

### Purpose
Creates natural-looking curves for edges that travel horizontally to reach their targets, avoiding straight diagonal lines.

### Implementation (`constraint-layout.js` lines 604-617)
```javascript
// Only for edges without obstacles (direct paths)
if (horizontalDist > 20 && verticalDist > 50) {
  const shoulderX = source.x + (target.x - source.x) * 0.6;  // 60% toward target
  const shoulderY = nodeBottom(source) + verticalDist * 0.5;  // 50% down
  edge.points.push({ x: shoulderX, y: shoulderY });
}
```

### Tuning
- Higher X percentage (e.g., 0.85) = more pronounced horizontal fan, sharper bend
- Lower X percentage (e.g., 0.5) = gentler, more diagonal curve
- Lower Y percentage = bend happens earlier (more dramatic)
- Higher Y percentage = bend happens later (smoother)

## Natural Path Calculation

### Target-Guided Routing
```javascript
const naturalX = source.x + (target.x - source.x) * 0.5;
```
- Routes edges toward their destination from the start
- Previously used tiny `sourceOffsetX` which kept edges bundled near center
- Now edges fan out early based on where they're going

### Corridor Routing (for edges with obstacles)
- Finds corridors around blocking nodes
- Uses `naturalX` to choose left or right corridor
- Tracks used corridor positions to prevent edge overlap

## Edge Styling

### Stroke Properties (`html_generator.py`)
- Width: 1.5px (reduced from 2px for cleaner look)
- Opacity: 90% (`rgba` with 0.9 alpha)
- Color: Theme-dependent (light: `#94a3b8`, dark: `#64748b`)

### Arrows/Markers
- Uses `MarkerType.ArrowClosed` from ReactFlow
- Marker color must be hex (rgba doesn't work with markers)
- Arrow visibility requires minimum `stemMinTarget` (~6-12px)

## Lessons from Kedro-viz

### What They Do Differently
1. **Stems are always centered** on nodes (no X offset on stems)
2. **Fanning happens in routing waypoints**, not at node attachment points
3. For short edges without obstacles, natural curve from `source.x` to `target.x`

### Why Stem Fanning Failed
- Adding X offsets to stems created awkward B-spline curves
- Vertical stems impose direction constraints at endpoints
- B-spline overshoots trying to smoothly connect offset stems
- Solution: Keep stems centered, add intermediate waypoints for fanning

## Common Issues and Fixes

### "Narrow funnel then sudden widening"
- **Cause**: Small `sourceOffsetX` in corridor routing kept edges bundled
- **Fix**: Use `naturalX = source.x + (target.x - source.x) * 0.5` to guide routing toward target

### "Awkward hook-shaped curves"
- **Cause**: Stem X offsets conflicting with B-spline interpolation
- **Fix**: Keep stems at node center, use shoulder waypoints instead

### "Edges missing arrows"
- **Cause**: `stemMinTarget` too small (0) - arrow hidden behind node
- **Fix**: Set `stemMinTarget` to at least 6-12px

### "Too much vertical segment at node edge"
- **Cause**: 3-point stem structure with vertical alignment
- **Fix**: Simplify to 2-point stem with minimal offset

## Centering and Bounds Calculation

### Bounds Calculation (`constraint-layout.js`)

The `bounds()` function must use **node edges**, not node centers:

```javascript
// CORRECT - use node edges
const left = nodeLeft(node);   // node.x - node.width * 0.5
const right = nodeRight(node); // node.x + node.width * 0.5
if (left < size.min.x) size.min.x = left;
if (right > size.max.x) size.max.x = right;

// WRONG - using centers causes clipping
if (node.x < size.min.x) size.min.x = node.x;  // Don't do this!
```

Helper functions `nodeLeft`, `nodeRight`, `nodeTop`, `nodeBottom` exist at the top of the file.

### Viewport Centering (`html_generator.py`)

Content must be centered in the **full viewport**, not "available width":

```javascript
// CORRECT - center in full viewport
const targetScreenCenterX = viewportWidth / 2;

// WRONG - excludes button panel, shifts content left
const targetScreenCenterX = availableWidth / 2;  // Don't do this!
```

### Node Dimension Calculation (`html_generator.py`)

The `calculateDimensions()` function must handle ALL node types. Missing a type causes width mismatch between layout and render:

```javascript
// INPUT nodes use same styling as DATA nodes
if (n.data?.nodeType === 'DATA' || n.data?.nodeType === 'INPUT') {
  height = 36;
  width = /* calculated from label */;
}
```

### Vertical Centering Correction (`html_generator.py`)

The graph's vertical centering requires a post-layout DOM measurement because:
1. Calculated positions don't account for actual rendered node dimensions
2. React Flow wrapper elements differ from visible node content
3. Shadows extend beyond node bounds but shouldn't affect centering

**Algorithm** (in `fitWithFixedPadding`):
1. Set initial viewport position (centered in true viewport center)
2. Wait for DOM update (double `requestAnimationFrame`)
3. Find **topmost** and **bottommost** edges across ALL visible nodes
   - Query all `.react-flow__node` elements
   - Get inner visible content (`.group.rounded-lg` or first child) - excludes shadows
   - Track min top and max bottom using `getBoundingClientRect()`
4. **Calculate ALL corrections at once** (avoid sequential adjustments that fight each other):
   - Vertical centering: `diffY = topMargin - bottomMargin`, shift by `diffY/2`
   - Horizontal centering: `diffX = topRowCenter - viewportCenter`, shift by `diffX`
   - Right margin constraint: After centering, would rightmost node be within `PADDING_RIGHT` (100px) of buttons? If so, calculate additional left shift needed.
5. **Apply ALL corrections in ONE setViewport call**
6. Verify correction with measurement (for debug display only)

**Key principle**: Calculate the final position first, then apply once. Never do: center → measure → adjust → measure again. This avoids the loop where centering and margin constraints fight each other.

```javascript
// Key: measure INNER node content, not React Flow wrapper
const innerNode = wrapper.querySelector('.group.rounded-lg') || wrapper.firstElementChild;
const rect = innerNode.getBoundingClientRect();  // Excludes shadows
```

### Debug Overlay

Enable with `window.__hypergraph_debug_viz = true` in browser console before rendering.

Shows:
- Green lines at content top/bottom edges
- Margin labels (T: top margin, B: bottom margin)
- Badge shows BEFORE/AFTER correction values
- Badge turns green when margins are equal (diff ≤ 2px)

## Double-Wiggle Issue (Unsolved)

### The Problem
Edges from left-to-right (e.g., `embed_expanded` → `search_expanded`) can have **two bends** instead of one smooth curve. The edge goes left first, then corrects back right.

### Root Cause Analysis
The corridor selection uses `naturalX` (midpoint between source and target):
```javascript
const naturalX = source.x + (target.x - source.x) * 0.5;
// Then chooses corridor closer to naturalX
if (Math.abs(naturalX - leftCorridorX) <= Math.abs(naturalX - rightCorridorX)) {
  corridorX = leftCorridorX;  // May go LEFT even when target is RIGHT
}
```

For a left-to-right edge, the midpoint can be closer to the LEFT corridor, causing the edge to route left first, then correct back right = double wiggle.

### Attempted Fix (Reverted)
Changed corridor selection to use target direction:
```javascript
if (target.x > source.x + 10) {
  corridorX = rightCorridorX;  // Target is right, go right
} else if (target.x < source.x - 10) {
  corridorX = leftCorridorX;   // Target is left, go left
}
```

**Why it failed**: This fix caused edges to go OVER nodes in the target row. The blocking detection only checks rows BETWEEN source and target (`i < target.row`), missing nodes in the same row as the target.

### Further Attempts (All Reverted)
1. **Include target row in blocking**: `i <= target.row` with `if (node === target) continue`
   - Caused edges to connect to bottom of nodes instead of top

2. **Fix y2 calculation**: Exit corridor above target row nodes
   - Caused corridor to go extremely far (included ALL nodes in row for bounds)

3. **Filter bounds to path-overlapping nodes only**
   - Still produced awkward angles

### Current State
Reverted to original logic. Double-wiggle persists for some left-to-right edges. A proper fix requires rethinking how blocking detection and corridor selection work together.

### Key Insight
The problem is architectural: the routing algorithm detects blocking based on `naturalX` (midpoint), but the actual path depends on corridor choice. When corridor direction differs from the detection path, edges can cross undetected nodes.

## Nested Graph Rendering

### Architecture

Nested graphs (pipelines within pipelines) are rendered using React Flow's parent-child node system:

1. **Python Renderer** (`renderer.py`):
   - Sets `parentNode` on child nodes to reference parent pipeline
   - Sets `extent: "parent"` to constrain children within parent bounds
   - Uses `_is_nested=True` to skip INPUT_GROUP creation for inner graphs (they're pass-through)

2. **Layout Engine** (`layout.js`):
   - `performRecursiveLayout()` handles nested graphs bottom-up (deepest children first)
   - Child layout runs first, then parent container is sized based on child bounds
   - Child positions are relative to parent's top-left corner

3. **Key Constants** (`layout.js`):
   - `GRAPH_PADDING = 40` - padding inside container around children
   - `HEADER_HEIGHT = 32` - height of pipeline label header

### Container Sizing

Container size is calculated from child layout bounds:
```javascript
nodeDimensions.set(graphNode.id, {
  width: childResult.size.width + GRAPH_PADDING * 2,
  height: childResult.size.height + GRAPH_PADDING * 2 + HEADER_HEIGHT,
});
```

### Child Position Calculation

Children are positioned relative to parent's content area:
```javascript
// Constraint layout already includes its own padding (50px default)
// Adjust from layout coordinates to parent-relative coordinates
var childX = n.x - w / 2 - layoutPadding + GRAPH_PADDING;
var childY = n.y - h / 2 - layoutPadding + GRAPH_PADDING;

// Final position includes header offset
position: {
  x: childX,
  y: childY + HEADER_HEIGHT
}
```

### Height-Based Separation (Overlap Prevention)

Row constraints now account for node heights to prevent tall containers from overlapping:
```javascript
// In createRowConstraints()
var heightBasedSeparation = (sourceHeight / 2) + (targetHeight / 2) + layoutConfig.spaceY;
```

This ensures containers don't overlap with external nodes below them.

### Common Issues

**"Duplicate __inputs__ nodes"**
- Cause: Both outer and inner graphs create INPUT_GROUP nodes with same ID
- Fix: Skip INPUT_GROUP creation for nested graphs (`_is_nested=True`)

**"External nodes appear inside container"**
- Cause: Fixed spaceY didn't account for tall container heights
- Fix: Height-based separation in row constraints

**"Too much space between container and external nodes"**
- Cause: spaceY (140px) + height-based separation was excessive
- Fix: Reduce spaceY to 50px since heights are now accounted for

**"Excessive padding inside container"**
- Cause: Container sizing added GRAPH_PADDING on top of layoutPadding already in childResult.size
- Fix: Subtract layoutPadding from childResult.size before adding GRAPH_PADDING

**"Edges not connecting to inner nodes"**
- Cause: Layout filtered edges into "root edges" (both ends at root) or "internal edges" (both ends inside container), missing cross-hierarchy edges
- Fix: Added Step 4 in `performRecursiveLayout` to detect and route cross-hierarchy edges using absolute positions stored in `nodePositions`

### Cross-Hierarchy Edge Routing

Edges that cross hierarchy levels (e.g., from root-level INPUT_GROUP to a child node inside a container) need special handling:

1. **Detection**: After processing root and internal edges, find any unprocessed edges
2. **Positioning**: Use absolute coordinates from `nodePositions` map (populated for all nodes)
3. **Path**: Simple two-point path from source center-bottom to target center-top

```javascript
// In performRecursiveLayout() - Step 4
var processedEdgeIds = new Set(allPositionedEdges.map(e => e.id));
var crossHierarchyEdges = edges.filter(e => !processedEdgeIds.has(e.id));

crossHierarchyEdges.forEach(function(e) {
  var sourcePos = nodePositions.get(e.source);  // Absolute position
  var targetPos = nodePositions.get(e.target);
  // ... create edge path using absolute coordinates
});
```

### Collapsed vs Expanded Edge Routing

When a pipeline container is:
- **Collapsed**: INPUT_GROUP edges connect to the container itself
- **Expanded**: INPUT_GROUP edges connect to the container for **layout** but visually route to inner nodes

This two-step approach is necessary because layout positioning uses edge targets:
1. **renderer.py**: Always creates edge to container, but stores `innerTargets` in edge data
2. **layout.js**: During root edge positioning, detects `innerTargets` and re-routes the visual path

```python
# renderer.py - always target container for layout, store inner info for visual
edges.append({
    "source": group_id,
    "target": target,  # Container ID for layout positioning
    "data": {
        "edgeType": "input",
        "innerTargets": inner_targets if inner_targets else None,  # For visual routing
    },
})
```

```javascript
// layout.js - visually route to inner node if innerTargets present
if (innerTargets && innerTargets.length > 0) {
    var innerPos = nodePositions.get(innerTargets[0]);
    // Create path to inner node instead of container
}
```

### innerSources for Outbound Edges

Similar to `innerTargets` for inbound edges, `innerSources` handles outbound edges from expanded pipelines:

1. **renderer.py**: When an edge originates from an expanded GraphNode, find inner nodes that produce the output
2. **renderer.py**: Store `innerSources` in edge data (e.g., the `normalize` node that outputs `normalized`)
3. **layout.js**: During edge routing, detect `innerSources` and reroute edge START to inner node

```python
# renderer.py - find inner sources for expanded pipeline outputs
if isinstance(source_node, GraphNode) and depth > 0:
    inner_graph = source_node.graph
    for inner_name, inner_node in inner_graph.nodes.items():
        if value_name in inner_node.outputs:
            inner_sources.append(inner_name)
```

### Shadow Offset

CSS `drop-shadow` on function nodes extends the visual bounds. Edges should connect to the **visual border**, not the shadow boundary.

**Constants** (`layout.js`):
- `SHADOW_OFFSET = 14` - pixels to subtract from function node bottom

**Application** (Step 4 edge rerouting):
```javascript
var hasShadow = innerSourceType === 'FUNCTION' || innerSourceType === 'PIPELINE';
var shadowAdjust = hasShadow ? SHADOW_OFFSET : 0;
startPoint = {
    x: innerSourcePos.x + innerSourceDims.width / 2,
    y: innerSourcePos.y + innerSourceDims.height - shadowAdjust
};
```

### CustomEdge and reroutedToInner Flag

When edges are rerouted to inner nodes, the `data.points` array contains custom start/end coordinates. React Flow provides its own `sourceX/sourceY` based on container positions, which would override our routing.

**Solution**: Set `data.reroutedToInner = true` and check it in CustomEdge:

```javascript
// components.js - CustomEdge
if (data && data.points && data.points.length > 0) {
    var points = data.points.slice();
    // Only override endpoints if NOT rerouted to inner nodes
    if (!data.reroutedToInner) {
        points[0] = { x: sourceX, y: sourceY };
        points[points.length - 1] = { x: targetX, y: targetY };
    }
    // Use actualSourceX/Y from points for path calculation
}
```

## File Locations

- **Edge routing logic**: `assets/constraint-layout.js` (routing function ~line 530)
- **Routing config**: `assets/constraint-layout.js` (layoutConfig ~line 820)
- **Bounds calculation**: `assets/constraint-layout.js` (bounds function ~line 848)
- **Edge rendering**: `html_generator.py` (CustomEdge component ~line 652)
- **Edge styling**: `html_generator.py` (edgeOptions ~line 2058)
- **Centering logic**: `html_generator.py` (fitWithFixedPadding ~line 1764)
- **Node dimensions**: `html_generator.py` (calculateDimensions ~line 1154)
