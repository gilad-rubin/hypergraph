# Visualization Gap Fix - Session Notes

## Summary of Changes (Commit 65fac0d)

### Problem
Visual gaps between nodes were inconsistent because the layout used **center-to-center** spacing (140px fixed), but visual gaps depend on node heights:
- FUNCTION→DATA: ~100px gap ✓
- BRANCH→FUNCTION: ~37px gap (too small)
- PIPELINE→FUNCTION: could be negative (overlap!)

### Solution Implemented

#### 1. `constraint-layout.js` - Conservative gap enforcement
```javascript
const createRowConstraints = (edges, layoutConfig) =>
  edges.map((edge) => {
    const sourceHeight = edge.sourceNode.height || 0;
    const targetHeight = edge.targetNode.height || 0;
    const defaultVisualGap = layoutConfig.spaceY - (sourceHeight + targetHeight) / 2;

    const MIN_VISUAL_GAP = 60;
    const separation = defaultVisualGap < MIN_VISUAL_GAP
      ? MIN_VISUAL_GAP + (sourceHeight + targetHeight) / 2
      : layoutConfig.spaceY;
    // ...
  });
```
- Only increases separation when gap would be < 60px
- Preserves original behavior for typical nodes

#### 2. `layout.js` - Separate outputs mode spacing
```javascript
function getLayoutOptions(layoutNodes) {
  var hasDATANodes = layoutNodes.some(n => n.data?.nodeType === 'DATA');
  return hasDATANodes
    ? { ...defaultOptions, layout: { ...defaultOptions.layout, spaceY: 94 } }
    : defaultOptions;
}
```
- When `separate_outputs=True`, uses spaceY=94 (targets ~60px visual gaps)
- Applied to both flat and recursive layout paths

#### 3. `core.py` - API exposure
- Added `separate_outputs` parameter to `Graph.visualize()` method

## Node Overlaps Issue - FIXED (Commit 43d3849)

**Problem**: INPUT nodes could overlap with nodes from other branches because they didn't share edges (no constraints between them).

**Example** (complex_rag graph):
- `format_context` (bottom at y=1401)
- `input_max_tokens` (top at y=1385)
- These overlapped by ~16px but had no edge between them

**Failed fix attempts**:
- `fixNodeOverlaps()` that shifted all nodes below - too aggressive, broke layouts
- Several reverted commits trying horizontal spreading post-layout

**Solution implemented (Commit 43d3849)**:

Added `createSharedTargetConstraints()` which uses the constraint solver itself:

```javascript
const createSharedTargetConstraints = (edges, layoutConfig) => {
  // Group edges by target node
  const sourcesByTarget = {};
  for (const edge of edges) {
    const targetId = edge.targetNode.id;
    if (!sourcesByTarget[targetId]) sourcesByTarget[targetId] = [];
    if (!sourcesByTarget[targetId].includes(edge.sourceNode)) {
      sourcesByTarget[targetId].push(edge.sourceNode);
    }
  }

  // For each target with multiple sources, add separation constraints
  for (const targetId in sourcesByTarget) {
    const sources = sourcesByTarget[targetId];
    if (sources.length < 2) continue;

    sources.sort((a, b) => a[coordPrimary] - b[coordPrimary]);

    for (let i = 0; i < sources.length - 1; i++) {
      const separation = nodeA.width * 0.5 + spaceX * 0.5 + nodeB.width * 0.5;
      constraints.push({
        base: separationConstraint,
        property: coordPrimary,
        a: sources[i],
        b: sources[i + 1],
        separation,
      });
    }
  }
  return constraints;
};
```

**Why it works**:
- Parallel constraints still pull sources toward target's X (centering)
- New separation constraints ensure minimum horizontal spacing
- Solver finds balanced solution: sources spread horizontally while staying centered

**Tested by**: `test_multiple_inputs_same_target_horizontal_spread` in `test_visual_layout_issues.py`

## Testing

```python
# Test workflow with separate_outputs
workflow.visualize(depth=1, separate_outputs=True)

# Test branching (BRANCH nodes)
branching.visualize()

# Test complex_rag (shows overlap issue)
complex_rag.visualize()
```

## Debug Commands

```python
from hypergraph.viz import extract_debug_data

data = extract_debug_data(graph, depth=0)

# Check heights
for n in data.nodes:
    print(f"{n['id']}: {n['height']}px, type={n.get('nodeType')}")

# Check gaps
nodes_by_id = {n['id']: n for n in data.nodes}
for e in data.edges:
    src, tgt = nodes_by_id.get(e.source), nodes_by_id.get(e.target)
    if src and tgt:
        gap = (tgt['y'] - tgt['height']/2) - (src['y'] + src['height']/2)
        print(f"{e.source} -> {e.target}: {gap:.0f}px")

# Check overlaps (all nodes, not just edges)
sorted_nodes = sorted(data.nodes, key=lambda x: x['y'])
for i, n1 in enumerate(sorted_nodes):
    n1_bottom = n1['y'] + n1['height']/2
    for n2 in sorted_nodes[i+1:]:
        n2_top = n2['y'] - n2['height']/2
        if n2_top < n1_bottom:
            print(f"OVERLAP: {n1['id']} vs {n2['id']}")
```

## Files Modified

- `src/hypergraph/viz/assets/constraint-layout.js` - MIN_VISUAL_GAP logic, createSharedTargetConstraints()
- `src/hypergraph/viz/assets/layout.js` - getLayoutOptions() for separate_outputs
- `src/hypergraph/graph/core.py` - separate_outputs parameter

## Node Heights Reference

| Node Type | Typical Height |
|-----------|----------------|
| INPUT | 30px |
| DATA | 30px |
| FUNCTION (1 line) | 38px |
| FUNCTION (2 lines) | 76px |
| BRANCH | 130px |
| PIPELINE | varies (container) |
