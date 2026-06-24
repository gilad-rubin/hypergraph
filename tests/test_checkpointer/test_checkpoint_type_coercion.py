"""Checkpoint restore reconstructs typed models from deserialized dicts.

JsonSerializer loses Pydantic/dataclass type info on round-trip: serialize
calls model_dump(), but deserialize returns plain dicts. These tests verify
that initialize_state_with_checkpoint reconstructs typed values using the
graph's output type annotations.
"""

import asyncio
from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from hypergraph import Graph, interrupt, node
from hypergraph.checkpointers import SqliteCheckpointer
from hypergraph.runners import AsyncRunner

aiosqlite = pytest.importorskip("aiosqlite")


# -- Typed models --


class Score(BaseModel, frozen=True):
    value: float
    label: str


class Tag(BaseModel, frozen=True):
    name: str
    weight: float = 1.0


@dataclass(frozen=True)
class Metric:
    name: str
    value: float


# -- Graph nodes --


@node(output_name="score")
def compute_score(x: int) -> Score:
    return Score(value=x * 1.5, label="good" if x > 5 else "low")


@node(output_name="tags")
def compute_tags(score: Score) -> list[Tag]:
    tags = [Tag(name="base")]
    if score.value > 5:
        tags.append(Tag(name="high", weight=2.0))
    return tags


@node(output_name="summary")
def summarize(score: Score, tags: list[Tag]) -> str:
    tag_names = [t.name for t in tags]
    return f"{score.label}: {', '.join(tag_names)}"


# -- Tests --


class TestPydanticModelRestore:
    """Single Pydantic model survives checkpoint round-trip."""

    def test_model_restored_after_checkpoint_resume(self, tmp_path):
        @node(output_name="label")
        def read_score(score: Score) -> str:
            assert isinstance(score, Score), f"Expected Score, got {type(score)}"
            return score.label

        graph = Graph(
            nodes=[compute_score, read_score],
            edges=[(compute_score, read_score)],
        )
        cp = SqliteCheckpointer(str(tmp_path / "test.db"))
        runner = AsyncRunner(checkpointer=cp)
        wf_id = "pydantic-restore"

        result = asyncio.run(runner.run(graph, x=10, workflow_id=wf_id))
        assert result["label"] == "good"

        checkpoint = cp.checkpoint(wf_id)
        assert checkpoint is not None
        score_val = checkpoint.values.get("score")
        assert isinstance(score_val, dict), "checkpoint stores Score as dict"

        # Fork to prove the restored score is a real Score, not a dict
        graph2 = Graph(nodes=[read_score])
        result2 = asyncio.run(runner.run(graph2, checkpoint=checkpoint, workflow_id="fork-1"))
        assert result2["label"] == "good"


class TestListModelRestore:
    """list[Model] outputs survive checkpoint round-trip."""

    def test_list_of_models_restored(self, tmp_path):
        graph = Graph(
            nodes=[compute_score, compute_tags, summarize],
            edges=[
                (compute_score, compute_tags),
                (compute_score, summarize),
                (compute_tags, summarize),
            ],
        )
        cp = SqliteCheckpointer(str(tmp_path / "test.db"))
        runner = AsyncRunner(checkpointer=cp)
        wf_id = "list-restore"

        result = asyncio.run(runner.run(graph, x=10, workflow_id=wf_id))
        assert "high" in result["summary"]

        checkpoint = cp.checkpoint(wf_id)
        tags_val = checkpoint.values.get("tags")
        # After serialization, tags should be a list of dicts
        assert isinstance(tags_val, list)
        if tags_val:
            assert isinstance(tags_val[0], dict), "checkpoint stores Tag as dict"

        # Fork and run summarize — it needs tags as list[Tag] not list[dict]
        graph2 = Graph(nodes=[summarize])
        result2 = asyncio.run(runner.run(graph2, checkpoint=checkpoint, workflow_id="fork-list"))
        assert "base" in result2["summary"]


class TestInterruptResumeCoercion:
    """Interrupt resume payloads are coerced to typed models."""

    def test_interrupt_resume_value_coerced(self, tmp_path):
        @interrupt(output_name="decision")
        def wait_for_decision(score: Score) -> Score | None:
            if score.value > 100:
                return score
            return None

        @node(output_name="final")
        def use_decision(decision: Score) -> str:
            assert isinstance(decision, Score), f"Expected Score, got {type(decision)}"
            return f"decided: {decision.label}"

        graph = Graph(
            nodes=[compute_score, wait_for_decision, use_decision],
            edges=[
                (compute_score, wait_for_decision),
                (wait_for_decision, use_decision),
            ],
        )
        cp = SqliteCheckpointer(str(tmp_path / "test.db"))
        runner = AsyncRunner(checkpointer=cp)
        wf_id = "interrupt-coerce"

        paused = asyncio.run(runner.run(graph, x=3, workflow_id=wf_id))
        assert paused.paused

        resumed = asyncio.run(
            runner.run(
                graph,
                workflow_id=wf_id,
                decision=Score(value=99.0, label="override"),
            )
        )
        assert not resumed.paused
        assert resumed["final"] == "decided: override"


class TestDataclassRestore:
    """Dataclass outputs survive checkpoint round-trip."""

    def test_dataclass_restored_after_checkpoint(self, tmp_path):
        @node(output_name="metric")
        def compute_metric(x: int) -> Metric:
            return Metric(name="accuracy", value=x / 100)

        @node(output_name="report")
        def format_metric(metric: Metric) -> str:
            assert isinstance(metric, Metric), f"Expected Metric, got {type(metric)}"
            return f"{metric.name}={metric.value}"

        graph = Graph(
            nodes=[compute_metric, format_metric],
            edges=[(compute_metric, format_metric)],
        )
        cp = SqliteCheckpointer(str(tmp_path / "test.db"))
        runner = AsyncRunner(checkpointer=cp)
        wf_id = "dataclass-restore"

        result = asyncio.run(runner.run(graph, x=95, workflow_id=wf_id))
        assert result["report"] == "accuracy=0.95"

        checkpoint = cp.checkpoint(wf_id)
        metric_val = checkpoint.values.get("metric")
        assert isinstance(metric_val, dict), "checkpoint stores Metric as dict"

        # Fork — format_metric needs a real Metric, not a dict
        graph2 = Graph(nodes=[format_metric])
        result2 = asyncio.run(runner.run(graph2, checkpoint=checkpoint, workflow_id="fork-dc"))
        assert result2["report"] == "accuracy=0.95"
