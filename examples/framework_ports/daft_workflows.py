"""Hypergraph ports of Daft-style dataset workflows.

These examples mirror the shape of common Daft examples:
- quickstart-style row processing over a DataFrame
- dataset-scale LLM scoring over nested document chunks
- image/asset-style nested patch analysis
"""

from __future__ import annotations

from collections import Counter

import daft

from hypergraph import DaftRunner, Graph, node


@node(output_name="cleaned_text")
def clean_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


@node(output_name="token_count")
def count_tokens(cleaned_text: str) -> int:
    return len(cleaned_text.split())


@node(output_name="review_bucket")
def bucket_text(token_count: int) -> str:
    return "long" if token_count >= 5 else "short"


def build_daft_quickstart_graph() -> Graph:
    return Graph([clean_text, count_tokens, bucket_text], name="daft_quickstart_port")


def demo_quickstart() -> list[dict[str, object]]:
    frame = daft.from_pylist(
        [
            {"text": "  Alpha beta alpha  "},
            {"text": "Gamma delta epsilon zeta eta"},
        ]
    )
    runner = DaftRunner()
    graph = build_daft_quickstart_graph()
    results = runner.map_dataframe(graph, frame)
    return [result.values for result in results]


@node(output_name="chunk_score")
def score_chunk(chunk: str, query: str) -> int:
    return chunk.lower().split().count(query.lower())


@node(output_name="top_chunk")
def select_top_chunk(chunks: list[str], chunk_score: list[int]) -> str:
    best_index = max(range(len(chunk_score)), key=lambda index: (chunk_score[index], -index))
    return chunks[best_index]


@node(output_name="dataset_summary")
def summarize_bundle(top_chunk: str, chunk_score: list[int]) -> dict[str, object]:
    return {
        "top_chunk": top_chunk,
        "matching_chunks": sum(1 for score in chunk_score if score > 0),
        "max_score": max(chunk_score) if chunk_score else 0,
    }


def build_daft_llm_dataset_graph() -> Graph:
    chunk_graph = Graph([score_chunk], name="chunk_ranker")
    return Graph(
        [
            chunk_graph.as_node(name="score_chunks").with_inputs(chunk="chunks").map_over("chunks"),
            select_top_chunk,
            summarize_bundle,
        ],
        name="daft_llm_dataset_port",
    )


def demo_llm_dataset() -> list[dict[str, object]]:
    frame = daft.from_pylist(
        [
            {
                "query": "alpha",
                "chunks": [
                    "alpha alpha beta",
                    "alpha beta gamma",
                    "delta epsilon",
                ],
            },
            {
                "query": "refund",
                "chunks": [
                    "webhook retry policy",
                    "refund api timeout fixed in patch 2026.03",
                ],
            },
        ]
    )
    runner = DaftRunner()
    graph = build_daft_llm_dataset_graph()
    results = runner.map_dataframe(graph, frame)
    return [result.values for result in results]


@node(output_name="patch_brightness")
def compute_patch_brightness(patch: dict[str, int]) -> int:
    return patch["r"] + patch["g"] + patch["b"]


@node(output_name="patch_label")
def label_patch(patch_brightness: int, threshold: int) -> str:
    return "bright" if patch_brightness >= threshold else "dark"


@node(output_name="asset_summary")
def summarize_asset(patch_label: list[str], patch_brightness: list[int]) -> dict[str, object]:
    counts = Counter(patch_label)
    return {
        "labels": dict(counts),
        "brightest_patch": max(patch_brightness) if patch_brightness else 0,
        "dominant_label": max(counts, key=counts.get) if counts else "unknown",
    }


def build_daft_image_query_graph() -> Graph:
    patch_graph = Graph([compute_patch_brightness, label_patch], name="patch_classifier")
    return Graph(
        [
            patch_graph.as_node(name="classify_patches").with_inputs(patch="patches").map_over("patches"),
            summarize_asset,
        ],
        name="daft_image_query_port",
    )


def demo_image_query() -> list[dict[str, object]]:
    frame = daft.from_pylist(
        [
            {
                "patches": [
                    {"r": 80, "g": 70, "b": 60},
                    {"r": 10, "g": 20, "b": 10},
                ]
            },
            {
                "patches": [
                    {"r": 20, "g": 30, "b": 20},
                    {"r": 25, "g": 25, "b": 20},
                ]
            },
        ]
    )
    runner = DaftRunner()
    graph = build_daft_image_query_graph()
    results = runner.map_dataframe(graph, frame, threshold=180)
    return [result.values for result in results]


if __name__ == "__main__":
    print("Quickstart:", demo_quickstart())
    print("LLM dataset:", demo_llm_dataset())
    print("Image query:", demo_image_query())
