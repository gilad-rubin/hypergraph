# Edge Routing Pitfalls for Nested/Compound Graphs

**Domain:** Graph visualization with hierarchical/nested node structures
**Researched:** 2026-01-21
**Confidence:** MEDIUM (verified with official documentation and known issues)

## Executive Summary

Edge routing for nested graphs fails in predictable ways when implementations special-case nesting depth instead of generalizing coordinate transformations. The core problem: **coordinate systems change at each nesting level**, but implementations often assume a single global coordinate space. When a fix works for 1 level of nesting but breaks at 2+ levels, it's usually because the fix hardcoded assumptions about the coordinate hierarchy.

## Critical Pitfalls

### Pitfall 1: Coordinate System Confusion

**What goes wrong:** Edges rendered in the wrong coordinate space appear to cross through nodes or have gaps at boundaries. Logic that works for flat graphs breaks when nodes have parents.

**Why it happens:**
- In nested structures, each parent node establishes its own coordinate system
- Child positions are relative to parent's top-left corner `{ x: 0, y: 0 }`
- Edge routing calculations use absolute coordinates but edge rendering uses relative coordinates
- Mixing coordinate systems without transformation causes misalignment

**Real-world example from ReactFlow:**
```javascript
// Child node position is relative to parent
child.position = { x: 50, y: 100 }  // 50px from parent's left edge

// But edge routing needs ABSOLUTE screen coordinates
// Must transform: absoluteX = parent.x + child.position.x
```

**Consequences:**
- Edges connect to wrong points (gaps at boundaries)
- Edges appear to cross through nodes they should avoid
- Edge endpoints drift as nesting depth increases

**Prevention:**
- Maintain explicit coordinate transformation chain from root to target
- Never assume edge coordinates are in the same space as node positions
- Use transformation helpers: `getAbsolutePosition(node)`, `transformToParentSpace(point, parent)`
- Test with at least 3 levels of nesting (flat, 1-deep, 2-deep)

**Detection:**
- Edges work at depth 1, fail at depth 2+
- Edge endpoints have consistent offset (indicates missing transformation)
- Zoom level changes visibility of the problem (zoom affects coordinate scaling)

**Sources:**
- [ReactFlow Sub Flows documentation](https://reactflow.dev/learn/layouting/sub-flows) - explains relative positioning
- [ReactFlow nested component position offset issue](https://github.com/xyflow/xyflow/discussions/4743) - describes transform problems

### Pitfall 2: Special-Casing Nesting Levels

**What goes wrong:** Code has separate logic for "no nesting", "1 level of nesting", and "deeper nesting". Works initially but becomes unmaintainable and breaks when patterns don't match assumptions.

**Why it happens:**
- Developer fixes immediate problem (1 level) without understanding root cause
- Each special case bakes in assumptions about parent-child relationships
- New edge cases (diagonal cross-hierarchy edges, skip-level connections) don't fit any special case

**Example anti-pattern:**
```javascript
// BAD: Special-casing depth
if (node.parentNode) {
  // Level 1 logic
  edgeY = parent.y + node.position.y + node.height;
} else {
  // Level 0 logic
  edgeY = node.y + node.height;
}
// Breaks at level 2+ because only checks immediate parent
```

**Better approach:**
```javascript
// GOOD: Recursive transformation
function getAbsolutePosition(node) {
  if (!node.parentNode) return { x: node.position.x, y: node.position.y };
  const parentPos = getAbsolutePosition(parent);
  return {
    x: parentPos.x + node.position.x,
    y: parentPos.y + node.position.y
  };
}
```

**Consequences:**
- Each "fix" requires another special case
- Combinatorial explosion when multiple features interact (nesting + expansion + zooming)
- Regression bugs when changing one special case affects others

**Prevention:**
- Write recursive/iterative algorithms that work for any depth
- Use uniform coordinate transformation from leaf to root
- Replace depth checks with structural checks: "Does node have parent?" not "Is this depth 1?"
- Test suite must include: flat graph, 1-level nested, 2-level nested, 3-level nested

**Detection:**
- Code has `if (depth === 1)` or `if (node.parent?.parent)` checks
- Fix for depth N breaks depth N+1
- Test coverage drops sharply for depth > 1

**Sources:**
- [Medium article on special cases and generality](https://r-shuo-wang.medium.com/special-cases-and-generality-77c43308628) - discusses hidden special cases

### Pitfall 3: Bounds Calculation Using Node Centers

**What goes wrong:** Graph appears clipped or off-center because bounds calculation uses node centers instead of node edges. Left/right nodes are cut off, viewport centering is wrong.

**Why it happens:**
- Intuitive to track `node.x` and `node.y` (center positions)
- Forgetting that nodes have width/height extending beyond center
- Bounds calculation needs **extremal points** (leftmost edge, rightmost edge, etc.)

**Example bug:**
```javascript
// BAD: Using centers
let minX = Infinity;
for (const node of nodes) {
  if (node.x < minX) minX = node.x;  // This is node CENTER
}
// Graph's left edge is actually at: minX - node.width/2

// GOOD: Using edges
let minX = Infinity;
for (const node of nodes) {
  const leftEdge = node.x - node.width * 0.5;
  if (leftEdge < minX) minX = leftEdge;
}
```

**Consequences:**
- Viewport too narrow: rightmost nodes cut off by edge
- Centering wrong: graph shifted left/right because center calculated from wrong bounds
- Zoom-to-fit fails: doesn't fit actual content

**Prevention:**
- Create helper functions for all edge calculations:
  ```javascript
  const nodeLeft = (node) => node.x - node.width * 0.5;
  const nodeRight = (node) => node.x + node.width * 0.5;
  const nodeTop = (node) => node.y - node.height * 0.5;
  const nodeBottom = (node) => node.y + node.height * 0.5;
  ```
- Use edge helpers consistently in bounds, centering, and collision detection
- Test with wide variation in node sizes (small text nodes + large group nodes)

**Detection:**
- Visual inspection: content appears shifted or clipped
- Bounds width: `calculatedBounds.width < actualVisualWidth`
- Debug overlay: draw calculated bounds, compare to visible nodes

**Source:** Internal documentation (CLAUDE.md in hypergraph viz system)

### Pitfall 4: Incorrect Blocking Detection for Nested Nodes

**What goes wrong:** Edge routing algorithm doesn't detect that it's crossing a node, so edges route through node interiors instead of around them.

**Why it happens:**
- Blocking detection checks if edge path intersects node bounding box
- **But in nested graphs, node's visual bounds must include all children**
- Algorithm checks node's rectangle but ignores that children extend beyond it
- For collapsed nested nodes, the node rectangle may be small but represents large internal content

**Example scenario:**
```
Source → [Large nested node containing many children] → Target
```
Edge routes through middle of nested node because:
1. Algorithm checks node's own bounding box (small collapsed representation)
2. Doesn't account for visual extent when expanded
3. Treats nested node like a simple node of the same dimensions

**Consequences:**
- Edges cross through group node boundaries
- Edges cross over child nodes inside groups
- Different routing when same graph is expanded vs collapsed

**Prevention:**
- When checking blocking, use node's **visual bounds** not just position
- For nested nodes: `visualBounds = union(nodeBounds, ...childBounds)`
- Consider expansion state: collapsed node's bounds should match expanded bounds for routing purposes
- Account for node padding/margins when calculating group bounds

**Detection:**
- Edge passes through node interior (not just close to edge)
- Routing changes dramatically between collapsed/expanded states
- Works for simple nodes, breaks for group nodes

**Sources:**
- [ELK nested edge containment issue](https://github.com/kieler/elkjs/issues/164) - describes problems with nested edge routing coordinates
- [An Overview/Detail Layout for Visualizing Compound Graphs](https://arxiv.org/html/2408.04045v1) - discusses visual clutter when edge routing doesn't account for hierarchy

## Moderate Pitfalls

### Pitfall 5: Post-Layout DOM Measurement Timing

**What goes wrong:** Attempting to measure DOM elements immediately after layout calculation returns stale/incorrect dimensions.

**Why it happens:**
- React/browser rendering is asynchronous
- Setting viewport position doesn't immediately update DOM
- Calling `getBoundingClientRect()` before render completes returns old values
- Single `requestAnimationFrame` sometimes insufficient for complex layouts

**Prevention:**
```javascript
// BAD: Immediate measurement
setViewport({ x, y, zoom });
const rect = element.getBoundingClientRect();  // Stale!

// GOOD: Wait for render
setViewport({ x, y, zoom });
requestAnimationFrame(() => {
  requestAnimationFrame(() => {
    const rect = element.getBoundingClientRect();  // Fresh!
  });
});
```

**Detection:**
- Measurements seem "one frame behind"
- First render wrong, subsequent renders correct
- Debug values don't match visual appearance

**Source:** Internal documentation (CLAUDE.md centering algorithm)

### Pitfall 6: Measuring Wrong DOM Elements

**What goes wrong:** Measuring React Flow wrapper elements instead of actual visible content leads to incorrect bounds (includes shadows, padding, invisible elements).

**Why it happens:**
- Wrapper divs have different dimensions than visible content
- Shadows extend beyond node visual bounds but shouldn't affect centering
- Easy to grab first element without checking what it represents

**Prevention:**
```javascript
// BAD: Measuring wrapper
const wrapper = element.querySelector('.react-flow__node');
const rect = wrapper.getBoundingClientRect();  // Includes shadows, padding

// GOOD: Measuring visual content
const wrapper = element.querySelector('.react-flow__node');
const inner = wrapper.querySelector('.group.rounded-lg') || wrapper.firstElementChild;
const rect = inner.getBoundingClientRect();  // Actual visual bounds
```

**Detection:**
- Calculated bounds larger than visible content
- Shadow offsets affecting centering
- Different dimensions between dev tools and calculation

**Source:** Internal documentation (CLAUDE.md vertical centering section)

### Pitfall 7: Sequential Corrections Fighting Each Other

**What goes wrong:** Applying centering, then adjusting for margins, then re-centering creates oscillation. Each correction interferes with previous ones.

**Why it happens:**
- Centering calculation moves graph to center
- Margin constraint moves graph left
- Re-centering undoes margin adjustment
- Each correction assumes other constraints are fixed

**Prevention:**
```javascript
// BAD: Sequential corrections
centerVertically();
ensureRightMargin();
centerHorizontally();  // Undoes margin adjustment!

// GOOD: Calculate all offsets, apply once
const centeringOffset = calculateCenteringOffset();
const marginOffset = calculateMarginOffset();
const totalOffset = {
  x: centeringOffset.x + marginOffset.x,
  y: centeringOffset.y + marginOffset.y,
};
setViewport(totalOffset);
```

**Detection:**
- Multiple `setViewport` calls in correction logic
- Debug shows position changing multiple times per frame
- Final position depends on correction order

**Source:** Internal documentation (CLAUDE.md: "Calculate ALL corrections at once")

## Testing Blind Spots

### Missing Test Cases

Common test gaps that hide edge routing bugs:

| Missing Case | Why Critical | What It Reveals |
|--------------|--------------|-----------------|
| **2+ nesting levels** | Special-case depth logic breaks | Coordinate transformation bugs |
| **Cross-hierarchy edges** | Source/target in different coordinate spaces | Transform chain failures |
| **Large size variance** | Small nodes + large groups | Bounds calculation using centers |
| **Collapsed ↔ expanded** | Same graph, different visual representations | Blocking detection using wrong bounds |
| **Diagonal long-distance edges** | Path crosses multiple potential blockers | Incomplete blocking detection |
| **Edge to deeply nested child** | Coordinate transformation at multiple levels | Missing intermediate transforms |

### Test Strategy

**Minimal test suite for edge routing:**

1. **Flat graph** (no nesting) - baseline
2. **1-level nesting** - simple parent-child
3. **2-level nesting** - grandparent-parent-child
4. **3-level nesting** - great-grandparent...
5. **Cross-hierarchy edge** - source at depth 1, target at depth 2
6. **Skip-level edge** - source at depth 0, target at depth 2 (skips depth 1)
7. **Collapsed group** - same graph as #2 but group collapsed
8. **Mixed expansion** - some groups expanded, others collapsed

**For each test:**
- Visual inspection: no edges through nodes
- Programmatic check: edge path doesn't intersect node bounds
- Coordinate verification: edge endpoints at node boundaries (no gaps)

### Regression Prevention

When fixing edge routing bugs:

1. **Capture failing case as test** before fixing
2. **Test all nesting depths**, not just the reported failure
3. **Test expansion state changes** (same graph, collapsed vs expanded)
4. **Record screenshots** for visual regression testing
5. **Measure edge-to-node distances** programmatically (gap detection)

## Architecture Recommendations

### Coordinate System Abstraction

```javascript
class CoordinateTransformer {
  // Transforms point from node's local space to root space
  toRootSpace(point, node) {
    let current = node;
    let x = point.x;
    let y = point.y;
    while (current.parentNode) {
      const parent = getParent(current);
      x += current.position.x;
      y += current.position.y;
      current = parent;
    }
    return { x, y };
  }

  // Transforms point from root space to node's local space
  toLocalSpace(point, node) {
    const rootPos = this.toRootSpace({ x: 0, y: 0 }, node);
    return {
      x: point.x - rootPos.x,
      y: point.y - rootPos.y,
    };
  }
}
```

### Uniform Blocking Detection

```javascript
function getVisualBounds(node) {
  if (!node.children?.length) {
    // Simple node: just its rectangle
    return {
      left: nodeLeft(node),
      right: nodeRight(node),
      top: nodeTop(node),
      bottom: nodeBottom(node),
    };
  }

  // Group node: union of node bounds + all children
  const childBounds = node.children.map(getVisualBounds);
  return {
    left: Math.min(nodeLeft(node), ...childBounds.map(b => b.left)),
    right: Math.max(nodeRight(node), ...childBounds.map(b => b.right)),
    top: Math.min(nodeTop(node), ...childBounds.map(b => b.top)),
    bottom: Math.max(nodeBottom(node), ...childBounds.map(b => b.bottom)),
  };
}
```

### Edge Helper Functions

```javascript
// Helper functions defined once, used everywhere
const nodeLeft = (node) => node.x - node.width * 0.5;
const nodeRight = (node) => node.x + node.width * 0.5;
const nodeTop = (node) => node.y - node.height * 0.5;
const nodeBottom = (node) => node.y + node.height * 0.5;

// Use consistently in:
// - Bounds calculation
// - Collision detection
// - Edge endpoint positioning
// - Viewport centering
```

## Known Limitations and Workarounds

### ELK Layout Engine

**Issue:** Edge coordinates may be relative to root graph instead of lowest common ancestor (LCA) container.

**Workaround:** Post-process edge coordinates to transform into correct container space.

**Source:** [ELK nested edge containment issue #164](https://github.com/kieler/elkjs/issues/164)

### ReactFlow Nested Components

**Issue:** Nested ReactFlow inside custom nodes causes position offset due to transform conflicts.

**Workaround:** Avoid nesting ReactFlow components. Use single ReactFlow with parent-child node relationships instead.

**Source:** [ReactFlow nested component discussion #4743](https://github.com/xyflow/xyflow/discussions/4743)

### Browser Transform Inconsistencies

**Issue:** SVG transform behavior differs between browsers, especially for nested `<svg>` elements.

**Workaround:** Wrap nested SVGs in `<g>` elements to ensure transformations are in containing SVG's scope.

**Source:** [Sara Soueidan's article on nesting SVGs](https://www.sarasoueidan.com/blog/nesting-svgs/)

## Confidence Assessment

| Finding | Confidence | Source |
|---------|------------|--------|
| Coordinate transformation bugs | HIGH | Official ReactFlow docs + known issues |
| Special-case depth problems | HIGH | Common pattern in graph algorithms |
| Bounds using centers | HIGH | Internal documentation of actual bug |
| Blocking detection issues | MEDIUM | Inferred from nested graph literature + ELK issues |
| DOM measurement timing | HIGH | Internal documentation of actual bug |
| Sequential correction interference | HIGH | Internal documentation of actual bug |
| Testing blind spots | MEDIUM | Best practices from graph viz community |

## Sources

### Official Documentation
- [ReactFlow Sub Flows](https://reactflow.dev/learn/layouting/sub-flows)
- [ReactFlow Custom Edges](https://reactflow.dev/examples/edges/custom-edges)
- [ELK Edge Routing options](https://eclipse.dev/elk/reference/options/org-eclipse-elk-edgeRouting.html)
- [ELK Layered algorithm](https://eclipse.dev/elk/reference/algorithms/org-eclipse-elk-layered.html)

### Known Issues
- [ReactFlow nested component position offset #4743](https://github.com/xyflow/xyflow/discussions/4743)
- [ELK nested edge containment issue #164](https://github.com/kieler/elkjs/issues/164)
- [Firefox SVG transform bug #1064151](https://bugzilla.mozilla.org/show_bug.cgi?id=1064151)

### Research Papers
- [An Overview/Detail Layout for Visualizing Compound Graphs](https://arxiv.org/html/2408.04045v1)
- [Fast Edge-Routing for Large Graphs (Springer)](https://link.springer.com/chapter/10.1007/978-3-642-11805-0_15)
- [The Eclipse Layout Kernel (GD 2024)](https://drops.dagstuhl.de/storage/00lipics/lipics-vol320-gd2024/LIPIcs.GD.2024.56/LIPIcs.GD.2024.56.pdf)

### Community Resources
- [Sara Soueidan: Understanding SVG Coordinate Systems](https://www.sarasoueidan.com/blog/nesting-svgs/)
- [Sara Soueidan: Mimic Relative Positioning in SVG](https://www.sarasoueidan.com/blog/mimic-relative-positioning-in-svg/)
- [Jotform: Better Positioning with Nested SVGs](https://www.jotform.com/blog/better-positioning-and-transforming-with-nested-svgs/)
- [CSS-Tricks: Transforms on SVG Elements](https://css-tricks.com/transforms-on-svg-elements/)

### Internal Documentation
- `/src/hypergraph/viz/CLAUDE.md` - Hypergraph visualization system insights and known bugs
