"""Hypergraph port of adaptive / agentic RAG patterns.

Inspired primarily by LangGraph's adaptive RAG examples, but rewritten to lean
into Hypergraph's automatic wiring and nested-graph composition.
"""

from __future__ import annotations

from collections import Counter

from hypergraph import Graph, SyncRunner, node, route


def _keywords(text: str) -> set[str]:
    return {token.strip(".,?!:;()").lower() for token in text.split() if token.strip(".,?!:;()")}


def _match_documents(question: str, documents: list[str]) -> list[str]:
    query_terms = _keywords(question)
    scored: list[tuple[int, str]] = []
    for document in documents:
        overlap = len(query_terms & _keywords(document))
        if overlap:
            scored.append((overlap, document))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [document for _, document in scored]


@node(output_name="search_source")
def classify_search_source(question: str) -> str:
    lowered = question.lower()
    if any(term in lowered for term in ("latest", "today", "current", "release", "recent")):
        return "search_web"
    return "search_local"


@route(targets=["search_local", "search_web"])
def dispatch_search(search_source: str) -> str:
    return search_source


@node(output_name="raw_context")
def search_local(question: str, local_documents: list[str]) -> list[str]:
    matches = _match_documents(question, local_documents)
    return matches[:3] or local_documents[:1]


@node(output_name="raw_context")
def search_web(question: str, web_documents: list[str]) -> list[str]:
    matches = _match_documents(question, web_documents)
    return matches[:3] or web_documents[:1]


@node(output_name="ranked_context")
def rank_context(question: str, raw_context: list[str]) -> list[str]:
    query_terms = _keywords(question)
    ranked = sorted(
        raw_context,
        key=lambda document: (
            -sum(Counter(_keywords(document))[term] for term in query_terms),
            document,
        ),
    )
    return ranked[:2]


retrieval_graph = Graph(
    [
        classify_search_source,
        dispatch_search,
        search_local,
        search_web,
        rank_context,
    ],
    name="retrieval",
)


@node(output_name="answer")
def draft_answer(question: str, ranked_context: list[str], search_source: str) -> str:
    lead = ranked_context[0] if ranked_context else "No context found."
    return f"[{search_source}] {question} -> {lead}"


@node(output_name="confidence")
def confidence_label(ranked_context: list[str]) -> str:
    return "grounded" if len(ranked_context) >= 2 else "thin"


def build_agentic_rag_graph() -> Graph:
    return Graph(
        [
            retrieval_graph.as_node(name="retrieve_context"),
            draft_answer,
            confidence_label,
        ],
        name="agentic_rag_port",
    )


def demo() -> dict[str, object]:
    graph = build_agentic_rag_graph()
    runner = SyncRunner()
    return runner.run(
        graph,
        {
            "question": "What changed in the latest hypergraph release for interrupts?",
            "local_documents": [
                "Hypergraph supports interrupt nodes for human review.",
                "Nested graphs use automatic wiring and composable outputs.",
            ],
            "web_documents": [
                "Latest release notes mention better interrupt resume semantics.",
                "Recent release adds improved run logs for nested graphs.",
            ],
        },
    ).values


if __name__ == "__main__":
    print(demo())
