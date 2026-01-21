"""Shared fixtures for visualization tests."""

from pathlib import Path
from typing import Any

import pytest

from hypergraph import Graph, node
from hypergraph.nodes.gate import END, ifelse, route


# =============================================================================
# Simple Node Functions
# =============================================================================


@node(output_name="doubled")
def double(x: int) -> int:
    """Double a number."""
    return x * 2


@node(output_name="tripled")
def triple(x: int) -> int:
    """Triple a number."""
    return x * 3


@node(output_name="result")
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@node(output_name="output")
def identity(x: int) -> int:
    """Return input unchanged."""
    return x


# =============================================================================
# Branch Nodes
# =============================================================================


@ifelse(when_true="double", when_false="triple")
def is_even(x: int) -> bool:
    """Check if a number is even."""
    return x % 2 == 0


@route(targets=["double", "triple", END])
def classify(x: int) -> str:
    """Route based on value."""
    if x > 10:
        return "double"
    elif x > 5:
        return "triple"
    else:
        return END


# =============================================================================
# Graph Fixtures
# =============================================================================


@pytest.fixture
def simple_graph() -> Graph:
    """Single node graph."""
    return Graph(nodes=[double])


@pytest.fixture
def linear_graph() -> Graph:
    """Three-node linear data flow."""
    @node(output_name="doubled")
    def double_fn(x: int) -> int:
        return x * 2

    @node(output_name="tripled")
    def triple_fn(doubled: int) -> int:
        return doubled * 3

    @node(output_name="result")
    def add_fn(tripled: int, y: int) -> int:
        return tripled + y

    return Graph(nodes=[double_fn, triple_fn, add_fn])


@pytest.fixture
def branching_graph() -> Graph:
    """Graph with ifelse branch node."""
    return Graph(nodes=[is_even, double, triple])


@pytest.fixture
def nested_graph() -> Graph:
    """Graph with one level of nesting."""
    inner = Graph(nodes=[double], name="inner")
    return Graph(nodes=[inner.as_node(), add])


@pytest.fixture
def double_nested_graph() -> Graph:
    """Graph with two levels of nesting."""
    innermost = Graph(nodes=[double], name="innermost")
    middle = Graph(nodes=[innermost.as_node(), triple], name="middle")
    return Graph(nodes=[middle.as_node(), add])


@pytest.fixture
def bound_graph() -> Graph:
    """Graph with bound input."""
    return Graph(nodes=[add]).bind(a=5)


@pytest.fixture
def complex_rag_graph() -> Graph:
    """Complex RAG pipeline with 19 nodes (matches test_viz_layout.ipynb)."""
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

    @node(output_name="normalized_embeddings")
    def normalize(embeddings: list[list[float]]) -> list[list[float]]:
        return embeddings

    @node(output_name="index")
    def build_index(normalized_embeddings: list[list[float]]) -> dict:
        return {"vectors": normalized_embeddings}

    @node(output_name="query_text")
    def parse_query(user_input: str) -> str:
        return user_input.strip()

    @node(output_name="query_embedding")
    def embed_query(query_text: str, model_name: str) -> list[float]:
        return [0.1] * 768

    @node(output_name="expanded_queries")
    def expand_query(query_text: str) -> list[str]:
        return [query_text, f"{query_text} synonym"]

    @node(output_name="query_embeddings")
    def embed_expanded(
        expanded_queries: list[str], model_name: str
    ) -> list[list[float]]:
        return [[0.1] * 768 for _ in expanded_queries]

    @node(output_name="candidates")
    def search_index(
        index: dict, query_embedding: list[float], top_k: int
    ) -> list[int]:
        return list(range(top_k))

    @node(output_name="expanded_candidates")
    def search_expanded(
        index: dict, query_embeddings: list[list[float]], top_k: int
    ) -> list[int]:
        return list(range(top_k * 2))

    @node(output_name="merged_candidates")
    def merge_results(
        candidates: list[int], expanded_candidates: list[int]
    ) -> list[int]:
        return list(set(candidates + expanded_candidates))

    @node(output_name="retrieved_docs")
    def fetch_documents(
        merged_candidates: list[int], chunks: list[list[str]]
    ) -> list[str]:
        return [" ".join(chunks[i]) for i in merged_candidates if i < len(chunks)]

    @node(output_name="context")
    def format_context(retrieved_docs: list[str]) -> str:
        return "\n\n".join(retrieved_docs)

    @node(output_name="prompt")
    def build_prompt(context: str, query_text: str, system_prompt: str) -> str:
        return f"{system_prompt}\n\nContext:\n{context}\n\nQuery: {query_text}"

    @node(output_name="raw_response")
    def call_llm(prompt: str, temperature: float, max_tokens: int) -> str:
        return "Generated response..."

    @node(output_name="final_answer")
    def postprocess(raw_response: str) -> str:
        return raw_response.strip()

    return Graph(
        nodes=[
            chunk,
            load_data,
            clean,
            tokenize,
            embed_chunks,
            normalize,
            build_index,
            parse_query,
            embed_query,
            expand_query,
            embed_expanded,
            search_index,
            search_expanded,
            merge_results,
            fetch_documents,
            format_context,
            build_prompt,
            call_llm,
            postprocess,
        ],
        name="rag_pipeline",
    )


# =============================================================================
# Playwright Fixtures
# =============================================================================


@pytest.fixture
def serve_graph_html(tmp_path: Path):
    """Factory fixture that renders a graph to an HTML file and returns its path.

    Usage:
        def test_something(serve_graph_html):
            html_path = serve_graph_html(my_graph)
            # Use html_path with Playwright
    """
    from hypergraph.viz.html_generator import generate_widget_html
    from hypergraph.viz.renderer import render_graph

    def _serve(graph: Graph, **render_kwargs) -> Path:
        """Render graph to HTML file and return path."""
        # Convert graph to viz format
        viz_graph = graph.to_viz_graph()
        graph_data = render_graph(viz_graph, **render_kwargs)
        html = generate_widget_html(graph_data)

        # Write to temp file
        html_path = tmp_path / "graph.html"
        html_path.write_text(html, encoding="utf-8")
        return html_path

    return _serve


@pytest.fixture
def page_with_graph(serve_graph_html):
    """Factory fixture that navigates a Playwright page to a rendered graph.

    Usage:
        def test_something(page, page_with_graph):
            page_with_graph(page, my_graph)
            # Now page is loaded with the graph
    """

    def _load(page, graph: Graph, **render_kwargs):
        """Navigate page to rendered graph HTML."""
        html_path = serve_graph_html(graph, **render_kwargs)
        page.goto(f"file://{html_path}")
        # Wait for React Flow to be ready
        page.wait_for_selector(".react-flow", timeout=5000)

    return _load


# =============================================================================
# Utility Functions
# =============================================================================


def normalize_render_output(render_output: dict[str, Any]) -> dict[str, Any]:
    """Normalize render output for comparison.

    Removes position-dependent fields and sorts collections to make
    structural comparisons easier.

    Args:
        render_output: Output from render_graph()

    Returns:
        Normalized output with:
        - Positions removed
        - Nodes sorted by id
        - Edges sorted by id
        - Collections within nodes sorted
    """
    normalized = {
        "nodes": [],
        "edges": [],
        "meta": render_output.get("meta", {}),
    }

    # Normalize nodes - remove positions, sort
    for node in render_output.get("nodes", []):
        norm_node = {
            "id": node["id"],
            "type": node["type"],
            "data": dict(node["data"]),
        }

        # Include parent reference if present
        if "parentNode" in node:
            norm_node["parentNode"] = node["parentNode"]

        # Sort inputs/outputs if present
        if "inputs" in norm_node["data"]:
            norm_node["data"]["inputs"] = sorted(
                norm_node["data"]["inputs"], key=lambda x: x["name"]
            )
        if "outputs" in norm_node["data"]:
            norm_node["data"]["outputs"] = sorted(
                norm_node["data"]["outputs"], key=lambda x: x["name"]
            )

        normalized["nodes"].append(norm_node)

    # Sort nodes by id
    normalized["nodes"].sort(key=lambda x: x["id"])

    # Normalize edges - just copy structure, sort
    for edge in render_output.get("edges", []):
        norm_edge = {
            "id": edge["id"],
            "source": edge["source"],
            "target": edge["target"],
            "data": dict(edge.get("data", {})),
        }
        normalized["edges"].append(norm_edge)

    # Sort edges by id
    normalized["edges"].sort(key=lambda x: x["id"])

    return normalized
