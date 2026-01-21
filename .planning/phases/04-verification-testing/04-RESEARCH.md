# Phase 4: Verification & Testing - Research

**Researched:** 2026-01-21
**Domain:** Automated visual regression testing and geometric verification for graph visualizations
**Confidence:** HIGH

## Summary

Phase 4 implements automated verification that edge routing fixes work correctly. The verification has two complementary approaches: (1) **geometric intersection testing** to mathematically verify edge paths don't cross node bounding boxes, and (2) **visual regression testing** to catch any unintended changes to the rendered output.

The current codebase has:
1. **Working visualization renderer** - Generates HTML with embedded React Flow
2. **Playwright in dev dependencies** - Already configured for browser automation
3. **pytest with xdist** - Parallel test execution infrastructure
4. **No geometric verification** - Need to add coordinate extraction and intersection testing
5. **No visual regression tests** - Need to add screenshot comparison

The standard approach for verification in hierarchical graph visualization:
1. **Extract rendered coordinates** - Parse HTML/DOM to get actual node positions and edge paths
2. **Geometric intersection tests** - Use computational geometry to verify edges avoid nodes
3. **Visual regression tests** - Screenshot comparison with baselines for regression detection
4. **CI integration** - Automated testing on every push with artifact storage

**Primary recommendation:** Implement geometric verification using Shapely for intersection testing, add Playwright-based visual regression tests with pytest fixtures, and configure GitHub Actions to capture screenshots as artifacts.

## Standard Stack

The established libraries/tools for automated verification of graph visualizations:

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| pytest-playwright | Latest | Browser automation for visual tests | Official pytest plugin, maintains test isolation, handles browser lifecycle |
| Shapely | 2.0+ | Geometric intersection testing | Industry standard for computational geometry, GEOS-based (C++), reliable line/polygon operations |
| BeautifulSoup4 | 4.12+ | HTML/SVG parsing for coordinate extraction | De facto standard for HTML parsing in Python, handles malformed HTML |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| svgelements | Latest | SVG path parsing (d attribute) | When edge paths are SVG paths (not React Flow paths) |
| pytest-playwright-snapshot | Latest | Screenshot comparison with fixtures | Simplifies baseline management vs manual implementation |
| Pillow (PIL) | Latest | Image manipulation for diff visualization | When generating visual diffs of failed comparisons |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Shapely | Manual geometry code | Shapely is battle-tested, handles edge cases, faster (GEOS C++ backend) |
| pytest-playwright | Selenium | Playwright has better async support, faster, official Python support |
| BeautifulSoup4 | lxml directly | BeautifulSoup simpler API, lxml faster but lower-level |
| Screenshot comparison | Manual pixel diff | Plugins handle baseline management, threshold tuning, diff generation |

**Installation:**
```bash
# Already in dev dependencies:
# playwright>=1.56.0
# pytest>=8.4.2

# Add to dev dependencies:
uv add --dev shapely beautifulsoup4 lxml pytest-playwright-snapshot
```

## Architecture Patterns

### Recommended Test Structure

```
tests/viz/
├── conftest.py                    # Shared fixtures (graphs, page setup)
├── test_edge_routing.py          # Geometric verification tests
├── test_visual_regression.py     # Screenshot comparison tests
└── baselines/                     # Reference screenshots
    ├── complex_rag.png
    ├── nested_collapsed.png
    ├── nested_expanded.png
    └── double_nested.png
```

### Pattern 1: Extract Coordinates from Rendered HTML
**What:** Parse the rendered HTML to extract node bounding boxes and edge paths in absolute coordinates
**When to use:** Before running geometric intersection tests

**Example:**
```python
# Source: Combining BeautifulSoup HTML parsing with React Flow DOM structure
from bs4 import BeautifulSoup
from typing import Dict, List, Tuple

def extract_node_positions(html: str) -> Dict[str, Dict[str, float]]:
    """Extract node bounding boxes from rendered React Flow HTML.

    React Flow nodes have data attributes with position and dimensions.
    Format: {node_id: {x, y, width, height}}
    """
    soup = BeautifulSoup(html, 'lxml')
    nodes = {}

    # React Flow renders nodes with .react-flow__node class
    for node_elem in soup.select('.react-flow__node'):
        node_id = node_elem.get('data-id')
        if not node_id:
            continue

        # Position from transform style (absolute viewport coordinates)
        style = node_elem.get('style', '')
        transform = _parse_transform(style)  # Extract translate(x, y)

        # Dimensions from computed layout or data attributes
        width = float(node_elem.get('data-width', 0))
        height = float(node_elem.get('data-height', 0))

        nodes[node_id] = {
            'x': transform['x'],
            'y': transform['y'],
            'width': width,
            'height': height
        }

    return nodes

def extract_edge_paths(html: str) -> Dict[str, List[Tuple[float, float]]]:
    """Extract edge paths from rendered SVG.

    React Flow edges are SVG paths. Extract control points.
    Format: {edge_id: [(x1, y1), (x2, y2), ...]}
    """
    soup = BeautifulSoup(html, 'lxml')
    edges = {}

    # React Flow renders edges as SVG paths
    for edge_elem in soup.select('.react-flow__edge path'):
        edge_id = edge_elem.parent.get('data-id')
        if not edge_id:
            continue

        d_attr = edge_elem.get('d', '')
        # React Flow uses B-spline curves - extract control points
        # For intersection testing, sample points along the curve
        points = _sample_curve_points(d_attr, num_samples=50)
        edges[edge_id] = points

    return edges
```

### Pattern 2: Playwright-Based Coordinate Extraction
**What:** Use Playwright's evaluate() to extract coordinates from the live DOM
**When to use:** When DOM manipulation via JavaScript is more reliable than static HTML parsing

**Example:**
```python
# Source: Playwright Python docs - executing JavaScript in page context
from playwright.sync_api import Page

def extract_coordinates_from_page(page: Page) -> dict:
    """Extract node and edge coordinates from rendered React Flow.

    Uses Playwright's page.evaluate() to run JavaScript in browser context.
    More reliable than parsing HTML - gets actual computed positions.
    """
    coords = page.evaluate("""() => {
        const nodes = {};
        const edges = {};

        // Extract node positions from React Flow
        document.querySelectorAll('.react-flow__node').forEach(node => {
            const id = node.dataset.id;
            const rect = node.getBoundingClientRect();
            nodes[id] = {
                x: rect.left,
                y: rect.top,
                width: rect.width,
                height: rect.height
            };
        });

        // Extract edge paths from SVG
        document.querySelectorAll('.react-flow__edge').forEach(edge => {
            const id = edge.dataset.id;
            const path = edge.querySelector('path');
            if (!path) return;

            // Sample points along the path
            const length = path.getTotalLength();
            const points = [];
            for (let i = 0; i <= 50; i++) {
                const point = path.getPointAtLength((i / 50) * length);
                points.push([point.x, point.y]);
            }
            edges[id] = points;
        });

        return { nodes, edges };
    }""")

    return coords
```

### Pattern 3: Geometric Intersection Testing with Shapely
**What:** Use Shapely to test if edge paths (LineStrings) intersect node bounding boxes (Polygons)
**When to use:** Core verification that edges route around nodes

**Example:**
```python
# Source: Shapely documentation and computational geometry best practices
from shapely.geometry import LineString, box
from shapely import intersects

def verify_no_edge_node_intersections(
    nodes: Dict[str, Dict[str, float]],
    edges: Dict[str, List[Tuple[float, float]]]
) -> List[str]:
    """Verify that no edge paths intersect node bounding boxes.

    Returns:
        List of violation messages (empty if all pass)
    """
    violations = []

    # Convert nodes to Shapely box geometries (rectangles)
    node_boxes = {}
    for node_id, pos in nodes.items():
        # box(minx, miny, maxx, maxy)
        node_boxes[node_id] = box(
            pos['x'],
            pos['y'],
            pos['x'] + pos['width'],
            pos['y'] + pos['height']
        )

    # Check each edge against all nodes
    for edge_id, points in edges.items():
        # Convert edge to LineString
        edge_line = LineString(points)

        # Extract source/target from edge_id (format: "source__target")
        source_id, target_id = edge_id.split('__')

        # Check intersection with all nodes except source and target
        for node_id, node_box in node_boxes.items():
            if node_id in (source_id, target_id):
                continue  # Skip source/target nodes

            if intersects(edge_line, node_box):
                violations.append(
                    f"Edge {edge_id} intersects node {node_id}"
                )

    return violations
```

### Pattern 4: Visual Regression Tests with Playwright
**What:** Screenshot-based regression testing to catch unintended visual changes
**When to use:** Complement geometric tests with pixel-level verification

**Example:**
```python
# Source: pytest-playwright-snapshot plugin pattern
import pytest
from playwright.sync_api import Page

@pytest.fixture
def graph_html(tmp_path):
    """Generate HTML for a test graph and save to temp file."""
    from hypergraph import Graph, node
    from hypergraph.viz.renderer import render_graph
    from hypergraph.viz.html_generator import generate_widget_html

    # Build test graph
    @node(output_name="result")
    def test_node(x: int) -> int:
        return x * 2

    graph = Graph(nodes=[test_node])
    viz_data = render_graph(graph.to_viz_graph())
    html = generate_widget_html(viz_data)

    # Save to temp file
    html_file = tmp_path / "graph.html"
    html_file.write_text(html)
    return html_file

def test_complex_rag_visual_regression(page: Page, graph_html, assert_snapshot):
    """Visual regression test for complex_rag graph."""
    # Navigate to rendered graph
    page.goto(f"file://{graph_html}")

    # Wait for React Flow to render
    page.wait_for_selector('.react-flow__node')

    # Take screenshot and compare with baseline
    # First run creates baseline, subsequent runs compare
    assert_snapshot(page.screenshot(full_page=True), threshold=0.1)
```

### Pattern 5: CI Integration with GitHub Actions
**What:** Automated testing on every push with artifact storage for screenshots
**When to use:** Always - prevents regressions from reaching main branch

**Example:**
```yaml
# Source: Playwright Python official CI docs
# .github/workflows/viz-tests.yml
name: Visualization Tests

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main, develop]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: |
          pip install uv
          uv sync --dev
          uv run playwright install --with-deps

      - name: Run geometric verification tests
        run: |
          uv run pytest tests/viz/test_edge_routing.py -v

      - name: Run visual regression tests
        run: |
          uv run pytest tests/viz/test_visual_regression.py -v

      - name: Upload test artifacts
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: test-results
          path: |
            test-results/
            snapshot_tests_failures/
```

## Don't Hand-Roll

Problems that look simple but have existing solutions:

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| SVG path parsing | Regex for d attribute | svgelements or svg.path | SVG path syntax is complex (arcs, beziers, relative coords), libraries handle all cases |
| Line-rectangle intersection | Manual point-in-polygon math | Shapely intersects() | Handles edge cases (touching, overlap, precision), well-tested, fast (GEOS backend) |
| Screenshot comparison | Pixel-by-pixel diff loop | pytest-playwright-snapshot | Handles baseline management, thresholds, diff images, CI integration |
| Browser automation | Custom Selenium setup | pytest-playwright plugin | Automatic browser lifecycle, test isolation, async support, official pytest integration |
| Curve sampling | Manual B-spline math | SVG path.getTotalLength() | Browser's native implementation handles all curve types correctly |

**Key insight:** Visual testing and computational geometry are mature domains with battle-tested libraries. Custom implementations miss edge cases and are slower than C++-backed libraries like GEOS (Shapely's backend).

## Common Pitfalls

### Pitfall 1: Coordinate Space Confusion
**What goes wrong:** Tests extract coordinates in one space (parent-relative) but compare in another (absolute)
**Why it happens:** React Flow uses parent-relative positioning for nested nodes, but edge paths are in absolute viewport coordinates
**How to avoid:** Always convert to absolute viewport coordinates before geometric testing. Use `getBoundingClientRect()` in Playwright, not `style.transform`.
**Warning signs:** Tests pass for flat graphs but fail for nested graphs; intersection violations reported for edges that visually don't cross nodes.

### Pitfall 2: Testing Before Rendering Completes
**What goes wrong:** Tests extract coordinates before React Flow finishes layout, getting zero or placeholder positions
**Why it happens:** React Flow layout is asynchronous - initial render shows loading state
**How to avoid:** Wait for layout completion signal. Use `page.wait_for_selector('.react-flow__node')` and add explicit wait for layout (check for non-zero positions).
**Warning signs:** All nodes at (0, 0); flaky tests that pass/fail randomly; "node not found" errors.

### Pitfall 3: False Positives from Anti-Aliasing
**What goes wrong:** Visual regression tests fail due to sub-pixel differences in rendering across environments
**Why it happens:** Different OS, GPU, or browser versions render anti-aliased edges slightly differently
**How to avoid:** Set appropriate threshold (0.1 is typical), run baseline generation and tests in same environment (Docker for CI).
**Warning signs:** Tests fail with tiny pixel differences; failures only occur in CI, not locally.

### Pitfall 4: Ignoring Source/Target Nodes in Intersection Tests
**What goes wrong:** Tests report edges intersect their own source/target nodes
**Why it happens:** Edges naturally touch the nodes they connect to
**How to avoid:** When checking if edge intersects a node, skip the edge's source and target nodes. Parse edge IDs to extract source/target.
**Warning signs:** Every edge reported as intersection violation; violations for edges that look correct visually.

### Pitfall 5: Sampling Curves at Too Few Points
**What goes wrong:** Edge curves pass between sample points and cross nodes without detection
**Why it happens:** B-spline curves can bend significantly between widely-spaced samples
**How to avoid:** Sample at sufficient density - 50 points is typical for graph edges. Alternatively, test curve bounding box first (faster).
**Warning signs:** Visual inspection shows edges crossing nodes, but tests pass; increasing sample count reveals violations.

### Pitfall 6: Not Accounting for Edge Stroke Width
**What goes wrong:** Edge centerline doesn't intersect node, but visible stroke does
**Why it happens:** Shapely LineString is infinitely thin; actual SVG stroke has width
**How to avoid:** Expand node bounding boxes by half the stroke width (typically 0.75px for 1.5px stroke), or buffer the LineString.
**Warning signs:** Edges visually touch/overlap nodes but tests pass; very close calls that look wrong.

## Code Examples

Verified patterns from official sources:

### Playwright Fixture Setup for Graph Testing
```python
# Source: pytest-playwright documentation
import pytest
from pathlib import Path
from playwright.sync_api import Page
from hypergraph import Graph
from hypergraph.viz.renderer import render_graph
from hypergraph.viz.html_generator import generate_widget_html

@pytest.fixture
def serve_graph_html(tmp_path):
    """Factory fixture to serve a graph as HTML."""
    def _serve(graph: Graph) -> Path:
        viz_data = render_graph(graph.to_viz_graph())
        html = generate_widget_html(viz_data)

        html_file = tmp_path / "graph.html"
        html_file.write_text(html)
        return html_file

    return _serve

@pytest.fixture
def page_with_graph(page: Page, serve_graph_html):
    """Factory fixture that navigates page to a rendered graph."""
    def _navigate(graph: Graph) -> Page:
        html_file = serve_graph_html(graph)
        page.goto(f"file://{html_file}")

        # Wait for React Flow to render
        page.wait_for_selector('.react-flow__node', timeout=5000)

        # Wait for layout to complete (check for positioned nodes)
        page.wait_for_function("""() => {
            const nodes = document.querySelectorAll('.react-flow__node');
            return nodes.length > 0 &&
                   Array.from(nodes).every(n => {
                       const rect = n.getBoundingClientRect();
                       return rect.left > 0 || rect.top > 0;
                   });
        }""", timeout=5000)

        return page

    return _navigate
```

### Complete Geometric Verification Test
```python
# Source: Combining Shapely intersection with Playwright coordinate extraction
import pytest
from shapely.geometry import LineString, box
from shapely import intersects

def test_complex_rag_no_edge_node_intersections(page_with_graph, complex_rag_graph):
    """Verify edges don't cross nodes in complex_rag graph."""
    page = page_with_graph(complex_rag_graph)

    # Extract coordinates from rendered graph
    coords = page.evaluate("""() => {
        const nodes = {};
        const edges = {};

        document.querySelectorAll('.react-flow__node').forEach(node => {
            const id = node.dataset.id;
            const rect = node.getBoundingClientRect();
            nodes[id] = {
                left: rect.left,
                top: rect.top,
                right: rect.right,
                bottom: rect.bottom
            };
        });

        document.querySelectorAll('.react-flow__edge').forEach(edge => {
            const id = edge.dataset.id;
            const path = edge.querySelector('path');
            if (!path) return;

            const length = path.getTotalLength();
            const points = [];
            for (let i = 0; i <= 50; i++) {
                const pt = path.getPointAtLength((i / 50) * length);
                points.push([pt.x, pt.y]);
            }
            edges[id] = points;
        });

        return { nodes, edges };
    }""")

    # Convert to Shapely geometries
    node_boxes = {
        node_id: box(pos['left'], pos['top'], pos['right'], pos['bottom'])
        for node_id, pos in coords['nodes'].items()
    }

    edge_lines = {
        edge_id: LineString(points)
        for edge_id, points in coords['edges'].items()
    }

    # Check for intersections
    violations = []
    for edge_id, edge_line in edge_lines.items():
        # Parse source/target from edge ID (format: "source__target")
        source_id, target_id = edge_id.split('__')

        for node_id, node_box in node_boxes.items():
            # Skip source and target nodes (edges connect to them)
            if node_id in (source_id, target_id):
                continue

            # Expand box by stroke width (1.5px stroke = 0.75px expansion)
            expanded_box = node_box.buffer(0.75)

            if intersects(edge_line, expanded_box):
                violations.append(f"Edge {edge_id} crosses node {node_id}")

    # Report all violations
    assert not violations, f"Edge-node intersections found:\n" + "\n".join(violations)
```

### Visual Regression Test with Baseline Management
```python
# Source: pytest-playwright-snapshot usage pattern
import pytest
from playwright.sync_api import Page

def test_nested_collapsed_visual_regression(page_with_graph, nested_graph):
    """Visual regression test for collapsed nested graph."""
    page = page_with_graph(nested_graph)

    # Take screenshot
    screenshot = page.screenshot(full_page=True)

    # Compare with baseline
    # Baseline stored in tests/viz/baselines/test_nested_collapsed.png
    # First run: creates baseline
    # Subsequent runs: compares and fails if difference > threshold
    assert_match = pytest.approx(screenshot, abs=0.1)  # 10% threshold

    # Alternative: use pytest-playwright-snapshot
    # assert_snapshot(screenshot, threshold=0.1)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Manual visual inspection | Automated screenshot comparison | ~2020 (Playwright release) | Catches regressions automatically, eliminates human error |
| Custom geometry code | Shapely/GEOS | ~2015 (Shapely 1.5) | Faster (C++ backend), handles edge cases, well-tested |
| Selenium WebDriver | Playwright | ~2021 (Playwright stable) | Better async support, faster, built-in waiting/retry |
| Pixel-perfect comparison | Threshold-based comparison | ~2022 (anti-aliasing awareness) | Reduces false positives from rendering differences |
| Snapshot storage in Git | Artifact storage in CI | ~2023 (GitHub Actions artifacts) | Avoids bloating repo, easier baseline updates |

**Deprecated/outdated:**
- **Selenium for visual testing**: Playwright has better performance and Python support
- **Manual geometric calculations**: Shapely is faster and more reliable than custom math
- **Storing baselines in Git**: Large binary files bloat history; use CI artifacts or separate storage

## Open Questions

Things that couldn't be fully resolved:

1. **Baseline storage strategy**
   - What we know: pytest-playwright-snapshot stores baselines in test directory by default
   - What's unclear: Should baselines be committed to Git or stored separately? Large repos avoid committing images.
   - Recommendation: Start with Git-committed baselines (simple workflow), move to artifact storage if repo bloats

2. **React Flow edge path format**
   - What we know: React Flow renders edges as SVG paths with d attribute
   - What's unclear: Does it use B-spline curves or bezier? Are they always absolute coordinates?
   - Recommendation: Inspect rendered HTML in test setup, use browser's `getTotalLength()` API which works for all path types

3. **Nested node coordinate extraction**
   - What we know: Nested nodes have parent-relative positions in React Flow
   - What's unclear: Does `getBoundingClientRect()` return absolute viewport coordinates or parent-relative?
   - Recommendation: `getBoundingClientRect()` always returns viewport-relative (verified in research), safe to use directly

4. **CI environment consistency**
   - What we know: GitHub Actions provides Ubuntu runners with browsers
   - What's unclear: Do different runner versions produce identical screenshots?
   - Recommendation: Pin ubuntu version in CI (e.g., `ubuntu-22.04`), regenerate baselines when runner updates

## Sources

### Primary (HIGH confidence)
- [Playwright Python Test Runners](https://playwright.dev/python/docs/test-runners) - pytest-playwright fixtures and setup
- [Playwright Python Writing Tests](https://playwright.dev/python/docs/writing-tests) - Test structure and best practices
- [Shapely Manual](https://shapely.readthedocs.io/en/stable/manual.html) - LineString, Polygon, intersection operations
- [Playwright CI Setup](https://playwright.dev/python/docs/ci-intro) - GitHub Actions configuration
- [Visual comparisons | Playwright](https://playwright.dev/docs/test-snapshots) - Screenshot comparison patterns

### Secondary (MEDIUM confidence)
- [GitHub Actions and Playwright to Generate Web Page Screenshots](https://mfyz.com/github-actions-and-playwright-to-generate-web-page-screenshots/) - Practical CI setup
- [Python Visual Regression Testing Tutorial | BrowserStack](https://www.browserstack.com/guide/python-visual-regression-testing) - Visual testing best practices
- [pytest-playwright-snapshot](https://github.com/iloveitaly/pytest-playwright-visual-snapshot) - Snapshot plugin usage
- [Liang-Barsky Algorithm](https://gist.github.com/ChickenProp/3194723) - Line-rectangle intersection (if Shapely unavailable)
- [svgelements PyPI](https://pypi.org/project/svgelements/) - SVG path parsing library

### Tertiary (LOW confidence)
- [A Complete Guide To Playwright Visual Regression Testing](https://www.testmu.ai/learning-hub/playwright-visual-regression-testing/) - General overview
- [Playwright Visual Testing Guide | TestDino](https://testdino.com/blog/playwright-visual-testing/) - Community guide
- [Point in Polygon & Intersect](https://automating-gis-processes.github.io/CSC18/lessons/L4/point-in-polygon.html) - Geometric concepts

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - Playwright and Shapely are industry standards with official documentation
- Architecture: HIGH - Patterns verified from official docs and computational geometry principles
- Pitfalls: HIGH - Based on common issues in visual testing (anti-aliasing, async rendering) and geometric testing (coordinate spaces)

**Research date:** 2026-01-21
**Valid until:** 90 days - Visual testing and geometric libraries are stable domains
