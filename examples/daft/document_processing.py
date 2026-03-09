"""Document-processing example inspired by Daft's tutorial-style workflows."""

from __future__ import annotations

from hypergraph import DaftRunner, Graph, node


@node(output_name="clean_document")
def normalize_document(document: str) -> str:
    return " ".join(document.strip().lower().split())


@node(output_name=("sentence_count", "word_count"))
def document_stats(clean_document: str) -> tuple[int, int]:
    sentence_count = sum(1 for sentence in clean_document.split(".") if sentence.strip())
    word_count = len(clean_document.replace(".", " ").split())
    return sentence_count, word_count


@node(output_name="document_label")
def label_document(clean_document: str, keywords: tuple[str, ...]) -> str:
    lowered = clean_document.lower()
    return "keyword_match" if any(keyword in lowered for keyword in keywords) else "background"


@node(output_name="document_report")
def build_document_report(clean_document: str, sentence_count: int, word_count: int, document_label: str) -> dict:
    return {
        "preview": clean_document[:60],
        "sentence_count": sentence_count,
        "word_count": word_count,
        "label": document_label,
    }


@node(output_name="corpus_summary")
def summarize_corpus(document_reports: list[dict], document_labels: list[str], sentence_counts: list[int]) -> dict:
    return {
        "documents_processed": len(document_reports),
        "keyword_matches": sum(1 for label in document_labels if label == "keyword_match"),
        "total_sentences": sum(sentence_counts),
    }


def build_document_processing_graph() -> Graph:
    """Build a nested document-processing graph with bound configuration."""
    single_document_graph = Graph(
        [normalize_document, document_stats, label_document, build_document_report],
        name="single_document",
    ).bind(keywords=("hypergraph", "daft", "workflow"))

    mapped_documents = (
        single_document_graph.as_node(name="process_documents")
        .with_inputs(document="documents")
        .with_outputs(
            document_report="document_reports",
            document_label="document_labels",
            sentence_count="sentence_counts",
        )
        .map_over("documents")
    )
    return Graph([mapped_documents, summarize_corpus], name="document_processing")


def main() -> None:
    graph = build_document_processing_graph()
    runner = DaftRunner()
    documents = [
        "Hypergraph composes graphs into larger workflows. Daft scales dataset fan-out.",
        "This background note talks about deployment plans and rollout windows.",
    ]

    result = runner.run(graph, {"documents": documents})
    print(result["corpus_summary"])
    print(result["document_reports"])


if __name__ == "__main__":
    main()
