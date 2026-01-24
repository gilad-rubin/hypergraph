"""Test that edges crossing container boundaries connect to visible nodes."""
import pytest
from hypergraph import Graph, node
from hypergraph.viz.renderer import render_graph


# Innermost graph: retrieval
@node(output_name="retrieved_pages")
def retrieve_pages(query: str, vector_store: object, top_k_pages: int) -> list:
    return []


@node(output_name="documents")
def aggregate_pages(retrieved_pages: list, aggregator: object) -> list:
    return []


@node(output_name="sorted_documents")
def sort_documents(documents: list) -> list:
    return []


@node(output_name="top_documents")
def limit_documents(sorted_documents: list, top_k_documents: int) -> list:
    return []


# Middle graph: retrieval_recall
@node(output_name="query")
def extract_retrieval_query(eval_pair: dict) -> str:
    return ""


@node(output_name="retrieved_ids")
def extract_retrieved_ids(top_documents: list) -> list:
    return []


@node(output_name="retrieval_eval_result")
def compute_recall(eval_pair: dict, retrieved_ids: list, top_documents: list) -> dict:
    return {}


# Outer graph nodes
@node(output_name="eval_queries")
def load_queries(max_queries: int, filter_local: bool) -> list:
    return []


@node(output_name="eval_pairs")
def build_retrieval_pairs(eval_queries: list) -> list:
    return []


@node(output_name="retrieval_metrics")
def compute_retrieval_metrics(retrieval_eval_results: list) -> dict:
    return {}


def build_triple_nested_graph():
    """Build the retrieval_recall_batch graph with triple nesting."""
    retrieval_graph = Graph(
        nodes=[retrieve_pages, aggregate_pages, sort_documents, limit_documents],
        name="retrieval"
    )
    bound_retrieval = retrieval_graph.bind(
        vector_store="mock_vector_store",
        top_k_pages=30,
        top_k_documents=6,
        aggregator="mock_aggregator",
    )

    retrieval_node = bound_retrieval.as_node(name="retrieval")
    retrieval_recall_graph = Graph(
        nodes=[extract_retrieval_query, retrieval_node, extract_retrieved_ids, compute_recall],
        name="retrieval_recall"
    )

    batch_recall = (
        retrieval_recall_graph.as_node(name="batch_recall")
        .with_inputs(eval_pair="eval_pairs")
        .with_outputs(retrieval_eval_result="retrieval_eval_results")
        .map_over("eval_pairs")
    )

    outer_graph = Graph(
        nodes=[load_queries, build_retrieval_pairs, batch_recall, compute_retrieval_metrics],
        name="retrieval_recall_batch"
    )
    return outer_graph.bind(max_queries=10, filter_local=True)


class TestCrossBoundaryEdge:
    """Test edges that cross from inside a container to outside."""

    def test_edge_to_external_node_has_internal_source(self):
        """Edge to compute_retrieval_metrics should come from compute_recall (internal)."""
        bound_graph = build_triple_nested_graph()
        flat_graph = bound_graph.to_flat_graph()
        result = render_graph(flat_graph, depth=1)

        # Find the edge to compute_retrieval_metrics
        edge_to_metrics = None
        for e in result["edges"]:
            if e["target"] == "compute_retrieval_metrics":
                edge_to_metrics = e
                break

        assert edge_to_metrics is not None, "Should have edge to compute_retrieval_metrics"

        # The source should be compute_recall (the internal producer)
        # NOT batch_recall (the container) and NOT extract_retrieval_query (wrong node)
        assert edge_to_metrics["source"] == "compute_recall", (
            f"Edge source should be 'compute_recall' but got '{edge_to_metrics['source']}'"
        )

    def test_edge_source_node_exists_in_rendered_nodes(self):
        """The edge source node must exist in the rendered nodes."""
        bound_graph = build_triple_nested_graph()
        flat_graph = bound_graph.to_flat_graph()
        result = render_graph(flat_graph, depth=1)

        node_ids = {n["id"] for n in result["nodes"]}

        for edge in result["edges"]:
            source = edge["source"]
            # Skip synthetic nodes (input_, data_)
            if source.startswith("input_") or source.startswith("data_"):
                continue
            assert source in node_ids, (
                f"Edge source '{source}' not found in nodes. "
                f"Edge: {edge['source']} -> {edge['target']}"
            )

    def test_precomputed_edges_depth1_correct_source(self):
        """Pre-computed edges for depth=1 should have correct source."""
        bound_graph = build_triple_nested_graph()
        flat_graph = bound_graph.to_flat_graph()
        result = render_graph(flat_graph, depth=1)

        # Find the edge state key for batch_recall:1 (expanded)
        edges_by_state = result["meta"]["edgesByState"]

        # Find the key that has batch_recall expanded
        depth1_key = None
        for key in edges_by_state:
            if "batch_recall:1" in key and "sep:0" in key:
                depth1_key = key
                break

        assert depth1_key is not None, f"Should have depth=1 edge state. Keys: {list(edges_by_state.keys())}"

        depth1_edges = edges_by_state[depth1_key]

        # Find edge to compute_retrieval_metrics
        edge_to_metrics = None
        for e in depth1_edges:
            if e["target"] == "compute_retrieval_metrics":
                edge_to_metrics = e
                break

        assert edge_to_metrics is not None, "Should have edge to compute_retrieval_metrics in depth=1 state"
        assert edge_to_metrics["source"] == "compute_recall", (
            f"Pre-computed edge source should be 'compute_recall' but got '{edge_to_metrics['source']}'"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
