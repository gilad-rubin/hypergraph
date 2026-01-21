# Code Smells: Hypergraph Visualization

**Domain:** Graph visualization with hierarchical nesting
**Researched:** 2026-01-21
**Observation:** Fix for single-level nesting didn't generalize to double nesting

## Executive Summary

The edge routing regression that required separate fixes for single-level and double-level nesting reveals systemic code smells in the visualization codebase. The primary issues are:

1. **Recursive depth mishandling** - Code written for flat graphs, patched for one level, breaks at two levels
2. **Coordinate system confusion** - Multiple conflicting coordinate spaces without clear transformation boundaries
3. **Duplication between Python and JavaScript** - Same logic implemented differently in renderer.py and layout.js
4. **Special-case proliferation** - Each nesting level requires new conditional logic
5. **Missing abstraction for hierarchy traversal** - Ad-hoc loops instead of reusable tree operations

These smells directly contributed to the regression: the fix for single-level nesting was implemented as a special case rather than a general solution, so it failed when depth increased.

## Critical Smells (Fix First)

### 1. Recursive Depth as Magic Number

**Location:** Multiple files
- `renderer.py:32-87` - `_find_deepest_consumers` and `_find_deepest_producers`
- `layout.js:115-137` - `getLayoutOrder` depth calculation
- `layout.js:361-424` - `performRecursiveLayout` depth handling

**The Smell:**
```python
# renderer.py
def _find_deepest_consumers(
    graph_node: GraphNode, param: str, remaining_depth: int
) -> list[str]:
    """Find the deepest nodes..."""
    if isinstance(inner_node, GraphNode) and remaining_depth > 1:
        deeper = _find_deepest_consumers(inner_node, param, remaining_depth - 1)
```

The `remaining_depth` parameter is manually threaded through recursion, decremented at each level. This is error-prone and forces every function to understand depth semantics.

**Why it's fragile:**
- Easy to forget to decrement depth (infinite recursion)
- Easy to check wrong threshold (`> 1` vs `> 0` vs `>= 1`)
- Each new depth-aware function needs careful depth handling
- Depth semantics unclear: "remaining depth" vs "current depth" vs "max depth"

**Root cause connection:**
The single-level fix added depth=1 logic. The double-level fix added depth=2 logic. Each new nesting level requires new conditional branches because depth is manual, not structural.

**Refactoring recommendation:**
Replace depth parameter with iterator/visitor pattern:

```python
def traverse_to_leaves(node, predicate):
    """Visit all leaf nodes matching predicate, automatically handling depth."""
    if predicate(node):
        if isinstance(node, GraphNode):
            for child in node.graph.nodes.values():
                yield from traverse_to_leaves(child, predicate)
        else:
            yield node

# Usage
consumers = list(traverse_to_leaves(
    graph_node,
    lambda n: param in n.inputs
))
```

This eliminates depth tracking entirely - the recursion naturally terminates at leaves.

---

### 2. Coordinate System Confusion

**Location:**
- `layout.js:507-575` - Child positioning with multiple offset calculations
- `layout.js:520-532` - Comments explain coordinate confusion
- `constraint-layout.js:848+` - Bounds calculation uses node edges

**The Smell:**
```javascript
// layout.js:520-532
// Get the layout's internal padding (used by ConstraintLayout.graph)
// The constraint layout already offsets nodes by size.min, so returned positions
// are already normalized. We just need to convert from center to top-left.
var layoutPadding = ConstraintLayout.defaultOptions.layout.padding || 50;

// For absolute positioning (edge routing), offset from parent's top-left
var absOffsetX = parentPos.x + GRAPH_PADDING;
var absOffsetY = parentPos.y + GRAPH_PADDING + HEADER_HEIGHT;

// Convert from center to top-left
// The constraint layout positions are already offset with internal padding (50px default)
// We want positions relative to parent's content area (which has GRAPH_PADDING)
// So we adjust: subtract layout padding, add our GRAPH_PADDING
var childX = n.x - w / 2 - layoutPadding + GRAPH_PADDING;
```

**Multiple coordinate systems:**
1. **Layout coordinate space** - Centers with internal padding (50px)
2. **Parent-relative space** - Top-left relative to parent's content area
3. **Absolute screen space** - For edge routing
4. **React Flow space** - Top-left positions

The code manually converts between these spaces with arithmetic operations scattered throughout.

**Why it's fragile:**
- Each conversion site can get the formula wrong
- No single source of truth for transformations
- Comments required to explain what coordinates mean
- Edge routing breaks when coordinate conversions miss a case

**Root cause connection:**
The git history shows multiple fixes for "correct edge routing coordinates" (commits 21005e6, 11dc167, 066bddd). Each was a coordinate system mismatch that required tracking down the specific transformation error.

**Refactoring recommendation:**
Create explicit coordinate transformation types:

```javascript
class CoordinateSpace {
  static layoutToParentRelative(layoutPos, layoutPadding, graphPadding) {
    return {
      x: layoutPos.x - layoutPos.width/2 - layoutPadding + graphPadding,
      y: layoutPos.y - layoutPos.height/2 - layoutPadding + graphPadding
    };
  }

  static parentRelativeToAbsolute(childPos, parentPos, headerHeight) {
    return {
      x: parentPos.x + childPos.x,
      y: parentPos.y + childPos.y + headerHeight
    };
  }
}
```

Each transformation has a clear name and single implementation. No arithmetic duplication.

---

### 3. Duplication: Python vs JavaScript Hierarchy Logic

**Location:**
- `renderer.py:32-87` - Python: `_find_deepest_consumers`, `_find_deepest_producers`
- `layout.js:342-582` - JavaScript: `performRecursiveLayout`, `getLayoutOrder`

**The Smell:**

**Python (renderer.py):**
```python
def _find_deepest_consumers(graph_node: GraphNode, param: str, remaining_depth: int):
    inner_graph = graph_node.graph
    consumers = []
    for inner_name, inner_node in inner_graph.nodes.items():
        if param in inner_node.inputs:
            if isinstance(inner_node, GraphNode) and remaining_depth > 1:
                deeper = _find_deepest_consumers(inner_node, param, remaining_depth - 1)
                if deeper:
                    consumers.extend(deeper)
                else:
                    consumers.append(inner_name)
            else:
                consumers.append(inner_name)
    return consumers
```

**JavaScript (layout.js):**
```javascript
function getLayoutOrder(nodes, expansionState) {
    var getDepth = function(nodeId, depth) {
        depth = depth || 0;
        var node = nodeById.get(nodeId);
        if (!node || !node.parentNode) return depth;
        return getDepth(node.parentNode, depth + 1);
    };

    return nodes
        .filter(function(n) {
            return n.data && n.data.nodeType === 'PIPELINE' &&
                   expansionState.get(n.id) === true && !n.hidden;
        })
        .sort(function(a, b) {
            return getDepth(b.id) - getDepth(a.id);
        });
}
```

Both implement hierarchy traversal, but differently:
- Python: Bottom-up (recurse into children)
- JavaScript: Top-down (look up to parents)
- Python: Returns node names
- JavaScript: Returns sorted nodes
- Python: Uses `remaining_depth`
- JavaScript: Uses `getDepth` calculated from parent chain

**Why it's fragile:**
- Two implementations = two places to fix bugs
- Different traversal directions make bugs harder to spot
- No guarantee Python hierarchy matches JavaScript hierarchy
- Fix in one language doesn't propagate to other

**Root cause connection:**
The commit ad91f03 "support dynamic edge routing on expansion state change" had to refactor BOTH Python (renderer.py) and JavaScript (layout.js) to add hierarchy trees. The duplication doubled the work and risk.

**Refactoring recommendation:**
Choose ONE source of truth for hierarchy:

**Option A:** Python builds full hierarchy, JavaScript consumes it
```python
# renderer.py
def _build_hierarchy_tree(graph_node, depth):
    """Returns nested dict representing full hierarchy."""
    return {
        "id": graph_node.name,
        "children": [
            _build_hierarchy_tree(child, depth-1)
            for child in graph_node.graph.nodes.values()
            if isinstance(child, GraphNode) and depth > 1
        ]
    }

# Include in edge data
rf_edge["data"]["hierarchyTree"] = _build_hierarchy_tree(source_node, depth)
```

JavaScript just walks the tree, no reimplementation needed.

**Option B:** JavaScript builds hierarchy from flat node list
```javascript
// Build parent map once
const parentMap = new Map(nodes.map(n => [n.id, n.parentNode]));

// Provide reusable hierarchy utilities
function getAncestors(nodeId) { /* walk parentMap */ }
function getDepth(nodeId) { /* count ancestors */ }
```

Either way, eliminate the duplication.

---

### 4. Special-Case Proliferation

**Location:**
- `layout.js:507-575` - Different logic for root vs children vs grandchildren
- `layout.js:361-424` - Step 1 uses deepest-first, Step 3 uses parent-first
- `renderer.py:318-368` - Expanded vs collapsed pipeline handling

**The Smell:**
```javascript
// layout.js:533-540
// Position children within their parents
// IMPORTANT: We need parent-first order here (opposite of Step 1)
// so that parent positions are known before positioning grandchildren
var positioningOrder = layoutOrder.slice().reverse();
```

Step 1 iterates deepest-first (for layout). Step 3 iterates parent-first (for positioning). Same nodes, different orders, each with special-case logic.

**More special cases:**
```javascript
// layout.js:520-532
if (childNode is root) {
    // Use absolute positioning
    nodePositions.set(n.id, { x: x, y: y });
} else if (childNode has parent) {
    // Use parent-relative positioning
    position: { x: childX, y: childY + HEADER_HEIGHT }
} else if (childNode has grandparent) {
    // Use absolute for edge routing but relative for React Flow
    nodePositions.set(n.id, { x: absOffsetX + childX, y: absOffsetY + childY });
}
```

Each nesting level adds a new case.

**Why it's fragile:**
- Adding a new nesting level (triple-nested) requires new branches
- Hard to verify all combinations are covered
- Each special case has different coordinate formulas
- Comment "IMPORTANT: opposite of Step 1" shows lack of symmetry

**Root cause connection:**
Commit 39def26 "fix double-nested graphs" added `.reverse()` because Step 1 and Step 3 need opposite orders. This asymmetry is a symptom of special-casing rather than general algorithm.

**Refactoring recommendation:**
Use uniform tree traversal with clear semantics:

```javascript
function traverseBottomUp(rootNodes, nodeGroups, fn) {
    function visit(node, depth) {
        const children = nodeGroups.get(node.id) || [];
        // Visit children first (bottom-up)
        children.forEach(child => visit(child, depth + 1));
        fn(node, depth);
    }
    rootNodes.forEach(node => visit(node, 0));
}

function traverseTopDown(rootNodes, nodeGroups, fn) {
    function visit(node, depth) {
        fn(node, depth);
        const children = nodeGroups.get(node.id) || [];
        // Visit children after (top-down)
        children.forEach(child => visit(child, depth + 1));
    }
    rootNodes.forEach(node => visit(node, 0));
}

// Usage - explicit, no reversal tricks
traverseBottomUp(roots, groups, (node, depth) => {
    // Layout phase
});

traverseTopDown(roots, groups, (node, depth) => {
    // Positioning phase
});
```

No special cases. Each phase declares its traversal order.

---

## High Priority Smells

### 5. Node Dimension Calculation Spread Across Two Languages

**Location:**
- `layout_estimator.py:206-271` - Python dimension estimation
- `layout.js:36-95` - JavaScript `calculateDimensions`

**The Smell:**
Both Python and JavaScript calculate node dimensions with slightly different formulas:

**Python:**
```python
# layout_estimator.py:206-226
def _estimate_node_width(self, name: str) -> int:
    label_len = min(len(name), self.NODE_LABEL_MAX_CHARS)
    max_content_len = label_len

    if not self.separate_outputs:
        for output_name in hypernode.outputs:
            out_len = min(len(output_name), self.NODE_LABEL_MAX_CHARS)
            # ... type length calculation
            max_content_len = max(max_content_len, total_len)

    width = max_content_len * self.CHAR_WIDTH_PX + self.FUNCTION_NODE_BASE_PADDING
    return min(width, self.MAX_NODE_WIDTH)
```

**JavaScript:**
```javascript
// layout.js:41-89
function calculateDimensions(n) {
    if (n.data && (n.data.nodeType === 'DATA' || n.data.nodeType === 'INPUT')) {
        height = 36;
        var labelLen = Math.min((n.data.label && n.data.label.length) || 0, NODE_LABEL_MAX_CHARS);
        var typeLen = (n.data.showTypes && n.data.typeHint) ? Math.min(n.data.typeHint.length, TYPE_HINT_MAX_CHARS) + 2 : 0;
        width = Math.min(MAX_NODE_WIDTH, (labelLen + typeLen) * CHAR_WIDTH_PX + NODE_BASE_PADDING);
    }
    // ... more node type cases
}
```

**Why it's fragile:**
- Two implementations can diverge (different padding, different formula)
- Python estimates for iframe sizing, JavaScript for actual layout
- If Python underestimates, scrollbars appear unexpectedly
- If formulas differ, estimated size ≠ actual size

**Refactoring recommendation:**
Make JavaScript the single source of truth. Python should not duplicate calculations:

```python
# layout_estimator.py - simplified
def estimate_layout(graph):
    """Conservative estimate - actual size determined by JavaScript."""
    num_nodes = len(graph.nodes)
    num_levels = estimate_depth(graph)

    # Conservative formula that never underestimates
    width = max(400, num_nodes * 150)  # Generous per-node space
    height = max(300, num_levels * 200)  # Generous per-level space

    return width, height
```

Don't try to perfectly replicate JavaScript's calculation. Use conservative bounds and let JavaScript handle actual sizing.

---

### 6. Edge Rerouting Logic in JavaScript, Not Python

**Location:**
- `layout.js:507-575` - JavaScript determines which inner nodes to route to
- `renderer.py:318-368` - Python provides `innerTargets` list
- No coordination between the two

**The Smell:**
Python says "here are the inner nodes that could be targets" (`innerTargets`). JavaScript says "based on expansion state, pick the right one from the hierarchy". This split is fragile.

```python
# renderer.py:318-331
inner_targets = []
if is_expanded_pipeline:
    for param in params:
        consumers = _find_deepest_consumers(hypernode, param, depth)
        inner_targets.extend(consumers)

edges.append({
    # ...
    "data": {
        "innerTargets": inner_targets  # List of possibilities
    }
})
```

```javascript
// layout.js - JavaScript picks from innerTargets based on expansion
function findVisibleTarget(edge, expansionState, nodePositions) {
    if (!edge.data?.innerTargetsHierarchy) return edge.target;

    // Traverse hierarchy to find visible target
    // ...
}
```

**Why it's fragile:**
- Python doesn't know expansion state (browser-only)
- JavaScript doesn't know graph structure (Python-only)
- Mismatch between "possible targets" and "actual target" is error-prone
- Hierarchy structure duplicated (Python builds it, JavaScript walks it)

**Root cause connection:**
Commit ad91f03 added hierarchy trees to bridge Python and JavaScript. This is treating the symptom (need coordination) not the cause (split responsibility).

**Refactoring recommendation:**
Move ALL edge routing decision to one side:

**Option A:** Python decides everything at render time
```python
# renderer.py - no innerTargets, just final target
edges.append({
    "source": actual_source_id,  # Already resolved to deepest
    "target": actual_target_id,  # Already resolved to deepest
    # No innerTargets needed
})
```

Drawback: Can't change routing on browser expand/collapse without re-rendering.

**Option B:** JavaScript handles all hierarchy
```python
# renderer.py - provide full graph structure, not pre-processed lists
edges.append({
    "sourceGraph": source_node.to_dict() if isinstance(source_node, GraphNode) else None,
    "targetGraph": target_node.to_dict() if isinstance(target_node, GraphNode) else None,
})
```

JavaScript has full information to make routing decisions dynamically.

Option B is better for interactive visualization.

---

### 7. Layout Padding Constants Duplicated

**Location:**
- `layout_estimator.py:24-28` - Python constants
- `layout.js:32-34` - JavaScript constants
- `constraint-layout.js:820+` - Default layout options

**The Smell:**
```python
# layout_estimator.py
LAYOUT_SPACE_Y = 140
LAYOUT_LAYER_SPACE_Y = 120
LAYOUT_SPACE_X = 14
PADDING_LEFT = 20
PADDING_RIGHT = 70
```

```javascript
// layout.js
var GRAPH_PADDING = 40;
var HEADER_HEIGHT = 32;
```

```javascript
// constraint-layout.js (defaultOptions)
layout: {
    padding: 50,
    spaceY: 140,
    layerSpaceY: 120,
    spaceX: 14
}
```

Three files define overlapping padding/spacing constants. No single source of truth.

**Why it's fragile:**
- Changing spacing requires updating three files
- Easy to miss one update (spacing now inconsistent)
- Python estimates don't match JavaScript reality

**Refactoring recommendation:**
Define constants once, share everywhere:

```javascript
// viz-constants.js - single source of truth
export const VIZ_CONSTANTS = {
    LAYOUT_SPACE_Y: 140,
    LAYOUT_LAYER_SPACE_Y: 120,
    LAYOUT_SPACE_X: 14,
    GRAPH_PADDING: 40,
    HEADER_HEIGHT: 32,
    PADDING: { LEFT: 20, RIGHT: 70, TOP: 16, BOTTOM: 16 }
};
```

Python loads these constants from the JS file (parse as JSON) rather than duplicating:

```python
# layout_estimator.py
import json
from importlib.resources import files

_CONSTANTS = json.loads(
    files("hypergraph.viz.assets").joinpath("viz-constants.json").read_text()
)

LAYOUT_SPACE_Y = _CONSTANTS["LAYOUT_SPACE_Y"]
```

---

## Medium Priority Smells

### 8. `groupNodesByParent` Returns Map, but Iteration Expects Array

**Location:** `layout.js:102-110`

**The Smell:**
```javascript
function groupNodesByParent(nodes) {
    var groups = new Map();
    // ... build Map
    return groups;  // Returns Map
}

// Later usage:
var nodeGroups = groupNodesByParent(visibleNodes);
// ...
var children = nodeGroups.get(graphNode.id) || [];  // OK - Map usage

// But also:
layoutOrder.forEach(function(graphNode) {
    var children = nodeGroups.get(graphNode.id) || [];
    // Iteration assumes children is Array
    children.forEach(...)
});
```

The return type (`Map<string, Node[]>`) is used correctly, but the pattern of `get() || []` is repeated everywhere. This is defensive programming that hides missing data.

**Refactoring recommendation:**
Return a proper data structure with defaults:

```javascript
class NodeHierarchy {
    constructor(nodes) {
        this._groups = new Map();
        nodes.forEach(n => {
            const parent = n.parentNode || null;
            if (!this._groups.has(parent)) this._groups.set(parent, []);
            this._groups.get(parent).push(n);
        });
    }

    getChildren(parentId) {
        return this._groups.get(parentId) || [];  // Default in one place
    }

    getRoots() {
        return this.getChildren(null);
    }
}
```

---

### 9. `calculateDimensions` Giant Switch on Node Type

**Location:** `layout.js:41-95`

**The Smell:**
```javascript
function calculateDimensions(n) {
    var width = 80;
    var height = 90;

    if (n.data && (n.data.nodeType === 'DATA' || n.data.nodeType === 'INPUT')) {
        // DATA/INPUT calculation
    } else if (n.data && n.data.nodeType === 'INPUT_GROUP') {
        // INPUT_GROUP calculation
    } else if (n.data && n.data.nodeType === 'BRANCH') {
        // BRANCH calculation
    } else {
        // Function/Pipeline calculation
    }

    // Override with explicit style
    if (n.style && n.style.width) width = n.style.width;
    if (n.style && n.style.height) height = n.style.height;

    return { width: width, height: height };
}
```

Long if-else chain checking `nodeType`. Adding a new node type requires modifying this function.

**Refactoring recommendation:**
Use strategy pattern or lookup table:

```javascript
const DIMENSION_CALCULATORS = {
    'DATA': (n) => calculateDataNodeDimensions(n),
    'INPUT': (n) => calculateDataNodeDimensions(n),  // Same as DATA
    'INPUT_GROUP': (n) => calculateInputGroupDimensions(n),
    'BRANCH': (n) => ({ width: 140, height: 140 }),
    'FUNCTION': (n) => calculateFunctionNodeDimensions(n),
    'PIPELINE': (n) => calculateFunctionNodeDimensions(n)
};

function calculateDimensions(n) {
    const nodeType = n.data?.nodeType || 'FUNCTION';
    const calculator = DIMENSION_CALCULATORS[nodeType];

    if (!calculator) {
        console.warn('Unknown node type:', nodeType);
        return { width: 80, height: 90 };
    }

    const dims = calculator(n);

    // Apply style overrides
    if (n.style?.width) dims.width = n.style.width;
    if (n.style?.height) dims.height = n.style.height;

    return dims;
}
```

---

### 10. Magic Strings for Node Types

**Location:** Scattered throughout
- `renderer.py:21-30` - Returns string literals
- `layout.js:41-95` - Compares against string literals
- `components.js` - More string literal comparisons

**The Smell:**
```python
# renderer.py
def _get_node_type(hypernode: HyperNode) -> str:
    if isinstance(hypernode, GraphNode):
        return "PIPELINE"  # String literal
    if isinstance(hypernode, (RouteNode, IfElseNode)):
        return "BRANCH"  # String literal
    return "FUNCTION"
```

```javascript
// layout.js
if (n.data && (n.data.nodeType === 'DATA' || n.data.nodeType === 'INPUT')) {
    // String comparison
}
```

No constants defined. Easy to typo ("PIPLINE" vs "PIPELINE").

**Refactoring recommendation:**
Define node type constants once:

```python
# renderer.py
class NodeType:
    PIPELINE = "PIPELINE"
    BRANCH = "BRANCH"
    FUNCTION = "FUNCTION"
    DATA = "DATA"
    INPUT = "INPUT"
    INPUT_GROUP = "INPUT_GROUP"

def _get_node_type(hypernode: HyperNode) -> str:
    if isinstance(hypernode, GraphNode):
        return NodeType.PIPELINE
    # ...
```

JavaScript should also define these (can be generated from Python or vice versa).

---

## Pattern: Missing Abstractions

Across all these smells, the root cause is **missing abstractions for common operations**:

1. **No hierarchy traversal abstraction** → Manual depth tracking, reversed iteration
2. **No coordinate transformation abstraction** → Arithmetic scattered everywhere
3. **No node dimension abstraction** → Giant switch statement, duplication
4. **No edge routing abstraction** → Split between Python and JavaScript

Each missing abstraction forces developers to reimplement logic, leading to:
- Duplication (same logic in multiple places)
- Special cases (each occurrence handles edge cases differently)
- Fragility (one fix doesn't propagate to all occurrences)

## Refactoring Priority

**Priority 1 (Critical):**
1. **Hierarchy traversal abstraction** - Eliminates depth tracking, special-case iteration
2. **Coordinate transformation types** - Eliminates coordinate system confusion
3. **Edge routing unification** - Eliminates Python/JavaScript duplication

**Priority 2 (High):**
4. **Dimension calculation strategy** - Eliminates giant switch statement
5. **Node type constants** - Eliminates magic strings
6. **Layout constant unification** - Single source of truth for spacing

**Priority 3 (Medium):**
7. **NodeHierarchy class** - Cleaner hierarchy API
8. **Dimension calculator lookup** - Extensible node types

## Testing Strategy for Refactoring

To safely refactor without breaking existing behavior:

1. **Add characterization tests** for current behavior (even if buggy)
2. **Refactor to new abstractions** while keeping old behavior
3. **Compare outputs** (old vs new implementation)
4. **Remove old implementation** once tests pass

Example for hierarchy traversal:

```python
# Step 1: Characterization test
def test_find_deepest_consumers_depth_2():
    """Documents current behavior for depth=2."""
    result = _find_deepest_consumers(middle_node, "x", depth=2)
    assert result == ["process"]  # Current behavior

# Step 2: New implementation
def test_traverse_to_leaves_depth_2():
    """New implementation should match old behavior."""
    result = list(traverse_to_leaves(
        middle_node,
        lambda n: "x" in n.inputs
    ))
    assert result == ["process"]  # Same as old

# Step 3: Once tests pass, replace old with new
```

## Conclusion

The edge routing regression is a symptom of systemic issues:
- **Lack of abstractions** forces reimplementation for each use case
- **Duplication** means fixes don't propagate
- **Special-casing** means each nesting level needs new code

The fix for single-level nesting failed at double-level because it was implemented as a special case ("if depth > 1") rather than a general solution (recursive tree traversal).

**Refactoring these code smells will prevent future regressions** by:
1. Making hierarchy traversal automatic (no manual depth)
2. Making coordinate transformations explicit (no arithmetic errors)
3. Eliminating duplication (fix once, works everywhere)
4. Removing special cases (works for any nesting depth)

**Estimated effort:**
- Critical smells: 3-5 days (hierarchy, coordinates, edge routing)
- High priority: 2-3 days (dimensions, constants)
- Medium priority: 1-2 days (cleanup)

Total: **~1.5 weeks** for comprehensive refactoring, but can be done incrementally with each smell as a separate PR.
