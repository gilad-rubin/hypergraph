# Architecture Patterns for Edge Routing in Compound Graphs

**Domain:** Graph visualization with hierarchical/nested graphs
**Researched:** 2026-01-21
**Focus:** Edge routing algorithms for connecting nodes at arbitrary nesting depths

## Problem Statement

When graphs contain nested subgraphs (compound/hierarchical graphs), edges must route to the correct target node regardless of nesting depth. The challenge: **finding the actual destination node when containers are nested multiple levels deep**.

### Example Scenario

```
Graph structure (depth=2):
  outer
    └─ middle (GraphNode, expanded)
         └─ inner (GraphNode, expanded)
              └─ process (FunctionNode)

Edge routing problem:
  source → middle
```

**Question:** Should the edge terminate at `middle`, at `inner`, or at `process`?

**Answer:** If `middle` and `inner` are expanded containers, the edge should route to the deepest actual node: `process`.

### Code Smell: Special Cases Don't Generalize

```python
# Works for single nesting (depth=1)
for inner_name, inner_node in graph_node.graph.nodes.items():
    if param in inner_node.inputs:
        consumers.append(inner_name)

# Breaks for double nesting (depth=2)
# Returns 'inner' instead of 'process'
```

The algorithm needs to be **recursive or iterative** to handle arbitrary nesting depth.

## Algorithm Patterns from Professional Libraries

### Pattern 1: Lowest Common Ancestor (LCA) Based Routing

**Used by:** React Flow Automated Layout, Cytoscape compound graphs

**Concept:** Edges between non-sibling nodes route through their nearest common ancestor container.

**Algorithm:**
1. Find LCA of source and target nodes in containment hierarchy
2. Route edge to parent container boundary
3. Edge exits parent, travels to target's parent, enters target

**Pseudocode:**
```python
def route_edge(source, target, hierarchy_tree):
    lca = find_lowest_common_ancestor(source, target, hierarchy_tree)

    if lca == source.parent == target.parent:
        # Siblings - direct connection
        return direct_path(source, target)

    # Non-siblings - route through LCA
    exit_point = boundary_of(lca, side_toward(target))
    entry_point = boundary_of(target.parent, side_toward(target))

    return [
        stem_from(source),
        exit_point,
        entry_point,
        stem_to(target)
    ]
```

**Strengths:**
- Handles arbitrary nesting depth naturally
- Clear geometric interpretation
- Works for any tree structure

**Weaknesses:**
- Requires full containment hierarchy tree
- May create longer paths than necessary
- Boundary calculation complex for nested cases

### Pattern 2: Recursive Depth-First Search

**Used by:** Hypergraph's previous implementation (commit efd7504, now reverted)

**Concept:** Recursively descend into expanded containers until finding actual (non-container) nodes.

**Algorithm:**
```python
def find_deepest_consumers(graph_node, param, remaining_depth):
    """Find deepest nodes consuming a parameter.

    For depth=2: outer -> inner -> process
    If param consumed by inner (which passes to process),
    returns ['process'] not ['inner'].
    """
    consumers = []

    for inner_name, inner_node in graph_node.graph.nodes.items():
        if param in inner_node.inputs:
            # Found consumer - should we go deeper?
            if is_expanded_container(inner_node) and remaining_depth > 1:
                # Recurse into nested graph
                deeper = find_deepest_consumers(
                    inner_node,
                    param,
                    remaining_depth - 1
                )
                if deeper:
                    consumers.extend(deeper)
                else:
                    # No deeper consumers, use this node
                    consumers.append(inner_name)
            else:
                # Leaf node or depth limit reached
                consumers.append(inner_name)

    return consumers
```

**Strengths:**
- Intuitive recursive structure
- Directly uses graph structure
- Handles arbitrary depth naturally
- Returns multiple targets if parameter consumed by multiple deep nodes

**Weaknesses:**
- Requires depth parameter to limit recursion
- Must track expansion state (which containers are open)
- Returns empty list if no consumers found (caller must handle)

**Why it was reverted:** The commit message for removal suggests the special-case logic didn't generalize properly to all edge types. The recursive functions worked for some cases but broke others.

### Pattern 3: Iterative Unwrapping

**Used by:** Graphviz compound graphs (lhead/ltail)

**Concept:** Iteratively "unwrap" container nodes by checking if they're expanded and contain the logical target.

**Algorithm:**
```python
def resolve_edge_target(logical_target, expansion_state):
    """Resolve logical target to visual target node.

    If target is expanded container, find first inner node.
    If that's also expanded, recurse until hitting leaf.
    """
    current = logical_target

    while is_expanded_container(current, expansion_state):
        inner_nodes = get_inner_nodes(current)
        if not inner_nodes:
            # Empty container, use container itself
            break

        # Find entry node (e.g., first node in topological order)
        current = find_entry_node(inner_nodes)

    return current

def find_entry_node(nodes):
    """Find the 'entry point' node in a set of nodes.

    Could be:
    - Node with no internal predecessors (topologically first)
    - Node closest to container boundary
    - First node in some canonical ordering
    """
    # Implementation depends on desired routing semantics
    pass
```

**Strengths:**
- Simple iterative structure (no recursion stack)
- Clear termination condition (leaf node or collapsed container)
- Easy to add custom "entry node" logic

**Weaknesses:**
- Requires canonical "entry node" selection strategy
- May not handle multiple entry points well
- Must track expansion state

**Implementation note:** Graphviz's `compound=true` with `lhead`/`ltail` clips edges at container boundaries rather than routing to inner nodes. The unwrapping concept applies but the geometric clipping differs.

### Pattern 4: Hierarchy-Aware Edge Objects

**Used by:** ELK (Eclipse Layout Kernel)

**Concept:** Edges are hierarchy-aware - they know their containment level and target containment level.

**Algorithm:**
```python
class HierarchicalEdge:
    def __init__(self, source, target, containment_tree):
        self.source = source
        self.target = target
        self.source_level = containment_level(source, containment_tree)
        self.target_level = containment_level(target, containment_tree)
        self.is_hierarchical = (self.source_level != self.target_level)

    def routing_strategy(self):
        if not self.is_hierarchical:
            return "direct"  # Same level, simple routing
        elif self.source_level < self.target_level:
            return "descending"  # Enter nested container
        else:
            return "ascending"  # Exit nested container

def route_hierarchical_edge(edge, expansion_state):
    strategy = edge.routing_strategy()

    if strategy == "descending":
        # Route into nested container
        target_container = find_container(edge.target)
        entry_point = boundary_of(target_container, "top")
        return route_with_entry(edge.source, entry_point, edge.target)

    elif strategy == "ascending":
        # Route out of nested container
        source_container = find_container(edge.source)
        exit_point = boundary_of(source_container, "bottom")
        return route_with_exit(exit_point, edge.target)

    else:
        # Direct routing at same level
        return route_direct(edge.source, edge.target)
```

**Strengths:**
- Explicit hierarchy awareness in data model
- Separates concerns (hierarchy from geometry)
- Scales to complex hierarchies
- Supports multiple routing strategies

**Weaknesses:**
- Requires full containment tree structure
- More complex data model
- May over-engineer for simple cases

**ELK implementation note:** ELK Layered algorithm supports "hierarchical edges" - edges connecting nodes at different hierarchy levels. The documentation confirms capability but doesn't detail the internal algorithm.

## Comparison Matrix

| Approach | Handles Depth=∞ | Requires Tree | Expansion Aware | Complexity | Multiple Targets |
|----------|-----------------|---------------|-----------------|------------|------------------|
| LCA-based | ✅ Yes | ✅ Required | ⚠️ Indirect | Medium | ⚠️ Unclear |
| Recursive DFS | ✅ Yes | ❌ No | ✅ Yes | Low | ✅ Yes |
| Iterative Unwrap | ✅ Yes | ❌ No | ✅ Yes | Low | ⚠️ Complex |
| Hierarchy-Aware | ✅ Yes | ✅ Required | ✅ Yes | High | ✅ Yes |

## Recommended Algorithm: Recursive DFS with Fixes

For hypergraph's use case, **Pattern 2 (Recursive DFS)** is most appropriate because:

1. **Already partially implemented** - commit efd7504 had working recursive functions
2. **No tree structure required** - works directly with graph.nodes dictionary
3. **Expansion-aware** - naturally uses depth/expansion state
4. **Returns multiple targets** - handles parameter consumed by multiple deep nodes
5. **Low complexity** - straightforward recursive structure

### Why It Was Reverted

The commit message doesn't specify the exact failure mode. Likely issues:

1. **Incomplete edge type coverage** - may have handled INPUT → node but not node → node or node → OUTPUT
2. **Depth parameter confusion** - `remaining_depth` vs global `depth` vs actual nesting level
3. **Empty result handling** - recursive calls returning `[]` may cause edge to disappear
4. **Expansion state tracking** - may not correctly check which containers are actually expanded
5. **Multiple nested GraphNodes** - parameter passed through 3+ levels may confuse recursion

### Generalized Recursive Algorithm

```python
def find_visual_target(
    logical_target_id: str,
    nodes: dict[str, HyperNode],
    expansion_state: dict[str, bool],
    max_depth: int = 10  # Safety limit
) -> list[str]:
    """Find visual target node(s) for edge routing.

    If logical_target is an expanded GraphNode, recursively find
    the actual inner nodes. Otherwise return logical_target itself.

    Args:
        logical_target_id: Node ID from edge definition
        nodes: Graph nodes dictionary
        expansion_state: Which GraphNodes are expanded
        max_depth: Maximum recursion depth (safety)

    Returns:
        List of node IDs to route edges to (visual targets)
    """
    if max_depth <= 0:
        # Safety: prevent infinite recursion
        return [logical_target_id]

    node = nodes.get(logical_target_id)
    if not node:
        return []

    # Base case: not a GraphNode, or not expanded
    if not isinstance(node, GraphNode):
        return [logical_target_id]

    if not expansion_state.get(logical_target_id, False):
        return [logical_target_id]

    # Recursive case: expanded GraphNode
    # Find all entry nodes (nodes with no internal predecessors)
    inner_graph = node.graph
    entry_nodes = find_entry_nodes(inner_graph)

    visual_targets = []
    for entry_id in entry_nodes:
        # Recurse into each entry node
        deeper = find_visual_target(
            entry_id,
            inner_graph.nodes,
            expansion_state,
            max_depth - 1
        )
        visual_targets.extend(deeper)

    if not visual_targets:
        # No entry nodes found, use container itself
        return [logical_target_id]

    return visual_targets


def find_entry_nodes(graph: Graph) -> list[str]:
    """Find entry point nodes in a graph.

    Entry nodes have no predecessors within the graph.
    These are the first nodes data flows into.
    """
    all_targets = set()
    for edge in graph.edges:
        all_targets.add(edge.target)

    entry_nodes = []
    for name in graph.nodes.keys():
        if name not in all_targets:
            # No incoming edges within this graph
            entry_nodes.append(name)

    return entry_nodes if entry_nodes else list(graph.nodes.keys())[:1]


def find_visual_source(
    logical_source_id: str,
    output_name: str,
    nodes: dict[str, HyperNode],
    expansion_state: dict[str, bool],
    max_depth: int = 10
) -> list[str]:
    """Find visual source node(s) for edge routing.

    Similar to find_visual_target but looks for exit nodes
    (nodes with no successors within graph).
    """
    if max_depth <= 0:
        return [logical_source_id]

    node = nodes.get(logical_source_id)
    if not node:
        return []

    if not isinstance(node, GraphNode):
        return [logical_source_id]

    if not expansion_state.get(logical_source_id, False):
        return [logical_source_id]

    # Find which inner node(s) produce this output
    inner_graph = node.graph
    producers = []
    for inner_name, inner_node in inner_graph.nodes.items():
        if output_name in inner_node.outputs:
            producers.append(inner_name)

    if not producers:
        return [logical_source_id]

    visual_sources = []
    for producer_id in producers:
        deeper = find_visual_source(
            producer_id,
            output_name,
            inner_graph.nodes,
            expansion_state,
            max_depth - 1
        )
        visual_sources.extend(deeper)

    return visual_sources if visual_sources else [logical_source_id]
```

## Implementation Checklist

To implement this algorithm correctly:

- [ ] **Separate visual from logical** - edge definitions use logical IDs, rendering uses visual IDs
- [ ] **Recurse for all edge types** - INPUT→node, node→node, node→OUTPUT
- [ ] **Track expansion state** - only recurse into expanded GraphNodes
- [ ] **Handle empty results** - fallback to container node if no inner nodes found
- [ ] **Safety limits** - max recursion depth to prevent infinite loops
- [ ] **Test at multiple depths** - depth=1 (single nesting), depth=2 (double), depth=3+
- [ ] **Test partial expansion** - outer expanded but inner collapsed
- [ ] **Test multiple targets** - one parameter consumed by multiple deep nodes

## Pitfalls to Avoid

### Pitfall 1: Mixing Logical and Visual IDs

**Problem:** Edge data contains logical source/target, but rendering needs visual source/target.

**Solution:** Store both in edge data:
```javascript
{
  id: "edge_1",
  source: "middle",           // Logical source
  target: "output",           // Logical target
  data: {
    visualSource: "process",  // Where to render from
    visualTarget: "output",   // Where to render to
    innerSources: ["process"] // For JS layout to use
  }
}
```

### Pitfall 2: Forgetting Expansion State

**Problem:** Recursing into GraphNode even when it's collapsed.

**Solution:** Always check expansion_state before recursing:
```python
if isinstance(node, GraphNode) and expansion_state.get(node_id):
    # Only recurse if expanded
```

### Pitfall 3: Infinite Recursion

**Problem:** Circular references or malformed graphs cause stack overflow.

**Solution:**
- Add max_depth parameter (default 10)
- Decrement on each recursive call
- Return early if max_depth <= 0

### Pitfall 4: Empty Results

**Problem:** Recursive function returns empty list, edge disappears.

**Solution:** Fallback to container node:
```python
deeper = find_visual_target(...)
if deeper:
    return deeper
else:
    return [current_node_id]  # Fallback
```

### Pitfall 5: Not Testing All Nesting Levels

**Problem:** Works for depth=1, breaks at depth=2.

**Solution:** Test suite must include:
- Depth=0 (collapsed)
- Depth=1 (single nesting)
- Depth=2 (double nesting)
- Depth=3+ (triple nesting)
- Partial expansion (outer open, inner closed)

## Academic References

The algorithm patterns described above are synthesized from:

1. **Sander, G.** "Layout of Compound Directed Graphs" - Foundational work on cluster containment and hierarchical layout using Sugiyama framework
2. **Dogrusöz et al.** "A Layout Algorithm For Undirected Compound Graphs" (2009) - CoSE algorithm with compound node support
3. **React Flow Automated Layout** - LCA-based edge routing for nested graphs with parent-child relationships
4. **ELK (Eclipse Layout Kernel)** - Hierarchical edge routing with cross-hierarchy edge support
5. **Graphviz** - Compound graphs with `lhead`/`ltail` for cluster-to-cluster edges

## Sources

### Library Documentation
- [ELK Layered Algorithm](https://eclipse.dev/elk/reference/algorithms/org-eclipse-elk-layered.html) - Hierarchical graph layout
- [Graphviz Compound Graphs](https://graphviz.org/docs/attrs/compound/) - Cluster edge routing with lhead/ltail
- [Cytoscape CoSE-Bilkent](https://github.com/cytoscape/cytoscape.js-cose-bilkent) - Compound Spring Embedder layout
- [React Flow Automated Layout](https://github.com/Jalez/react-flow-automated-layout) - LCA detection for nested node graphs
- [React Flow Sub Flows](https://reactflow.dev/learn/layouting/sub-flows) - React Flow's approach to nested graphs

### Academic Papers
- [Sander - Layout of Compound Directed Graphs](https://publikationen.sulb.uni-saarland.de/handle/20.500.11880/25862) - Technical report on cluster containment
- [Dogrusöz et al. - A Layout Algorithm For Undirected Compound Graphs](https://dl.acm.org/doi/10.1016/j.ins.2008.11.017) - CoSE algorithm paper

### Algorithm Theory
- [Lowest Common Ancestor Algorithms](https://cp-algorithms.com/graph/lca.html) - LCA computation techniques
- [Depth-First Search for Tree Traversal](https://medium.com/@silverraining/cracking-dfs-with-javascript-07807b038367) - Finding deepest nodes with DFS
- [NetworkX LCA Guide](https://networkx.org/nx-guides/content/algorithms/lca/LCA.html) - Graph LCA implementation

### Hypergraph Implementation
- Commit efd7504 - "fix(viz): route edges to deepest inner nodes in nested graphs" (reverted)
- Commit 63e0753 - "docs(viz): document deep nesting edge routing fix"
- Current codebase - `src/hypergraph/viz/renderer.py` (recursive functions removed)
