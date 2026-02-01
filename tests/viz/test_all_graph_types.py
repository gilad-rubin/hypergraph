"""TDD tests for all graph types from notebooks/test_viz_layout.ipynb.

Tests verify:
1. All nodes are connected (no orphans)
2. Nested graphs have children within bounding boxes
3. Edge validation passes (structural, not rendered positions)
"""

import pytest
from hypergraph import Graph, node
from hypergraph.viz.debug import VizDebugger, validate_graph, find_issues


# =============================================================================
# Graph Definitions (extracted from notebooks/test_viz_layout.ipynb)
# =============================================================================

# --- Simple 2-node graph ---
@node(output_name="y")
def double(x: int) -> int:
    return x * 2


@node(output_name="z")
def square(y: int) -> int:
    return y**2


# --- RAG pipeline (3 nodes) ---
@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return [0.1] * 10


@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]:
    return ["doc1", "doc2"]


@node(output_name="answer")
def generate(docs: list[str], query: str) -> str:
    return "Answer"


# --- Diamond pattern (4 nodes) ---
@node(output_name="a")
def start(x: int) -> int:
    return x


@node(output_name="b")
def left(a: int) -> int:
    return a + 1


@node(output_name="c")
def right(a: int) -> int:
    return a * 2


@node(output_name="d")
def merge(b: int, c: int) -> int:
    return b + c


# --- 1-level nested: workflow ---
@node(output_name="cleaned")
def clean_text(text: str) -> str:
    return text.strip()


@node(output_name="normalized")
def normalize_text(cleaned: str) -> str:
    return cleaned.lower()


@node(output_name="result")
def analyze(normalized: str) -> dict:
    return {"length": len(normalized)}


# --- 2-level nested: outer ---
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


# --- Complex RAG (subset - key nodes) ---
@node(output_name="raw_text")
def load_data(filepath: str) -> str:
    return "raw content"


@node(output_name="cleaned_text")
def clean(raw_text: str) -> str:
    return raw_text.strip()


@node(output_name="tokens")
def tokenize(cleaned_text: str) -> list[str]:
    return cleaned_text.split()


@node(output_name="chunks")
def chunk(tokens: list[str], chunk_size: int) -> list[list[str]]:
    return [tokens[i : i + chunk_size] for i in range(0, len(tokens), chunk_size)]


@node(output_name="embeddings")
def embed_chunks(chunks: list[list[str]], model_name: str) -> list[list[float]]:
    return [[0.1] * 768 for _ in chunks]


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def simple_graph():
    """Simple 2-node linear graph."""
    return Graph(nodes=[double, square])


@pytest.fixture
def rag_graph():
    """3-node RAG pipeline."""
    return Graph(nodes=[embed, retrieve, generate])


@pytest.fixture
def diamond_graph():
    """4-node diamond pattern with fan-out/fan-in."""
    return Graph(nodes=[start, left, right, merge])


@pytest.fixture
def workflow_graph():
    """1-level nested graph: preprocess -> analyze."""
    preprocess = Graph(nodes=[clean_text, normalize_text], name="preprocess")
    return Graph(nodes=[preprocess.as_node(), analyze])


@pytest.fixture
def outer_graph():
    """2-level nested graph: inner -> middle -> outer."""
    inner = Graph(nodes=[step1, step2], name="inner")
    middle = Graph(nodes=[inner.as_node(), validate], name="middle")
    return Graph(nodes=[middle.as_node(), log_result])


@pytest.fixture
def etl_chain():
    """ETL-style chain: load -> clean -> tokenize."""
    return Graph(nodes=[load_data, clean, tokenize])


# =============================================================================
# Structural Validation Tests
# =============================================================================


class TestStructuralValidation:
    """Tests for basic graph structure validation."""

    def test_simple_graph_valid(self, simple_graph):
        """Simple graph should pass validation."""
        result = validate_graph(simple_graph)
        assert result.valid is True
        assert result.errors == []

    def test_rag_graph_valid(self, rag_graph):
        """RAG pipeline should pass validation."""
        result = validate_graph(rag_graph)
        assert result.valid is True

    def test_diamond_graph_valid(self, diamond_graph):
        """Diamond pattern should pass validation."""
        result = validate_graph(diamond_graph)
        assert result.valid is True

    def test_workflow_graph_valid(self, workflow_graph):
        """Nested workflow should pass validation."""
        result = validate_graph(workflow_graph)
        assert result.valid is True

    def test_outer_graph_valid(self, outer_graph):
        """2-level nested graph should pass validation."""
        result = validate_graph(outer_graph)
        assert result.valid is True

    def test_etl_chain_valid(self, etl_chain):
        """ETL chain should pass validation."""
        result = validate_graph(etl_chain)
        assert result.valid is True


# =============================================================================
# Connectivity Tests
# =============================================================================


class TestNodeConnectivity:
    """Tests to verify all nodes are properly connected."""

    def test_simple_graph_no_orphans(self, simple_graph):
        """Simple graph has no disconnected nodes."""
        issues = find_issues(simple_graph)
        assert issues.disconnected_nodes == []

    def test_rag_graph_all_connected(self, rag_graph):
        """RAG graph has proper edge connections."""
        debugger = VizDebugger(rag_graph)

        # Check embed -> retrieve
        edge1 = debugger.trace_edge("embed", "retrieve")
        assert edge1.edge_found is True, "embed -> retrieve should exist"

        # Check retrieve -> generate
        edge2 = debugger.trace_edge("retrieve", "generate")
        assert edge2.edge_found is True, "retrieve -> generate should exist"

    def test_diamond_fan_out(self, diamond_graph):
        """Diamond graph has correct fan-out from start."""
        debugger = VizDebugger(diamond_graph)

        # start produces 'a', both left and right consume it
        edge_left = debugger.trace_edge("start", "left")
        edge_right = debugger.trace_edge("start", "right")

        assert edge_left.edge_found is True, "start -> left should exist"
        assert edge_right.edge_found is True, "start -> right should exist"

    def test_diamond_fan_in(self, diamond_graph):
        """Diamond graph has correct fan-in to merge."""
        debugger = VizDebugger(diamond_graph)

        # merge consumes from both left and right
        edge_from_left = debugger.trace_edge("left", "merge")
        edge_from_right = debugger.trace_edge("right", "merge")

        assert edge_from_left.edge_found is True, "left -> merge should exist"
        assert edge_from_right.edge_found is True, "right -> merge should exist"

    def test_nested_internal_edges(self, workflow_graph):
        """Nested graph internal edges are connected."""
        debugger = VizDebugger(workflow_graph)

        # Inside preprocess: clean_text -> normalize_text (hierarchical IDs)
        edge = debugger.trace_edge("preprocess/clean_text", "preprocess/normalize_text")
        assert edge.edge_found is True, "preprocess/clean_text -> preprocess/normalize_text should exist"


# =============================================================================
# Nested Graph Structure Tests
# =============================================================================


class TestNestedGraphStructure:
    """Tests for nested graph parent-child relationships."""

    def test_workflow_parent_child(self, workflow_graph):
        """Workflow children have correct parent."""
        debugger = VizDebugger(workflow_graph)
        G = debugger.flat_graph

        # clean_text and normalize_text should have parent="preprocess" (hierarchical IDs)
        clean_attrs = G.nodes.get("preprocess/clean_text", {})
        normalize_attrs = G.nodes.get("preprocess/normalize_text", {})

        assert clean_attrs.get("parent") == "preprocess", \
            "preprocess/clean_text should have parent=preprocess"
        assert normalize_attrs.get("parent") == "preprocess", \
            "preprocess/normalize_text should have parent=preprocess"

    def test_workflow_graph_node_type(self, workflow_graph):
        """Preprocess node should be type GRAPH."""
        debugger = VizDebugger(workflow_graph)
        info = debugger.trace_node("preprocess")

        assert info.status == "FOUND"
        assert info.node_type == "GRAPH", "preprocess should be type GRAPH"

    def test_workflow_graph_children(self, workflow_graph):
        """Preprocess node should list its children."""
        debugger = VizDebugger(workflow_graph)
        info = debugger.trace_node("preprocess")

        assert "children" in info.details
        # Children now have hierarchical IDs
        assert "preprocess/clean_text" in info.details["children"]
        assert "preprocess/normalize_text" in info.details["children"]

    def test_outer_graph_nesting_depth(self, outer_graph):
        """2-level nested graph has correct parent chain."""
        debugger = VizDebugger(outer_graph)
        G = debugger.flat_graph

        # step1 and step2 -> parent=middle/inner (hierarchical IDs)
        step1_attrs = G.nodes.get("middle/inner/step1", {})
        assert step1_attrs.get("parent") == "middle/inner", "middle/inner/step1 should have parent=middle/inner"

        # inner -> parent=middle (hierarchical ID: middle/inner)
        inner_attrs = G.nodes.get("middle/inner", {})
        assert inner_attrs.get("parent") == "middle", "middle/inner should have parent=middle"

        # middle -> parent=None (top-level)
        middle_attrs = G.nodes.get("middle", {})
        assert middle_attrs.get("parent") is None, "middle should have no parent"


# =============================================================================
# Edge Tracing Tests (Points From / Points To)
# =============================================================================


class TestEdgeTracing:
    """Tests for 'points from' and 'points to' edge tracing."""

    def test_trace_incoming_edges(self, diamond_graph):
        """merge node should have 2 incoming edges."""
        debugger = VizDebugger(diamond_graph)
        info = debugger.trace_node("merge")

        assert len(info.incoming_edges) == 2, "merge should have 2 incoming edges"
        sources = {e["from"] for e in info.incoming_edges}
        assert sources == {"left", "right"}

    def test_trace_outgoing_edges(self, diamond_graph):
        """start node should have 2 outgoing edges."""
        debugger = VizDebugger(diamond_graph)
        info = debugger.trace_node("start")

        assert len(info.outgoing_edges) == 2, "start should have 2 outgoing edges"
        targets = {e["to"] for e in info.outgoing_edges}
        assert targets == {"left", "right"}

    def test_trace_nested_edges(self, workflow_graph):
        """Trace edges crossing nested graph boundary."""
        debugger = VizDebugger(workflow_graph)

        # preprocess -> analyze (cross boundary)
        info = debugger.trace_node("analyze")

        # The edge comes from preprocess or normalize_text depending on flattening
        assert len(info.incoming_edges) >= 1, "analyze should have incoming edges"


# =============================================================================
# Stats and Dump Tests
# =============================================================================


class TestDebugDump:
    """Tests for debug_dump() output structure."""

    def test_dump_node_count(self, diamond_graph):
        """Dump should report correct node count."""
        debugger = VizDebugger(diamond_graph)
        dump = debugger.debug_dump()

        assert dump["stats"]["total_nodes"] == 4

    def test_dump_edge_count(self, diamond_graph):
        """Dump should report correct edge count."""
        debugger = VizDebugger(diamond_graph)
        dump = debugger.debug_dump()

        # start->left, start->right, left->merge, right->merge = 4 edges
        assert dump["stats"]["total_edges"] == 4

    def test_dump_edges_by_source(self, rag_graph):
        """edges_by_source map should be correct."""
        debugger = VizDebugger(rag_graph)
        dump = debugger.debug_dump()

        edges_by_source = dump["metadata"]["edges_by_source"]
        assert "embed" in edges_by_source
        assert "retrieve" in edges_by_source["embed"]

    def test_dump_edges_by_target(self, rag_graph):
        """edges_by_target map should be correct."""
        debugger = VizDebugger(rag_graph)
        dump = debugger.debug_dump()

        edges_by_target = dump["metadata"]["edges_by_target"]
        assert "retrieve" in edges_by_target
        assert "embed" in edges_by_target["retrieve"]


# =============================================================================
# Rendered Position Tests (Playwright required)
# =============================================================================

try:
    import playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestRenderedPositions:
    """Tests for rendered visualization positions."""

    def test_simple_graph_edges_valid(self, simple_graph):
        """Simple graph should have valid edge positions (target below source)."""
        from hypergraph.viz import extract_debug_data

        data = extract_debug_data(simple_graph, depth=0)
        assert data.summary["edgeIssues"] == 0, (
            f"Expected 0 edge issues, got {data.summary['edgeIssues']}"
        )

    def test_diamond_graph_edges_valid(self, diamond_graph):
        """Diamond graph should have valid edge positions."""
        from hypergraph.viz import extract_debug_data

        data = extract_debug_data(diamond_graph, depth=0)
        assert data.summary["edgeIssues"] == 0, (
            f"Expected 0 edge issues, got {data.summary['edgeIssues']}"
        )

    def test_rag_graph_edges_valid(self, rag_graph):
        """RAG graph should have valid edge positions."""
        from hypergraph.viz import extract_debug_data

        data = extract_debug_data(rag_graph, depth=0)
        assert data.summary["edgeIssues"] == 0, (
            f"Expected 0 edge issues, got {data.summary['edgeIssues']}"
        )

    def test_etl_chain_edges_valid(self, etl_chain):
        """ETL chain should have valid edge positions."""
        from hypergraph.viz import extract_debug_data

        data = extract_debug_data(etl_chain, depth=0)
        assert data.summary["edgeIssues"] == 0, (
            f"Expected 0 edge issues, got {data.summary['edgeIssues']}"
        )

    def test_workflow_expanded_edges_valid(self, workflow_graph):
        """Expanded workflow should have valid edge positions."""
        from hypergraph.viz import extract_debug_data

        data = extract_debug_data(workflow_graph, depth=1)
        assert data.summary["edgeIssues"] == 0, (
            f"Expected 0 edge issues:\n{_format_edge_issues(data)}"
        )

    def test_outer_expanded_edges_valid(self, outer_graph):
        """Expanded outer graph should have valid edge positions."""
        from hypergraph.viz import extract_debug_data

        data = extract_debug_data(outer_graph, depth=2)
        assert data.summary["edgeIssues"] == 0, (
            f"Expected 0 edge issues:\n{_format_edge_issues(data)}"
        )


def _format_edge_issues(data) -> str:
    """Format edge issues for error message."""
    return "\n".join(
        f"  {e.source} -> {e.target}: {e.issue}"
        for e in data.edge_issues
    )
