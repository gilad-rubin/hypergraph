"""Shared fixtures for visualization tests.

This module provides:
1. HAS_PLAYWRIGHT constant for skip decorators
2. Common test graph fixtures (make_workflow, make_outer, etc.)
3. Shared Playwright helpers (page fixture, temp file handling)
4. Common extraction helpers for debugging and validation
"""

import hashlib
import os
import shutil
import tempfile

import pytest

from hypergraph import Graph, node
from hypergraph.viz.html_generator import generate_widget_html
from hypergraph.viz.renderer import render_graph

# =============================================================================
# Playwright Detection
# =============================================================================

try:
    import playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


# =============================================================================
# Test Node Definitions
# =============================================================================

# --- Simple graph nodes ---
@node(output_name="a_out")
def node_a(x: int) -> int:
    return x + 1


@node(output_name="b_out")
def node_b(a_out: int) -> int:
    return a_out * 2


@node(output_name="c_out")
def node_c(b_out: int) -> int:
    return b_out + 10


# --- 1-level nesting: workflow nodes ---
@node(output_name="cleaned")
def clean_text(text: str) -> str:
    """First step: clean the input text."""
    return text.strip()


@node(output_name="normalized")
def normalize_text(cleaned: str) -> str:
    """Second step: normalize the cleaned text."""
    return cleaned.lower()


@node(output_name="result")
def analyze(normalized: str) -> dict:
    """Final step: analyze the normalized text."""
    return {"length": len(normalized)}


# --- 2-level nesting: outer nodes ---
@node(output_name="step1_out")
def step1(x: int) -> int:
    return x + 1


@node(output_name="step2_out")
def step2(step1_out: int) -> int:
    return step1_out * 2


@node(output_name="validated")
def validate(step2_out: int) -> int:
    return step2_out


@node(output_name="logged")
def log_result(validated: int) -> int:
    return validated


# =============================================================================
# Test Graph Factories
# =============================================================================

def make_simple_graph() -> Graph:
    """Simple 2-node graph: a -> b."""
    return Graph(nodes=[node_a, node_b])


def make_chain_graph() -> Graph:
    """3-node chain: a -> b -> c."""
    return Graph(nodes=[node_a, node_b, node_c])


def make_workflow() -> Graph:
    """1-level nested graph: preprocess[clean_text, normalize_text] -> analyze."""
    preprocess = Graph(nodes=[clean_text, normalize_text], name="preprocess")
    return Graph(nodes=[preprocess.as_node(), analyze])


def make_outer() -> Graph:
    """2-level nested graph: middle[inner[step1, step2], validate] -> log_result."""
    inner = Graph(nodes=[step1, step2], name="inner")
    middle = Graph(nodes=[inner.as_node(), validate], name="middle")
    return Graph(nodes=[middle.as_node(), log_result])


# =============================================================================
# Render Cache
# =============================================================================

_VIZ_HTML_CACHE_DIR = tempfile.TemporaryDirectory(prefix="hypergraph-viz-cache-")


def _viz_cache_key(
    graph: Graph,
    depth: int,
    theme: str,
    show_types: bool,
    separate_outputs: bool,
    debug_overlays: bool,
) -> tuple:
    """Build a stable cache key for HTML rendering within a test run."""
    input_spec = graph.inputs
    bound_items = tuple(
        sorted((key, repr(value)) for key, value in input_spec.bound.items())
    )
    return (
        graph.definition_hash,
        graph.name,
        input_spec.required,
        input_spec.optional,
        input_spec.seeds,
        bound_items,
        depth,
        theme,
        show_types,
        separate_outputs,
        debug_overlays,
    )


def _cached_html_path(
    graph: Graph,
    *,
    depth: int,
    theme: str = "auto",
    show_types: bool = False,
    separate_outputs: bool = False,
    debug_overlays: bool = False,
) -> str:
    """Render HTML once per cache key and return the cached file path."""
    cache_key = _viz_cache_key(
        graph,
        depth,
        theme,
        show_types,
        separate_outputs,
        debug_overlays,
    )
    digest = hashlib.sha256(repr(cache_key).encode("utf-8")).hexdigest()
    cache_path = os.path.join(_VIZ_HTML_CACHE_DIR.name, f"{digest}.html")

    if not os.path.exists(cache_path):
        flat_graph = graph.to_flat_graph()
        graph_data = render_graph(
            flat_graph,
            depth=depth,
            theme=theme,
            show_types=show_types,
            separate_outputs=separate_outputs,
            debug_overlays=debug_overlays,
        )
        html_content = generate_widget_html(graph_data)
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(html_content)

    return cache_path


# =============================================================================
# Playwright Fixtures
# =============================================================================

@pytest.fixture(scope="session")
def _playwright_instance():
    """Shared Playwright instance for the test session."""
    if not HAS_PLAYWRIGHT:
        pytest.skip("playwright not installed")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def _browser(_playwright_instance):
    """Shared browser instance for the test session."""
    browser = _playwright_instance.chromium.launch(headless=True)
    yield browser
    browser.close()


@pytest.fixture
def page(_browser):
    """Create a Playwright page for testing."""
    page = _browser.new_page()
    yield page
    page.close()


@pytest.fixture
def temp_html_file():
    """Create a temporary HTML file for rendering visualizations.

    Yields the file path, cleans up after test.

    Usage:
        def test_something(self, temp_html_file):
            visualize(graph, filepath=temp_html_file)
            page.goto(f"file://{temp_html_file}")
    """
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        temp_path = f.name
    yield temp_path
    if os.path.exists(temp_path):
        os.unlink(temp_path)


# =============================================================================
# Common Extraction Helpers
# =============================================================================

def wait_for_debug_ready(page, timeout: int = 10000) -> None:
    """Wait for the hypergraph debug API and centering to be ready.

    Waits for both:
    1. __hypergraphVizDebug.version > 0 (layout complete)
    2. __hypergraphVizReady == true (centering complete, graph visible)
    """
    page.wait_for_function(
        "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0 && window.__hypergraphVizReady === true",
        timeout=timeout,
    )


def extract_debug_nodes(page) -> list[dict]:
    """Extract node data from the debug API."""
    return page.evaluate("window.__hypergraphVizDebug.nodes")


def extract_debug_edges(page) -> list[dict]:
    """Extract edge data from the debug API."""
    return page.evaluate("window.__hypergraphVizDebug.edges")


def extract_debug_summary(page) -> dict:
    """Extract summary data from the debug API."""
    return page.evaluate("window.__hypergraphVizDebug.summary")


def extract_edge_routing(page) -> dict:
    """Extract edge routing data from the debug API.

    Returns dict with:
    - edges: mapping edge ID to routing info
    - nodeIds: list of all node IDs
    - summary: debug summary data
    """
    wait_for_debug_ready(page)

    return page.evaluate("""() => {
        const debug = window.__hypergraphVizDebug;
        const edges = {};

        // Get edges from debug data
        for (const edge of debug.edges || []) {
            // Use actual routing targets if available (for re-routed edges)
            const actualSource = (edge.data && edge.data.actualSource) || edge.source;
            const actualTarget = (edge.data && edge.data.actualTarget) || edge.target;

            edges[edge.id] = {
                source: actualSource,
                target: actualTarget,
                originalSource: edge.source,
                originalTarget: edge.target,
                data: edge.data || {},
            };
        }

        return {
            edges: edges,
            nodeIds: debug.nodes.map(n => n.id),
            summary: debug.summary,
        };
    }""")


def extract_inner_bounds_and_edge_paths(page) -> dict:
    """Extract INNER element bounds and edge path coordinates from rendered visualization.

    MEASUREMENT SOURCES:
    1. wrapperBounds: getBoundingClientRect() on .react-flow__node elements
       - React Flow's wrapper that includes padding, handles, and layout spacing
       - ~10px larger than calculated dimensions due to React Flow chrome

    2. innerBounds: getBoundingClientRect() on .group.rounded-lg elements
       - The actual visible node element (background, border, content)
       - 6-14px smaller than wrapper due to wrapper containing handle elements
       - NOTE: CSS shadow-lg does NOT affect these bounds (proven by width matching)

    3. shadowOffsets: difference between wrapper and inner bounds
       - Measures how much wrapper extends beyond inner element
       - This offset is from handle elements, NOT from CSS shadows
       - Shadow is purely visual and doesn't affect getBoundingClientRect()

    4. edgePaths: coordinates extracted from SVG <path> elements
       - Start coordinates from 'M x y' (move-to command) in path 'd' attribute
       - End coordinates from last coordinate pair in path 'd' attribute
       - Coordinates are in layout space (transformed by viewport zoom/pan)

    Returns dict with:
    - innerBounds: {nodeId: {top, bottom, left, right, centerX}} for inner elements
    - wrapperBounds: {nodeId: {top, bottom, left, right, centerX}} for wrapper elements
    - shadowOffsets: {nodeId: {topOffset, bottomOffset}} wrapper-to-inner difference
    - edgePaths: [{source, target, startX, startY, endX, endY, pathD}] from SVG paths
    - viewportTransform: {x, y, zoom} for coordinate conversion

    This is useful for testing edge-to-shadow gap detection - comparing edges
    to VISIBLE node boundaries rather than wrapper bounds.
    """
    return page.evaluate("""() => {
        const result = {
            innerBounds: {},
            wrapperBounds: {},
            shadowOffsets: {},
            edgePaths: [],
            viewportTransform: null,
            errors: []
        };

        // Get viewport transform from ReactFlow
        const viewport = document.querySelector('.react-flow__viewport');
        if (viewport) {
            const transform = viewport.style.transform;
            const match = transform.match(/translate\\(([\\d.-]+)px,\\s*([\\d.-]+)px\\)\\s*scale\\(([\\d.-]+)\\)/);
            if (match) {
                result.viewportTransform = {
                    x: parseFloat(match[1]),
                    y: parseFloat(match[2]),
                    zoom: parseFloat(match[3])
                };
            }
        }

        // Get all node wrappers
        const nodeWrappers = document.querySelectorAll('.react-flow__node');

        for (const wrapper of nodeWrappers) {
            // Get node ID from data attribute
            const nodeId = wrapper.getAttribute('data-id');
            if (!nodeId) continue;

            // Get wrapper bounds (includes shadow area)
            const wrapperRect = wrapper.getBoundingClientRect();
            result.wrapperBounds[nodeId] = {
                top: wrapperRect.top,
                bottom: wrapperRect.bottom,
                left: wrapperRect.left,
                right: wrapperRect.right,
                centerX: (wrapperRect.left + wrapperRect.right) / 2
            };

            // Get inner element bounds (excludes shadow)
            // Look for the actual visible node element
            const innerNode = wrapper.querySelector('.group.rounded-lg') ||
                              wrapper.querySelector('.rounded-lg') ||
                              wrapper.firstElementChild;

            if (innerNode) {
                const innerRect = innerNode.getBoundingClientRect();
                result.innerBounds[nodeId] = {
                    top: innerRect.top,
                    bottom: innerRect.bottom,
                    left: innerRect.left,
                    right: innerRect.right,
                    centerX: (innerRect.left + innerRect.right) / 2
                };

                // Calculate shadow offset (how much wrapper extends beyond inner)
                result.shadowOffsets[nodeId] = {
                    topOffset: innerRect.top - wrapperRect.top,  // positive if inner is lower
                    bottomOffset: wrapperRect.bottom - innerRect.bottom  // positive if wrapper extends below
                };
            }
        }

        // Get debug edges which have proper source/target fields
        const debugEdges = window.__hypergraphVizDebug ? window.__hypergraphVizDebug.edges : [];

        // Get edge paths from SVG and match with debug edges
        const edgeGroups = document.querySelectorAll('.react-flow__edge');
        for (const group of edgeGroups) {
            const path = group.querySelector('path.react-flow__edge-path');
            if (!path) continue;

            const pathD = path.getAttribute('d');
            if (!pathD) continue;

            // Get edge ID from data-testid (format: rf__edge-{edgeId})
            const testId = group.getAttribute('data-testid') || '';

            // Parse start Y from "M x y" pattern
            const startMatch = pathD.match(/M\\s*([\\d.-]+)[,\\s]+([\\d.-]+)/);
            let startX = null, startY = null;
            if (startMatch) {
                startX = parseFloat(startMatch[1]);
                startY = parseFloat(startMatch[2]);
            }

            // Parse end Y - last coordinate pair in path
            const allCoords = pathD.match(/[\\d.-]+/g);
            let endX = null, endY = null;
            if (allCoords && allCoords.length >= 2) {
                endX = parseFloat(allCoords[allCoords.length - 2]);
                endY = parseFloat(allCoords[allCoords.length - 1]);
            }

            // Find matching debug edge by checking if testId contains the edge id
            // Debug edges have format like {id: "e_source_target", source: "source", target: "target"}
            let source = null, target = null;
            for (const debugEdge of debugEdges) {
                if (testId.includes(debugEdge.id)) {
                    // Use actualSource/actualTarget if available (for re-routed edges)
                    source = (debugEdge.data && debugEdge.data.actualSource) || debugEdge.source;
                    target = (debugEdge.data && debugEdge.data.actualTarget) || debugEdge.target;
                    break;
                }
            }

            result.edgePaths.push({
                testId: testId,
                source: source,
                target: target,
                startX: startX,
                startY: startY,
                endX: endX,
                endY: endY,
                pathD: pathD.substring(0, 100)
            });
        }

        return result;
    }""")


def convert_layout_to_screen(layout_y: float, viewport_transform: dict) -> float:
    """Convert layout Y coordinate to screen Y coordinate."""
    if viewport_transform is None:
        return layout_y
    return layout_y * viewport_transform['zoom'] + viewport_transform['y']


def click_to_expand_container(page, container_id: str) -> None:
    """Click on a collapsed container node to expand it.

    Waits for layout to settle after expansion.
    """
    wait_for_debug_ready(page)
    initial_version = page.evaluate("window.__hypergraphVizDebug.version")

    # Find and click the container node
    # React Flow nodes have data-id attribute or id in class
    node_selector = f'[data-id="{container_id}"], .react-flow__node-custom[id*="{container_id}"]'

    # Try multiple strategies to find the node
    node_element = page.locator(node_selector).first
    if node_element.count() == 0:
        # Fallback: find by node label text
        node_element = page.locator(f'.react-flow__node:has-text("{container_id}")').first

    node_element.click()

    # Wait for layout to update (version should increment) AND centering to complete
    page.wait_for_function(
        f"window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > {initial_version} && window.__hypergraphVizReady === true",
        timeout=10000,
    )


def click_to_collapse_container(page, container_id: str) -> None:
    """Click on an expanded container node to collapse it.

    For expanded containers, there's a button at the top with the container label.
    Clicking this button triggers the collapse. The button is rendered as:
    <button className="...absolute -top-3 left-4...">
        <Icon /> {label}
    </button>

    Waits for layout to settle after collapse.
    """
    wait_for_debug_ready(page)
    initial_version = page.evaluate("window.__hypergraphVizDebug.version")

    # Find the node container
    node_selector = f'[data-id="{container_id}"]'
    node_element = page.locator(node_selector).first

    if node_element.count() == 0:
        # Fallback: find by node label text
        node_element = page.locator(f'.react-flow__node:has-text("{container_id}")').first

    # For expanded containers, find the collapse button inside the node
    # The button has the container label text and is styled with "absolute -top-3"
    collapse_button = node_element.locator('button').first
    if collapse_button.count() > 0:
        collapse_button.click()
    else:
        # Fallback: try clicking the title text directly
        title_element = node_element.locator(f'text={container_id}').first
        if title_element.count() > 0:
            title_element.click()
        else:
            # Last resort: click the node itself
            node_element.click()

    # Wait for layout to update (version should increment) AND centering to complete
    page.wait_for_function(
        f"window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > {initial_version} && window.__hypergraphVizReady === true",
        timeout=10000,
    )


def render_and_extract(page, graph: Graph, depth: int, temp_path: str) -> dict:
    """Render graph at given depth and extract edge routing."""
    cache_path = _cached_html_path(graph, depth=depth)
    shutil.copyfile(cache_path, temp_path)
    page.goto(f"file://{temp_path}")
    return extract_edge_routing(page)


def render_to_page(page, graph: Graph, depth: int, temp_path: str) -> None:
    """Render graph to a temp HTML file and navigate the page to it."""
    cache_path = _cached_html_path(graph, depth=depth)
    shutil.copyfile(cache_path, temp_path)
    page.goto(f"file://{temp_path}")
    wait_for_debug_ready(page)
