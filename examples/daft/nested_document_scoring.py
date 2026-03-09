"""Nested document scoring — GraphNode with map_over in DaftRunner.

Demonstrates:
- Nested GraphNode with map_over (inner list processing)
- DaftRunner.map() for outer batch (multiple documents)
- Composition: write once, scale twice (inner + outer)

This is the core Hypergraph pattern:
1. Write logic for ONE sentence
2. Compose into a graph for ONE document
3. Scale over many documents with DaftRunner.map()
"""

from hypergraph import DaftRunner, Graph, node


# Inner pipeline: process ONE sentence
@node(output_name="cleaned")
def clean_sentence(text: str) -> str:
    return " ".join(text.lower().strip().split())


@node(output_name="score")
def score_sentence(cleaned: str) -> int:
    return len(cleaned.split())


sentence_graph = Graph([clean_sentence, score_sentence], name="sentence_scorer")


# Outer pipeline: process ONE document
@node(output_name="sentences")
def split_document(document: str) -> list[str]:
    return [s.strip() for s in document.split(".") if s.strip()]


document_graph = Graph(
    [
        split_document,
        sentence_graph.as_node(name="analyze").with_inputs(text="sentences").map_over("sentences"),
    ],
    name="document_pipeline",
)


def main():
    runner = DaftRunner()

    # Single document
    result = runner.run(
        document_graph,
        document="Refund requested. Checkout blocked. Please help.",
    )
    print("Single document:")
    print(f"  sentences: {result['sentences']}")
    print(f"  scores: {result['score']}")
    print()

    # Batch of documents
    documents = [
        "Refund requested. Checkout blocked.",
        "Weekly roadmap update. Sprint planning next Tuesday.",
        "Great product. Will buy again. Recommended to friends.",
    ]
    results = runner.map(
        document_graph,
        {"document": documents},
        map_over="document",
    )
    print("Batch of documents:")
    for doc, r in zip(documents, results, strict=True):
        print(f"  '{doc[:40]}...' → scores={r['score']}")


if __name__ == "__main__":
    main()
