from __future__ import annotations

import pytest

from examples.framework_ports.agentic_rag import build_agentic_rag_graph
from examples.framework_ports.document_batch_pipeline import build_document_batch_graph
from examples.framework_ports.ml_model_selection import build_ml_model_selection_graph
from examples.framework_ports.support_inbox import build_support_inbox_graph
from hypergraph import AsyncRunner, DaftRunner, SyncRunner

try:
    import daft
except ImportError:  # pragma: no cover - optional dependency
    daft = None
    pytestmark = pytest.mark.skip(reason="daft not installed")
else:
    from examples.framework_ports.daft_workflows import (
        build_daft_image_query_graph,
        build_daft_llm_dataset_graph,
        build_daft_quickstart_graph,
    )


def test_agentic_rag_routes_latest_questions_to_web():
    graph = build_agentic_rag_graph()
    runner = SyncRunner()

    result = runner.run(
        graph,
        {
            "question": "What changed in the latest release for interrupts?",
            "local_documents": [
                "Hypergraph supports interrupt nodes for human review.",
                "Nested graphs make composition natural.",
            ],
            "web_documents": [
                "Latest release notes mention better interrupt resume semantics.",
                "Recent release adds richer run logs for nested graphs.",
            ],
        },
    )

    assert result["search_source"] == "search_web"
    assert result["confidence"] == "grounded"
    assert "latest release notes" in result["answer"].lower()


async def test_support_inbox_nested_interrupt_propagates_pause():
    graph = build_support_inbox_graph()
    runner = AsyncRunner()
    values = {
        "ticket_id": "T-100",
        "tickets_db": {
            "T-100": {
                "customer": "Acme",
                "kind": "technical",
                "priority": "critical",
                "issue": "refund API timeout after release",
            }
        },
        "knowledge_base": [
            "General account changes are applied within one hour.",
        ],
        "release_notes": [
            "Refund API timeout fixed in patch 2026.03.",
            "Release 2026.03 improves webhook retries.",
        ],
    }

    paused = await runner.run(graph, values)

    assert paused.paused is True
    assert paused.pause is not None
    assert paused.pause.node_name == "technical_support/request_developer_review"
    assert paused.pause.response_key == "technical_support.developer_reply"


def test_ml_model_selection_uses_mapped_trials_to_pick_best_model():
    graph = build_ml_model_selection_graph()
    runner = SyncRunner()

    result = runner.run(
        graph,
        {
            "dataset_name": "toy_flowers",
            "feature_names": ("length", "width"),
            "model_types": ["threshold", "centroid"],
        },
    )

    assert result["evaluated_model_type"] == ["threshold", "centroid"]
    assert len(result["accuracy"]) == 2
    assert result["best_model_summary"]["model_type"] in {"threshold", "centroid"}
    assert result["best_model_summary"]["accuracy"] >= 0.5


def test_document_batch_pipeline_maps_one_document_graph_over_many_inputs():
    graph = build_document_batch_graph()
    runner = SyncRunner()

    result = runner.run(
        graph,
        {
            "documents": [
                "Hypergraph composes graphs into larger workflows. It keeps nodes testable.",
                "Mapped graph nodes let one document pipeline scale across a batch. Automatic wiring keeps the graph readable.",
            ]
        },
    )

    assert len(result["document_summaries"]) == 2
    assert result["ingestion_report"]["documents_processed"] == 2
    assert result["ingestion_report"]["total_sentences"] == 4


def test_daft_quickstart_port_processes_dataframe_rows():
    graph = build_daft_quickstart_graph()
    runner = DaftRunner()
    frame = daft.from_pylist(
        [
            {"text": "  Alpha beta alpha  "},
            {"text": "Gamma delta epsilon zeta eta"},
        ]
    )

    result_df = runner.map_dataframe(graph, frame)
    results = result_df.collect().to_pydict()

    assert results["cleaned_text"] == ["alpha beta alpha", "gamma delta epsilon zeta eta"]
    assert results["review_bucket"] == ["short", "long"]


def test_daft_llm_dataset_port_uses_nested_chunk_graphs():
    graph = build_daft_llm_dataset_graph()
    runner = DaftRunner()
    frame = daft.from_pylist(
        [
            {
                "query": "alpha",
                "chunks": ["alpha alpha beta", "alpha beta gamma", "delta epsilon"],
            },
            {
                "query": "refund",
                "chunks": ["webhook retry policy", "refund api timeout fixed in patch 2026.03"],
            },
        ]
    )

    result_df = runner.map_dataframe(graph, frame)
    results = result_df.collect().to_pydict()

    assert results["chunk_score"][0] == [2, 1, 0]
    assert results["dataset_summary"][0]["matching_chunks"] == 2
    assert "refund api timeout" in results["top_chunk"][1]


def test_daft_image_query_port_maps_patch_classifier_inside_each_asset():
    graph = build_daft_image_query_graph()
    runner = DaftRunner()
    frame = daft.from_pylist(
        [
            {"patches": [{"r": 80, "g": 70, "b": 60}, {"r": 10, "g": 20, "b": 10}]},
            {"patches": [{"r": 20, "g": 30, "b": 20}, {"r": 25, "g": 25, "b": 20}]},
        ]
    )

    result_df = runner.map_dataframe(graph, frame, threshold=180)
    results = result_df.collect().to_pydict()

    assert results["patch_label"][0] == ["bright", "dark"]
    assert results["asset_summary"][0]["dominant_label"] == "bright"
    assert results["asset_summary"][1]["dominant_label"] == "dark"
