"""Tests for the HTML generator."""

import pytest
from hypergraph.viz.html import generate_widget_html


# Sample graph data for testing
SAMPLE_GRAPH_DATA = {
    "nodes": [
        {
            "id": "double",
            "type": "custom",
            "position": {"x": 0, "y": 0},
            "data": {
                "nodeType": "FUNCTION",
                "label": "double",
                "outputs": [{"name": "doubled", "type": "int"}],
                "inputs": [{"name": "x", "type": "int", "is_bound": False}],
                "theme": "dark",
                "showTypes": False,
            },
        }
    ],
    "edges": [],
    "meta": {
        "theme_preference": "dark",
        "show_types": False,
        "separate_outputs": False,
        "initial_depth": 1,
    },
}


class TestGenerateHtml:
    """Tests for generate_widget_html function."""

    def test_returns_string(self):
        """Test that generate_widget_html returns a string."""
        html = generate_widget_html(SAMPLE_GRAPH_DATA)
        assert isinstance(html, str)

    def test_contains_doctype(self):
        """Test that HTML starts with DOCTYPE."""
        html = generate_widget_html(SAMPLE_GRAPH_DATA)
        assert html.strip().startswith("<!DOCTYPE html>")

    def test_contains_vendor_libs(self):
        """Test that all vendor libraries are bundled."""
        html = generate_widget_html(SAMPLE_GRAPH_DATA)

        # React
        assert "React" in html
        # ReactDOM
        assert "ReactDOM" in html
        # ReactFlow
        assert "ReactFlow" in html
        # HypergraphViz (single visualization module)
        assert "HypergraphViz" in html

    def test_no_external_scripts(self):
        """Test that HTML has no external script sources."""
        html = generate_widget_html(SAMPLE_GRAPH_DATA)

        # Should not have any src= attributes pointing to CDNs
        assert 'src="http' not in html
        assert 'src="https' not in html
        assert "cdnjs" not in html.lower()
        assert "unpkg" not in html.lower()
        assert "jsdelivr" not in html.lower()

    def test_contains_graph_data(self):
        """Test that graph data is embedded as JSON."""
        html = generate_widget_html(SAMPLE_GRAPH_DATA)

        # The graph data should be embedded somewhere in the HTML
        assert '"double"' in html  # Node ID should be in the JSON

    def test_contains_custom_styles(self):
        """Test that custom CSS styles are included."""
        html = generate_widget_html(SAMPLE_GRAPH_DATA)

        # Should have Tailwind CSS or its classes
        assert "tailwind" in html.lower() or "text-slate" in html or "bg-slate" in html

    def test_contains_root_element(self):
        """Test that root element for React is present."""
        html = generate_widget_html(SAMPLE_GRAPH_DATA)

        assert 'id="root"' in html

    def test_contains_iife_initialization(self):
        """Test that app uses IIFE (not ES modules) for VSCode compatibility."""
        html = generate_widget_html(SAMPLE_GRAPH_DATA)

        # Should use IIFE pattern
        assert "(function()" in html or "(function ()" in html
        # Should NOT use ES modules for script tags (ok in comments)
        assert '<script type="module"' not in html

    def test_uses_domcontentloaded(self):
        """Test that initialization waits for DOM to be ready."""
        html = generate_widget_html(SAMPLE_GRAPH_DATA)

        assert "DOMContentLoaded" in html


class TestHtmlStructure:
    """Tests for overall HTML structure."""

    def test_has_head_and_body(self):
        """Test that HTML has proper head and body sections."""
        html = generate_widget_html(SAMPLE_GRAPH_DATA)

        assert "<head>" in html
        assert "</head>" in html
        assert "<body>" in html
        assert "</body>" in html

    def test_has_meta_charset(self):
        """Test that HTML declares UTF-8 charset."""
        html = generate_widget_html(SAMPLE_GRAPH_DATA)

        assert 'charset="UTF-8"' in html or "charset=UTF-8" in html

    def test_style_tags_present(self):
        """Test that style tags are present for CSS."""
        html = generate_widget_html(SAMPLE_GRAPH_DATA)

        assert "<style>" in html
        assert "</style>" in html

    def test_script_tags_present(self):
        """Test that script tags are present for JavaScript."""
        html = generate_widget_html(SAMPLE_GRAPH_DATA)

        assert "<script>" in html
        assert "</script>" in html
