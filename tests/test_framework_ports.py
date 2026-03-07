from __future__ import annotations

from examples.framework_ports.agentic_rag import build_agentic_rag_graph
from examples.framework_ports.document_batch_pipeline import build_document_batch_graph
from examples.framework_ports.ml_model_selection import build_ml_model_selection_graph
from examples.framework_ports.support_inbox import build_support_inbox_graph
from hypergraph import AsyncRunner, SyncRunner


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


async def test_support_inbox_nested_interrupt_propagates_and_resumes():
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

    resumed = await runner.run(
        graph,
        {
            **values,
            paused.pause.response_key: "Hotfix deployed and refund requests are safe to retry.",
        },
    )

    assert resumed.paused is False
    assert "Hotfix deployed" in resumed["customer_message"]
    assert resumed["support_track"] == "technical_support"


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
