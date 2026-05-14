from __future__ import annotations

import pytest

pytest.importorskip("daft")

from examples.daft.document_processing import build_document_processing_graph
from examples.daft.quickstart_orders import build_quickstart_orders_graph
from examples.daft.scenario_sweeps import build_scenario_sweep_graph
from hypergraph import DaftRunner


def test_quickstart_orders_example():
    graph = build_quickstart_orders_graph()
    runner = DaftRunner()

    result = runner.run(
        graph,
        {
            "orders": [
                {"order_id": "A-1", "customer": " ada ", "country": "us", "unit_price": 25, "quantity": 2},
                {"order_id": "B-2", "customer": "grace", "country": "uk", "unit_price": 80, "quantity": 2},
                {"order_id": "C-3", "customer": "linus", "country": "de", "unit_price": 10, "quantity": 1, "rush": True},
            ]
        },
    )

    assert result["fulfillment_summary"] == {
        "orders_processed": 3,
        "priority_orders": 1,
        "manual_review_orders": 1,
        "gross_revenue": 220.0,
    }


def test_document_processing_example():
    graph = build_document_processing_graph()
    runner = DaftRunner()

    result = runner.run(
        graph,
        {
            "documents": [
                "Hypergraph composes graphs into larger workflows. Daft scales dataset fan-out.",
                "This background note talks about deployment plans and rollout windows.",
            ]
        },
    )

    assert result["corpus_summary"] == {
        "documents_processed": 2,
        "keyword_matches": 1,
        "total_sentences": 3,
    }
    assert result["document_labels"] == ["keyword_match", "background"]


def test_scenario_sweep_example():
    graph = build_scenario_sweep_graph()
    runner = DaftRunner()

    # `weights` and `candidates` are projected flat from the
    # `evaluate_candidates` GraphNode; `scenario_id` is consumed at outer.
    results = runner.map(
        graph,
        {
            "scenario_id": ["latency_sensitive", "quality_first"],
            "weights": [
                {"accuracy": 1.0, "latency": 0.01, "cost": 0.5},
                {"accuracy": 2.5, "latency": 0.002, "cost": 0.2},
            ],
            "candidates": [
                [
                    {"name": "fast-small", "accuracy": 0.81, "latency_ms": 40, "cost": 0.01},
                    {"name": "balanced", "accuracy": 0.9, "latency_ms": 85, "cost": 0.03},
                ],
                [
                    {"name": "balanced", "accuracy": 0.9, "latency_ms": 85, "cost": 0.03},
                    {"name": "accurate-large", "accuracy": 0.96, "latency_ms": 140, "cost": 0.06},
                ],
            ],
        },
        map_over=["scenario_id", "weights", "candidates"],
    )

    assert results["best_candidate"] == [
        {"scenario_id": "latency_sensitive", "name": "fast-small", "score": 0.405, "accuracy": 0.81, "latency_ms": 40.0},
        {"scenario_id": "quality_first", "name": "accurate-large", "score": 2.108, "accuracy": 0.96, "latency_ms": 140.0},
    ]


def test_daft_map_rejects_stale_flat_graphnode_input_address():
    graph = build_scenario_sweep_graph()
    runner = DaftRunner()

    with pytest.raises(ValueError, match=r"'evaluate_candidates\.weights' is no longer valid\. Use 'weights'"):
        runner.map(
            graph,
            {
                "scenario_id": ["latency_sensitive"],
                "evaluate_candidates.weights": [{"accuracy": 1.0, "latency": 0.01, "cost": 0.5}],
                "evaluate_candidates.candidates": [[{"name": "fast-small", "accuracy": 0.81, "latency_ms": 40, "cost": 0.01}]],
            },
            map_over=["scenario_id", "evaluate_candidates.weights", "evaluate_candidates.candidates"],
        )
