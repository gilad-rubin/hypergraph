"""Advanced Daft UDF patterns expressed with Hypergraph.

Inspired by Daft's UDF patterns docs:
- plain Hypergraph nodes still auto-lower to daft.func
- daft_node(..., batch=True) lowers to daft.func.batch
- @stateful lowers bound worker resources to daft.cls
"""

from __future__ import annotations

import daft

from hypergraph import Graph, node
from hypergraph.integrations.daft import DaftRunner, stateful
from hypergraph.integrations.daft import node as daft_node


@node(output_name="clean_text")
def clean_text(text: str) -> str:
    return " ".join(text.lower().strip().split())


@daft_node(
    output_name="word_count",
    batch=True,
    return_dtype=daft.DataType.int64(),
    batch_size=2,
)
def count_words(clean_text: daft.Series) -> list[int]:
    return [len(value.split()) for value in clean_text.to_pylist()]


@stateful(max_concurrency=2)
class KeywordClassifier:
    def __init__(self) -> None:
        self._labels = {
            "urgent": ("blocked", "crash", "down"),
            "billing": ("refund", "invoice", "charge"),
        }

    def classify(self, text: str) -> str:
        for label, keywords in self._labels.items():
            if any(keyword in text for keyword in keywords):
                return label
        return "general"


@node(output_name="ticket_label")
def classify_ticket(clean_text: str, classifier: KeywordClassifier) -> str:
    return classifier.classify(clean_text)


graph = Graph([clean_text, count_words, classify_ticket], name="advanced_daft_udfs")
graph = graph.bind(classifier=KeywordClassifier())


def main() -> list[dict[str, object]]:
    frame = daft.from_pydict(
        {
            "ticket_id": [101, 102, 103],
            "text": [
                "Checkout is blocked after deploy",
                "Need invoice for annual charge",
                "Dark mode feedback for the dashboard",
            ],
        }
    )

    result = DaftRunner().map_dataframe(graph, frame).collect().to_pylist()
    for row in result:
        print(row)
    return result


if __name__ == "__main__":
    main()
