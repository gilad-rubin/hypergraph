"""ML embeddings — DaftStateful protocol for model loading.

Demonstrates:
- DaftStateful protocol: model loaded once per Daft worker
- @daft.cls wrapping for stateful UDFs
- graph.bind() for injecting the model

Inspired by Daft's stateful UDF tutorial.
"""

from hypergraph import DaftRunner, Graph, node


class MockEmbedder:
    """Simulates a heavy ML model loaded once per worker.

    Implements DaftStateful protocol so DaftRunner uses @daft.cls.
    """

    __daft_stateful__ = True
    init_count = 0

    def __init__(self):
        MockEmbedder.init_count += 1
        # Simulate heavy model loading
        self._weights = [0.1, 0.2, 0.3, 0.4, 0.5]

    def embed(self, text: str) -> list[float]:
        """Create a mock embedding from text."""
        return [w * len(text) for w in self._weights]


@node(output_name="embedding")
def embed(text: str, embedder: MockEmbedder) -> list[float]:
    return embedder.embed(text)


@node(output_name="similarity")
def cosine_similarity(embedding: list[float]) -> float:
    """Mock similarity against a reference vector."""
    ref = [1.0, 2.0, 3.0, 4.0, 5.0]
    dot = sum(a * b for a, b in zip(embedding, ref, strict=True))
    norm_a = sum(x**2 for x in embedding) ** 0.5
    norm_b = sum(x**2 for x in ref) ** 0.5
    return round(dot / (norm_a * norm_b), 4) if norm_a and norm_b else 0.0


# Build graph and bind the model
graph = Graph([embed, cosine_similarity], name="embedding_pipeline")
graph = graph.bind(embedder=MockEmbedder())


def main():
    runner = DaftRunner()

    texts = ["hello world", "machine learning is great", "short", "a"]
    results = runner.map(graph, {"text": texts}, map_over="text")

    print("ML Embedding pipeline:")
    for text, r in zip(texts, results, strict=True):
        print(f"  '{text}' → similarity={r['similarity']}")

    print(f"\nMockEmbedder was initialized {MockEmbedder.init_count} time(s)")


if __name__ == "__main__":
    main()
