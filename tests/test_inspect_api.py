"""Public inspect-mode contract for issue #156."""

from __future__ import annotations

import asyncio
import dataclasses
import threading

import pytest

from hypergraph import (
    AsyncRunner,
    Graph,
    InMemoryCache,
    MapResult,
    RunResult,
    RunStatus,
    SyncRunner,
    interrupt,
    node,
)
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


def test_sync_map_inspection_keeps_completed_siblings_after_failure_and_falsifies_output() -> None:
    """One batch artifact preserves original item identity and real behavior."""

    @node(output_name="prepared_customer")
    def prepare(customer_id: str) -> str:
        return f"prepared:{customer_id}"

    @node(output_name="decision")
    def decide(prepared_customer: str) -> str:
        if prepared_customer == "prepared:maya-23":
            raise ValueError("manual review required for maya-23")
        return f"approved:{prepared_customer}"

    graph = Graph([prepare, decide], name="customer-review-map")
    runner = SyncRunner()

    first = runner.map(
        graph,
        {"customer_id": ["ari-2", "maya-23", "noa-9"]},
        map_over="customer_id",
        inspect=True,
        error_handling="continue",
    )
    changed = runner.map(
        graph,
        {"customer_id": ["ari-2", "maya-23", "tamar-11"]},
        map_over="customer_id",
        inspect=True,
        error_handling="continue",
    )

    artifact = first.inspect()._artifact
    changed_artifact = changed.inspect()._artifact

    assert artifact.status == "partial"
    assert artifact.requested_count == 3
    assert artifact.completed_count == 2
    assert artifact.failed_count == 1
    assert artifact.restored_count == 0
    assert artifact.unstarted_item_indexes == ()
    assert [item.item_index for item in artifact.items] == [0, 1, 2]
    assert [item.status for item in artifact.items] == ["completed", "failed", "completed"]
    assert artifact.items[1].requested_inputs == {"customer_id": "maya-23"}
    assert artifact.items[1].run is not None
    assert artifact.items[1].run.failures[0].inputs == {"prepared_customer": "prepared:maya-23"}
    assert artifact.items[0].run is not None
    assert artifact.items[2].run is not None
    assert artifact.items[0].run.nodes[-1].outputs == {"decision": "approved:prepared:ari-2"}
    assert artifact.items[2].run.nodes[-1].outputs == {"decision": "approved:prepared:noa-9"}
    assert changed_artifact.items[2].run is not None
    assert changed_artifact.items[2].run.nodes[-1].outputs == {"decision": "approved:prepared:tamar-11"}


def test_sync_start_map_inspection_returns_the_same_settled_artifact_without_handle_growth() -> None:
    @node(output_name="doubled")
    def double(value: int) -> int:
        return value * 2

    handle = SyncRunner().start_map(
        Graph([double], name="sync-start-map-inspection"),
        {"value": [3, 5]},
        map_over="value",
        inspect=True,
    )
    result = handle.result(raise_on_failure=False)
    artifact = result.inspect()._artifact

    assert {name for name in vars(type(handle)) if not name.startswith("_")} == {
        "done",
        "stop",
        "result",
    }
    assert artifact.captured is True
    assert [item.run.nodes[-1].outputs for item in artifact.items if item.run] == [
        {"doubled": 6},
        {"doubled": 10},
    ]
    assert result[0].inspect()._artifact is artifact.items[0].run
    assert result[1].inspect()._artifact is artifact.items[1].run


def test_sync_stopped_map_inspection_has_claimed_items_and_sparse_unstarted_metadata() -> None:
    second_entered = threading.Event()
    release_second = threading.Event()
    entered: list[int] = []

    @node(output_name="processed")
    def process(item: int) -> int:
        entered.append(item)
        if item == 0:
            raise ValueError("item zero failed before stop")
        if item == 1:
            second_entered.set()
            if not release_second.wait(timeout=5):
                raise AssertionError("claimed item was not released")
        return item * 10

    handle = SyncRunner().start_map(
        Graph([process], name="sparse-inspected-map"),
        {"item": [0, 1, 2, 3, 4]},
        map_over="item",
        inspect=True,
    )
    try:
        assert second_entered.wait(timeout=5), "second item was never claimed"
        handle.stop(info={"reason": "operator stopped early"})
    finally:
        release_second.set()

    result = handle.result(raise_on_failure=False)
    artifact = result.inspect()._artifact

    assert entered == [0, 1]
    assert artifact.status == "stopped"
    assert artifact.requested_count == 5
    assert [item.item_index for item in artifact.items] == [0, 1]
    assert [item.status for item in artifact.items] == ["failed", "stopped"]
    assert artifact.unstarted_item_indexes == (2, 3, 4)
    assert artifact.unstarted_count == 3
    assert all(item.item_index not in {2, 3, 4} for item in artifact.items)


def test_sync_blocking_map_raise_keeps_the_error_and_terminal_live_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hypergraph.runners._shared import template_sync

    failure = ValueError("map failure identity must survive")
    executed: list[int] = []
    sessions: list[template_sync.MapInspectionSession] = []
    original_session_type = template_sync.MapInspectionSession

    class RecordingMapInspectionSession(original_session_type):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            sessions.append(self)

    monkeypatch.setattr(template_sync, "MapInspectionSession", RecordingMapInspectionSession)

    @node(output_name="answer")
    def calculate(value: int) -> int:
        executed.append(value)
        if value == 1:
            raise failure
        return value * 10

    with pytest.raises(ValueError) as raised:
        SyncRunner().map(
            Graph([calculate], name="raising-inspected-map"),
            {"value": [0, 1, 2]},
            map_over="value",
            inspect=True,
        )

    assert raised.value is failure
    assert executed == [0, 1]
    assert len(sessions) == 1
    artifact = sessions[0].snapshot()
    assert artifact.status == "failed"
    assert artifact.terminal is True
    assert [item.item_index for item in artifact.items] == [0, 1]
    assert [item.status for item in artifact.items] == ["completed", "failed"]
    assert artifact.unstarted_item_indexes == (2,)


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


def test_sync_map_inspect_option_is_boolean_and_named_graph_input_uses_values() -> None:
    @node(output_name="echoed")
    def echo(inspect: str, value: int) -> str:
        return f"{inspect}:{value}"

    graph = Graph([echo], name="map-inspect-input-collision")
    result = SyncRunner().map(
        graph,
        values={"inspect": "graph-owned value", "value": [1, 2]},
        map_over="value",
        inspect=True,
    )

    assert result["echoed"] == ["graph-owned value:1", "graph-owned value:2"]
    assert [item.requested_inputs for item in result.inspect()._artifact.items] == [
        {"inspect": "graph-owned value", "value": 1},
        {"inspect": "graph-owned value", "value": 2},
    ]
    with pytest.raises(TypeError, match="inspect must be a bool"):
        SyncRunner().map(
            graph,
            values={"inspect": "value", "value": [1]},
            map_over="value",
            inspect="yes",  # type: ignore[arg-type]
        )


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


def test_map_without_capture_is_inspectable_but_does_not_invent_item_values() -> None:
    @node(output_name="secret_result")
    def calculate(secret_input: str) -> str:
        return f"derived:{secret_input}"

    result = SyncRunner().map(
        Graph([calculate], name="degraded-map-inspection"),
        {"secret_input": ["map-secret-a", "map-secret-b"]},
        map_over="secret_input",
    )
    artifact = result.inspect()._artifact
    html = result.inspect()._repr_html_()

    assert artifact.captured is False
    assert [item.item_index for item in artifact.items] == [0, 1]
    assert all(item.requested_inputs is None for item in artifact.items)
    assert all(item.run is not None and item.run.captured is False for item in artifact.items)
    assert all(
        node.inputs is None and node.outputs is None and node.values_captured is False
        for item in artifact.items
        if item.run is not None
        for node in item.run.nodes
    )
    assert "not captured; rerun with inspect=True" in html
    assert "map-secret-a" not in html
    assert "map-secret-b" not in html
    assert "derived:map-secret-a" not in html
    assert "derived:map-secret-b" not in html


def test_degraded_map_reconstructs_original_indexes_around_sparse_unstarted_items() -> None:
    @node(output_name="answer")
    def calculate(value: int) -> int:
        return value * 10

    graph = Graph([calculate], name="degraded-original-index-map")
    runner = SyncRunner()
    first = runner.run(graph, {"value": 1})
    second = runner.run(graph, {"value": 3})
    curtailed = MapResult(
        (first, second),
        "curtailed-map-run",
        1.0,
        ("value",),
        "zip",
        graph.name or "",
        (0, 2),
    )

    artifact = curtailed.inspect()._artifact

    assert artifact.requested_count == 4
    assert artifact.unstarted_item_indexes == (0, 2)
    assert [item.item_index for item in artifact.items] == [1, 3]
    assert [item.run.run_id for item in artifact.items if item.run is not None] == [
        first.run_id,
        second.run_id,
    ]
    assert all(item.requested_inputs is None for item in artifact.items)


def test_empty_inspected_map_returns_a_terminal_captured_batch_artifact() -> None:
    @node(output_name="answer")
    def calculate(value: int) -> int:
        return value * 10

    result = SyncRunner().map(
        Graph([calculate], name="empty-inspected-map"),
        {"value": []},
        map_over="value",
        inspect=True,
    )
    artifact = result.inspect()._artifact

    assert result.run_id is None
    assert artifact.run_id is None
    assert artifact.status == "completed"
    assert artifact.requested_count == 0
    assert artifact.items == ()
    assert artifact.unstarted_item_indexes == ()
    assert artifact.captured is True
    assert artifact.terminal is True


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


def test_map_inspection_data_is_final_private_and_preserves_old_surfaces() -> None:
    private_sentinel = "PRIVATE-MAP-INSPECTION-SENTINEL"

    @node(output_name="prepared")
    def prepare(secret_input: str) -> str:
        return f"{private_sentinel}:{secret_input}"

    @node(output_name="public")
    def publish(prepared: str) -> str:
        return "safe-public-output"

    graph = Graph([prepare, publish], name="map-inspection-compatibility").select("public")
    captured = SyncRunner().map(
        graph,
        {"secret_input": ["customer-1"]},
        map_over="secret_input",
        inspect=True,
    )
    without_private_artifact = dataclasses.replace(captured, _inspection=None)
    positional = MapResult(
        captured.results,
        "stable-map-run-id",
        1.0,
        ("secret_input",),
        "zip",
        "stable-graph-name",
        (),
    )

    assert private_sentinel in captured.inspect()._repr_html_()
    assert captured == without_private_artifact
    assert captured == list(captured.results)
    assert repr(captured) == repr(without_private_artifact)
    assert captured.summary() == without_private_artifact.summary()
    assert captured.to_dict() == without_private_artifact.to_dict()
    assert "_inspection" not in captured.to_dict()
    assert private_sentinel not in repr(captured)
    assert private_sentinel not in captured.summary()
    assert private_sentinel not in str(captured.to_dict())
    assert dataclasses.fields(MapResult)[-1].name == "_inspection"
    assert positional.requested_count == 1


def test_sync_nested_inspection_uses_slash_paths_and_real_child_run_identity() -> None:
    @node(output_name="inner_answer")
    def inner_leaf(value: int) -> int:
        return value + 1

    inner = Graph([inner_leaf], name="inner")
    outer = Graph([inner.as_node(name="child")], name="outer")

    result = SyncRunner().run(outer, {"value": 3}, inspect=True)
    artifact = result.inspect()._artifact
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
    artifact = result.inspect()._artifact

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

    assert [item.qualified_name for item in result.inspect()._artifact.nodes] == ["call_side_runner"]


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
async def test_async_map_inspection_keeps_original_indexes_when_items_finish_out_of_order() -> None:
    entered = [asyncio.Event() for _ in range(3)]
    release = [asyncio.Event() for _ in range(3)]
    finished = [asyncio.Event() for _ in range(3)]
    physical_completion_order: list[int] = []

    @node(output_name="answer")
    async def calculate(value: int) -> int:
        entered[value].set()
        await release[value].wait()
        physical_completion_order.append(value)
        finished[value].set()
        return value * 10

    pending = asyncio.create_task(
        AsyncRunner().map(
            Graph([calculate], name="async-map-order"),
            {"value": [0, 1, 2]},
            map_over="value",
            max_concurrency=3,
            inspect=True,
        )
    )
    for event in entered:
        await asyncio.wait_for(event.wait(), timeout=5)
    for item_index in (1, 2, 0):
        release[item_index].set()
        await asyncio.wait_for(finished[item_index].wait(), timeout=5)

    result = await asyncio.wait_for(pending, timeout=5)
    artifact = result.inspect()._artifact

    assert physical_completion_order == [1, 2, 0]
    assert [item.item_index for item in artifact.items] == [0, 1, 2]
    assert [item.requested_inputs for item in artifact.items] == [
        {"value": 0},
        {"value": 1},
        {"value": 2},
    ]
    assert [item.run.nodes[-1].outputs for item in artifact.items if item.run] == [
        {"answer": 0},
        {"answer": 10},
        {"answer": 20},
    ]


@pytest.mark.asyncio
async def test_async_map_inspect_option_is_boolean_and_named_graph_input_uses_values() -> None:
    @node(output_name="echoed")
    async def echo(inspect: str, value: int) -> str:
        return f"{inspect}:{value}"

    graph = Graph([echo], name="async-map-inspect-input-collision")
    result = await AsyncRunner().map(
        graph,
        values={"inspect": "graph-owned value", "value": [1, 2]},
        map_over="value",
        inspect=True,
    )

    assert result["echoed"] == ["graph-owned value:1", "graph-owned value:2"]
    assert [item.requested_inputs for item in result.inspect()._artifact.items] == [
        {"inspect": "graph-owned value", "value": 1},
        {"inspect": "graph-owned value", "value": 2},
    ]
    with pytest.raises(TypeError, match="inspect must be a bool"):
        await AsyncRunner().map(
            graph,
            values={"inspect": "value", "value": [1]},
            map_over="value",
            inspect="yes",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_async_start_map_inspection_returns_the_same_settled_artifact_without_handle_growth() -> None:
    @node(output_name="doubled")
    async def double(value: int) -> int:
        return value * 2

    handle = AsyncRunner().start_map(
        Graph([double], name="async-start-map-inspection"),
        {"value": [3, 5]},
        map_over="value",
        inspect=True,
    )
    result = await handle.result(raise_on_failure=False)
    artifact = result.inspect()._artifact

    assert {name for name in vars(type(handle)) if not name.startswith("_")} == {
        "done",
        "stop",
        "result",
    }
    assert artifact.captured is True
    assert [item.run.nodes[-1].outputs for item in artifact.items if item.run] == [
        {"doubled": 6},
        {"doubled": 10},
    ]
    assert result[0].inspect()._artifact is artifact.items[0].run
    assert result[1].inspect()._artifact is artifact.items[1].run


@pytest.mark.asyncio
async def test_async_blocking_map_raise_keeps_the_error_and_terminal_live_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hypergraph.runners._shared import template_async

    failure = ValueError("async map failure identity must survive")
    executed: list[int] = []
    sessions: list[template_async.MapInspectionSession] = []
    original_session_type = template_async.MapInspectionSession

    class RecordingMapInspectionSession(original_session_type):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            sessions.append(self)

    monkeypatch.setattr(template_async, "MapInspectionSession", RecordingMapInspectionSession)

    @node(output_name="answer")
    async def calculate(value: int) -> int:
        executed.append(value)
        if value == 1:
            raise failure
        return value * 10

    with pytest.raises(ValueError) as raised:
        await AsyncRunner().map(
            Graph([calculate], name="async-raising-inspected-map"),
            {"value": [0, 1, 2]},
            map_over="value",
            max_concurrency=1,
            inspect=True,
        )

    assert raised.value is failure
    assert executed == [0, 1]
    assert len(sessions) == 1
    artifact = sessions[0].snapshot()
    assert artifact.status == "failed"
    assert artifact.terminal is True
    assert [item.item_index for item in artifact.items] == [0, 1]
    assert [item.status for item in artifact.items] == ["completed", "failed"]
    assert artifact.unstarted_item_indexes == (2,)


@pytest.mark.asyncio
async def test_async_map_inspection_separates_restored_items_from_fresh_capture() -> None:
    restored_only_secret = "RESTORED-MAP-OUTPUT-MUST-NOT-BE-INVENTED"
    executed: list[int] = []

    @node(output_name="answer")
    async def calculate(value: int) -> str:
        executed.append(value)
        if value == 10:
            return restored_only_secret
        return f"fresh:{value * 2}"

    runner = AsyncRunner(checkpointer=MemoryCheckpointer())
    graph = Graph([calculate], name="partially-restored-inspected-map")
    await runner.map(
        graph,
        {"value": [10]},
        map_over="value",
        workflow_id="map-inspection-restore",
    )
    executed.clear()

    resumed = await runner.map(
        graph,
        {"value": [10, 20]},
        map_over="value",
        workflow_id="map-inspection-restore",
        inspect=True,
    )
    artifact = resumed.inspect()._artifact

    assert executed == [20]
    assert artifact.status == "completed"
    assert artifact.requested_count == 2
    assert artifact.completed_count == 2
    assert artifact.restored_count == 1
    assert [item.status for item in artifact.items] == ["restored", "completed"]
    assert [item.restored for item in artifact.items] == [True, False]
    assert artifact.items[0].requested_inputs == {"value": 10}
    assert artifact.items[0].run is not None
    assert artifact.items[0].run.captured is False
    assert artifact.items[0].run.nodes[0].values_captured is False
    assert artifact.items[0].run.nodes[0].inputs is None
    assert artifact.items[0].run.nodes[0].outputs is None
    assert artifact.items[1].run is resumed[1].inspect()._artifact
    assert artifact.items[1].run.captured is True
    assert artifact.items[1].run.nodes[-1].outputs == {"answer": "fresh:40"}
    assert restored_only_secret not in resumed.inspect()._repr_html_()


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
    artifact = result.inspect()._artifact
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
    artifact = result.inspect()._artifact
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
    artifact = resumed.inspect()._artifact
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

    artifact = resumed.inspect()._artifact
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
    artifact = result.inspect()._artifact

    assert artifact.status == "failed"
    assert len(artifact.nodes) == 1
    assert artifact.nodes[0].status == "failed"
    assert artifact.nodes[0].failure is None
    assert artifact.failures == ()
    assert result.node_failures == ()


@pytest.mark.parametrize("cache_enabled", [False, True])
def test_sync_inspection_completes_only_after_state_application(monkeypatch, cache_enabled: bool) -> None:
    from hypergraph.runners.sync import superstep

    class StateApplyError(RuntimeError):
        pass

    @node(output_name="answer", cache=cache_enabled)
    def calculate(value: int) -> int:
        return value * 2

    graph = Graph([calculate], name="sync-state-apply-inspection")
    runner = SyncRunner(cache=InMemoryCache())
    if cache_enabled:
        runner.run(graph, {"value": 6})

    def fail_apply(*args: object, **kwargs: object) -> None:
        raise StateApplyError("state application failed after executor success")

    with monkeypatch.context() as patch:
        patch.setattr(superstep, "apply_node_result", fail_apply)
        failed = runner.run(
            graph,
            {"value": 6},
            inspect=True,
            error_handling="continue",
        )

    failed_artifact = failed.inspect()._artifact
    assert failed_artifact.status == "failed"
    assert [item.status for item in failed_artifact.nodes] == ["failed"]
    assert failed_artifact.nodes[0].failure is None
    assert failed_artifact.failures == ()
    assert failed.node_failures == ()

    succeeded = runner.run(graph, {"value": 6}, inspect=True)
    successful_node = succeeded.inspect()._artifact.nodes[0]
    assert successful_node.status == "completed"
    assert successful_node.outputs == {"answer": 12}
    assert successful_node.cached is cache_enabled
    assert successful_node.duration_ms >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_enabled", [False, True])
async def test_async_inspection_settles_all_executors_after_state_application_failure(
    monkeypatch,
    cache_enabled: bool,
) -> None:
    from hypergraph.runners.async_ import superstep

    class StateApplyError(RuntimeError):
        pass

    @node(output_name="left", cache=cache_enabled)
    async def calculate_left(value: int) -> int:
        return value * 2

    @node(output_name="right", cache=cache_enabled)
    async def calculate_right(value: int) -> int:
        return value * 3

    graph = Graph([calculate_left, calculate_right], name="async-state-apply-inspection")
    runner = AsyncRunner(cache=InMemoryCache())
    if cache_enabled:
        await runner.run(graph, {"value": 6})

    def fail_apply(*args: object, **kwargs: object) -> None:
        raise StateApplyError("state application failed after executors succeeded")

    with monkeypatch.context() as patch:
        patch.setattr(superstep, "apply_node_result", fail_apply)
        failed = await runner.run(
            graph,
            {"value": 6},
            inspect=True,
            error_handling="continue",
        )

    failed_artifact = failed.inspect()._artifact
    assert failed_artifact.status == "failed"
    assert [item.status for item in failed_artifact.nodes] == ["failed", "failed"]
    assert all(item.failure is None for item in failed_artifact.nodes)
    assert failed_artifact.failures == ()
    assert failed.node_failures == ()
    assert all(item.status != "running" for item in failed_artifact.nodes)

    succeeded = await runner.run(graph, {"value": 6}, inspect=True)
    successful_nodes = succeeded.inspect()._artifact.nodes
    assert [item.status for item in successful_nodes] == ["completed", "completed"]
    assert {item.qualified_name: item.outputs for item in successful_nodes} == {
        "calculate_left": {"left": 12},
        "calculate_right": {"right": 18},
    }
    assert all(item.cached is cache_enabled for item in successful_nodes)
    assert all(item.duration_ms >= 0 for item in successful_nodes)
