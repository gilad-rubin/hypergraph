# Phase 1: Add Core Abstractions - Research

**Researched:** 2026-01-21
**Domain:** Python graph visualization with NetworkX, React Flow, and hierarchical layouts
**Confidence:** HIGH

## Summary

Phase 1 refactors the visualization layer to eliminate coupling between viz code and hypergraph domain types. The standard approach is to serialize domain objects into pure data structures (NetworkX graphs with attributes) that the renderer consumes without type checking. For hierarchy traversal, Python's recursive patterns with predicate functions handle depth automatically. Coordinate transformations require explicit tracking of 4 spaces: layout-local, parent-relative, absolute, and React Flow viewport.

The research confirms that:
1. **NetworkX serialization** is the standard decoupling pattern - store all needed data as node/edge attributes
2. **Recursive traversal with predicates** eliminates manual depth tracking
3. **Explicit coordinate space classes** prevent transformation bugs
4. **Characterization tests** (golden master/approval tests) document behavior before refactoring

**Primary recommendation:** Follow the hypernodes reference implementation - it already demonstrates clean separation with VisualizationGraph as pure data structure consumed by JSRenderer.

## Standard Stack

The established libraries/tools for Python graph visualization with hierarchical layouts:

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| NetworkX | 3.6.1+ | Graph data structure | Industry standard for Python graph operations, used by hypergraph core |
| React Flow | Latest | Interactive graph rendering | Modern, performant web-based graph visualization with nested node support |
| pytest | Latest | Testing framework | Python standard for unit/integration testing |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pytest-golden | 1.0.1+ | Approval/characterization tests | When documenting current behavior before refactoring (Phase 1 use case) |
| dataclasses | stdlib | Immutable data structures | For CoordinateSpace and other value objects |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| NetworkX attrs | Custom VisualizationGraph class | Custom class is more explicit but requires conversion logic; NetworkX is already in use |
| Recursive traversal | Iterative with stack | Iterative is harder to read but avoids stack overflow (not a concern for typical graph depths 1-3) |

**Installation:**
```bash
# Core dependencies already in pyproject.toml
uv add pytest-golden  # For characterization tests only
```

## Architecture Patterns

### Recommended Project Structure
```
src/hypergraph/viz/
├── renderer.py          # Takes nx.DiGraph, returns React Flow JSON (no isinstance checks)
├── html_generator.py    # Embeds JS/CSS, generates HTML
├── traversal.py         # NEW: traverse_to_leaves(node, predicate)
├── coordinates.py       # NEW: CoordinateSpace class
└── assets/
    ├── constraint-layout.js  # Layout algorithm
    ├── layout.js             # Applies layout, manages hierarchy
    ├── app.js                # React Flow app, coordinate transforms
    └── components.js         # React components
```

### Pattern 1: Domain-to-Data Serialization
**What:** Convert domain objects (Graph, HyperNode) to pure data structures (nx.DiGraph with attributes)
**When to use:** Decoupling visualization from domain types

**Example:**
```python
# Source: hypernodes reference implementation (renderer.py pattern)
class Graph:
    def to_viz_graph(self) -> nx.DiGraph:
        """Flatten graph to NetworkX with all viz-needed attributes."""
        G = nx.DiGraph()

        # Add graph-level attributes (including InputSpec)
        G.graph['input_spec'] = {
            'required': list(self.inputs.required),
            'optional': list(self.inputs.optional),
            'seeds': list(self.inputs.seeds),
            'bound': dict(self.inputs.bound),
        }

        # Add nodes with all viz-needed attributes
        for name, hypernode in self.nodes.items():
            G.add_node(name, **self._node_attrs(hypernode))

        # Add edges with attributes
        for u, v, data in self.nx_graph.edges(data=True):
            G.add_edge(u, v, **data)

        return G

    def _node_attrs(self, hypernode: HyperNode) -> dict:
        """Extract viz attributes from HyperNode without isinstance."""
        return {
            'node_type': self._classify_node_type(hypernode),
            'inputs': list(hypernode.inputs),
            'outputs': list(hypernode.outputs),
            'input_types': {p: hypernode.get_input_type(p) for p in hypernode.inputs},
            'output_types': {o: hypernode.get_output_type(o) for o in hypernode.outputs},
            'has_defaults': {p: hypernode.has_default_for(p) for p in hypernode.inputs},
            'parent': getattr(hypernode, 'parent_id', None),
            # Branch-specific data (if applicable)
            'branch_data': self._extract_branch_data(hypernode),
        }
```

### Pattern 2: Attribute-Based Type Dispatch
**What:** Use string-valued 'node_type' attribute instead of isinstance checks
**When to use:** Renderer needs to behave differently for different node types

**Example:**
```python
# BEFORE (couples viz to domain types)
if isinstance(hypernode, GraphNode):
    return "PIPELINE"
elif isinstance(hypernode, RouteNode):
    return "BRANCH"
else:
    return "FUNCTION"

# AFTER (reads from attributes)
node_type = node_attrs['node_type']  # "PIPELINE", "BRANCH", "FUNCTION"
if node_type == "PIPELINE":
    # Render pipeline
```

### Pattern 3: Recursive Traversal with Predicate
**What:** Recursive function takes predicate to determine whether to recurse into child
**When to use:** Hierarchy traversal where depth is implicit in structure

**Example:**
```python
# Source: Standard tree traversal pattern
def traverse_to_leaves(node: dict, predicate: Callable[[dict], bool]) -> Iterator[dict]:
    """Recursively traverse tree, yielding leaves matching predicate.

    Args:
        node: Node dict with optional 'children' key
        predicate: Function that returns True to recurse into this node's children

    Yields:
        Leaf nodes (no children or predicate returns False)
    """
    if not predicate(node) or 'children' not in node:
        yield node
        return

    for child in node['children']:
        yield from traverse_to_leaves(child, predicate)

# Usage: expand graphs up to depth N
def expand_to_depth(depth: int):
    remaining_depth = [depth]  # Mutable to track across recursion

    def should_expand(node: dict) -> bool:
        if node['node_type'] != 'PIPELINE':
            return False
        if remaining_depth[0] <= 0:
            return False
        remaining_depth[0] -= 1
        return True

    return should_expand
```

### Pattern 4: Coordinate Space Abstraction
**What:** Explicit class representing coordinate transformations between layout/parent/absolute/viewport spaces
**When to use:** Complex nested layouts requiring multiple coordinate transforms

**Example:**
```python
# Source: Graphics rendering pipeline (OpenGL, Unity coordinate systems)
from dataclasses import dataclass

@dataclass(frozen=True)
class CoordinateSpace:
    """Represents a 2D coordinate with its reference space.

    Four coordinate spaces:
    - Layout: Position calculated by layout algorithm (relative to layout origin)
    - Parent: Position relative to parent node (for nested graphs)
    - Absolute: Position relative to root canvas (layout + all parent offsets)
    - Viewport: Position after React Flow zoom/pan transform
    """
    x: float
    y: float
    space: str  # "layout" | "parent" | "absolute" | "viewport"

    def to_parent(self, parent_offset: tuple[float, float]) -> "CoordinateSpace":
        """Transform from layout space to parent-relative space."""
        assert self.space == "layout"
        px, py = parent_offset
        return CoordinateSpace(self.x + px, self.y + py, space="parent")

    def to_absolute(self, ancestors: list[tuple[float, float]]) -> "CoordinateSpace":
        """Transform to absolute space by summing all ancestor offsets."""
        assert self.space in ("layout", "parent")
        abs_x, abs_y = self.x, self.y
        for ax, ay in ancestors:
            abs_x += ax
            abs_y += ay
        return CoordinateSpace(abs_x, abs_y, space="absolute")

    def to_viewport(self, viewport_transform: dict) -> "CoordinateSpace":
        """Apply React Flow viewport transform (zoom, pan)."""
        assert self.space == "absolute"
        zoom = viewport_transform['zoom']
        pan_x, pan_y = viewport_transform['x'], viewport_transform['y']
        return CoordinateSpace(
            self.x * zoom + pan_x,
            self.y * zoom + pan_y,
            space="viewport"
        )
```

### Anti-Patterns to Avoid

- **Manual depth tracking**: Passing `remaining_depth` or `depth` parameters through recursive calls - predicate handles this
- **Implicit coordinate spaces**: Adding offsets without documenting which space you're in - leads to double-offset bugs
- **isinstance in renderer**: Couples viz to domain type hierarchy - use node_type attribute
- **Mutable NetworkX graph during render**: Renderer should read-only consume graph - mutations belong in to_viz_graph()

## Don't Hand-Roll

Problems that look simple but have existing solutions:

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Graph serialization to JSON | Custom dict builder | nx.node_link_data() with custom attrs | NetworkX has battle-tested serialization; handles edge cases like reserved names |
| Tree traversal | Manual stack/queue | Recursive generator with predicate | Python generators compose cleanly; recursion depth ~3 levels is safe |
| Coordinate math | Inline x/y arithmetic | CoordinateSpace dataclass | Explicit space tracking prevents offset bugs (e.g., adding parent offset twice) |
| Characterization tests | Manual JSON snapshots | pytest-golden or ApprovalTests.Python | Handles diff display, approval workflow, file management |

**Key insight:** Coordinate bugs are pernicious - the code "looks right" but produces subtle visual glitches. Explicit space tracking makes bugs impossible by construction.

## Common Pitfalls

### Pitfall 1: NetworkX Attribute Name Conflicts
**What goes wrong:** Node attribute named "id" gets silently dropped during serialization
**Why it happens:** NetworkX reserves "id", "source", "target", "key", "links" for internal use
**How to avoid:** Use prefixed names (e.g., "node_id" not "id") or check NetworkX docs for reserved names
**Warning signs:** Attributes present in Python but missing in serialized JSON

### Pitfall 2: Recursive Depth Without Base Case
**What goes wrong:** Stack overflow or infinite recursion when traversing cyclic structures
**Why it happens:** Predicate doesn't account for cycles or missing children
**How to avoid:** Always check if node has children before recursing; predicate should return False for leaves
**Warning signs:** RecursionError or hanging during graph traversal

### Pitfall 3: Double-Offset in Nested Coordinates
**What goes wrong:** Nodes positioned twice as far from center as expected
**Why it happens:** Parent offset added both in Python (to_viz_graph) and JavaScript (layout)
**How to avoid:** CoordinateSpace class makes space explicit; layout always outputs "layout" space, JavaScript converts to absolute
**Warning signs:** Nested graphs positioned far outside parent boundaries

### Pitfall 4: Forgetting to Flatten Nested Graphs
**What goes wrong:** Renderer only sees outer graph, inner nodes never rendered
**Why it happens:** to_viz_graph() doesn't recursively include children of GraphNode
**How to avoid:** Explicitly traverse GraphNode.graph and add children to flat NetworkX with parent_id attribute
**Warning signs:** Nested graphs show as single collapsed node even when depth > 0

### Pitfall 5: isinstance During Rendering
**What goes wrong:** Renderer breaks when domain types change (e.g., adding new node subclass)
**Why it happens:** Tight coupling - renderer imports and checks concrete domain types
**How to avoid:** Renderer receives nx.DiGraph, dispatches on string-valued node_type attribute
**Warning signs:** Import of hypergraph.nodes classes in renderer.py

## Code Examples

Verified patterns from reference implementation and research:

### NetworkX Attribute Serialization
```python
# Source: NetworkX documentation (set_node_attributes pattern)
import networkx as nx

def add_viz_attributes(G: nx.DiGraph, nodes_data: dict) -> None:
    """Add visualization attributes to NetworkX graph nodes.

    Args:
        G: NetworkX DiGraph (modified in-place)
        nodes_data: Dict mapping node_id -> attribute dict
    """
    # Set attributes by node (batch operation)
    for node_id, attrs in nodes_data.items():
        # Use dict unpacking to set multiple attrs at once
        nx.set_node_attributes(G, {node_id: attrs})

    # Or set single attribute across all nodes
    node_types = {nid: data['node_type'] for nid, data in nodes_data.items()}
    nx.set_node_attributes(G, node_types, name='node_type')

# Reading attributes
node_types = nx.get_node_attributes(G, 'node_type')  # Returns dict {node_id: type}
```

### Hierarchy Traversal with Depth Control
```python
# Source: Visitor pattern with predicate (Python tree traversal patterns)
from typing import Iterator, Callable, Any

def traverse_with_depth(
    nodes: dict[str, dict],
    max_depth: int,
    node_filter: Callable[[dict], bool] = lambda n: True
) -> Iterator[tuple[str, dict, int]]:
    """Traverse node hierarchy up to max_depth.

    Args:
        nodes: Dict of node_id -> node attributes (with optional 'parent' key)
        max_depth: Maximum depth to traverse (0 = only roots)
        node_filter: Optional predicate to skip nodes

    Yields:
        (node_id, node_attrs, depth) tuples
    """
    # Build parent->children map
    children: dict[str, list[str]] = {}
    roots: list[str] = []

    for node_id, node_attrs in nodes.items():
        parent = node_attrs.get('parent')
        if parent is None:
            roots.append(node_id)
        else:
            children.setdefault(parent, []).append(node_id)

    def recurse(node_id: str, depth: int) -> Iterator[tuple[str, dict, int]]:
        node_attrs = nodes[node_id]

        if not node_filter(node_attrs):
            return

        yield (node_id, node_attrs, depth)

        # Recurse into children if within depth limit
        if depth < max_depth:
            for child_id in children.get(node_id, []):
                yield from recurse(child_id, depth + 1)

    for root_id in roots:
        yield from recurse(root_id, 0)

# Usage
for node_id, attrs, depth in traverse_with_depth(nodes, max_depth=2):
    print(f"{'  ' * depth}{node_id}: {attrs['node_type']}")
```

### Coordinate Space Transforms
```python
# Source: Unity/Unreal coordinate system docs (parent-relative to world transform)
from dataclasses import dataclass

@dataclass(frozen=True)
class Point:
    x: float
    y: float

def layout_to_absolute(
    node_id: str,
    nodes: dict[str, dict],
    layout_positions: dict[str, Point]
) -> Point:
    """Convert layout-space position to absolute space.

    Args:
        node_id: Target node
        nodes: All nodes with 'parent' attributes
        layout_positions: Layout algorithm output (layout space)

    Returns:
        Absolute position (sum of layout + all ancestor offsets)
    """
    node = nodes[node_id]
    layout_pos = layout_positions[node_id]

    # Start with layout position
    abs_x, abs_y = layout_pos.x, layout_pos.y

    # Walk up parent chain, accumulating offsets
    current = node
    while current.get('parent'):
        parent_id = current['parent']
        parent_pos = layout_positions[parent_id]
        abs_x += parent_pos.x
        abs_y += parent_pos.y
        current = nodes[parent_id]

    return Point(abs_x, abs_y)
```

### Characterization Test Setup
```python
# Source: pytest-golden documentation
import pytest

def test_render_graph_structure(golden):
    """Characterization test: document current render output."""
    from hypergraph import Graph, node

    @node(output_name="y")
    def double(x: int) -> int:
        return x * 2

    graph = Graph(nodes=[double])
    result = render_graph(graph.to_viz_graph())

    # golden.out writes to .out file, compares with .golden on next run
    golden.out["nodes"] = [
        {k: v for k, v in n.items() if k not in ('position', 'style')}
        for n in result['nodes']
    ]  # Exclude non-deterministic fields
    golden.out["edges"] = result['edges']

    # First run: creates .out file, test passes
    # Second run: compares .out with .golden (auto-created from first .out)
    # If different: test fails, shows diff, asks approval
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Renderer takes Graph object | Renderer takes nx.DiGraph | Phase 1 (this work) | Decouples viz from domain types |
| isinstance checks in renderer | String-valued node_type attribute | Phase 1 (this work) | No import of domain types in viz |
| Manual depth parameters | Predicate-based traversal | Phase 1 (this work) | Automatic depth handling |
| Implicit coordinate math | CoordinateSpace class | Phase 1 (this work) | Eliminates double-offset bugs |

**Deprecated/outdated:**
- Direct Graph object to render_graph() - will be replaced with graph.to_viz_graph() returning nx.DiGraph
- _get_node_type() with isinstance checks - will read node_type attribute
- Passing remaining_depth through recursive calls - will use predicate

## Open Questions

Things that couldn't be fully resolved:

1. **Should CoordinateSpace track parent_id for automatic ancestry lookup?**
   - What we know: Current approach requires passing ancestors list explicitly
   - What's unclear: Whether tracking parent in CoordinateSpace would make transforms cleaner or more complex
   - Recommendation: Start simple (explicit ancestors), refactor if repetitive

2. **How to handle non-deterministic layout for characterization tests?**
   - What we know: Layout algorithm produces different positions each run (constraint relaxation is order-dependent)
   - What's unclear: Whether to exclude position from golden files or normalize it
   - Recommendation: Exclude position/style, test only structure (node_type, edges, parent relationships)

3. **Should InputSpec be serialized flat or nested in graph attrs?**
   - What we know: InputSpec has required, optional, seeds, bound fields
   - What's unclear: Best JSON structure for JavaScript consumption
   - Recommendation: Flat dict with all four fields - JavaScript destructures easily

## Sources

### Primary (HIGH confidence)
- [NetworkX 3.6.1 node_link_data documentation](https://networkx.org/documentation/stable/reference/readwrite/generated/networkx.readwrite.json_graph.node_link_data.html) - Serialization patterns
- [NetworkX set_node_attributes documentation](https://networkx.org/documentation/stable/reference/generated/networkx.classes.function.set_node_attributes.html) - Attribute setting best practices
- [React Flow Viewport documentation](https://reactflow.dev/api-reference/types/viewport) - Coordinate system and transforms
- [pytest-golden PyPI page](https://pypi.org/project/pytest-golden/) - Characterization testing library (v1.0.1, released Jan 2026)

### Secondary (MEDIUM confidence)
- [Python tree traversal patterns](https://inventwithpython.com/recursion/chapter4.html) - Recursive depth tracking approaches
- [Visitor pattern in Python](https://refactoring.guru/design-patterns/visitor/python/example) - Predicate-based traversal patterns
- [Unity coordinate systems documentation](https://docs.unity3d.com/6000.0/Documentation/Manual/UIE-coordinate-and-position-system.html) - Parent-relative to absolute transforms
- [Characterization testing (Wikipedia)](https://en.wikipedia.org/wiki/Characterization_test) - Golden master testing concepts

### Tertiary (LOW confidence)
- Multiple blog posts on tree traversal - general patterns apply but need verification against actual codebase
- WebSearch results on coordinate transformations - concepts apply but specific React Flow APIs may differ

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - NetworkX and React Flow are already in use, pytest-golden is established tool
- Architecture: HIGH - Patterns verified from hypernodes reference implementation and official docs
- Pitfalls: MEDIUM - Based on common issues in similar codebases, not all observed in hypergraph yet

**Research date:** 2026-01-21
**Valid until:** ~30 days (stack is stable, no breaking changes expected in NetworkX 3.x or React Flow)
