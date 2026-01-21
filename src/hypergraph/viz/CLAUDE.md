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
1. Set initial viewport position
2. Wait for DOM update (double `requestAnimationFrame`)
3. Find **topmost** and **bottommost** edges across ALL visible nodes
   - Query all `.react-flow__node` elements
   - Get inner visible content (`.group.rounded-lg` or first child) - excludes shadows
   - Track min top and max bottom using `getBoundingClientRect()`
4. Calculate margins: `topMargin = topmostEdge - viewport.top`, `bottomMargin = viewport.bottom - bottommostEdge`
5. If `|topMargin - bottomMargin| > 2px`, adjust viewport.y by half the difference
6. Verify correction with another measurement

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

## File Locations

- **Edge routing logic**: `assets/constraint-layout.js` (routing function ~line 530)
- **Routing config**: `assets/constraint-layout.js` (layoutConfig ~line 820)
- **Bounds calculation**: `assets/constraint-layout.js` (bounds function ~line 848)
- **Edge rendering**: `html_generator.py` (CustomEdge component ~line 652)
- **Edge styling**: `html_generator.py` (edgeOptions ~line 2058)
- **Centering logic**: `html_generator.py` (fitWithFixedPadding ~line 1764)
- **Node dimensions**: `html_generator.py` (calculateDimensions ~line 1154)
