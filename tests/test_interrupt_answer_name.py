"""Behavioral contract tests for v4 interrupt questions and answer slots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import pytest

from hypergraph import END, AsyncRunner, Graph, InterruptNode, RunStatus, interrupt, node, route
from hypergraph.cache import InMemoryCache
from hypergraph.checkpointers import MemoryCheckpointer
from hypergraph.graph.validation import GraphConfigError

MULTI_ANSWER_TYPE = tuple[str, ...]


@dataclass(frozen=True)
class MultiChoice:
    """Minimal test fake for the engine's structural ask seam."""

    answer_type: ClassVar[object] = MULTI_ANSWER_TYPE
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple[object, ...] = ()


@dataclass(frozen=True)
class Choice:
    """Minimal str-answering ask fake."""

    answer_type: ClassVar[object] = str
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple[object, ...] = ()


@dataclass(frozen=True)
class Confirm:
    """Minimal bool-answering ask fake used by the docs contract example."""

    answer_type: ClassVar[object] = bool
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple[object, ...] = ()


@dataclass(frozen=True)
class MissingOptionsQuestion:
    answer_type: ClassVar[object] = str
    prompt: str
    evidence: tuple[object, ...] = ()


def test_answer_name_is_the_single_typed_output_and_old_shapes_fail_loudly():
    def review(draft: str) -> MultiChoice:
        return MultiChoice(prompt=f"Review {draft}")

    review_node = InterruptNode(review, answer_name="decisions")

    assert review_node.outputs == ("decisions",)
    assert review_node.output_annotation == {
        "decisions": MULTI_ANSWER_TYPE,
    }
    assert review_node.get_output_type("decisions") is MULTI_ANSWER_TYPE

    with pytest.raises(TypeError, match="answer_name"):
        interrupt(output_name="decision")

    with pytest.raises(TypeError, match="answer_name"):
        interrupt(answer_name=("decision", "notes"))


@pytest.mark.parametrize("annotation", ["missing", "plain"])
def test_graph_rejects_interrupt_without_ask_return_annotation(annotation: str):
    if annotation == "missing":

        @interrupt(answer_name="decision")
        def review(draft: str):
            return Choice(prompt=f"Review {draft}")

    else:

        @interrupt(answer_name="decision")
        def review(draft: str) -> str:
            return "not an ask"

    with pytest.raises(GraphConfigError, match="answer_type"):
        Graph([review])


def test_strict_types_compares_the_answer_type_to_consumers():
    @interrupt(answer_name="decision")
    def review(draft: str) -> Choice:
        return Choice(prompt=f"Review {draft}")

    @node(output_name="published")
    def publish(decision: int) -> bool:
        return bool(decision)

    with pytest.raises(GraphConfigError, match="output 'decision' has type: <class 'str'>"):
        Graph([review, publish], strict_types=True)


@pytest.mark.asyncio
async def test_question_payload_pauses_and_only_the_answer_enters_dataflow():
    question = Choice(
        prompt="The upload looks duplicated. What should happen?",
        options=("replace_existing", "keep_both"),
        evidence=("upload-17",),
    )
    applied: list[str] = []

    @interrupt(answer_name="dup_decision")
    def review_duplicate(upload_path: str) -> Choice:
        return question

    @node(output_name="result")
    def apply(dup_decision: str) -> str:
        applied.append(dup_decision)
        return f"applied:{dup_decision}"

    graph = Graph([review_duplicate, apply])
    runner = AsyncRunner()

    assert graph.inputs.required == ("upload_path",)

    paused = await runner.run(graph, {"upload_path": "report.pdf"})

    assert paused.status == RunStatus.PAUSED
    assert paused.pause is not None
    assert paused.pause.value is question
    assert paused.pause.response_key == "dup_decision"
    assert applied == []
    assert all(value is not question for value in paused.values.values())

    resumed = await runner.run(
        graph,
        {"upload_path": "report.pdf", "dup_decision": "keep_both"},
    )

    assert resumed.status == RunStatus.COMPLETED
    assert resumed["result"] == "applied:keep_both"
    assert applied == ["keep_both"]


@pytest.mark.asyncio
async def test_dead_question_option_fails_before_pause_surfaces():
    @interrupt(answer_name="dup_decision")
    def review_duplicate(upload_path: str) -> Choice:
        return Choice(
            prompt=f"What should happen to {upload_path}?",
            options=("replace-existing", "archive-old"),
        )

    @route(targets=["replace_existing"], default_open=False)
    def choose_path(dup_decision: str) -> str:
        return dup_decision.replace("-", "_")

    @node(output_name="result")
    def replace_existing(dup_decision: str) -> str:
        return f"replaced:{dup_decision}"

    graph = Graph([review_duplicate, choose_path, replace_existing])

    with pytest.raises(RuntimeError, match="archive-old.*choose_path"):
        await AsyncRunner().run(graph, {"upload_path": "report.pdf"})


@pytest.mark.asyncio
async def test_dead_option_check_ignores_gate_outside_selected_scope():
    @interrupt(answer_name="decision")
    def review(draft: str) -> Choice:
        return Choice(prompt="Publish?", options=("publish", "revise"))

    @node(output_name="receipt")
    def record(decision: str) -> str:
        return f"recorded:{decision}"

    @route(targets=["archive"], default_open=False)
    def inactive_route(decision: str) -> str:
        return "archive"

    @node(output_name="archived")
    def archive(decision: str) -> str:
        return f"archived:{decision}"

    graph = Graph([review, record, inactive_route, archive]).select("receipt")

    result = await AsyncRunner().run(graph, {"draft": "v4"})

    assert result.status == RunStatus.PAUSED
    assert result.pause is not None
    assert result.pause.response_key == "decision"


@pytest.mark.asyncio
async def test_unsettled_sibling_gate_input_defers_option_check_to_routing():
    @node(output_name="policy")
    def prepare_policy(source: str) -> str:
        return f"policy:{source}"

    @interrupt(answer_name="decision")
    def ask(source: str) -> Choice:
        return Choice(prompt="What should happen?", options=("keep-both",))

    @route(targets=["keep_both"], default_open=False)
    def choose_path(decision: str, policy: str) -> str:
        assert policy == "policy:upload"
        return "missing_target"

    @node(output_name="kept")
    def keep_both(decision: str) -> str:
        return f"kept:{decision}"

    graph = Graph([prepare_policy, ask, choose_path, keep_both])
    runner = AsyncRunner()

    paused = await runner.run(graph, {"source": "upload"})

    assert paused.status == RunStatus.PAUSED
    assert paused.pause is not None
    assert paused.pause.response_key == "decision"

    with pytest.raises(ValueError, match="invalid target 'missing_target'"):
        await runner.run(
            graph,
            {"source": "upload", "decision": "keep-both"},
        )


@pytest.mark.asyncio
async def test_answer_routes_to_distinct_terminals_and_upfront_answer_skips_question():
    handler_calls = 0

    @interrupt(answer_name="dup_decision")
    def review_duplicate(upload_path: str) -> Choice:
        nonlocal handler_calls
        handler_calls += 1
        return Choice(
            prompt=f"What should happen to {upload_path}?",
            options=("replace-existing", "keep-both"),
        )

    @route(targets=["replace_existing", "keep_both"], default_open=False)
    def choose_path(dup_decision: str) -> str:
        return dup_decision.replace("-", "_")

    @node(output_name="replaced")
    def replace_existing(dup_decision: str) -> str:
        return f"replaced:{dup_decision}"

    @node(output_name="kept")
    def keep_both(dup_decision: str) -> str:
        return f"kept:{dup_decision}"

    graph = Graph([review_duplicate, choose_path, replace_existing, keep_both])
    runner = AsyncRunner()

    paused = await runner.run(graph, {"upload_path": "report.pdf"})
    assert paused.status == RunStatus.PAUSED
    assert handler_calls == 1

    replaced = await runner.run(
        graph,
        {"upload_path": "report.pdf", "dup_decision": "replace-existing"},
    )
    kept = await runner.run(
        graph,
        {"upload_path": "report.pdf", "dup_decision": "keep-both"},
    )

    assert replaced["replaced"] == "replaced:replace-existing"
    assert "kept" not in replaced.values
    assert kept["kept"] == "kept:keep-both"
    assert "replaced" not in kept.values
    assert handler_calls == 1


@pytest.mark.asyncio
async def test_interrupt_answers_are_never_replayed_from_node_cache():
    handler_calls = 0

    @interrupt(answer_name="decision", cache=True)
    def review(draft: str) -> Choice:
        nonlocal handler_calls
        handler_calls += 1
        return Choice(prompt="Publish?", evidence=(draft,))

    graph = Graph([review])
    runner = AsyncRunner(cache=InMemoryCache())

    supplied = await runner.run(graph, {"draft": "v4", "decision": "yes"})
    unanswered = await runner.run(graph, {"draft": "v4"})

    assert supplied.status == RunStatus.COMPLETED
    assert handler_calls == 1
    assert unanswered.status == RunStatus.PAUSED
    assert unanswered.pause is not None
    assert unanswered.pause.response_key == "decision"


@pytest.mark.asyncio
async def test_docs_review_confirm_publish_example_is_runnable():
    published: list[str] = []

    @interrupt(answer_name="decision")
    def review(draft: str) -> Confirm:
        return Confirm(prompt=f"Publish this draft?", evidence=(draft,))  # noqa: F541

    @node(output_name="result")
    def publish(draft: str, decision: bool) -> str:
        assert decision is True
        published.append(draft)
        return f"published:{draft}"

    graph = Graph([review, publish])
    runner = AsyncRunner()
    d = "Hypergraph v4"

    result = await runner.run(graph, {"draft": d})
    assert result.pause is not None
    assert result.pause.value == Confirm(
        prompt="Publish this draft?",
        evidence=(d,),
    )
    assert result.pause.response_key == "decision"

    resumed = await runner.run(graph, {"draft": d, "decision": True})
    assert resumed["result"] == "published:Hypergraph v4"
    assert published == [d]


@pytest.mark.asyncio
async def test_checkpointed_resume_needs_only_workflow_id_and_answer():
    @node(output_name="draft")
    def draft(query: str) -> str:
        return f"draft:{query}"

    @interrupt(answer_name="decision")
    def review(draft: str) -> Confirm:
        return Confirm(prompt="Publish?", evidence=(draft,))

    @node(output_name="result")
    def publish(draft: str, decision: bool) -> str:
        return f"published:{draft}:{decision}"

    graph = Graph([draft, review, publish])
    runner = AsyncRunner(checkpointer=MemoryCheckpointer())

    paused = await runner.run(
        graph,
        {"query": "v4"},
        workflow_id="docs-review",
    )
    assert paused.status == RunStatus.PAUSED
    assert paused.pause is not None

    resumed = await runner.run(
        graph,
        {paused.pause.response_key: True},
        workflow_id="docs-review",
    )

    assert resumed.status == RunStatus.COMPLETED
    assert resumed["result"] == "published:draft:v4:True"


@pytest.mark.asyncio
async def test_cycle_interrupt_resumes_from_explicit_entrypoint():
    @interrupt(answer_name="query")
    def ask_user(messages: list[str]) -> Choice:
        return Choice(prompt=f"Next question after {len(messages)} replies?")

    @node(output_name="response")
    def answer(query: str) -> str:
        return f"response:{query}"

    @node(output_name="messages")
    def remember(messages: list[str], response: str) -> list[str]:
        return [*messages, response]

    @route(targets=["ask_user", END])
    def continue_chat(messages: list[str]) -> str:
        return END if messages else "ask_user"

    graph = Graph(
        [ask_user, answer, remember, continue_chat],
        entrypoint="ask_user",
    )
    runner = AsyncRunner()

    assert graph.inputs.required == ("messages",)
    paused = await runner.run(graph, {"messages": []})
    assert paused.status == RunStatus.PAUSED
    assert paused.pause is not None
    assert paused.pause.response_key == "query"

    resumed = await runner.run(
        graph,
        {"messages": [], paused.pause.response_key: "What is RAG?"},
    )

    assert resumed.status == RunStatus.COMPLETED
    assert resumed["messages"] == ["response:What is RAG?"]


@pytest.mark.asyncio
async def test_question_payload_must_expose_every_runtime_seam_field():
    @interrupt(answer_name="decision")
    def review(draft: str) -> MissingOptionsQuestion:
        return MissingOptionsQuestion(prompt="Approve?", evidence=(draft,))

    with pytest.raises(RuntimeError, match="options must be"):
        await AsyncRunner().run(Graph([review]), {"draft": "v4"})
