"""Public inspect-mode contract for issue #156."""

from __future__ import annotations

import dataclasses

import pytest

from hypergraph import Graph, RunResult, RunStatus, SyncRunner, node


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
