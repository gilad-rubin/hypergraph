"""Visualization tests for complex nested graphs and batch patterns."""

from hypergraph import Graph, ifelse, node
from hypergraph.viz.renderer import render_graph


def _expanded_edges(result: dict, *, separate_outputs: bool = False) -> list[dict]:
    expandable = result["meta"]["expandableNodes"]
    exp_key = ",".join(f"{node_id}:1" for node_id in expandable)
    sep_key = "sep:1" if separate_outputs else "sep:0"
    key = f"{exp_key}|{sep_key}" if exp_key else sep_key
    return result["meta"]["edgesByState"][key]


# =============================================================================
# RAG-style nested graph (branch + nested + nested inside nested)
# =============================================================================


@node(output_name="validation")
def rag_validate_query(query: str) -> bool:
    return bool(query)


@ifelse(when_true="retrieval", when_false="rag_reject_query")
def rag_route_query(validation: bool) -> bool:
    return validation


@node(output_name="rejection")
def rag_reject_query(query: str) -> str:
    return f"reject:{query}"


@node(output_name="embedding")
def rag_embed_query(query: str) -> list[float]:
    return [float(len(query))]


@node(output_name="docs")
def rag_retrieve_docs(embedding: list[float]) -> list[str]:
    return [f"doc-{embedding[0]}"]


@node(output_name="top_documents")
def rag_limit_docs(docs: list[str]) -> list[str]:
    return docs[:1]


@node(output_name="selected_document")
def rag_select_document(top_documents: list[str]) -> str:
    return top_documents[0]


@node(output_name="system_prompt")
def rag_build_system_prompt() -> str:
    return "system"


@node(output_name="context")
def rag_build_context(selected_document: str) -> str:
    return f"context:{selected_document}"


@node(output_name="chat_messages")
def rag_build_prompt(system_prompt: str, context: str, query: str) -> list[str]:
    return [system_prompt, context, query]


@node(output_name="answer")
def rag_generate_answer(chat_messages: list[str]) -> str:
    return "|".join(chat_messages)


@node(output_name="response")
def rag_format_response(answer: str, query: str) -> dict:
    return {"answer": answer, "query": query}


def make_rag_style_graph() -> Graph:
    prompt_building = Graph(
        nodes=[rag_build_system_prompt, rag_build_context, rag_build_prompt],
        name="prompt_building",
    )
    generation = Graph(
        nodes=[prompt_building.as_node(), rag_generate_answer, rag_format_response],
        name="generation",
    )
    retrieval = Graph(
        nodes=[rag_embed_query, rag_retrieve_docs, rag_limit_docs],
        name="retrieval",
    )

    return Graph(
        nodes=[
            rag_validate_query,
            rag_route_query,
            rag_reject_query,
            retrieval.as_node(),
            rag_select_document,
            generation.as_node(),
        ],
        name="rag_style",
    )


def test_rag_style_query_edges_route_to_internal_nodes_when_expanded() -> None:
    graph = make_rag_style_graph()
    result = render_graph(graph.to_flat_graph(), depth=0)
    edges = _expanded_edges(result)

    query_targets = {
        edge["target"] for edge in edges if edge["source"] == "input_query"
    }

    assert "rag_validate_query" in query_targets
    assert "retrieval/rag_embed_query" in query_targets
    assert "generation/prompt_building/rag_build_prompt" in query_targets


def test_rag_style_retrieval_output_routes_from_internal_producer() -> None:
    graph = make_rag_style_graph()
    result = render_graph(graph.to_flat_graph(), depth=0)
    edges = _expanded_edges(result)

    sources = {
        edge["source"] for edge in edges if edge["target"] == "rag_select_document"
    }

    assert sources == {"retrieval/rag_limit_docs"}


def test_rag_style_control_edge_routes_to_entrypoint_when_expanded() -> None:
    graph = make_rag_style_graph()
    result = render_graph(graph.to_flat_graph(), depth=0)
    edges = _expanded_edges(result)

    control_edges = [
        edge for edge in edges
        if edge["source"] == "rag_route_query"
        and edge["data"].get("edgeType") == "control"
        and edge["data"].get("label") == "True"
    ]

    assert control_edges, "Expected a True control edge from rag_route_query"
    assert control_edges[0]["target"] == "retrieval/rag_embed_query"


# =============================================================================
# Batch graph (map_over with multiple inputs/outputs)
# =============================================================================


@node(output_name="recommendation")
def rec_build_recommendation(result: str, feedback: str) -> str:
    return f"{result}:{feedback}"


@node(output_name="chat_messages")
def rec_build_chat_messages(result: str, feedback: str) -> list[str]:
    return [f"{result}:{feedback}"]


@node(output_name="batch_prompt")
def rec_build_batch_prompt(per_query_recommendations: list[str]) -> str:
    return f"{per_query_recommendations}"


@node(output_name="final_recommendations")
def rec_consolidate(batch_prompt: str) -> str:
    return batch_prompt


def make_batch_recommendations_graph() -> Graph:
    single = Graph(
        nodes=[rec_build_recommendation, rec_build_chat_messages],
        name="single",
    )

    mapped = (
        single.as_node(name="map_recommendations")
        .with_inputs(result="results", feedback="feedbacks")
        .with_outputs(
            recommendation="per_query_recommendations",
            chat_messages="single_chat_messages",
        )
        .map_over("results")
    )

    return Graph(
        nodes=[mapped, rec_build_batch_prompt, rec_consolidate],
        name="batch_recommendations",
    )


def test_batch_recommendations_input_group_routes_to_container() -> None:
    graph = make_batch_recommendations_graph()
    result = render_graph(graph.to_flat_graph(), depth=0)
    edges = _expanded_edges(result)

    group_edges = [
        edge for edge in edges if edge["source"] == "input_group_feedbacks_results"
    ]

    assert group_edges, "Expected grouped input edges for results/feedbacks"
    targets = {edge["target"] for edge in group_edges}
    assert targets == {"map_recommendations"}


def test_batch_recommendations_outputs_route_from_internal_nodes() -> None:
    graph = make_batch_recommendations_graph()
    result = render_graph(graph.to_flat_graph(), depth=0)
    edges = _expanded_edges(result)

    sources = {
        edge["source"] for edge in edges if edge["target"] == "rec_build_batch_prompt"
    }

    # With hierarchical IDs, nodes inside map_recommendations get that prefix
    assert sources == {"map_recommendations/rec_build_recommendation"}


# =============================================================================
# Batch recall (map_over + nested retrieval)
# =============================================================================


@node(output_name="eval_pairs")
def batch_build_pairs(queries: list[str]) -> list[dict]:
    return [{"query": q} for q in queries]


@node(output_name="query")
def batch_extract_query(eval_pair: dict) -> str:
    return eval_pair["query"]


@node(output_name="embedding")
def batch_embed_query(query: str) -> list[float]:
    return [float(len(query))]


@node(output_name="docs")
def batch_retrieve_docs(embedding: list[float]) -> list[str]:
    return [f"doc-{embedding[0]}"]


@node(output_name="top_documents")
def batch_limit_docs(docs: list[str]) -> list[str]:
    return docs[:1]


@node(output_name="recall_score")
def batch_score_recall(top_documents: list[str]) -> float:
    return float(len(top_documents))


@node(output_name="metrics")
def batch_aggregate_scores(recall_scores: list[float]) -> dict:
    return {"count": len(recall_scores)}


def make_batch_recall_graph() -> Graph:
    retrieval = Graph(
        nodes=[batch_embed_query, batch_retrieve_docs, batch_limit_docs],
        name="retrieval",
    )
    recall = Graph(
        nodes=[batch_extract_query, retrieval.as_node(), batch_score_recall],
        name="recall",
    )

    mapped = (
        recall.as_node(name="batch_recall")
        .with_inputs(eval_pair="eval_pairs")
        .with_outputs(recall_score="recall_scores")
        .map_over("eval_pairs")
    )

    return Graph(
        nodes=[batch_build_pairs, mapped, batch_aggregate_scores],
        name="batch_recall_outer",
    )


def test_batch_recall_input_edges_route_from_external_builder() -> None:
    graph = make_batch_recall_graph()
    result = render_graph(graph.to_flat_graph(), depth=0)
    edges = _expanded_edges(result)

    # With hierarchical IDs, batch_extract_query is inside batch_recall
    sources = {
        edge["source"] for edge in edges if edge["target"] == "batch_recall/batch_extract_query"
    }

    assert sources == {"batch_build_pairs"}


def test_batch_recall_routes_through_nested_retrieval_when_expanded() -> None:
    graph = make_batch_recall_graph()
    result = render_graph(graph.to_flat_graph(), depth=0)
    edges = _expanded_edges(result)

    # With hierarchical IDs, nodes are inside batch_recall and batch_recall/retrieval
    assert any(
        edge["source"] == "batch_recall/batch_extract_query"
        and edge["target"] == "batch_recall/retrieval/batch_embed_query"
        for edge in edges
    ), "Expected query edge to route into nested retrieval when expanded"

    sources = {
        edge["source"] for edge in edges if edge["target"] == "batch_aggregate_scores"
    }

    assert sources == {"batch_recall/batch_score_recall"}
