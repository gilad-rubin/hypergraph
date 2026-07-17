"""Public contract tests for structured node-failure evidence (issue #154)."""

from __future__ import annotations

import asyncio
import traceback
from typing import Any

import pytest

from hypergraph import (
    AsyncRunner,
    ExecutionError,
    FailureEvidence,
    Graph,
    NodeContext,
    PauseInfo,
    RunResult,
    RunStatus,
    SyncRunner,
    get_failure_evidence,
    node,
)


class EvidenceError(Exception):
    """Distinct leaf error used to prove original-exception propagation."""


class CacheInfrastructureError(Exception):
    """Cache failure that must remain unattributed to the node executor."""


class ExplodingCache:
    """Cache backend proving cache read failures are not node failures."""

    def get(self, key: str) -> tuple[bool, Any]:
        raise CacheInfrastructureError(f"cache unavailable:{key}")

    def set(self, key: str, value: Any) -> None:
        raise AssertionError("cache set must not run after a failed read")


@node(output_name="failed")
def fail_with_x(x: int) -> int:
    raise EvidenceError(f"failed:{x}")


@node(output_name="left")
async def fail_left(x: int) -> int:
    raise EvidenceError(f"left:{x}")


@node(output_name="right")
async def fail_right(x: int) -> int:
    raise LookupError(f"right:{x}")


@node(output_name="good")
async def succeed_alongside_failures(x: int) -> int:
    return x * 10


@node(output_name="mapped")
def fail_on_odd(x: int) -> int:
    if x % 2:
        raise EvidenceError(f"odd:{x}")
    return x * 2


@node(output_name="mapped_async")
async def fail_on_odd_async(x: int) -> int:
    if x % 2:
        raise EvidenceError(f"odd-async:{x}")
    return x * 2


@node(output_name="leaf")
def nested_leaf_failure(x: int) -> int:
    raise EvidenceError(f"nested:{x}")


@node(output_name="leaf_async")
async def nested_leaf_failure_async(x: int) -> int:
    raise EvidenceError(f"nested-async:{x}")


class ExplosivePayload:
    """Input that fails if evidence capture implicitly renders or copies it."""

    def __repr__(self) -> str:
        raise AssertionError("payload repr must not be called")

    def __deepcopy__(self, memo: dict[int, Any]) -> ExplosivePayload:
        raise AssertionError("payload deepcopy must not be called")


@node(output_name="payload_result")
def fail_with_unrenderable_payload(payload: ExplosivePayload) -> str:
    raise EvidenceError("payload failed")


@node(output_name="context_failure")
def fail_with_node_context(x: int, ctx: NodeContext) -> int:
    assert ctx.workflow_id == "context-run"
    raise EvidenceError(f"context:{x}")


@node(output_name="cached_failure", cache=True)
def cached_node_that_must_not_run(x: int) -> int:
    raise AssertionError(f"executor must not run after cache read failure:{x}")


def test_sync_continue_captures_real_inputs_and_falsifies_on_change() -> None:
    graph = Graph([fail_with_x], name="sync-evidence")
    runner = SyncRunner()

    first = runner.run(graph, {"x": 7}, error_handling="continue", workflow_id="sync-7")
    second = runner.run(graph, {"x": 11}, error_handling="continue", workflow_id="sync-11")

    assert first.status == RunStatus.FAILED
    assert second.status == RunStatus.FAILED
    assert isinstance(first.failure, FailureEvidence)
    assert first.failure.inputs == {"x": 7}
    assert second.failure is not None
    assert second.failure.inputs == {"x": 11}
    assert str(first.failure.error) == "failed:7"
    assert str(second.failure.error) == "failed:11"
    assert first.failure.error is first.error
    assert first.node_failures == (first.failure,)
    assert first.failure.node_name == "fail_with_x"
    assert first.failure.graph_name == "sync-evidence"
    assert first.failure.workflow_id == "sync-7"
    assert first.failure.item_index is None
    assert first.failure.superstep >= 0
    assert first.failure.duration_ms >= 0


@pytest.mark.asyncio
async def test_async_parallel_preserves_every_failure_in_ready_order() -> None:
    first_started = asyncio.Event()
    second_finished = asyncio.Event()

    @node(output_name="slow_left")
    async def slow_first_failure(x: int) -> int:
        first_started.set()
        await second_finished.wait()
        raise EvidenceError(f"slow-left:{x}")

    @node(output_name="fast_right")
    async def fast_second_failure(x: int) -> int:
        await first_started.wait()
        second_finished.set()
        raise LookupError(f"fast-right:{x}")

    graph = Graph([slow_first_failure, fast_second_failure, succeed_alongside_failures], name="parallel-evidence")

    result = await AsyncRunner().run(graph, {"x": 3}, error_handling="continue", workflow_id="parallel")

    assert result.status == RunStatus.FAILED
    assert result.values["good"] == 30
    assert [failure.node_name for failure in result.node_failures] == ["slow_first_failure", "fast_second_failure"]
    assert [failure.inputs for failure in result.node_failures] == [{"x": 3}, {"x": 3}]
    assert [type(failure.error) for failure in result.node_failures] == [EvidenceError, LookupError]
    assert result.error is result.failure.error


def test_sync_raise_preserves_original_type_and_uses_public_accessor() -> None:
    graph = Graph([fail_with_x], name="raise-evidence")

    with pytest.raises(EvidenceError, match="failed:5") as exc_info:
        SyncRunner().run(graph, {"x": 5}, workflow_id="raise-sync")

    failures = get_failure_evidence(exc_info.value)
    assert len(failures) == 1
    assert failures[0].inputs == {"x": 5}
    assert failures[0].error is exc_info.value
    for forbidden in ("failure", "failure_case", "_failure_evidence"):
        assert not hasattr(exc_info.value, forbidden)
    assert exc_info.value.__dict__ == {}
    formatted = "".join(traceback.format_exception(exc_info.value))
    assert "ExecutionError" not in formatted
    assert "FailureEvidenceCarrier" not in formatted
    assert "FailureEvidenceContext" not in formatted


def test_sync_raise_preserves_the_exact_original_exception_object() -> None:
    original = EvidenceError("same-object")

    @node(output_name="never")
    def raise_existing_error(x: int) -> int:
        raise original

    with pytest.raises(EvidenceError) as exc_info:
        SyncRunner().run(Graph([raise_existing_error]), {"x": 1})

    assert exc_info.value is original
    assert get_failure_evidence(exc_info.value)[0].error is original


def test_accessor_bypasses_custom_context_hiding() -> None:
    class HidingContextError(Exception):
        def __getattribute__(self, name: str) -> Any:
            if name == "__context__":
                return None
            return super().__getattribute__(name)

    original = HidingContextError("hidden-context")

    @node(output_name="never")
    def raise_hiding_error(x: int) -> int:
        raise original

    with pytest.raises(HidingContextError) as exc_info:
        SyncRunner().run(Graph([raise_hiding_error]), {"x": 3})

    failures = get_failure_evidence(exc_info.value)
    assert len(failures) == 1
    assert failures[0].error is original
    assert failures[0].inputs == {"x": 3}


@pytest.mark.asyncio
async def test_async_raise_preserves_original_type_and_all_parallel_evidence() -> None:
    graph = Graph([fail_left, fail_right], name="raise-parallel")

    with pytest.raises(EvidenceError, match="left:9") as exc_info:
        await AsyncRunner().run(graph, {"x": 9}, workflow_id="raise-async")

    failures = get_failure_evidence(exc_info.value)
    assert [failure.node_name for failure in failures] == ["fail_left", "fail_right"]
    assert [failure.inputs for failure in failures] == [{"x": 9}, {"x": 9}]


def test_sync_map_attaches_source_item_indexes() -> None:
    results = SyncRunner().map(
        Graph([fail_on_odd], name="sync-map-evidence"),
        {"x": [1, 2, 3]},
        map_over="x",
        error_handling="continue",
        workflow_id="sync-map",
    )

    assert [result.failure.item_index if result.failure else None for result in results] == [0, None, 2]
    assert results[0].failure is not None
    assert results[0].failure.inputs == {"x": 1}
    assert results[1].node_failures == ()
    assert results[2].failure is not None
    assert results[2].failure.inputs == {"x": 3}


def test_sync_map_iter_attaches_source_item_indexes() -> None:
    streamed = list(
        SyncRunner().map_iter(
            Graph([fail_on_odd], name="sync-map-iter-evidence"),
            {"x": [1, 2, 3]},
            map_over="x",
            error_handling="continue",
        )
    )

    assert [index for index, _ in streamed] == [0, 1, 2]
    assert [result.failure.item_index if result.failure else None for _, result in streamed] == [0, None, 2]


def test_sync_map_iter_raise_accessor_retains_failing_item_index() -> None:
    with pytest.raises(EvidenceError, match="odd:3") as exc_info:
        list(
            SyncRunner().map_iter(
                Graph([fail_on_odd], name="sync-map-iter-raise-evidence"),
                {"x": [2, 3]},
                map_over="x",
                error_handling="raise",
            )
        )

    failures = get_failure_evidence(exc_info.value)
    assert len(failures) == 1
    assert failures[0].item_index == 1


def test_sync_map_raise_accessor_retains_failing_item_index() -> None:
    with pytest.raises(EvidenceError, match="odd:3") as exc_info:
        SyncRunner().map(
            Graph([fail_on_odd], name="sync-map-raise-evidence"),
            {"x": [2, 3]},
            map_over="x",
            error_handling="raise",
        )

    failures = get_failure_evidence(exc_info.value)
    assert len(failures) == 1
    assert failures[0].item_index == 1
    assert failures[0].inputs == {"x": 3}
    formatted = "".join(traceback.format_exception(exc_info.value))
    assert "FailureEvidenceCarrier" not in formatted
    assert "FailureEvidenceContext" not in formatted


def test_sync_map_raise_masks_stale_infrastructure_execution_error() -> None:
    from hypergraph.runners import GraphState

    cause = RuntimeError("cache unavailable")
    stale = FailureEvidence(
        node_name="stale",
        error=cause,
        inputs={"secret": "old"},
        superstep=9,
        duration_ms=1.0,
        graph_name="old",
        workflow_id=None,
        item_index=None,
    )
    cache_error = ExecutionError(cause, GraphState(), node_failures=(stale,))

    class StaleEvidenceCache:
        def get(self, key: str) -> tuple[bool, Any]:
            raise cache_error

        def set(self, key: str, value: Any) -> None:
            raise AssertionError("cache set must not run")

    with pytest.raises(RuntimeError) as exc_info:
        SyncRunner(cache=StaleEvidenceCache()).map(
            Graph([cached_node_that_must_not_run], name="stale-cache-map"),
            {"x": [5]},
            map_over="x",
            error_handling="raise",
        )

    assert exc_info.value is cause
    assert get_failure_evidence(exc_info.value) == ()


def test_sync_graph_node_rejects_stale_map_carrier_from_previous_call() -> None:
    reused_error = RuntimeError("reused after sync map")

    @node(output_name="first_out")
    def first_failure(secret: str) -> str:
        raise reused_error

    with pytest.raises(RuntimeError) as exc_info:
        SyncRunner().map(
            Graph([first_failure], name="first-map"),
            {"secret": ["TOP-SECRET"]},
            map_over="secret",
        )

    assert exc_info.value is reused_error
    assert get_failure_evidence(reused_error)[0].inputs == {"secret": "TOP-SECRET"}

    @node(output_name="child_out")
    def child_node(x: int) -> int:
        return x

    graph_node = Graph([child_node], name="child").as_node(name="custom_graph_node")
    runner = SyncRunner()

    def raise_reused_error(node, state, inputs, ctx):
        raise reused_error

    runner._executors[type(graph_node)] = raise_reused_error
    result = runner.run(Graph([graph_node], name="second"), {"x": 5}, error_handling="continue")

    assert result.error is reused_error
    assert result.node_failures == ()
    assert result.failure is None
    assert "TOP-SECRET" not in repr(result)
    assert "TOP-SECRET" not in repr(result.to_dict())


@pytest.mark.asyncio
async def test_async_map_attaches_source_item_indexes() -> None:
    results = await AsyncRunner().map(
        Graph([fail_on_odd_async], name="async-map-evidence"),
        {"x": [1, 2, 3]},
        map_over="x",
        error_handling="continue",
        workflow_id="async-map",
    )

    assert [result.failure.item_index if result.failure else None for result in results] == [0, None, 2]
    assert results[0].failure is not None
    assert results[0].failure.inputs == {"x": 1}
    assert results[2].failure is not None
    assert results[2].failure.inputs == {"x": 3}


@pytest.mark.asyncio
async def test_async_map_iter_attaches_source_item_indexes() -> None:
    streamed = [
        item
        async for item in AsyncRunner().map_iter(
            Graph([fail_on_odd_async], name="async-map-iter-evidence"),
            {"x": [1, 2, 3]},
            map_over="x",
            error_handling="continue",
            max_concurrency=2,
        )
    ]
    by_index = dict(streamed)

    assert sorted(by_index) == [0, 1, 2]
    assert by_index[0].failure is not None
    assert by_index[0].failure.item_index == 0
    assert by_index[1].failure is None
    assert by_index[2].failure is not None
    assert by_index[2].failure.item_index == 2


@pytest.mark.asyncio
async def test_async_map_iter_raise_accessor_retains_failing_item_index() -> None:
    with pytest.raises(EvidenceError, match="odd-async:3") as exc_info:
        async for _ in AsyncRunner().map_iter(
            Graph([fail_on_odd_async], name="async-map-iter-raise-evidence"),
            {"x": [2, 3]},
            map_over="x",
            error_handling="raise",
            max_concurrency=1,
        ):
            pass

    failures = get_failure_evidence(exc_info.value)
    assert len(failures) == 1
    assert failures[0].item_index == 1


@pytest.mark.asyncio
async def test_async_map_raise_accessor_retains_failing_item_index() -> None:
    with pytest.raises(EvidenceError, match="odd-async:3") as exc_info:
        await AsyncRunner().map(
            Graph([fail_on_odd_async], name="async-map-raise-evidence"),
            {"x": [2, 3]},
            map_over="x",
            error_handling="raise",
        )

    failures = get_failure_evidence(exc_info.value)
    assert len(failures) == 1
    assert failures[0].item_index == 1
    assert failures[0].inputs == {"x": 3}


@pytest.mark.asyncio
async def test_async_graph_node_rejects_stale_map_carrier_from_previous_call() -> None:
    reused_error = RuntimeError("reused after async map")

    @node(output_name="first_out")
    async def first_failure(secret: str) -> str:
        raise reused_error

    with pytest.raises(RuntimeError) as exc_info:
        await AsyncRunner().map(
            Graph([first_failure], name="first-map"),
            {"secret": ["TOP-SECRET"]},
            map_over="secret",
        )

    assert exc_info.value is reused_error
    assert get_failure_evidence(reused_error)[0].inputs == {"secret": "TOP-SECRET"}

    @node(output_name="child_out")
    def child_node(x: int) -> int:
        return x

    graph_node = Graph([child_node], name="child").as_node(name="custom_graph_node")
    runner = AsyncRunner()

    async def raise_reused_error(node, state, inputs, ctx):
        raise reused_error

    runner._executors[type(graph_node)] = raise_reused_error
    result = await runner.run(Graph([graph_node], name="second"), {"x": 5}, error_handling="continue")

    assert result.error is reused_error
    assert result.node_failures == ()
    assert result.failure is None
    assert "TOP-SECRET" not in repr(result)
    assert "TOP-SECRET" not in repr(result.to_dict())


def test_sync_nested_failure_is_qualified_once_per_boundary() -> None:
    inner = Graph([nested_leaf_failure], name="inner")
    middle = Graph([inner.as_node(name="inner_node")], name="middle")
    outer = Graph([middle.as_node(name="middle_node")], name="outer")

    result = SyncRunner().run(outer, {"x": 4}, error_handling="continue", workflow_id="nested-sync")

    assert result.failure is not None
    assert result.node_failures == (result.failure,)
    assert result.failure.node_name == "middle_node/inner_node/nested_leaf_failure"
    assert result.failure.graph_name == "inner"
    assert result.failure.inputs == {"x": 4}


def test_sync_nested_raise_accessor_reports_the_qualified_leaf() -> None:
    inner = Graph([nested_leaf_failure], name="inner-raise")
    outer = Graph([inner.as_node(name="inner_node")], name="outer-raise")

    with pytest.raises(EvidenceError, match="nested:4") as exc_info:
        SyncRunner().run(outer, {"x": 4})

    failures = get_failure_evidence(exc_info.value)
    assert len(failures) == 1
    assert failures[0].node_name == "inner_node/nested_leaf_failure"


@pytest.mark.asyncio
async def test_async_nested_failure_is_qualified_once_per_boundary() -> None:
    inner = Graph([nested_leaf_failure_async], name="inner-async")
    outer = Graph([inner.as_node(name="inner_node")], name="outer-async")

    result = await AsyncRunner().run(outer, {"x": 6}, error_handling="continue", workflow_id="nested-async")

    assert result.failure is not None
    assert result.node_failures == (result.failure,)
    assert result.failure.node_name == "inner_node/nested_leaf_failure_async"
    assert result.failure.graph_name == "inner-async"
    assert result.failure.inputs == {"x": 6}


def test_raw_input_is_ephemeral_and_never_implicitly_rendered_or_copied() -> None:
    payload = ExplosivePayload()
    provided = {"payload": payload}

    result = SyncRunner().run(
        Graph([fail_with_unrenderable_payload], name="private-evidence"),
        provided,
        error_handling="continue",
    )

    assert result.failure is not None
    assert result.failure.inputs is not provided
    assert result.failure.inputs["payload"] is payload
    assert "inputs=" not in repr(result.failure)
    repr(result)
    result._repr_html_()
    serialized = result.to_dict()
    assert "inputs" not in serialized["node_failures"][0]
    assert all(value is not payload for value in serialized.values())


def test_injected_node_context_is_not_captured_as_a_graph_input() -> None:
    result = SyncRunner().run(
        Graph([fail_with_node_context], name="context-evidence"),
        {"x": 8},
        workflow_id="context-run",
        error_handling="continue",
    )

    assert result.failure is not None
    assert result.failure.inputs == {"x": 8}


def test_cache_read_failure_remains_unattributable() -> None:
    result = SyncRunner(cache=ExplodingCache()).run(
        Graph([cached_node_that_must_not_run], name="cache-infrastructure"),
        {"x": 2},
        error_handling="continue",
    )

    assert isinstance(result.error, CacheInfrastructureError)
    assert result.node_failures == ()
    assert result.failure is None


def test_failed_result_without_node_attribution_has_empty_evidence() -> None:
    result = RunResult(values={}, status=RunStatus.FAILED, error=RuntimeError("pre-node"))

    assert result.node_failures == ()
    assert result.failure is None
    assert get_failure_evidence(result.error) == ()


def test_run_result_keeps_preexisting_positional_field_order() -> None:
    pause = PauseInfo(node_name="approval", value="draft", response_key="answer")

    result = RunResult({}, RunStatus.PAUSED, "run-positional", None, None, pause)

    assert result.pause is pause
    assert result.node_failures == ()


def test_public_accessor_terminates_on_an_unrelated_context_cycle() -> None:
    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__context__ = second
    second.__context__ = first

    assert get_failure_evidence(first) == ()


def test_to_dict_serializes_metadata_but_not_raw_inputs() -> None:
    result = SyncRunner().run(
        Graph([fail_with_x], name="serialized-evidence"),
        {"x": 13},
        error_handling="continue",
        workflow_id="serialized-run",
    )

    serialized = result.to_dict()
    # #233 privacy graduation: serialization carries the safe projection
    # (type name, stable code, static wording) — never str(exception), whose
    # raw message ("failed:13") could embed sensitive values.
    assert "failed:13" not in serialized["error"]
    assert "EvidenceError" in serialized["error"]
    assert "[HG_NODE_FAILED]" in serialized["error"]
    assert len(serialized["node_failures"]) == 1
    failure_entry = serialized["node_failures"][0]
    assert "failed:13" not in failure_entry["error"]
    assert "EvidenceError" in failure_entry["error"]
    assert failure_entry["diagnostic"]["schema"] == "hypergraph.diagnostic/v1"
    assert failure_entry["diagnostic"]["code"] == "HG_NODE_FAILED"
    assert {
        "node_name": failure_entry["node_name"],
        "superstep": failure_entry["superstep"],
        "duration_ms": failure_entry["duration_ms"],
        "graph_name": failure_entry["graph_name"],
        "workflow_id": failure_entry["workflow_id"],
        "item_index": failure_entry["item_index"],
    } == {
        "node_name": "fail_with_x",
        "superstep": result.failure.superstep,
        "duration_ms": result.failure.duration_ms,
        "graph_name": "serialized-evidence",
        "workflow_id": "serialized-run",
        "item_index": None,
    }


def test_public_exports_are_available_from_runners_namespace() -> None:
    from hypergraph.runners import FailureEvidence as RunnerFailureEvidence

    assert RunnerFailureEvidence is FailureEvidence
