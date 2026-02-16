"""Generate example graph JSON files for the viz dev environment.

Run from project root: uv run python viz-dev/generate_data.py
Or via npm:            cd viz-dev && npm run generate
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hypergraph import Graph, node, route, END
from hypergraph.viz.renderer import render_graph

OUTPUT_DIR = Path(__file__).parent / "public" / "data"


def _dump(graph_data: dict[str, Any], graph_id: str) -> None:
    """Write graph JSON to the output directory."""
    path = OUTPUT_DIR / f"{graph_id}.json"
    path.write_text(json.dumps(graph_data, indent=2))
    print(f"  {graph_id}.json ({len(graph_data['nodes'])} nodes, {len(graph_data['edges'])} edges)")


# ── Example graphs ──────────────────────────────────────────────


def make_simple_pipeline() -> tuple[str, str, Graph]:
    """3-node linear pipeline."""

    @node(output_name="cleaned")
    def preprocess(text: str) -> str:
        return text.strip()

    @node(output_name="tokens")
    def tokenize(cleaned: str) -> list[str]:
        return cleaned.split()

    @node(output_name="count")
    def count_tokens(tokens: list[str]) -> int:
        return len(tokens)

    return "simple_pipeline", "Simple Pipeline", Graph([preprocess, tokenize, count_tokens])


def make_fan_in_out() -> tuple[str, str, Graph]:
    """Fan-out then fan-in (RAG-like)."""

    @node(output_name="query_embedding")
    def embed_query(query: str) -> list[float]:
        return [0.1, 0.2]

    @node(output_name="results")
    def search(query_embedding: list[float], index: str) -> list[str]:
        return ["doc1", "doc2"]

    @node(output_name="context")
    def rerank(results: list[str], query: str) -> str:
        return "reranked"

    @node(output_name="answer")
    def generate(context: str, query: str) -> str:
        return "answer"

    return "fan_in_out", "Fan-in / Fan-out (RAG)", Graph([embed_query, search, rerank, generate])


def make_branching() -> tuple[str, str, Graph]:
    """Binary branching with route."""

    @node(output_name="score")
    def classify(text: str) -> float:
        return 0.8

    @node(output_name="result")
    def accept(score: float) -> str:
        return "accepted"

    @node(output_name="result")
    def reject(score: float) -> str:
        return "rejected"

    @route(targets=["accept", "reject"])
    def decide(score: float) -> str:
        return "accept" if score > 0.5 else "reject"

    return "branching", "Binary Branching", Graph([classify, decide, accept, reject])


def make_agent_loop() -> tuple[str, str, Graph]:
    """Cycle with emit/wait_for (agent pattern).

    emit/wait_for use a signal name ("turn_done") separate from data params.
    This creates an ordering edge (purple dashed) alongside the data edges.
    """

    @node(output_name="messages", emit="turn_done")
    def accumulate_query(messages: list[str], query: str) -> list[str]:
        return messages + [query]

    @node(output_name="messages", wait_for="turn_done")
    def accumulate_response(messages: list[str], response: str) -> list[str]:
        return messages + [response]

    @node(output_name="response")
    def call_llm(messages: list[str]) -> str:
        return "response"

    return "agent_loop", "Agent Loop (Cycle)", Graph([accumulate_query, accumulate_response, call_llm])


def make_nested_graph() -> tuple[str, str, Graph]:
    """Nested sub-graph (for depth=0 collapsed view)."""

    @node(output_name="cleaned")
    def clean(text: str) -> str:
        return text.strip()

    @node(output_name="tokens")
    def tokenize(cleaned: str) -> list[str]:
        return cleaned.split()

    inner = Graph([clean, tokenize], name="preprocess")

    @node(output_name="summary")
    def summarize(tokens: list[str]) -> str:
        return "summary"

    return "nested_collapsed", "Nested (Collapsed)", Graph([inner.as_node(), summarize])


def make_nested_expanded() -> tuple[str, str, Graph]:
    """Same nested graph but rendered expanded (depth=1)."""
    # Recreate since @node decorators mutate the function
    @node(output_name="cleaned")
    def clean(text: str) -> str:
        return text.strip()

    @node(output_name="tokens")
    def tokenize(cleaned: str) -> list[str]:
        return cleaned.split()

    inner = Graph([clean, tokenize], name="preprocess")

    @node(output_name="summary")
    def summarize(tokens: list[str]) -> str:
        return "summary"

    return "nested_expanded", "Nested (Expanded)", Graph([inner.as_node(), summarize])


def make_typed() -> tuple[str, str, Graph]:
    """Graph with type annotations visible."""

    @node(output_name="embedding")
    def embed(text: str, model: str = "default") -> list[float]:
        return [0.0]

    @node(output_name="similarity")
    def compare(embedding: list[float], reference: list[float]) -> float:
        return 0.95

    @node(output_name="label")
    def classify(similarity: float, threshold: float = 0.8) -> str:
        return "match"

    return "typed", "With Type Annotations", Graph([embed, compare, classify])


# ── Main ────────────────────────────────────────────────────────


GRAPH_BUILDERS = [
    make_simple_pipeline,
    make_fan_in_out,
    make_branching,
    make_agent_loop,
    make_nested_graph,
    make_nested_expanded,
    make_typed,
]

# Render options per graph (overrides defaults)
RENDER_OVERRIDES = {
    "nested_expanded": {"depth": 1},
    "typed": {"show_types": True},
}

# Also generate separate_outputs variants for these
SEPARATE_OUTPUTS_VARIANTS = ["simple_pipeline", "fan_in_out"]


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    manifest_entries = []
    print("Generating graph data...")

    for builder in GRAPH_BUILDERS:
        graph_id, name, graph = builder()
        flat = graph.to_flat_graph()

        overrides = RENDER_OVERRIDES.get(graph_id, {})
        data = render_graph(flat, theme="dark", **overrides)

        _dump(data, graph_id)
        manifest_entries.append({"id": graph_id, "name": name})

        # Generate separate_outputs variant if requested
        if graph_id in SEPARATE_OUTPUTS_VARIANTS:
            sep_id = f"{graph_id}_sep"
            sep_data = render_graph(flat, theme="dark", separate_outputs=True, **overrides)
            _dump(sep_data, sep_id)
            manifest_entries.append({"id": sep_id, "name": f"{name} (Separate Outputs)"})

    # Write manifest
    manifest = {"graphs": manifest_entries}
    manifest_path = OUTPUT_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest: {manifest_path} ({len(manifest_entries)} graphs)")


if __name__ == "__main__":
    main()
