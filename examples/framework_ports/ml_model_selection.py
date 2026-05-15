"""Hypergraph port of modular ML pipeline examples.

This example borrows its shape from Hamilton's modular dataflows and
scikit-learn's preprocessing/training pipelines. The implementation stays
dependency-light so it is runnable in the core Hypergraph repo.
"""

from __future__ import annotations

from statistics import mean

from hypergraph import Graph, SyncRunner, node

TOY_FLOWERS = [
    {"length": 1.0, "width": 0.8, "label": "compact"},
    {"length": 1.1, "width": 0.9, "label": "compact"},
    {"length": 0.9, "width": 0.7, "label": "compact"},
    {"length": 4.2, "width": 3.9, "label": "tall"},
    {"length": 4.0, "width": 4.1, "label": "tall"},
    {"length": 4.3, "width": 4.0, "label": "tall"},
]


@node(output_name="rows")
def load_rows(dataset_name: str) -> list[dict]:
    if dataset_name != "toy_flowers":
        raise ValueError(f"Unsupported dataset: {dataset_name}")
    return list(TOY_FLOWERS)


@node(output_name=("train_rows", "test_rows"))
def split_rows(rows: list[dict], holdout_size: int = 2) -> tuple[list[dict], list[dict]]:
    return rows[:-holdout_size], rows[-holdout_size:]


@node(output_name="evaluated_model_type")
def record_model_type(model_type: str) -> str:
    return model_type


@node(output_name="trained_model")
def fit_model(model_type: str, train_rows: list[dict], feature_names: tuple[str, ...]) -> dict:
    if model_type == "threshold":
        compact_mean = mean(row[feature_names[0]] for row in train_rows if row["label"] == "compact")
        tall_mean = mean(row[feature_names[0]] for row in train_rows if row["label"] == "tall")
        threshold = (compact_mean + tall_mean) / 2
        return {"model_type": "threshold", "feature": feature_names[0], "threshold": threshold}

    if model_type == "centroid":
        centroids = {}
        for label in {row["label"] for row in train_rows}:
            label_rows = [row for row in train_rows if row["label"] == label]
            centroids[label] = [mean(row[feature] for row in label_rows) for feature in feature_names]
        return {"model_type": "centroid", "feature_names": feature_names, "centroids": centroids}

    raise ValueError(f"Unsupported model type: {model_type}")


def _predict_row(model: dict, row: dict) -> str:
    if model["model_type"] == "threshold":
        return "compact" if row[model["feature"]] < model["threshold"] else "tall"

    assert model["model_type"] == "centroid"
    distances = {}
    for label, centroid in model["centroids"].items():
        distances[label] = sum((row[feature] - centroid[index]) ** 2 for index, feature in enumerate(model["feature_names"]))
    return min(distances, key=distances.get)


@node(output_name="predictions")
def predict_rows(trained_model: dict, test_rows: list[dict]) -> list[str]:
    return [_predict_row(trained_model, row) for row in test_rows]


@node(output_name="accuracy")
def accuracy(predictions: list[str], test_rows: list[dict]) -> float:
    correct = sum(prediction == row["label"] for prediction, row in zip(predictions, test_rows, strict=True))
    return correct / len(test_rows)


trial_graph = Graph(
    [
        record_model_type,
        fit_model,
        predict_rows,
        accuracy,
    ],
    name="model_trial",
)


@node(output_name="best_model_summary")
def select_best_model(evaluated_model_type: list[str], accuracy: list[float], trained_model: list[dict]) -> dict:
    best_index = max(range(len(accuracy)), key=lambda index: (accuracy[index], evaluated_model_type[index]))
    return {
        "model_type": evaluated_model_type[best_index],
        "accuracy": accuracy[best_index],
        "model": trained_model[best_index],
        "all_scores": dict(zip(evaluated_model_type, accuracy, strict=True)),
    }


def build_ml_model_selection_graph() -> Graph:
    mapped_trials = trial_graph.as_node(name="trial").rename_inputs(model_type="model_types").map_over("model_types")
    return Graph(
        [
            load_rows,
            split_rows,
            mapped_trials,
            select_best_model,
        ],
        name="ml_model_selection_port",
    )


def demo() -> dict[str, object]:
    graph = build_ml_model_selection_graph()
    runner = SyncRunner()
    return runner.run(
        graph,
        {
            "dataset_name": "toy_flowers",
            "feature_names": ("length", "width"),
            "model_types": ["threshold", "centroid"],
        },
    ).values


if __name__ == "__main__":
    print(demo())
