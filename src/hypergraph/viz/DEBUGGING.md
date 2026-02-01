# Visualization Debugging Guide

This guide documents all debugging tools and dataclasses for the hypergraph visualization system. Use these tools to diagnose edge routing, layout, and rendering issues.

---

## For AI Agents: Quick Debugging Checklist

**When debugging edge/layout issues, use this workflow:**

### 1. Pre-render validation (is the graph structure correct?)
```python
from hypergraph.viz.debug import VizDebugger
debugger = VizDebugger(graph)
issues = debugger.find_issues()
if issues.has_issues:
    print(issues)  # Shows orphan edges, missing parents, etc.
```

### 2. Post-render validation (are edges connecting correctly?)
```python
from hypergraph.viz.debug import extract_debug_data
data = extract_debug_data(graph, depth=1)
data.print_report()  # Shows expected vs actual for all edges
```

### 3. Detailed coordinate analysis (in Playwright tests)
```python
from tests.viz.conftest import extract_inner_bounds_and_edge_paths, wait_for_debug_ready

# After page.goto(html_file)
wait_for_debug_ready(page)
data = extract_inner_bounds_and_edge_paths(page)

# Check specific edge
for edge in data['edgePaths']:
    source = data['innerBounds'][edge['source']]
    print(f"Edge starts at Y={edge['startY']}, source bottom={source['bottom']}")
    print(f"Gap: {edge['startY'] - source['bottom']}px")
```

### Key Principle
CSS `box-shadow` is purely visual and does NOT affect `getBoundingClientRect()`. If edges don't connect to nodes, the problem is a **dimension mismatch** in the layout calculation, not shadows.

---

## Quick Reference

| Tool | Purpose | When to Use |
|------|---------|-------------|
| `VizDebugger` | Pre-render graph analysis | Before rendering, to check structure |
| `extract_debug_data()` | Post-render browser extraction | After rendering, to validate layout |
| `NodeGeometry` / `EdgeGeometry` | Precise coordinate validation | Testing edge-to-node connections |
| `extract_inner_bounds_and_edge_paths()` | Shadow gap detection | Testing edge endpoints vs visible bounds |

---

## 1. Pre-Render Debugging: `VizDebugger`

The `VizDebugger` class analyzes the graph structure **before** rendering. Use it to catch issues like orphan edges, missing parents, or disconnected nodes.

### Basic Usage

```python
from hypergraph.viz.debug import VizDebugger

debugger = VizDebugger(graph)

# Quick validation
result = debugger.validate()
if not result.valid:
    print("Errors:", result.errors)

# Trace a specific node
info = debugger.trace_node("my_node")
print(f"Incoming: {info.incoming_edges}")
print(f"Outgoing: {info.outgoing_edges}")

# Trace a specific edge (even missing ones)
edge_info = debugger.trace_edge("source_node", "target_node")

# Full diagnostics
issues = debugger.find_issues()
if issues.has_issues:
    print("Orphan edges:", issues.orphan_edges)
    print("Disconnected nodes:", issues.disconnected_nodes)

# Complete state dump
dump = debugger.debug_dump()
```

### Shortcut Access

```python
# From the graph object directly
debugger = graph.debug_viz()
debugger.visualize(depth=1)  # Shows viz with debug overlays
```

### Dataclasses

#### `ValidationResult`
```python
@dataclass
class ValidationResult:
    valid: bool                    # True if no errors
    errors: list[str]              # List of error messages
    warnings: list[str]            # List of warning messages
```

#### `NodeTrace`
```python
@dataclass
class NodeTrace:
    status: str                    # "FOUND" or "NOT_FOUND"
    node_id: str
    node_type: Optional[str]       # "FUNCTION", "GRAPH", "DATA", "INPUT"
    parent: Optional[str]          # Parent node ID
    incoming_edges: list[dict]     # [{from, value, type}, ...]
    outgoing_edges: list[dict]     # [{to, value, type}, ...]
    details: dict                  # {label, inputs, outputs, children}
    partial_matches: list[str]     # Similar IDs if not found
```

#### `EdgeTrace`
```python
@dataclass
class EdgeTrace:
    edge_query: str                # "source -> target"
    edge_found: bool
    source_info: dict              # Node analysis for source
    target_info: dict              # Node analysis for target
    analysis: dict                 # Why edge might be missing
```

#### `IssueReport`
```python
@dataclass
class IssueReport:
    validation_errors: list[str]
    orphan_edges: list[str]        # Edges with missing source/target
    disconnected_nodes: list[str]  # Nodes with no edges
    missing_parents: list[str]     # Nodes with invalid parent refs
    self_loops: list[str]          # Nodes pointing to themselves

    @property
    def has_issues(self) -> bool   # True if any issues found
```

---

## 2. Post-Render Debugging: `extract_debug_data()`

After rendering to HTML, use `extract_debug_data()` to extract validation data from the browser via Playwright.

### Basic Usage

```python
from hypergraph.viz.debug import extract_debug_data

# Requires: pip install playwright && playwright install chromium

data = extract_debug_data(graph, depth=1)
data.print_report()

# Check for edge issues
for edge in data.edge_issues:
    print(f"{edge.source} -> {edge.target}: {edge.issue}")
```

### Output

```
=== Edge Validation Report ===
Nodes: 5 | Edges: 4 | Issues: 1

INVALID EDGES
----------------------------------------------------------------------
Edge                                Expected        Actual
----------------------------------------------------------------------
validate → log_result               vDist >= 0      vDist = -16

VALID EDGES
----------------------------------------------------------------------
Edge                                vDist      hDist
----------------------------------------------------------------------
step1 → step2                       45         0
step2 → validate                    45         0
...
```

### Dataclasses

#### `RenderedEdge`
```python
@dataclass
class RenderedEdge:
    source: str
    target: str
    source_label: Optional[str]
    target_label: Optional[str]
    src_bottom: Optional[float]     # Y coord of source bottom
    tgt_top: Optional[float]        # Y coord of target top
    vert_dist: Optional[float]      # Distance: tgt_top - src_bottom
    horiz_dist: Optional[float]     # Horizontal offset
    status: str                     # "OK" or "ISSUE"
    issue: Optional[str]            # Description of issue
```

#### `RenderedDebugData`
```python
@dataclass
class RenderedDebugData:
    version: int
    timestamp: int
    nodes: list[dict]
    edges: list[RenderedEdge]
    summary: dict[str, int]         # {totalNodes, totalEdges, edgeIssues}

    @property
    def edge_issues(self) -> list[RenderedEdge]  # Edges with status != "OK"

    def print_report(self) -> None  # Human-readable report
```

---

## 3. Geometry Validation: `EdgeConnectionValidator`

For precise coordinate-level testing of edge connections to node boundaries.

### Basic Usage

```python
from hypergraph.viz.geometry import (
    NodeGeometry,
    EdgeGeometry,
    EdgeConnectionValidator,
    format_issues,
)

# Create node geometries from extracted bounds
nodes = {
    "node_a": NodeGeometry(id="node_a", x=100, y=50, width=80, height=40),
    "node_b": NodeGeometry(id="node_b", x=100, y=150, width=80, height=40),
}

# Create edge geometries from SVG paths
edges = [
    EdgeGeometry(
        source_id="node_a",
        target_id="node_b",
        start_point=(140, 90),   # Should be center-bottom of node_a
        end_point=(140, 150),    # Should be center-top of node_b
    )
]

# Validate with tolerance
validator = EdgeConnectionValidator(nodes=nodes, edges=edges, tolerance=5.0)
issues = validator.validate_all()

if issues:
    print(format_issues(issues))
```

### Dataclasses

#### `NodeGeometry`
```python
@dataclass(frozen=True)
class NodeGeometry:
    id: str
    x: float       # Left edge
    y: float       # Top edge
    width: float
    height: float

    @property
    def center_x(self) -> float            # Horizontal center
    @property
    def bottom(self) -> float              # Y coord of bottom edge
    @property
    def center_bottom(self) -> tuple       # (x, y) where edges should START
    @property
    def center_top(self) -> tuple          # (x, y) where edges should END
```

#### `EdgeGeometry`
```python
@dataclass(frozen=True)
class EdgeGeometry:
    source_id: str
    target_id: str
    start_point: tuple[float, float]  # First SVG path point (M command)
    end_point: tuple[float, float]    # Last SVG path point (arrow tip)
```

---

## 4. Shadow Gap Detection: `extract_inner_bounds_and_edge_paths()`

CSS shadows extend beyond visible node boundaries. This helper extracts the **inner** (visible) bounds separately from the wrapper bounds.

### The Problem

```
┌─────────────────┐ ← Wrapper bounds (includes shadow)
│  ╔═══════════╗  │
│  ║   NODE    ║  │ ← Inner bounds (visible box)
│  ╚═══════════╝  │
│      shadow     │
└─────────────────┘
```

Edges should connect to the **inner** bounds, not the wrapper bounds.

### Usage (in Playwright tests)

```python
from tests.viz.conftest import (
    extract_inner_bounds_and_edge_paths,
    wait_for_debug_ready,
)

# In a Playwright test
def test_edge_shadow_gap(page, temp_html_file):
    visualize(graph, filepath=temp_html_file, _debug_overlays=True)
    page.goto(f"file://{temp_html_file}")
    wait_for_debug_ready(page)

    data = extract_inner_bounds_and_edge_paths(page)

    # Check each edge
    for edge in data['edgePaths']:
        source_id = edge['source']
        inner = data['innerBounds'][source_id]

        # Edge should start at inner bottom, not wrapper bottom
        gap = edge['startY'] - inner['bottom']
        assert gap <= 5.0, f"Gap too large: {gap}px"
```

### Return Value

```python
{
    'innerBounds': {
        'node_id': {'top': 100, 'bottom': 140, 'left': 50, 'right': 130, 'centerX': 90}
    },
    'wrapperBounds': {
        'node_id': {'top': 94, 'bottom': 154, ...}  # Includes shadow
    },
    'shadowOffsets': {
        'node_id': {'topOffset': 6, 'bottomOffset': 14}  # How much shadow extends
    },
    'edgePaths': [
        {'source': 'a', 'target': 'b', 'startX': 90, 'startY': 145, 'endX': 90, 'endY': 200}
    ],
    'viewportTransform': {'x': 100, 'y': 50, 'zoom': 1.0}
}
```

---

## 5. Testing Helpers (conftest.py)

Shared fixtures and helpers for Playwright-based tests.

### Available Helpers

| Function | Purpose |
|----------|---------|
| `wait_for_debug_ready(page)` | Wait for `__hypergraphVizDebug` API |
| `extract_debug_nodes(page)` | Get nodes from debug API |
| `extract_debug_edges(page)` | Get edges from debug API |
| `extract_debug_summary(page)` | Get summary stats |
| `extract_edge_routing(page)` | Get edge source/target mappings |
| `extract_inner_bounds_and_edge_paths(page)` | Get shadow-aware bounds |
| `click_to_expand_container(page, id)` | Expand a collapsed container |
| `click_to_collapse_container(page, id)` | Collapse an expanded container |
| `render_and_extract(page, graph, depth, path)` | Render and extract in one call |

### Example Test

```python
import pytest
from tests.viz.conftest import (
    HAS_PLAYWRIGHT,
    make_outer,
    wait_for_debug_ready,
    extract_edge_routing,
    click_to_expand_container,
)

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright required")
class TestEdgeRouting:
    def test_edge_targets_after_expand(self, page, temp_html_file):
        from hypergraph.viz.widget import visualize

        graph = make_outer()
        visualize(graph, depth=0, filepath=temp_html_file, _debug_overlays=True)
        page.goto(f"file://{temp_html_file}")

        # Before expand: edges point to collapsed container
        routing = extract_edge_routing(page)
        assert routing['edges']['e_validate_log_result']['target'] == 'middle'

        # After expand: edges should re-route to internal nodes
        click_to_expand_container(page, 'middle')
        routing = extract_edge_routing(page)
        assert routing['edges']['e_validate_log_result']['target'] == 'log_result'
```

---

## 6. Coordinate Spaces (coordinates.py)

For hierarchical layouts with nested containers.

### Dataclasses

#### `Point`
```python
@dataclass(frozen=True)
class Point:
    x: float
    y: float

    def __add__(self, other: Point) -> Point
    def __sub__(self, other: Point) -> Point
```

#### `CoordinateSpace`
```python
@dataclass(frozen=True)
class CoordinateSpace:
    x: float                         # X offset in parent space
    y: float                         # Y offset in parent space
    space: str                       # Name identifier
    parent: CoordinateSpace | None   # Parent space (None for root)

    def to_parent(self, point: Point) -> Point
    def to_absolute(self, point: Point) -> Point
    def to_viewport(self, point: Point, viewport_offset: Point) -> Point
```

### Usage

```python
from hypergraph.viz.coordinates import Point, CoordinateSpace

root = CoordinateSpace(0, 0, "root")
container = CoordinateSpace(100, 50, "container", parent=root)
nested = CoordinateSpace(20, 20, "nested", parent=container)

# Point in nested space
local_point = Point(10, 10)

# Convert to absolute (root) space
absolute = nested.to_absolute(local_point)
# Result: Point(x=130, y=80)  # 100+20+10, 50+20+10
```

---

## 7. Browser Debug API

When rendering with `_debug_overlays=True`, the browser exposes `window.__hypergraphVizDebug`:

```javascript
window.__hypergraphVizDebug = {
    version: 1,          // Increments on layout changes
    timestamp: Date.now(),
    nodes: [
        { id: "node_a", label: "Node A", x: 100, y: 50, width: 80, height: 40 }
    ],
    edges: [
        {
            id: "e_node_a_node_b",
            source: "node_a",
            target: "node_b",
            srcBottom: 90,
            tgtTop: 150,
            vertDist: 60,
            horizDist: 0,
            status: "OK"
        }
    ],
    summary: {
        totalNodes: 2,
        totalEdges: 1,
        edgeIssues: 0
    }
}
```

### Enabling Debug Mode

```python
# In Python
visualize(graph, _debug_overlays=True)

# Or in browser console before rendering
window.__hypergraph_debug_viz = true
```

### Debug Overlays

When enabled, the visualization shows:
- **BOUNDS tab**: Node boundary boxes
- **WIDTHS tab**: Width measurements
- **TEXTS tab**: Text element bounds
- **Edge debug points**: Start/end markers on edges

---

## Common Debugging Scenarios

### 1. "Edge doesn't connect to node"

```python
# Use shadow gap detection
data = extract_inner_bounds_and_edge_paths(page)
for edge in data['edgePaths']:
    inner = data['innerBounds'][edge['source']]
    gap = edge['startY'] - inner['bottom']
    print(f"{edge['source']}: gap = {gap}px")
```

### 2. "Edge points to wrong node after expand/collapse"

```python
# Use edge routing extraction
routing_before = extract_edge_routing(page)
click_to_expand_container(page, 'container_id')
routing_after = extract_edge_routing(page)

# Compare targets
print(f"Before: {routing_before['edges']['e_a_b']['target']}")
print(f"After: {routing_after['edges']['e_a_b']['target']}")
```

### 3. "Missing edge in visualization"

```python
# Use pre-render debugger
debugger = VizDebugger(graph)
edge_info = debugger.trace_edge("source_node", "target_node")
if not edge_info.edge_found:
    print("Analysis:", edge_info.analysis)
```

### 4. "Node layout seems wrong"

```python
# Use full debug dump
debugger = VizDebugger(graph)
dump = debugger.debug_dump()
for node in dump['nodes']:
    print(f"{node['id']}: parent={node['parent']}, inputs={node['inputs']}")
```

---

## File Locations

| File | Contents |
|------|----------|
| `src/hypergraph/viz/debug.py` | VizDebugger, extract_debug_data, dataclasses |
| `src/hypergraph/viz/geometry.py` | NodeGeometry, EdgeGeometry, EdgeConnectionValidator |
| `src/hypergraph/viz/coordinates.py` | Point, CoordinateSpace |
| `tests/viz/conftest.py` | Playwright helpers, test fixtures |
| `src/hypergraph/viz/CLAUDE.md` | Architecture notes, known issues |
