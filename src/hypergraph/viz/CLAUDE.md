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

## File Locations

- **Edge routing logic**: `assets/constraint-layout.js` (routing function ~line 530)
- **Routing config**: `assets/constraint-layout.js` (layoutConfig ~line 820)
- **Edge rendering**: `html_generator.py` (CustomEdge component ~line 652)
- **Edge styling**: `html_generator.py` (edgeOptions ~line 2058)
