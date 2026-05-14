"""Scenario sweep example with nested fan-out on top of DaftRunner.map()."""

from __future__ import annotations

from hypergraph import DaftRunner, Graph, node


@node(output_name="normalized_candidate")
def normalize_candidate(candidate: dict) -> dict:
    return {
        "name": candidate["name"],
        "accuracy": float(candidate["accuracy"]),
        "latency_ms": float(candidate["latency_ms"]),
        "cost": float(candidate["cost"]),
    }


@node(output_name="candidate_score")
def score_candidate(normalized_candidate: dict, weights: dict) -> float:
    return round(
        normalized_candidate["accuracy"] * weights["accuracy"]
        - normalized_candidate["latency_ms"] * weights["latency"]
        - normalized_candidate["cost"] * weights["cost"],
        3,
    )


@node(output_name="candidate_report")
def build_candidate_report(normalized_candidate: dict, candidate_score: float) -> dict:
    return {
        "name": normalized_candidate["name"],
        "score": candidate_score,
        "accuracy": normalized_candidate["accuracy"],
        "latency_ms": normalized_candidate["latency_ms"],
    }


@node(output_name="best_candidate")
def choose_best(candidate_reports: list[dict], scenario_id: str) -> dict:
    winner = max(candidate_reports, key=lambda item: item["score"])
    return {"scenario_id": scenario_id, **winner}


def build_scenario_sweep_graph() -> Graph:
    """Build a graph for one scenario that fans out over many candidates."""
    candidate_graph = Graph([normalize_candidate, score_candidate, build_candidate_report], name="candidate_graph")
    mapped_candidates = (
        candidate_graph.as_node(name="evaluate_candidates")
        .with_inputs(candidate="candidates")
        .with_outputs(candidate_report="candidate_reports")
        .map_over("candidates")
    )
    return Graph([mapped_candidates, choose_best], name="scenario_sweep")


def main() -> None:
    graph = build_scenario_sweep_graph()
    runner = DaftRunner()
    # `weights` and `candidates` are projected flat from the
    # `evaluate_candidates` GraphNode. `scenario_id` is consumed at the outer
    # scope by `choose_best` so it is flat too.
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

    print(results["best_candidate"])


if __name__ == "__main__":
    main()
