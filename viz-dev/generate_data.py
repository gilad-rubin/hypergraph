"""Generate example graph JSON files for the viz dev environment.

Run from project root: uv run python viz-dev/generate_data.py
Or via npm:            cd viz-dev && npm run generate
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hypergraph import Graph, node, route, END
from hypergraph.nodes.gate import ifelse
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


def make_complex_nested() -> tuple[str, str, Graph]:
    """3-layer nested graph with ifelse, route, and subgraphs.

    Structure (inspired by a real indexing/retrieval/generation pipeline):

    Level 1 (root): ingestion pipeline
      - check_input → ifelse → process_item (subgraph) or skip_item
      - process_item contains Level 2

    Level 2: process_item subgraph
      - parse_content → enrich → build_chunks (subgraph) → store
      - build_chunks contains Level 3

    Level 3: build_chunks subgraph
      - split_text → embed_chunk → format_chunk
    """

    # ── Level 3: build_chunks ──────────────────────────────

    @node(output_name="segments")
    def split_text(content: str, max_length: int = 512) -> list[str]:
        return [content[:512]]

    @node(output_name="vectors")
    def embed_chunk(segments: list[str], model_name: str = "default") -> list[list[float]]:
        return [[0.1, 0.2]]

    @node(output_name="chunks")
    def format_chunk(segments: list[str], vectors: list[list[float]], source_id: str) -> list[dict]:
        return [{"text": s, "vec": v} for s, v in zip(segments, vectors)]

    build_chunks = Graph([split_text, embed_chunk, format_chunk], name="build_chunks")

    # ── Level 2: process_item ──────────────────────────────

    @node(output_name="content")
    def parse_content(raw_data: bytes, file_type: str) -> str:
        return "parsed content"

    @node(output_name="metadata")
    def extract_metadata(content: str, source_id: str) -> dict:
        return {"source": source_id}

    @node(output_name="enriched_content")
    def enrich(content: str, metadata: dict) -> str:
        return content

    @node(output_name="store_result")
    def store(chunks: list[dict], metadata: dict, collection: str) -> str:
        return "stored"

    process_item = Graph(
        [parse_content, extract_metadata, enrich, build_chunks.as_node(), store],
        name="process_item",
    )

    # ── Level 1: root pipeline with ifelse ──────────────────

    @node(output_name="is_valid")
    def check_input(raw_data: bytes, file_type: str) -> bool:
        return True

    @ifelse(when_true="process_item", when_false="skip_item")
    def should_process(is_valid: bool) -> bool:
        return is_valid

    @node(output_name="store_result")
    def skip_item(raw_data: bytes) -> str:
        return "skipped"

    return "complex_nested", "Complex Nested (3 layers)", Graph(
        [check_input, should_process, process_item.as_node(), skip_item],
    )


def make_complex_nested_expanded() -> tuple[str, str, Graph]:
    """Same complex graph, fully expanded (depth=8)."""

    # Must re-declare everything (decorators are one-shot)

    @node(output_name="segments")
    def split_text(content: str, max_length: int = 512) -> list[str]:
        return [content[:512]]

    @node(output_name="vectors")
    def embed_chunk(segments: list[str], model_name: str = "default") -> list[list[float]]:
        return [[0.1, 0.2]]

    @node(output_name="chunks")
    def format_chunk(segments: list[str], vectors: list[list[float]], source_id: str) -> list[dict]:
        return [{"text": s, "vec": v} for s, v in zip(segments, vectors)]

    build_chunks = Graph([split_text, embed_chunk, format_chunk], name="build_chunks")

    @node(output_name="content")
    def parse_content(raw_data: bytes, file_type: str) -> str:
        return "parsed content"

    @node(output_name="metadata")
    def extract_metadata(content: str, source_id: str) -> dict:
        return {"source": source_id}

    @node(output_name="enriched_content")
    def enrich(content: str, metadata: dict) -> str:
        return content

    @node(output_name="store_result")
    def store(chunks: list[dict], metadata: dict, collection: str) -> str:
        return "stored"

    process_item = Graph(
        [parse_content, extract_metadata, enrich, build_chunks.as_node(), store],
        name="process_item",
    )

    @node(output_name="is_valid")
    def check_input(raw_data: bytes, file_type: str) -> bool:
        return True

    @ifelse(when_true="process_item", when_false="skip_item")
    def should_process(is_valid: bool) -> bool:
        return is_valid

    @node(output_name="store_result")
    def skip_item(raw_data: bytes) -> str:
        return "skipped"

    return "complex_expanded", "Complex Nested (Expanded)", Graph(
        [check_input, should_process, process_item.as_node(), skip_item],
    )


def make_full_pipeline() -> tuple[str, str, Graph]:
    """Full pipeline with route: validate → retrieve → generate.

    Has multiple subgraphs + a route node for multi-branch logic.
    """

    # ── validation subgraph ────────────────────────────────

    @node(output_name="validation_prompt")
    def build_validation_prompt(query: str) -> str:
        return f"Validate: {query}"

    @node(output_name="validation")
    def validate_query(validation_prompt: str) -> dict:
        return {"is_valid": True, "reason": "ok"}

    validation = Graph([build_validation_prompt, validate_query], name="validation")

    # ── retrieval subgraph ─────────────────────────────────

    @node(output_name="raw_results")
    def search_index(query: str, top_k: int = 10) -> list[dict]:
        return [{"id": "1", "score": 0.9}]

    @node(output_name="ranked_results")
    def rerank_results(raw_results: list[dict], query: str) -> list[dict]:
        return raw_results

    @node(output_name="top_documents")
    def limit_results(ranked_results: list[dict], max_results: int = 5) -> list[dict]:
        return ranked_results[:5]

    retrieval = Graph([search_index, rerank_results, limit_results], name="retrieval")

    # ── generation subgraph ────────────────────────────────

    @node(output_name="context_text")
    def build_context(top_documents: list[dict]) -> str:
        return "context"

    @node(output_name="messages")
    def build_messages(context_text: str, query: str, system_prompt: str = "") -> list[dict]:
        return [{"role": "user", "content": query}]

    @node(output_name="raw_answer")
    def generate_answer(messages: list[dict], model: str = "default") -> str:
        return "answer"

    @node(output_name="response")
    def format_response(raw_answer: str, query: str) -> dict:
        return {"answer": raw_answer, "query": query}

    generation = Graph(
        [build_context, build_messages, generate_answer, format_response],
        name="generation",
    )

    # ── root: validate → route → retrieve+generate or reject ──

    @node(output_name="is_valid")
    def check_validity(validation: dict) -> bool:
        return validation.get("is_valid", False)

    @ifelse(when_true="retrieval", when_false="reject_query")
    def is_query_valid(is_valid: bool) -> bool:
        return is_valid

    @node(output_name="response")
    def reject_query(validation: dict) -> dict:
        return {"answer": "Invalid query", "reason": validation.get("reason", "")}

    return "full_pipeline", "Full Pipeline (Validate + Retrieve + Generate)", Graph([
        validation.as_node(),
        check_validity,
        is_query_valid,
        retrieval.as_node(),
        generation.as_node(),
        reject_query,
    ])


def make_full_pipeline_expanded() -> tuple[str, str, Graph]:
    """Same full pipeline, all subgraphs expanded."""

    @node(output_name="validation_prompt")
    def build_validation_prompt(query: str) -> str:
        return f"Validate: {query}"

    @node(output_name="validation")
    def validate_query(validation_prompt: str) -> dict:
        return {"is_valid": True, "reason": "ok"}

    validation = Graph([build_validation_prompt, validate_query], name="validation")

    @node(output_name="raw_results")
    def search_index(query: str, top_k: int = 10) -> list[dict]:
        return [{"id": "1", "score": 0.9}]

    @node(output_name="ranked_results")
    def rerank_results(raw_results: list[dict], query: str) -> list[dict]:
        return raw_results

    @node(output_name="top_documents")
    def limit_results(ranked_results: list[dict], max_results: int = 5) -> list[dict]:
        return ranked_results[:5]

    retrieval = Graph([search_index, rerank_results, limit_results], name="retrieval")

    @node(output_name="context_text")
    def build_context(top_documents: list[dict]) -> str:
        return "context"

    @node(output_name="messages")
    def build_messages(context_text: str, query: str, system_prompt: str = "") -> list[dict]:
        return [{"role": "user", "content": query}]

    @node(output_name="raw_answer")
    def generate_answer(messages: list[dict], model: str = "default") -> str:
        return "answer"

    @node(output_name="response")
    def format_response(raw_answer: str, query: str) -> dict:
        return {"answer": raw_answer, "query": query}

    generation = Graph(
        [build_context, build_messages, generate_answer, format_response],
        name="generation",
    )

    @node(output_name="is_valid")
    def check_validity(validation: dict) -> bool:
        return validation.get("is_valid", False)

    @ifelse(when_true="retrieval", when_false="reject_query")
    def is_query_valid(is_valid: bool) -> bool:
        return is_valid

    @node(output_name="response")
    def reject_query(validation: dict) -> dict:
        return {"answer": "Invalid query", "reason": validation.get("reason", "")}

    return "full_pipeline_expanded", "Full Pipeline (Expanded)", Graph([
        validation.as_node(),
        check_validity,
        is_query_valid,
        retrieval.as_node(),
        generation.as_node(),
        reject_query,
    ])


# ── Main ────────────────────────────────────────────────────────


GRAPH_BUILDERS = [
    make_simple_pipeline,
    make_fan_in_out,
    make_branching,
    make_agent_loop,
    make_nested_graph,
    make_nested_expanded,
    make_typed,
    make_complex_nested,
    make_complex_nested_expanded,
    make_full_pipeline,
    make_full_pipeline_expanded,
]

# Render options per graph (overrides defaults)
RENDER_OVERRIDES = {
    "nested_expanded": {"depth": 1},
    "typed": {"show_types": True},
    "complex_expanded": {"depth": 8},
    "full_pipeline_expanded": {"depth": 8},
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
