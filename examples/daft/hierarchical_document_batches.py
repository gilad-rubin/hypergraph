"""Hierarchical DaftRunner example with nested GraphNode.map_over().

Inspired by Daft tutorial patterns that mix UDFs with batch processing, but
structured as a reusable single-item graph that is fanned out over document
chunks and then over multiple documents.
"""

from __future__ import annotations

from hypergraph import DaftRunner, Graph, node


@node(output_name="sentences")
def split_sentences(document: str) -> list[str]:
    parts = [part.strip() for part in document.replace("!", ".").replace("?", ".").split(".")]
    return [part for part in parts if part]


@node(output_name="cleaned")
def clean_sentence(text: str) -> str:
    return " ".join(text.lower().split())


@node(output_name="score")
def score_sentence(cleaned: str) -> int:
    keywords = ("incident", "urgent", "refund", "blocked")
    return sum(3 for word in keywords if word in cleaned) + len(cleaned.split())


@node(output_name="priority")
def classify_sentence(score: int) -> str:
    if score >= 10:
        return "high"
    if score >= 6:
        return "medium"
    return "low"


sentence_graph = Graph(
    [
        clean_sentence,
        score_sentence,
        classify_sentence,
    ],
    name="sentence_graph",
)

analyze_sentences = sentence_graph.as_node(name="analyze_sentences").with_inputs(text="sentences").map_over("sentences")


@node(output_name="report")
def summarize_document(sentences: list[str], score: list[int], priority: list[str]) -> dict[str, object]:
    return {
        "sentence_count": len(sentences),
        "max_score": max(score),
        "highest_priority": "high" if "high" in priority else "medium" if "medium" in priority else "low",
        "flagged_sentences": [sentence for sentence, label in zip(sentences, priority, strict=True) if label != "low"],
    }


workflow = Graph(
    [
        split_sentences,
        analyze_sentences,
        summarize_document,
    ],
    name="document_triage",
)


def main() -> None:
    runner = DaftRunner()

    single = runner.run(
        workflow,
        document="Customer asked for a refund. Their checkout is blocked. Please respond today.",
    )
    print("Single report:", single["report"])

    batch = runner.map(
        workflow,
        {
            "document": [
                "Customer asked for a refund. Their checkout is blocked. Please respond today.",
                "Weekly note. Share the roadmap update with the team.",
            ],
        },
        map_over="document",
    )
    print(batch.summary())
    for report in batch["report"]:
        print(report)


if __name__ == "__main__":
    main()
