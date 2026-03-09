"""Async API calls — DaftRunner with async nodes.

Demonstrates:
- Async nodes (Daft handles the event loop natively)
- Diamond DAG: embed → [score, classify] → combine
- Mock API simulating latency

Inspired by Daft's async UDF documentation.
"""

import asyncio

from hypergraph import DaftRunner, Graph, node


# Mock async API calls
async def _mock_embed(text: str) -> list[float]:
    """Simulate an embedding API call."""
    await asyncio.sleep(0.01)
    return [float(ord(c)) / 100 for c in text[:5]]


async def _mock_classify(text: str) -> str:
    """Simulate a classification API call."""
    await asyncio.sleep(0.01)
    return "positive" if len(text) > 10 else "neutral"


@node(output_name="embedding")
async def embed(text: str) -> list[float]:
    return await _mock_embed(text)


@node(output_name="score")
async def score(embedding: list[float]) -> float:
    return sum(embedding) / len(embedding) if embedding else 0.0


@node(output_name="category")
async def classify(text: str) -> str:
    return await _mock_classify(text)


@node(output_name="result")
def combine(text: str, score: float, category: str) -> dict:
    return {"text": text, "score": round(score, 3), "category": category}


graph = Graph([embed, score, classify, combine], name="async_pipeline")


def main():
    runner = DaftRunner()

    texts = [
        "This is a positive review about the product",
        "Short",
        "Another longer review that should be classified",
        "OK",
    ]

    results = runner.map(graph, {"text": texts}, map_over="text")
    print("Async API pipeline results:")
    for r in results:
        print(f"  {r['result']}")


if __name__ == "__main__":
    main()
