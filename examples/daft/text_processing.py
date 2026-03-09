"""Text processing pipeline — basic DaftRunner columnar execution.

Demonstrates:
- Linear DAG with sync nodes
- DaftRunner.map() for batch processing
- Columnar execution: each node becomes a Daft UDF

Inspired by Daft's quickstart UDF tutorial.
"""

from hypergraph import DaftRunner, Graph, node


@node(output_name="cleaned")
def clean_text(text: str) -> str:
    """Lowercase and strip whitespace."""
    return " ".join(text.lower().strip().split())


@node(output_name="word_count")
def count_words(cleaned: str) -> int:
    """Count words in cleaned text."""
    return len(cleaned.split())


@node(output_name="summary")
def summarize(cleaned: str, word_count: int) -> str:
    """Create a one-line summary."""
    preview = cleaned[:50] + "..." if len(cleaned) > 50 else cleaned
    return f"[{word_count} words] {preview}"


graph = Graph([clean_text, count_words, summarize], name="text_pipeline")


def main():
    runner = DaftRunner()

    # Single run
    result = runner.run(graph, text="  Hello   World!  This is a TEST.  ")
    print("Single run:")
    print(f"  cleaned: {result['cleaned']}")
    print(f"  word_count: {result['word_count']}")
    print(f"  summary: {result['summary']}")
    print()

    # Batch run
    texts = [
        "  Hello World!  ",
        "Daft handles UDFs natively, including async functions.",
        "SHORT",
        "  This is a longer sentence that demonstrates text cleaning and word counting. ",
    ]
    results = runner.map(graph, {"text": texts}, map_over="text")
    print("Batch run:")
    for i, r in enumerate(results):
        print(f"  [{i}] {r['summary']}")


if __name__ == "__main__":
    main()
