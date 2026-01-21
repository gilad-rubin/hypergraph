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
