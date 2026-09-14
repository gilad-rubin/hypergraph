"""Issue #408, guard 2 — a checkpoint value that cannot be rebuilt says so.

Restore re-mints typed models from the graph's annotations. When that
failed, `_coerce_single` returned the raw dict: the resumed run handed a
downstream node a `dict` where its annotation said a model, and the failure
surfaced as an `AttributeError` several nodes later with nothing pointing at
the restore.

What this file falsifies:

1. The issue's probe — a dict that cannot validate — raises instead of
   returning itself, and the message names the value, the target type, and
   the underlying cause.
2. A restore whose values all coerce raises nothing and changes nothing.
3. The silent-by-design paths stay silent: a value that is not a dict, and a
   name whose annotation is not a model, pass through untouched.
4. A dataclass whose stored dict cannot construct the model raises too,
   rather than half-building it.
5. A host can catch it: the error is a root export, beside the other
   restore-time error (`CompactedRetentionError`).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import BaseModel

import hypergraph
from hypergraph import CheckpointCoercionError, Graph, node
from hypergraph.checkpointers.types import Checkpoint
from hypergraph.runners._shared.state_restore import (
    _coerce_single,
    coerce_checkpoint_values,
    initialize_state,
)


class Score(BaseModel, frozen=True):
    value: float
    label: str


@dataclass(frozen=True)
class Metric:
    name: str
    value: float


@node(output_name="score")
def produce(x: int) -> Score:
    return Score(value=float(x), label="ok")


@node(output_name="verdict")
def consume(score: Score) -> str:
    return score.label


@node(output_name="metric")
def produce_metric(x: int) -> Metric:
    return Metric(name="latency", value=float(x))


def scored_graph() -> Graph:
    return Graph([produce, consume], name="scored")


class TestTheProbe:
    def test_a_dict_that_cannot_validate_raises_instead_of_returning_itself(self):
        with pytest.raises(CheckpointCoercionError) as excinfo:
            _coerce_single({"value": "nope"}, Score)

        message = str(excinfo.value)
        assert "Score" in message
        assert "How to fix:" in message
        assert isinstance(excinfo.value.__cause__, Exception), "the validation error is kept as the cause"

    def test_the_restore_level_names_the_value_it_could_not_rebuild(self):
        with pytest.raises(CheckpointCoercionError) as excinfo:
            coerce_checkpoint_values(scored_graph(), {"score": {"value": "nope", "label": 2}})

        error = excinfo.value
        assert error.name == "score"
        assert error.model is Score
        message = str(error)
        assert "'score'" in message and "Score" in message
        assert "value" in message, "the pydantic cause is quoted, not swallowed"

    def test_a_stale_checkpoint_fails_at_the_restore_not_five_nodes_later(self):
        """The host's real story: old rows, new model, a resumed run."""
        checkpoint = Checkpoint(values={"x": 1, "score": {"value": 0.5}}, steps=[])

        with pytest.raises(CheckpointCoercionError, match="score"):
            initialize_state(scored_graph(), {}, checkpoint=checkpoint)


class TestAHostCanCatchIt:
    def test_the_error_is_a_root_export_beside_the_other_restore_time_error(self):
        assert CheckpointCoercionError is hypergraph.exceptions.CheckpointCoercionError
        assert "CheckpointCoercionError" in hypergraph.__all__
        # The canon neighbour: the other restore-time refusal lives here too.
        assert "CompactedRetentionError" in hypergraph.__all__

    def test_the_caught_error_carries_the_value_the_model_and_the_cause(self):
        try:
            coerce_checkpoint_values(scored_graph(), {"score": {"value": "nope", "label": 2}})
        except CheckpointCoercionError as refused:
            assert (refused.name, refused.model) == ("score", Score)
            assert refused.__cause__ is refused.cause
        else:
            raise AssertionError("a value that cannot be rebuilt must refuse")


class TestWhatStaysQuiet:
    def test_a_restore_whose_values_all_coerce_rebuilds_them_silently(self, recwarn):
        restored = coerce_checkpoint_values(scored_graph(), {"x": 1, "score": {"value": 0.5, "label": "ok"}})

        assert restored["score"] == Score(value=0.5, label="ok")
        assert restored["x"] == 1
        assert list(recwarn) == []

    def test_values_with_nothing_to_rebuild_pass_through_untouched(self):
        already = Score(value=1.0, label="ok")
        restored = coerce_checkpoint_values(scored_graph(), {"score": already, "x": {"not": "annotated as a model"}})

        assert restored["score"] is already
        assert restored["x"] == {"not": "annotated as a model"}

    def test_a_value_that_is_not_a_dict_is_left_alone(self):
        assert _coerce_single("already a string", Score) == "already a string"
        assert _coerce_single([1, 2], Score) == [1, 2]


class TestDataclasses:
    def test_a_dataclass_dict_that_cannot_construct_raises(self):
        graph = Graph([produce_metric], name="metered")

        with pytest.raises(CheckpointCoercionError) as excinfo:
            coerce_checkpoint_values(graph, {"metric": {"name": "latency"}})  # `value` missing

        assert excinfo.value.name == "metric" and excinfo.value.model is Metric

    def test_a_dataclass_that_constructs_is_rebuilt(self):
        graph = Graph([produce_metric], name="metered")

        restored = coerce_checkpoint_values(graph, {"metric": {"name": "latency", "value": 0.25}})

        assert restored["metric"] == Metric(name="latency", value=0.25)
