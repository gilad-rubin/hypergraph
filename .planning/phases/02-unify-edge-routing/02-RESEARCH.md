# Phase 2: Unify Edge Routing Logic - Research

**Researched:** 2026-01-21
**Domain:** JavaScript edge routing in hierarchical graphs with dynamic expand/collapse
**Confidence:** MEDIUM

## Summary

Phase 2 eliminates the Python/JavaScript duplication in edge routing logic by making JavaScript the single source of truth. The standard approach is to have Python provide a complete, flat graph structure (NetworkX with all node attributes and parent references), and JavaScript builds the hierarchy tree from this flat list and makes all routing decisions dynamically based on browser-side expansion state.

The research confirms that:
1. **Flat-to-hierarchy conversion** in O(n) time using JavaScript object references is the standard pattern
2. **React Flow sub-flows** use `parentId` for relative positioning - child nodes inherit parent coordinate space
3. **Edge routing in compound graphs** resolves logical to visual targets based on expansion state
4. **State management** for expand/collapse should use lightweight approaches (Map-based) to avoid re-render cascades

**Primary recommendation:** Remove `_find_deepest_consumers`/`_find_deepest_producers` from Python. JavaScript builds hierarchy from flat node list and resolves edge targets at render time based on expansion state.

## Standard Stack

The established libraries/tools for JavaScript edge routing in hierarchical graphs:

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| React Flow | 11.11+ | Interactive graph UI with sub-flow support | Industry standard for browser-based graph viz, built-in parent-child positioning |
| JavaScript Map | ES6 | Expansion state tracking | Native, performant, prevents unnecessary re-renders |
| Object references | Native JS | Hierarchy tree building | O(n) flat-to-tree conversion without recursion |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| d3-hierarchy | 3.x | Tree operations | If needing LCA (Lowest Common Ancestor) calculations |
| Zustand/Jotai | Latest | Atomic state management | For complex multi-component state (not needed for single viz component) |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| JavaScript routing | Python pre-computes all targets | Python can't know browser expansion state; loses interactivity |
| Map state | React Context | Context causes re-render cascades; Map is faster for local state |
| Flat node list | Nested hierarchy from Python | Nested JSON harder to serialize; flat list more flexible |

**Installation:**
```bash
# No new dependencies - uses existing React Flow and native JavaScript
```

## Architecture Patterns

### Recommended Approach

```
Python (renderer.py):
├── Provides flat NetworkX graph with parent attributes
├── No deepest consumer/producer logic
└── Edge data uses logical source/target IDs

JavaScript (layout.js):
├── Builds hierarchy tree from flat node list (O(n))
├── Tracks expansion state (Map<nodeId, boolean>)
├── Resolves logical IDs to visual IDs at render time
└── Routes edges to correct targets based on expansion
```

### Pattern 1: Flat-to-Hierarchy Tree Building
**What:** Convert flat node array with parent references to hierarchical tree structure
**When to use:** JavaScript needs to traverse hierarchy for routing decisions

**Example:**
```javascript
// Source: Object reference pattern (TypeOfNaN, Medium articles)
function buildHierarchy(flatNodes) {
    // Step 1: Create ID-to-node mapping
    const nodeMap = new Map();
    const roots = [];

    // Initialize all nodes with empty children array
    flatNodes.forEach(node => {
        nodeMap.set(node.id, { ...node, children: [] });
    });

    // Step 2: Build parent-child relationships using object references
    flatNodes.forEach(node => {
        const nodeWithChildren = nodeMap.get(node.id);
        const parentId = node.parentNode || node.parent;

        if (parentId) {
            const parent = nodeMap.get(parentId);
            if (parent) {
                parent.children.push(nodeWithChildren);
            }
        } else {
            roots.push(nodeWithChildren);
        }
    });

    return { nodeMap, roots };
}

// O(n) time complexity - each node processed once
// Uses object references - modifying child automatically updates parent
```

### Pattern 2: Logical-to-Visual Edge Resolution
**What:** Resolve edge source/target from logical node IDs to visual node IDs based on expansion state
**When to use:** Edge targets may be nested inside expanded parent nodes

**Example:**
```javascript
// Source: Compound graph routing patterns (arXiv paper, Cytoscape patterns)
function resolveVisualTarget(logicalTargetId, expansionState, hierarchy) {
    const target = hierarchy.nodeMap.get(logicalTargetId);
    if (!target) return logicalTargetId;

    // Base case: not a container OR collapsed
    const isExpanded = expansionState.get(logicalTargetId);
    if (!target.data || target.data.nodeType !== 'PIPELINE' || !isExpanded) {
        return logicalTargetId;
    }

    // Recursive case: expanded container - find entry nodes
    const entryNodes = findEntryNodes(target.children);
    if (entryNodes.length === 0) {
        // Empty container, route to container itself
        return logicalTargetId;
    }

    // Recurse into first entry node
    return resolveVisualTarget(entryNodes[0].id, expansionState, hierarchy);
}

function findEntryNodes(children) {
    // Entry nodes have no incoming edges from siblings
    const hasIncoming = new Set();

    // Build set of nodes with incoming edges (from edges in children)
    // This requires edge data - simplified here

    return children.filter(child => !hasIncoming.has(child.id));
}
```

### Pattern 3: Expansion State Management
**What:** Track which container nodes are expanded using JavaScript Map
**When to use:** User can dynamically expand/collapse nodes in browser

**Example:**
```javascript
// Source: React Flow expand/collapse pattern, Jotai atomic state principles
function useExpansionState(initialDepth, nodes) {
    const [expansionState, setExpansionState] = React.useState(() => {
        // Initialize from initial depth
        const state = new Map();
        nodes.forEach(node => {
            if (node.data?.nodeType === 'PIPELINE') {
                const depth = calculateDepth(node, nodes);
                state.set(node.id, depth < initialDepth);
            }
        });
        return state;
    });

    const toggleExpansion = React.useCallback((nodeId) => {
        setExpansionState(prev => {
            const next = new Map(prev);
            next.set(nodeId, !prev.get(nodeId));
            return next;
        });
    }, []);

    return [expansionState, toggleExpansion];
}

function calculateDepth(node, allNodes) {
    let depth = 0;
    let current = node;

    while (current.parentNode) {
        depth++;
        current = allNodes.find(n => n.id === current.parentNode);
        if (!current) break;
    }

    return depth;
}

// Map state is performant - only components using specific keys re-render
// No Context needed - prevents re-render cascade
```

### Pattern 4: React Flow Sub-Flow Positioning
**What:** Child nodes positioned relative to parent using `parentId` and local coordinates
**When to use:** Nested graphs where children should move with parents

**Example:**
```javascript
// Source: React Flow sub-flows documentation
function createChildNode(childData, parentId, localPosition) {
    return {
        id: childData.id,
        type: 'custom',
        position: localPosition,  // Relative to parent's top-left
        parentNode: parentId,     // Links to parent
        extent: 'parent',         // Constrained to parent bounds
        data: childData
    };
}

// Key insight: position { x: 0, y: 0 } means parent's top-left corner
// Edges connected to child nodes render ABOVE nodes by default (z-index)
// Parent must appear before children in nodes array
```

### Anti-Patterns to Avoid

- **Python computes innerTargets** - Can't know browser expansion state; loses dynamic expand/collapse
- **Nested JSON hierarchy from Python** - Harder to serialize; flat list with parent refs more flexible
- **React Context for expansion state** - Causes re-render cascade when any node expands; use Map
- **Recursive hierarchy building** - O(n²) when O(n) solution exists using object references
- **Duplicate hierarchy logic** - Python builds one hierarchy, JavaScript builds another; single source of truth

## Don't Hand-Roll

Problems that look simple but have existing solutions:

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Flat list to tree | Recursive traversal | Object reference pattern (O(n)) | Recursion is O(n²) worst case; object references are O(n) guaranteed |
| LCA calculation | Manual tree walk | d3-hierarchy.ancestors() | Battle-tested, handles edge cases (cycles, missing nodes) |
| Expansion state | Global object | JavaScript Map | Map prevents key collision, has clear semantics, faster lookups |
| Visual target resolution | Multiple if/else | Recursive function with base case | Handles arbitrary nesting depth without special cases |

**Key insight:** The object reference pattern leverages JavaScript's memory model - since objects are references, adding a child to parent's array automatically propagates the relationship without manual updates.

## Common Pitfalls

### Pitfall 1: Forgetting Parent Must Precede Children
**What goes wrong:** React Flow fails to establish parent-child relationship
**Why it happens:** React Flow processes nodes array in order; child processed before parent doesn't find parent yet
**How to avoid:** Sort nodes so all ancestors appear before descendants in array
**Warning signs:** Child nodes positioned absolutely instead of relative to parent; dragging parent doesn't move children

### Pitfall 2: Modifying Hierarchy During Iteration
**What goes wrong:** JavaScript throws "Cannot iterate while modifying" or skips nodes
**Why it happens:** Adding/removing nodes during forEach on hierarchy tree
**How to avoid:** Build new hierarchy or collect changes first, then apply after iteration
**Warning signs:** Inconsistent node counts, mysterious "undefined" nodes

### Pitfall 3: Confusing Logical and Visual IDs
**What goes wrong:** Edge connects to wrong node (container instead of inner node)
**Why it happens:** Edge data contains logical target, but rendering needs visual target
**How to avoid:** Store both logical and visual IDs in edge data; resolve logical → visual before rendering
**Warning signs:** Edges terminate at parent node boundary instead of routing to inner nodes

### Pitfall 4: Re-rendering Entire Graph on Expansion
**What goes wrong:** Expand/collapse is slow, causes flicker
**Why it happens:** Changing expansion state triggers full re-render instead of incremental update
**How to avoid:** Use Map for expansion state; only layout nodes change visibility, others stay cached
**Warning signs:** Console shows "Layout recalculated" on every expand; performance degrades with graph size

### Pitfall 5: Circular Parent References
**What goes wrong:** Infinite loop when building hierarchy or resolving targets
**Why it happens:** Node A's parent is B, B's parent is A (cycle in hierarchy)
**How to avoid:** Add cycle detection (Set of visited IDs) in hierarchy building and target resolution
**Warning signs:** Browser tab freezes, "Maximum call stack size exceeded" error

## Code Examples

Verified patterns from React Flow and research sources:

### Complete Hierarchy Building (O(n))
```javascript
// Source: Medium articles on flat-to-tree conversion, React Flow patterns
function buildHierarchyWithCycleDetection(flatNodes) {
    const nodeMap = new Map();
    const roots = [];
    const processed = new Set();

    // Phase 1: Create node objects with empty children
    flatNodes.forEach(node => {
        nodeMap.set(node.id, {
            id: node.id,
            data: node.data,
            parentNode: node.parentNode,
            children: [],
            _original: node
        });
    });

    // Phase 2: Build relationships with cycle detection
    flatNodes.forEach(node => {
        if (processed.has(node.id)) return;

        const visitedInChain = new Set();
        let current = node;

        // Walk up parent chain to detect cycles
        while (current) {
            if (visitedInChain.has(current.id)) {
                console.warn(`Cycle detected involving node ${current.id}`);
                break;
            }

            visitedInChain.add(current.id);

            if (!current.parentNode) {
                // Found root
                const rootNode = nodeMap.get(current.id);
                if (!roots.includes(rootNode)) {
                    roots.push(rootNode);
                }
                break;
            }

            // Link child to parent
            const childNode = nodeMap.get(current.id);
            const parentNode = nodeMap.get(current.parentNode);

            if (parentNode && !parentNode.children.includes(childNode)) {
                parentNode.children.push(childNode);
            }

            current = flatNodes.find(n => n.id === current.parentNode);
        }

        processed.add(node.id);
    });

    return { nodeMap, roots };
}
```

### Edge Target Resolution with Expansion
```javascript
// Source: Compound graph routing literature, Cytoscape expand-collapse
function resolveEdgeEndpoints(edge, expansionState, hierarchy, maxDepth = 10) {
    const resolveNode = (logicalId, depth) => {
        if (depth <= 0) {
            console.warn(`Max depth reached resolving ${logicalId}`);
            return logicalId;
        }

        const node = hierarchy.nodeMap.get(logicalId);
        if (!node) return logicalId;

        // Not a container OR not expanded
        const isExpanded = expansionState.get(logicalId);
        const isPipeline = node.data?.nodeType === 'PIPELINE';

        if (!isPipeline || !isExpanded) {
            return logicalId;
        }

        // Expanded container - find entry/exit nodes
        if (node.children.length === 0) {
            return logicalId;  // Empty container
        }

        // For target: find entry nodes (no incoming edges from siblings)
        // For source: find exit nodes (no outgoing edges to siblings)
        // Simplified: use first child as entry, last as exit
        const entryNode = node.children[0];

        // Recurse
        return resolveNode(entryNode.id, depth - 1);
    };

    return {
        visualSource: resolveNode(edge.source, maxDepth),
        visualTarget: resolveNode(edge.target, maxDepth),
        logicalSource: edge.source,
        logicalTarget: edge.target
    };
}
```

### Applying Visibility Based on Expansion
```javascript
// Source: React Flow expand/collapse pattern, visibility filtering
function applyVisibility(nodes, edges, expansionState) {
    // Hide nodes whose parent is collapsed
    const visibleNodes = nodes.map(node => {
        let isVisible = true;

        // Walk up parent chain
        let current = node;
        while (current.parentNode) {
            const isParentExpanded = expansionState.get(current.parentNode);
            if (isParentExpanded === false) {
                isVisible = false;
                break;
            }

            current = nodes.find(n => n.id === current.parentNode);
            if (!current) break;
        }

        return { ...node, hidden: !isVisible };
    });

    // Hide edges where source or target is hidden
    const visibleNodeIds = new Set(
        visibleNodes.filter(n => !n.hidden).map(n => n.id)
    );

    const visibleEdges = edges.map(edge => {
        const sourceVisible = visibleNodeIds.has(edge.source);
        const targetVisible = visibleNodeIds.has(edge.target);

        return {
            ...edge,
            hidden: !sourceVisible || !targetVisible
        };
    });

    return { nodes: visibleNodes, edges: visibleEdges };
}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Python computes deepest consumers | JavaScript resolves at render time | Phase 2 (this work) | Dynamic expand/collapse without re-render |
| Nested hierarchy in JSON | Flat list with parent refs | Phase 2 (this work) | Simpler serialization, more flexible |
| Split routing logic (Python + JS) | JavaScript owns all routing | Phase 2 (this work) | Single source of truth, no duplication |
| Manual depth tracking | Expansion state Map | Phase 2 (this work) | Automatic hierarchy handling |

**Deprecated/outdated:**
- `_find_deepest_consumers()` in renderer.py - will be removed
- `_find_deepest_producers()` in renderer.py - will be removed
- `innerTargets` in edge data - JavaScript resolves dynamically instead
- Manual depth parameter passing - expansion state replaces depth

## Open Questions

Things that couldn't be fully resolved:

1. **Should entry/exit node selection be topological or positional?**
   - What we know: Can use first/last child (positional) or nodes with no internal edges (topological)
   - What's unclear: Which produces more intuitive edge routing for users
   - Recommendation: Start with topological (nodes with no incoming edges = entry), validate with user testing

2. **How to handle multiple entry nodes (diamond pattern)?**
   - What we know: A container might have multiple valid entry points
   - What's unclear: Should edges split to all entries, or pick one?
   - Recommendation: Route to all entry nodes (creates multiple visual edges from one logical edge)

3. **Should Python include ANY routing hints in edge data?**
   - What we know: Python could pre-compute possible targets to assist JavaScript
   - What's unclear: Whether hints help or just add complexity
   - Recommendation: Start with pure JavaScript routing; add hints only if performance issues emerge

4. **How to handle edge routing when parent is collapsed?**
   - What we know: Edge target is inside collapsed parent - should edge connect to parent boundary?
   - What's unclear: Where exactly on boundary (top? side? based on entry node position?)
   - Recommendation: Connect to parent node's top edge (standard Sugiyama layout approach)

## Sources

### Primary (HIGH confidence)
- [React Flow Sub-Flows documentation](https://reactflow.dev/learn/layouting/sub-flows) - Parent-child positioning, relative coordinates
- [React Flow Expand-Collapse example](https://reactflow.dev/examples/layout/expand-collapse) - State management for expansion
- [Building a hierarchical tree from a flat list | Medium](https://medium.com/@lizhuohang.selina/building-a-hierarchical-tree-from-a-flat-list-an-easy-to-understand-solution-visualisation-19cb24bdfa33) - O(n) object reference pattern
- [An Easy Way to Build a Tree in JavaScript Using Object References | TypeOfNaN](https://typeofnan.dev/an-easy-way-to-build-a-tree-with-object-references/) - Implementation details

### Secondary (MEDIUM confidence)
- [An Overview+Detail Layout for Visualizing Compound Graphs | arXiv 2024](https://arxiv.org/html/2408.04045v1) - Entry/exit ports, compound graph routing
- [Cytoscape.js expand-collapse extension | GitHub](https://github.com/iVis-at-Bilkent/cytoscape.js-expand-collapse) - Browser-side expansion patterns
- [State Management in Vanilla JS: 2026 Trends | Medium](https://medium.com/@chirag.dave/state-management-in-vanilla-js-2026-trends-f9baed7599de) - Map vs Context for local state
- [React State Management in 2025 | DeveloperWay](https://www.developerway.com/posts/react-state-management-2025) - Atomic state patterns (Jotai/Zustand)

### Tertiary (LOW confidence)
- WebSearch results on compound graph edge routing - concepts are sound but need verification with React Flow specifics
- Multiple blog posts on tree traversal - standard algorithms but need testing with actual graph structure

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - React Flow sub-flows and JavaScript Map are established patterns
- Architecture: MEDIUM - Patterns verified from documentation but need validation in hypergraph context
- Pitfalls: MEDIUM - Based on common issues in similar codebases and research, not all tested in hypergraph yet

**Research date:** 2026-01-21
**Valid until:** ~30 days (React Flow API stable, JavaScript patterns evergreen)
