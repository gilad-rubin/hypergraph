"""Tests for edge routing - verify edges connect to node centers."""
import pytest
from hypergraph import Graph, node
from hypergraph.viz.renderer import render_graph
from hypergraph.viz.html_generator import generate_widget_html


@pytest.fixture
def nested_graph():
    @node(output_name='cleaned')
    def clean_text(text: str) -> str:
        return text.strip()

    @node(output_name='normalized')
    def normalize(cleaned: str) -> str:
        return cleaned.lower()

    @node(output_name='result')
    def analyze(normalized: str) -> str:
        return f"analyzed: {normalized}"

    inner = Graph(nodes=[clean_text, normalize], name='preprocess')
    return Graph(nodes=[inner.as_node(), analyze])


class TestEdgeRoutingToCenters:
    @pytest.mark.asyncio
    async def test_edge_to_inner_node_reaches_center(self, nested_graph, tmp_path):
        """INPUT_GROUP -> clean_text edge should reach center-top of inner node."""
        from playwright.async_api import async_playwright

        html_path = tmp_path / "test.html"
        graph_data = render_graph(nested_graph, depth=1)
        html_content = generate_widget_html(graph_data)
        html_path.write_text(html_content)

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={'width': 1200, 'height': 900})
            await page.goto(f'file://{html_path}')
            await page.wait_for_timeout(2000)

            # Get edge endpoint and node center in SCREEN coordinates
            result = await page.evaluate('''() => {
                const edge = document.querySelector('[data-testid*="inputs"] path');
                if (!edge) return { error: 'Edge not found' };

                const node = document.querySelector('[data-id="clean_text"]');
                if (!node) return { error: 'Node not found' };

                // Get visual endpoint of edge path using SVG methods
                const pathLen = edge.getTotalLength();
                const endPoint = edge.getPointAtLength(pathLen);

                // Transform to screen coordinates
                const svg = edge.ownerSVGElement;
                const ctm = svg.getScreenCTM();
                const screenEndX = ctm.a * endPoint.x + ctm.c * endPoint.y + ctm.e;
                const screenEndY = ctm.b * endPoint.x + ctm.d * endPoint.y + ctm.f;

                // Get node center in screen coordinates
                const nodeRect = node.getBoundingClientRect();
                const nodeCenterX = nodeRect.x + nodeRect.width / 2;
                const nodeCenterY = nodeRect.y;  // Top edge

                return {
                    edgeEndX: screenEndX,
                    edgeEndY: screenEndY,
                    nodeCenterX: nodeCenterX,
                    nodeTopY: nodeCenterY,
                };
            }''')

            await browser.close()

        assert 'error' not in result, result.get('error', '')

        diff_x = abs(result['edgeEndX'] - result['nodeCenterX'])
        diff_y = abs(result['edgeEndY'] - result['nodeTopY'])

        assert diff_x < 15, \
            f"Edge X={result['edgeEndX']:.1f} not at node center X={result['nodeCenterX']:.1f} (diff={diff_x:.1f})"
        assert diff_y < 15, \
            f"Edge Y={result['edgeEndY']:.1f} not at node top Y={result['nodeTopY']:.1f} (diff={diff_y:.1f})"

    @pytest.mark.asyncio
    async def test_edge_from_nested_node_to_external(self, nested_graph, tmp_path):
        """Edge from nested output should reach external analyze node."""
        from playwright.async_api import async_playwright

        html_path = tmp_path / "test.html"
        graph_data = render_graph(nested_graph, depth=1)
        html_content = generate_widget_html(graph_data)
        html_path.write_text(html_content)

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={'width': 1200, 'height': 900})
            await page.goto(f'file://{html_path}')
            await page.wait_for_timeout(2000)

            # Get edge endpoint and node center in SCREEN coordinates
            result = await page.evaluate('''() => {
                // Find edge from preprocess to analyze
                const edges = document.querySelectorAll('.react-flow__edge');
                let edge = null;
                for (const e of edges) {
                    const testId = e.getAttribute('data-testid') || '';
                    if (testId.includes('analyze') && testId.includes('preprocess')) {
                        edge = e.querySelector('path');
                        break;
                    }
                }
                if (!edge) return { error: 'Edge not found' };

                const node = document.querySelector('[data-id="analyze"]');
                if (!node) return { error: 'Node not found' };

                // Get visual endpoint of edge path
                const pathLen = edge.getTotalLength();
                const endPoint = edge.getPointAtLength(pathLen);

                // Transform to screen coordinates
                const svg = edge.ownerSVGElement;
                const ctm = svg.getScreenCTM();
                const screenEndX = ctm.a * endPoint.x + ctm.c * endPoint.y + ctm.e;
                const screenEndY = ctm.b * endPoint.x + ctm.d * endPoint.y + ctm.f;

                // Get node center in screen coordinates
                const nodeRect = node.getBoundingClientRect();
                const nodeCenterX = nodeRect.x + nodeRect.width / 2;
                const nodeTopY = nodeRect.y;

                return {
                    edgeEndX: screenEndX,
                    edgeEndY: screenEndY,
                    nodeCenterX: nodeCenterX,
                    nodeTopY: nodeTopY,
                };
            }''')

            await browser.close()

        assert 'error' not in result, result.get('error', '')

        diff_x = abs(result['edgeEndX'] - result['nodeCenterX'])
        diff_y = abs(result['edgeEndY'] - result['nodeTopY'])

        assert diff_x < 20, \
            f"Edge X={result['edgeEndX']:.1f} not at node center X={result['nodeCenterX']:.1f} (diff={diff_x:.1f})"
        assert diff_y < 20, \
            f"Edge Y={result['edgeEndY']:.1f} not at node top Y={result['nodeTopY']:.1f} (diff={diff_y:.1f})"

    @pytest.mark.asyncio
    async def test_edge_from_inner_node_starts_at_inner_node(self, nested_graph, tmp_path):
        """Edge from inner node should START at inner node's center-bottom, not container edge."""
        from playwright.async_api import async_playwright

        html_path = tmp_path / "test.html"
        graph_data = render_graph(nested_graph, depth=1)
        html_content = generate_widget_html(graph_data)
        html_path.write_text(html_content)

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={'width': 1200, 'height': 900})
            await page.goto(f'file://{html_path}')
            await page.wait_for_timeout(2000)

            # Get edge START point and inner node position
            result = await page.evaluate('''() => {
                // Find edge from preprocess to analyze
                const edges = document.querySelectorAll('.react-flow__edge');
                let edge = null;
                for (const e of edges) {
                    const testId = e.getAttribute('data-testid') || '';
                    if (testId.includes('analyze') && testId.includes('preprocess')) {
                        edge = e.querySelector('path');
                        break;
                    }
                }
                if (!edge) return { error: 'Edge not found' };

                // Get the inner node that produces the output (normalize)
                const innerNode = document.querySelector('[data-id="normalize"]');
                if (!innerNode) return { error: 'Inner node (normalize) not found' };

                // Get visual START point of edge path (point at length 0)
                const startPoint = edge.getPointAtLength(0);

                // Transform to screen coordinates
                const svg = edge.ownerSVGElement;
                const ctm = svg.getScreenCTM();
                const screenStartX = ctm.a * startPoint.x + ctm.c * startPoint.y + ctm.e;
                const screenStartY = ctm.b * startPoint.x + ctm.d * startPoint.y + ctm.f;

                // Get inner node center-bottom in screen coordinates
                const nodeRect = innerNode.getBoundingClientRect();
                const nodeCenterX = nodeRect.x + nodeRect.width / 2;
                const nodeBottomY = nodeRect.y + nodeRect.height;

                // Also get container position for debugging
                const container = document.querySelector('[data-id="preprocess"]');
                const containerRect = container ? container.getBoundingClientRect() : null;

                return {
                    edgeStartX: screenStartX,
                    edgeStartY: screenStartY,
                    nodeCenterX: nodeCenterX,
                    nodeBottomY: nodeBottomY,
                    containerBottom: containerRect ? containerRect.y + containerRect.height : null,
                };
            }''')

            await browser.close()

        assert 'error' not in result, result.get('error', '')

        diff_x = abs(result['edgeStartX'] - result['nodeCenterX'])
        diff_y = abs(result['edgeStartY'] - result['nodeBottomY'])

        assert diff_x < 15, \
            f"Edge start X={result['edgeStartX']:.1f} not at inner node center X={result['nodeCenterX']:.1f} (diff={diff_x:.1f})"
        assert diff_y < 15, \
            f"Edge start Y={result['edgeStartY']:.1f} not at inner node bottom Y={result['nodeBottomY']:.1f} (diff={diff_y:.1f}, container bottom={result.get('containerBottom', 'N/A')})"


class TestDynamicExpansion:
    """Tests for edge routing when expansion state changes at runtime."""

    @pytest.mark.asyncio
    async def test_edge_updates_when_pipeline_expanded(self, tmp_path):
        """When user expands a pipeline, edges should update to route to inner nodes.

        This tests the dynamic expansion case:
        1. Render with depth=1 (inner container visible but collapsed)
        2. Click to expand inner container
        3. Verify edge now routes to the deepest node inside
        """
        from playwright.async_api import async_playwright

        @node(output_name='processed')
        def process(x: str) -> str:
            return x.upper()

        # inner: contains 'process' which consumes 'x'
        inner = Graph(nodes=[process], name='inner')
        # outer: contains 'inner' as a node
        outer = Graph(nodes=[inner.as_node()])

        html_path = tmp_path / "test.html"
        # Render with depth=0 - 'inner' is visible but collapsed
        graph_data = render_graph(outer, depth=0)
        html_content = generate_widget_html(graph_data)
        html_path.write_text(html_content)

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={'width': 1200, 'height': 900})
            await page.goto(f'file://{html_path}')
            await page.wait_for_timeout(2000)

            # Get edge endpoint BEFORE expansion (should point to 'inner' container)
            before_result = await page.evaluate('''() => {
                const edge = document.querySelector('[data-testid*="inputs"] path');
                if (!edge) return { error: 'Edge not found' };

                const innerContainer = document.querySelector('[data-id="inner"]');
                if (!innerContainer) return { error: 'Inner container not found' };

                // Edge should currently target the container
                const pathLen = edge.getTotalLength();
                const endPoint = edge.getPointAtLength(pathLen);

                const svg = edge.ownerSVGElement;
                const ctm = svg.getScreenCTM();
                const screenEndY = ctm.b * endPoint.x + ctm.d * endPoint.y + ctm.f;

                const containerRect = innerContainer.getBoundingClientRect();

                return {
                    edgeEndY: screenEndY,
                    containerTopY: containerRect.top,
                    containerBottomY: containerRect.bottom,
                };
            }''')

            if 'error' in before_result:
                await browser.close()
                pytest.fail(before_result['error'])

            # Now CLICK to expand the 'inner' pipeline
            await page.click('[data-id="inner"]')
            await page.wait_for_timeout(1000)  # Wait for expansion + relayout

            # Get edge endpoint AFTER expansion (should now point to 'process' node)
            after_result = await page.evaluate('''() => {
                const edge = document.querySelector('[data-testid*="inputs"] path');
                if (!edge) return { error: 'Edge not found after expansion' };

                const processNode = document.querySelector('[data-id="process"]');
                if (!processNode) return { error: 'Process node not found after expansion' };

                // Edge should now target the inner 'process' node
                const pathLen = edge.getTotalLength();
                const endPoint = edge.getPointAtLength(pathLen);

                const svg = edge.ownerSVGElement;
                const ctm = svg.getScreenCTM();
                const screenEndX = ctm.a * endPoint.x + ctm.c * endPoint.y + ctm.e;
                const screenEndY = ctm.b * endPoint.x + ctm.d * endPoint.y + ctm.f;

                const nodeRect = processNode.getBoundingClientRect();
                const nodeCenterX = nodeRect.x + nodeRect.width / 2;
                const nodeTopY = nodeRect.top;

                // Also get the container bounds to verify edge goes INSIDE
                const innerContainer = document.querySelector('[data-id="inner"]');
                const containerRect = innerContainer ? innerContainer.getBoundingClientRect() : null;

                return {
                    edgeEndX: screenEndX,
                    edgeEndY: screenEndY,
                    nodeCenterX: nodeCenterX,
                    nodeTopY: nodeTopY,
                    containerTopY: containerRect ? containerRect.top : null,
                };
            }''')

            await browser.close()

        if 'error' in after_result:
            pytest.fail(after_result['error'])

        # After expansion, the edge should point to the 'process' node, not the container
        diff_x = abs(after_result['edgeEndX'] - after_result['nodeCenterX'])
        diff_y = abs(after_result['edgeEndY'] - after_result['nodeTopY'])

        # Edge Y should be at or below container top (meaning it goes INTO the container)
        edge_enters_container = after_result['edgeEndY'] > after_result['containerTopY']

        assert edge_enters_container, (
            f"Edge endpoint Y={after_result['edgeEndY']:.1f} is above container top "
            f"Y={after_result['containerTopY']:.1f} - edge doesn't enter expanded container"
        )
        assert diff_x < 20, (
            f"Edge X={after_result['edgeEndX']:.1f} not at process node center "
            f"X={after_result['nodeCenterX']:.1f} (diff={diff_x:.1f})"
        )
        assert diff_y < 20, (
            f"Edge Y={after_result['edgeEndY']:.1f} not at process node top "
            f"Y={after_result['nodeTopY']:.1f} (diff={diff_y:.1f})"
        )


class TestDoubleNestedGraphs:
    @pytest.mark.asyncio
    async def test_double_nested_nodes_visible(self, tmp_path):
        """Nodes inside double-nested graphs should be visible when depth=2."""
        from playwright.async_api import async_playwright

        @node(output_name='processed')
        def process(x: str) -> str:
            return x.upper()

        @node(output_name='validated')
        def validate(processed: str) -> str:
            return f"valid: {processed}"

        inner = Graph(nodes=[process], name='inner')
        middle = Graph(nodes=[inner.as_node()], name='middle')
        outer = Graph(nodes=[middle.as_node(), validate])

        html_path = tmp_path / "test.html"
        graph_data = render_graph(outer, depth=2)
        html_content = generate_widget_html(graph_data)
        html_path.write_text(html_content)

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={'width': 1200, 'height': 900})
            await page.goto(f'file://{html_path}')
            await page.wait_for_timeout(2000)

            result = await page.evaluate('''() => {
                const nodeIds = ['middle', 'inner', 'process'];
                const results = {};
                for (const id of nodeIds) {
                    const node = document.querySelector(`[data-id="${id}"]`);
                    if (node) {
                        const rect = node.getBoundingClientRect();
                        results[id] = {
                            found: true,
                            visible: rect.width > 0 && rect.height > 0,
                            width: rect.width,
                            height: rect.height
                        };
                    } else {
                        results[id] = { found: false, visible: false };
                    }
                }
                return results;
            }''')

            await browser.close()

        # All three nodes should be visible
        assert result['middle']['found'], "middle container not found"
        assert result['middle']['visible'], "middle container not visible"

        assert result['inner']['found'], "inner container not found"
        assert result['inner']['visible'], "inner container not visible"

        assert result['process']['found'], "process node not found in double-nested graph"
        assert result['process']['visible'], "process node not visible in double-nested graph"
