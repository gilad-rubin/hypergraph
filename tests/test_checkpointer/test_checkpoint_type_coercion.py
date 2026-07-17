"""Checkpoint restore reconstructs typed models from deserialized dicts.

JsonSerializer loses Pydantic/dataclass type info on round-trip: serialize
calls model_dump(), but deserialize returns plain dicts. These tests verify
that initialize_state_with_checkpoint reconstructs typed values using the
graph's output type annotations.
"""

from dataclasses import dataclass
from typing import ClassVar

import pytest
import pytest_asyncio
from pydantic import BaseModel

from hypergraph import Graph, interrupt, node
from hypergraph.checkpointers import SqliteCheckpointer
from hypergraph.runners import AsyncRunner

aiosqlite = pytest.importorskip("aiosqlite")


# -- Typed models --


class Score(BaseModel, frozen=True):
    value: float
    label: str


@dataclass(frozen=True)
class ScoreQuestion:
    answer_type: ClassVar[object] = Score
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple[object, ...] = ()


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


# -- Fixtures --


@pytest_asyncio.fixture
async def checkpointer(tmp_path):
    cp = SqliteCheckpointer(str(tmp_path / "test.db"))
    yield cp
    await cp.close()


# -- Tests --


class TestPydanticModelRestore:
    """Single Pydantic model survives checkpoint round-trip."""

    async def test_model_restored_after_checkpoint_resume(self, checkpointer):
        @node(output_name="label")
        def read_score(score: Score) -> str:
            assert isinstance(score, Score), f"Expected Score, got {type(score)}"
            return score.label

        graph = Graph(
            nodes=[compute_score, read_score],
            edges=[(compute_score, read_score)],
        )
        runner = AsyncRunner(checkpointer=checkpointer)
        wf_id = "pydantic-restore"

        result = await runner.run(graph, x=10, workflow_id=wf_id)
        assert result["label"] == "good"

        checkpoint = checkpointer.checkpoint(wf_id)
        assert checkpoint is not None
        score_val = checkpoint.values.get("score")
        assert isinstance(score_val, dict), "checkpoint stores Score as dict"

        graph2 = Graph(nodes=[read_score])
        result2 = await runner.run(graph2, checkpoint=checkpoint, workflow_id="fork-1")
        assert result2["label"] == "good"


class TestListModelRestore:
    """list[Model] outputs survive checkpoint round-trip."""

    async def test_list_of_models_restored(self, checkpointer):
        graph = Graph(
            nodes=[compute_score, compute_tags, summarize],
            edges=[
                (compute_score, compute_tags),
                (compute_score, summarize),
                (compute_tags, summarize),
            ],
        )
        runner = AsyncRunner(checkpointer=checkpointer)
        wf_id = "list-restore"

        result = await runner.run(graph, x=10, workflow_id=wf_id)
        assert "high" in result["summary"]

        checkpoint = checkpointer.checkpoint(wf_id)
        tags_val = checkpoint.values.get("tags")
        assert isinstance(tags_val, list)
        if tags_val:
            assert isinstance(tags_val[0], dict), "checkpoint stores Tag as dict"

        graph2 = Graph(nodes=[summarize])
        result2 = await runner.run(graph2, checkpoint=checkpoint, workflow_id="fork-list")
        assert "base" in result2["summary"]


class TestInterruptResumeCoercion:
    """Interrupt resume payloads are coerced to typed models."""

    async def test_interrupt_resume_dict_coerced(self, checkpointer):
        @interrupt(answer_name="decision")
        def wait_for_decision(score: Score) -> ScoreQuestion:
            return ScoreQuestion(prompt="Override this score?", evidence=(score,))

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
        runner = AsyncRunner(checkpointer=checkpointer)
        wf_id = "interrupt-coerce"

        paused = await runner.run(graph, x=3, workflow_id=wf_id)
        assert paused.paused

        # Provide decision as a plain dict to exercise runtime_values coercion
        resumed = await runner.run(
            graph,
            workflow_id=wf_id,
            decision={"value": 99.0, "label": "override"},
        )
        assert not resumed.paused
        assert resumed["final"] == "decided: override"


class TestOptionalModelRestore:
    """PEP 604 union (Model | None) survives checkpoint round-trip."""

    async def test_pep604_optional_coerced(self, checkpointer):
        @node(output_name="maybe_score")
        def maybe_compute(x: int) -> Score | None:
            if x > 0:
                return Score(value=x * 1.5, label="ok")
            return None

        @node(output_name="label")
        def read_maybe(maybe_score: Score | None) -> str:
            if maybe_score is None:
                return "none"
            assert isinstance(maybe_score, Score), f"Expected Score, got {type(maybe_score)}"
            return maybe_score.label

        graph = Graph(
            nodes=[maybe_compute, read_maybe],
            edges=[(maybe_compute, read_maybe)],
        )
        runner = AsyncRunner(checkpointer=checkpointer)
        wf_id = "pep604-restore"

        result = await runner.run(graph, x=10, workflow_id=wf_id)
        assert result["label"] == "ok"

        checkpoint = checkpointer.checkpoint(wf_id)
        score_val = checkpoint.values.get("maybe_score")
        assert isinstance(score_val, dict), "checkpoint stores Score as dict"

        graph2 = Graph(nodes=[read_maybe])
        result2 = await runner.run(graph2, checkpoint=checkpoint, workflow_id="fork-604")
        assert result2["label"] == "ok"


class TestDataclassRestore:
    """Dataclass outputs survive checkpoint round-trip."""

    async def test_dataclass_restored_after_checkpoint(self, checkpointer):
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
        runner = AsyncRunner(checkpointer=checkpointer)
        wf_id = "dataclass-restore"

        result = await runner.run(graph, x=95, workflow_id=wf_id)
        assert result["report"] == "accuracy=0.95"

        checkpoint = checkpointer.checkpoint(wf_id)
        metric_val = checkpoint.values.get("metric")
        assert isinstance(metric_val, dict), "checkpoint stores Metric as dict"

        graph2 = Graph(nodes=[format_metric])
        result2 = await runner.run(graph2, checkpoint=checkpoint, workflow_id="fork-dc")
        assert result2["report"] == "accuracy=0.95"


class TestTupleOutputRestore:
    """JSON round-trip turns tuples into lists; restore must coerce them back.

    Systemic companion to the nested crash-window restore tests: ordinary
    checkpoint resume flows through the same coerce_checkpoint_values.
    """

    async def test_ordinary_resume_restores_annotated_tuple_output(self, tmp_path):
        received: list[object] = []
        fail = [True]

        @node(output_name="pair")
        def make_pair(x: int) -> tuple[int, int]:
            return (x, x + 1)

        @node(output_name="total")
        def consume(pair: tuple[int, int]) -> int:
            if fail[0]:
                raise RuntimeError("boom on first run")
            received.append(pair)
            return pair[0] + pair[1]

        graph = Graph(nodes=[make_pair, consume], name="g")
        cp = SqliteCheckpointer(str(tmp_path / "test.db"), durability="sync")
        try:
            runner = AsyncRunner(checkpointer=cp)
            with pytest.raises(RuntimeError, match="boom on first run"):
                await runner.run(graph, {"x": 4}, workflow_id="wf")

            fail[0] = False
            result = await runner.run(graph, workflow_id="wf")

            assert result.values["total"] == 9
            assert received == [(4, 5)]
            assert isinstance(received[0], tuple)
            assert result.values["pair"] == (4, 5)
            assert isinstance(result.values["pair"], tuple)
        finally:
            await cp.close()
