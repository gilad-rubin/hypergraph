"""Public inspect-mode contract for issue #156."""

from __future__ import annotations

import dataclasses

import pytest

from hypergraph import AsyncRunner, Graph, RunResult, RunStatus, SyncRunner, interrupt, node
from hypergraph.checkpointers import MemoryCheckpointer, SqliteCheckpointer


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


@pytest.mark.asyncio
async def test_async_inspection_marks_the_interrupt_boundary_paused() -> None:
    @node(output_name="draft")
    async def prepare(value: int) -> str:
        return f"draft:{value}"

    @interrupt(output_name="decision")
    def review(draft: str) -> str | None:
        return None

    result = await AsyncRunner().run(
        Graph([prepare, review], name="pause-inspection"),
        {"value": 3},
        inspect=True,
    )
    artifact = result.inspect().artifact
    by_name = {item.qualified_name: item for item in artifact.nodes}

    assert artifact.status == "paused"
    assert by_name["prepare"].status == "completed"
    assert by_name["prepare"].values_captured is True
    assert by_name["review"].status == "paused"
    assert by_name["review"].inputs == {"draft": "draft:3"}
    assert by_name["review"].outputs is None
    assert by_name["review"].values_captured is True
    assert all(item.status != "running" for item in artifact.nodes)


@pytest.mark.asyncio
async def test_async_nested_interrupt_marks_container_and_leaf_paused() -> None:
    @interrupt(output_name="decision")
    def review(value: int) -> str | None:
        return None

    inner = Graph([review], name="inner-pause")
    outer = Graph([inner.as_node(name="child")], name="outer-pause")

    result = await AsyncRunner().run(outer, {"value": 7}, inspect=True)
    artifact = result.inspect().artifact
    by_name = {item.qualified_name: item for item in artifact.nodes}

    assert artifact.status == "paused"
    assert set(by_name) == {"child", "child/review"}
    assert by_name["child"].status == "paused"
    assert by_name["child/review"].status == "paused"
    assert by_name["child/review"].inputs == {"value": 7}
    assert all(item.status != "running" for item in artifact.nodes)


@pytest.mark.asyncio
async def test_async_resume_inspection_separates_restored_metadata_from_fresh_values() -> None:
    restored_only_sentinel = "RESTORED-ONLY-SENTINEL"

    @node(output_name="draft")
    async def prepare(value: int) -> str:
        return f"draft:{value}"

    @node(output_name="checkpoint_secret")
    async def remember_secret(value: int) -> str:
        return restored_only_sentinel

    @interrupt(output_name="decision")
    def review(draft: str) -> str | None:
        return None

    @node(output_name="final")
    async def finalize(decision: str) -> str:
        return f"final:{decision}"

    graph = Graph([prepare, remember_secret, review, finalize], name="resume-inspection")
    checkpointer = MemoryCheckpointer()
    runner = AsyncRunner(checkpointer=checkpointer)

    paused = await runner.run(graph, {"value": 5}, workflow_id="inspect-resume")
    assert paused.pause is not None
    source_steps = {step.node_name: step for step in await checkpointer.get_steps("inspect-resume") if step.status.value == "completed"}

    resumed = await runner.run(
        graph,
        {paused.pause.response_key: "approved"},
        workflow_id="inspect-resume",
        inspect=True,
    )
    artifact = resumed.inspect().artifact
    by_name = {item.qualified_name: item for item in artifact.nodes}

    assert artifact.captured is True
    assert artifact.status == "completed"
    assert [item.qualified_name for item in artifact.nodes if item.status == "restored"] == [
        "prepare",
        "remember_secret",
    ]
    for name in ("prepare", "remember_secret"):
        restored = by_name[name]
        assert restored.run_id == source_steps[name].run_id
        assert restored.superstep == source_steps[name].superstep
        assert restored.duration_ms == source_steps[name].duration_ms
        assert restored.cached == source_steps[name].cached
        assert restored.values_captured is False
        assert restored.inputs is None
        assert restored.outputs is None
        assert not hasattr(restored, "input_versions")

    assert by_name["review"].status == "completed"
    assert by_name["review"].inputs == {"draft": "draft:5"}
    assert by_name["review"].outputs == {"decision": "approved"}
    assert by_name["review"].values_captured is True
    assert by_name["finalize"].status == "completed"
    assert by_name["finalize"].values_captured is True

    html = resumed.inspect()._repr_html_()
    assert "restored values not captured" in html
    assert restored_only_sentinel not in html


def test_sync_checkpoint_started_inspection_restores_metadata_without_values(tmp_path) -> None:
    restored_only_sentinel = "SYNC-RESTORED-ONLY-SENTINEL"

    @node(output_name="checkpoint_secret")
    def remember_secret(value: int) -> str:
        return restored_only_sentinel

    @node(output_name="seeded")
    def prepare(value: int) -> int:
        return value

    @node(output_name="fresh")
    def calculate(seeded: int) -> str:
        return f"fresh:{seeded}"

    checkpointer = SqliteCheckpointer(str(tmp_path / "inspect-sync.db"))
    checkpointer._sync_db()
    try:
        runner = SyncRunner(checkpointer=checkpointer)
        runner.run(
            Graph([remember_secret, prepare], name="sync-inspect-source"),
            {"value": 4},
            workflow_id="sync-inspect-source",
        )
        checkpoint = checkpointer.checkpoint("sync-inspect-source")
        source_steps = {step.node_name: step for step in checkpoint.steps}
        assert checkpoint.values["checkpoint_secret"] == restored_only_sentinel

        resumed = runner.run(
            Graph([calculate], name="sync-inspect-resume"),
            checkpoint=checkpoint,
            workflow_id="sync-inspect-fork",
            inspect=True,
        )
    finally:
        if checkpointer._sync_conn is not None:
            checkpointer._sync_conn.close()

    artifact = resumed.inspect().artifact
    by_name = {item.qualified_name: item for item in artifact.nodes}

    assert artifact.captured is True
    assert by_name["remember_secret"].status == "restored"
    assert by_name["remember_secret"].run_id == source_steps["remember_secret"].run_id
    assert by_name["remember_secret"].superstep == source_steps["remember_secret"].superstep
    assert by_name["remember_secret"].values_captured is False
    assert by_name["remember_secret"].inputs is None
    assert by_name["remember_secret"].outputs is None
    assert by_name["prepare"].status == "restored"
    assert by_name["prepare"].values_captured is False
    assert by_name["prepare"].inputs is None
    assert by_name["prepare"].outputs is None
    assert by_name["calculate"].status == "completed"
    assert by_name["calculate"].values_captured is True
    assert by_name["calculate"].inputs == {"seeded": 4}
    assert by_name["calculate"].outputs == {"fresh": "fresh:4"}

    html = resumed.inspect()._repr_html_()
    assert "restored values not captured" in html
    assert restored_only_sentinel not in html


def test_inspection_does_not_report_completed_after_cache_write_failure() -> None:
    class CacheWriteError(RuntimeError):
        pass

    class FailingWriteCache:
        def get(self, key: str) -> tuple[bool, object]:
            return False, None

        def set(self, key: str, value: object) -> None:
            raise CacheWriteError(f"cache write failed:{key}")

    @node(output_name="answer", cache=True)
    def calculate(value: int) -> int:
        return value * 2

    result = SyncRunner(cache=FailingWriteCache()).run(
        Graph([calculate], name="inspection-cache-failure"),
        {"value": 6},
        inspect=True,
        error_handling="continue",
    )
    artifact = result.inspect().artifact

    assert artifact.status == "failed"
    assert len(artifact.nodes) == 1
    assert artifact.nodes[0].status == "failed"
    assert artifact.nodes[0].failure is None
    assert artifact.failures == ()
    assert result.node_failures == ()
