"""Public inspect-mode contract for issue #156."""

from __future__ import annotations

import dataclasses

import pytest

from hypergraph import AsyncRunner, Graph, RunResult, RunStatus, SyncRunner, node


def test_sync_inspect_renders_captured_outputs_and_changes_with_real_input() -> None:
    """The inspect seam must report behavior, not merely carry an option flag."""

    @node(output_name="prepared")
    def prepare(value: int) -> int:
        return value

    @node(output_name="answer")
    def calculate(prepared: int) -> str:
        return f"independent-answer:{prepared * 2}"

    graph = Graph([prepare, calculate], name="inspect-falsifier")
    runner = SyncRunner()

    first = runner.run(graph, {"value": 2}, inspect=True)
    changed = runner.run(graph, {"value": 9}, inspect=True)

    first_html = first.inspect()._repr_html_()
    changed_html = changed.inspect()._repr_html_()

    assert "independent-answer:4" in first_html
    assert "independent-answer:18" not in first_html
    assert "independent-answer:18" in changed_html
    assert "independent-answer:4" not in changed_html


def test_sync_inspect_option_is_boolean_and_named_graph_input_uses_values() -> None:
    @node(output_name="echoed")
    def echo(inspect: str) -> str:
        return inspect

    graph = Graph([echo], name="inspect-input-collision")

    result = SyncRunner().run(
        graph,
        values={"inspect": "graph-owned value"},
        inspect=True,
    )

    assert result["echoed"] == "graph-owned value"
    assert "graph-owned value" in result.inspect()._repr_html_()
    with pytest.raises(TypeError, match="inspect must be a bool"):
        SyncRunner().run(graph, values={"inspect": "value"}, inspect="yes")  # type: ignore[arg-type]


def test_result_without_capture_is_inspectable_but_does_not_invent_values() -> None:
    @node(output_name="secret_result")
    def calculate(secret_input: str) -> str:
        return f"derived:{secret_input}"

    result = SyncRunner().run(
        Graph([calculate], name="degraded-inspection"),
        {"secret_input": "do-not-invent"},
    )

    html = result.inspect()._repr_html_()

    assert "not captured; rerun with inspect=True" in html
    assert "do-not-invent" not in html
    assert "derived:do-not-invent" not in html


def test_inspection_data_does_not_change_result_compatibility_surfaces() -> None:
    @node(output_name="answer")
    def calculate(value: int) -> int:
        return value * 2

    graph = Graph([calculate], name="compatibility")
    captured = SyncRunner().run(graph, {"value": 4}, inspect=True)
    without_private_artifact = dataclasses.replace(captured, _inspection=None)
    positional = RunResult(
        {"answer": 8},
        RunStatus.COMPLETED,
        "stable-run-id",
        None,
        None,
        None,
        None,
        True,
        (),
        False,
        (),
    )

    assert captured == without_private_artifact
    assert captured.to_dict() == without_private_artifact.to_dict()
    assert "_inspection" not in captured.to_dict()
    assert dataclasses.fields(RunResult)[-1].name == "_inspection"
    assert positional["answer"] == 8


def test_sync_nested_inspection_uses_slash_paths_and_real_child_run_identity() -> None:
    @node(output_name="inner_answer")
    def inner_leaf(value: int) -> int:
        return value + 1

    inner = Graph([inner_leaf], name="inner")
    outer = Graph([inner.as_node(name="child")], name="outer")

    result = SyncRunner().run(outer, {"value": 3}, inspect=True)
    artifact = result.inspect().artifact
    by_name = {item.qualified_name: item for item in artifact.nodes}

    assert set(by_name) == {"child", "child/inner_leaf"}
    assert by_name["child/inner_leaf"].outputs == {"inner_answer": 4}
    assert by_name["child/inner_leaf"].run_id != artifact.run_id
    assert by_name["child"].run_id == artifact.run_id


def test_sync_nested_failure_is_recorded_once_at_the_qualified_leaf() -> None:
    class NestedInspectionError(Exception):
        pass

    @node(output_name="never")
    def fail_leaf(value: int) -> int:
        raise NestedInspectionError(f"nested:{value}")

    inner = Graph([fail_leaf], name="inner-failure")
    outer = Graph([inner.as_node(name="child")], name="outer-failure")

    result = SyncRunner().run(
        outer,
        {"value": 7},
        inspect=True,
        error_handling="continue",
    )
    artifact = result.inspect().artifact

    assert [failure.node_name for failure in artifact.failures] == ["child/fail_leaf"]
    assert artifact.failures[0].inputs == {"value": 7}
    assert [item.qualified_name for item in artifact.nodes] == [
        "child",
        "child/fail_leaf",
    ]
    assert [item.status for item in artifact.nodes] == ["failed", "failed"]
    assert artifact.nodes[1].failure is not None
    assert artifact.nodes[1].failure.node_name == "child/fail_leaf"


def test_inspected_node_does_not_capture_an_unrelated_runner() -> None:
    @node(output_name="side_answer")
    def side_leaf(value: int) -> int:
        return value * 10

    side_graph = Graph([side_leaf], name="side-graph")

    @node(output_name="answer")
    def call_side_runner(value: int) -> int:
        return SyncRunner().run(side_graph, {"value": value})["side_answer"]

    result = SyncRunner().run(
        Graph([call_side_runner], name="outer-graph"),
        {"value": 3},
        inspect=True,
    )

    assert [item.qualified_name for item in result.inspect().artifact.nodes] == ["call_side_runner"]


@pytest.mark.asyncio
async def test_async_inspect_changes_rendered_behavior_with_real_input() -> None:
    @node(output_name="prepared")
    async def prepare(value: int) -> int:
        return value

    @node(output_name="answer")
    async def calculate(prepared: int) -> str:
        return f"async-independent-answer:{prepared * 2}"

    graph = Graph([prepare, calculate], name="async-inspect-falsifier")
    runner = AsyncRunner()

    first = await runner.run(graph, {"value": 2}, inspect=True)
    changed = await runner.run(graph, {"value": 9}, inspect=True)

    first_html = first.inspect()._repr_html_()
    changed_html = changed.inspect()._repr_html_()
    assert "async-independent-answer:4" in first_html
    assert "async-independent-answer:18" not in first_html
    assert "async-independent-answer:18" in changed_html
    assert "async-independent-answer:4" not in changed_html
