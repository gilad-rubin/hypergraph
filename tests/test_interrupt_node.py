"""Core InterruptNode tests for the question/answer-name contract."""

from __future__ import annotations

import inspect

import pytest

from hypergraph import (
    AsyncRunner,
    Graph,
    InterruptNode,
    PauseInfo,
    RunStatus,
    SyncRunner,
    interrupt,
    node,
)
from hypergraph.exceptions import IncompatibleRunnerError
from hypergraph.nodes.function import FunctionNode
from tests._interrupt_questions import BoolQuestion, StringQuestion


def _review(draft: str) -> StringQuestion:
    return StringQuestion(prompt="Approve?", evidence=(draft,))


class TestInterruptNodeConstruction:
    def test_constructor_declares_single_answer_port(self):
        review = InterruptNode(_review, answer_name="decision")

        assert review.name == "_review"
        assert review.inputs == ("draft",)
        assert review.outputs == ("decision",)
        assert review.data_outputs == ("decision",)
        assert review.answer_name == "decision"
        assert review.get_output_type("decision") is str
        assert review.get_output_type("missing") is None
        assert review.is_interrupt is True
        assert isinstance(review, FunctionNode)

    def test_answer_name_is_required_and_must_be_a_string(self):
        assert inspect.signature(interrupt).parameters["answer_name"].default is inspect.Parameter.empty
        assert inspect.signature(InterruptNode).parameters["answer_name"].default is inspect.Parameter.empty
        with pytest.raises(TypeError, match="answer_name"):
            InterruptNode(_review)
        with pytest.raises(TypeError, match="answer_name"):
            InterruptNode(_review, output_name="decision")
        with pytest.raises(TypeError, match="answer_name"):
            InterruptNode(_review, answer_name=("decision", "notes"))

    def test_constructor_configuration_matches_function_nodes(self):
        review = InterruptNode(
            _review,
            name="approval",
            answer_name="decision",
            cache=True,
            hide=True,
            emit="reviewed",
            wait_for="draft_ready",
        )

        assert review.name == "approval"
        assert review.cache is True
        assert review.hide is True
        assert review.outputs == ("decision", "reviewed")
        assert review.wait_for == ("draft_ready",)
        assert "approval" in repr(review)

    def test_decorator_preserves_direct_handler_testing(self):
        @interrupt(answer_name="decision")
        def review(draft: str) -> StringQuestion:
            return StringQuestion(prompt="Approve?", evidence=(draft,))

        question = review("draft")

        assert question == StringQuestion(prompt="Approve?", evidence=("draft",))
        assert review.__wrapped__("other") == StringQuestion(
            prompt="Approve?",
            evidence=("other",),
        )

    def test_renames_keep_answer_type_and_current_answer_name(self):
        review = InterruptNode(_review, answer_name="decision")

        renamed = review.rename_inputs(draft="document").rename_outputs(decision="verdict")

        assert review.inputs == ("draft",)
        assert review.outputs == ("decision",)
        assert renamed.inputs == ("document",)
        assert renamed.outputs == ("verdict",)
        assert renamed.answer_name == "verdict"
        assert renamed.get_input_type("document") is str
        assert renamed.get_output_type("verdict") is str
        assert renamed.map_inputs_to_params({"document": "draft"}) == {"draft": "draft"}

    def test_multiple_question_inputs_still_produce_one_answer(self):
        def review(draft: str, author: str) -> BoolQuestion:
            return BoolQuestion(prompt="Publish?", evidence=(draft, author))

        review_node = InterruptNode(review, answer_name="approved")

        assert review_node.inputs == ("draft", "author")
        assert review_node.data_outputs == ("approved",)
        assert review_node.get_output_type("approved") is bool

    def test_function_mode_metadata_is_preserved(self):
        async def async_review(draft: str) -> StringQuestion:
            return StringQuestion(prompt=draft)

        def generator_review(draft: str):
            yield StringQuestion(prompt=draft)

        assert InterruptNode(_review, answer_name="decision").is_async is False
        assert InterruptNode(async_review, answer_name="decision").is_async is True
        assert InterruptNode(generator_review, answer_name="decision").is_generator is True

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"emit": "decision"}, "emit names overlap"),
            ({"wait_for": "draft"}, "wait_for names overlap"),
            (
                {"emit": "signal", "wait_for": "signal"},
                "emit and wait_for share",
            ),
        ],
    )
    def test_emit_and_wait_for_validation_is_unchanged(self, kwargs, message):
        with pytest.raises(ValueError, match=message):
            InterruptNode(_review, answer_name="decision", **kwargs)


class TestPauseInfo:
    def test_pause_envelope_has_only_the_single_answer_slot(self):
        question = StringQuestion(prompt="Approve?")
        pause = PauseInfo(
            node_name="review",
            value=question,
            response_key="decision",
        )

        assert pause.node_name == "review"
        assert pause.value is question
        assert pause.response_key == "decision"
        assert not hasattr(pause, "output_param")
        assert not hasattr(pause, "output_params")
        assert not hasattr(pause, "values")
        assert not hasattr(pause, "response_keys")


class TestGraphAndRunnerContract:
    def test_graph_detects_interrupts(self):
        review = InterruptNode(_review, answer_name="decision")
        graph = Graph([review])

        assert graph.has_interrupts is True
        assert graph.interrupt_nodes == [review]

    def test_sync_runner_still_rejects_interrupts(self):
        graph = Graph([InterruptNode(_review, answer_name="decision")])

        with pytest.raises(IncompatibleRunnerError, match="InterruptNode"):
            SyncRunner().run(graph, {"draft": "hello"})

    @pytest.mark.asyncio
    async def test_async_handler_return_is_awaited_then_paused(self):
        question = StringQuestion(prompt="Approve?")

        @interrupt(answer_name="decision")
        async def review(draft: str) -> StringQuestion:
            return question

        result = await AsyncRunner().run(Graph([review]), {"draft": "hello"})

        assert result.status == RunStatus.PAUSED
        assert result.pause is not None
        assert result.pause.value is question

    @pytest.mark.asyncio
    async def test_none_question_is_a_loud_handler_bug(self):
        @interrupt(answer_name="decision")
        def review(draft: str) -> StringQuestion:
            return None  # type: ignore[return-value]

        with pytest.raises(RuntimeError, match="returned None.*question payload"):
            await AsyncRunner().run(Graph([review]), {"draft": "hello"})

    @pytest.mark.asyncio
    async def test_handler_failure_keeps_interrupt_context(self):
        @interrupt(answer_name="decision")
        def review(draft: str) -> StringQuestion:
            raise ValueError("handler broke")

        with pytest.raises(
            RuntimeError,
            match="Handler for InterruptNode 'review' failed: ValueError: handler broke",
        ):
            await AsyncRunner().run(Graph([review]), {"draft": "hello"})

    @pytest.mark.asyncio
    async def test_nested_pause_projects_response_key_and_preserves_question(self):
        question = StringQuestion(prompt="Approve?")

        @interrupt(answer_name="decision")
        def review(draft: str) -> StringQuestion:
            return question

        @node(output_name="result")
        def publish(verdict: str) -> str:
            return f"published:{verdict}"

        inner = Graph([review], name="inner")
        outer = Graph([inner.as_node().rename_outputs(decision="verdict"), publish])
        runner = AsyncRunner()

        paused = await runner.run(outer, {"draft": "hello"})

        assert paused.pause is not None
        assert paused.pause.node_name == "inner/review"
        assert paused.pause.value is question
        assert paused.pause.response_key == "verdict"

        resumed = await runner.run(
            outer,
            {"draft": "hello", paused.pause.response_key: "yes"},
        )
        assert resumed["result"] == "published:yes"
