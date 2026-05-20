"""Hypergraph port of batch document processing examples.

This example sits between DBOS's document pipelines, Restate's RAG ingestion,
and pipefunc's mapped examples. The focus is the Hypergraph-native shape:
write one document pipeline, then fan it out cleanly over a batch.
"""

from __future__ import annotations

from hypergraph import Graph, SyncRunner, node


@node(output_name="normalized_document")
def normalize_document(document: str) -> str:
    return " ".join(document.replace("\n", " ").split()).strip().lower()


@node(output_name="sentences")
def split_sentences(normalized_document: str) -> list[str]:
    return [sentence.strip() for sentence in normalized_document.split(".") if sentence.strip()]


@node(output_name="keywords")
def extract_keywords(sentences: list[str], top_k: int = 3) -> list[str]:
    counts: dict[str, int] = {}
    for sentence in sentences:
        for word in sentence.split():
            counts[word] = counts.get(word, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [word for word, _ in ranked[:top_k]]


@node(output_name="summary")
def summarize_document(sentences: list[str], keywords: list[str]) -> str:
    if not sentences:
        return ""
    important = [sentence for sentence in sentences if any(keyword in sentence for keyword in keywords)]
    selected = important[:2] or sentences[:1]
    return ". ".join(selected)


@node(output_name="doc_stats")
def document_stats(sentences: list[str], keywords: list[str]) -> dict:
    return {"sentence_count": len(sentences), "keywords": keywords}


document_graph = Graph(
    [
        normalize_document,
        split_sentences,
        extract_keywords,
        summarize_document,
        document_stats,
    ],
    name="document_pipeline",
)


@node(output_name="ingestion_report")
def build_ingestion_report(doc_stats: list[dict]) -> dict:
    keyword_pool = sorted({keyword for stats in doc_stats for keyword in stats["keywords"]})
    return {
        "documents_processed": len(doc_stats),
        "total_sentences": sum(stats["sentence_count"] for stats in doc_stats),
        "unique_keywords": keyword_pool,
    }


def build_document_batch_graph() -> Graph:
    mapped_documents = (
        document_graph.as_node(name="process_document")
        .rename_inputs(document="documents")
        .rename_outputs(summary="document_summaries")
        .map_over("documents")
    )
    return Graph(
        [
            mapped_documents,
            build_ingestion_report,
        ],
        name="document_batch_port",
    )


def demo() -> dict[str, object]:
    graph = build_document_batch_graph()
    runner = SyncRunner()
    return runner.run(
        graph,
        {
            "documents": [
                "Hypergraph composes graphs into larger workflows. It keeps nodes testable.",
                "Mapped graph nodes let one document pipeline scale across a batch. Automatic wiring keeps the graph readable.",
            ]
        },
    ).values


if __name__ == "__main__":
    print(demo())
