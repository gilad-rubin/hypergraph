"""Typed side-channel fields replacing runner monkey-patches (issue #146, audit R2).

Pins the typed replacements for the old dynamically-attached attributes:

- ``ExecutionError.attempted_node_names`` / ``.node_errors`` (constructor params,
  formerly ``_attempted_node_names`` / ``_node_errors`` monkey-patches)
- ``GraphState.stopped`` / ``.stop_info`` (real fields, formerly ``_stopped`` /
  ``_stop_info`` monkey-patches; must flow through ``copy()``)
- ``PauseExecution.partial_state`` / ``.stopped`` (constructor params, formerly
  ``_partial_state`` / ``_stopped`` monkey-patches)
"""

from __future__ import annotations

import asyncio

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, node
from hypergraph.events import EventDispatcher, EventProcessor
from hypergraph.events.types import Event, NodeErrorEvent
from hypergraph.exceptions import ExecutionError, get_failure_evidence
from hypergraph.runners._shared.readiness import get_ready_nodes
from hypergraph.runners._shared.results import FailureEvidence, PauseInfo
from hypergraph.runners._shared.state import (
    ExecutionContext,
    GraphState,
    PauseExecution,
)
from hypergraph.runners._shared.state_restore import initialize_state
from hypergraph.runners.async_.executors import AsyncFunctionNodeExecutor
from hypergraph.runners.async_.superstep import run_superstep_async
from hypergraph.runners.sync.executors import SyncFunctionNodeExecutor
from hypergraph.runners.sync.superstep import run_superstep_sync

# === Fixtures ===


@node(output_name="ok_out")
def ok_node(x: int) -> int:
    return x + 1


@node(output_name="bad_out")
def bad_node(x: int) -> int:
    raise ValueError("bad exploded")


# === ExecutionError constructor metadata (B1.1a) ===


class TestExecutionErrorConstructor:
    def test_defaults_are_empty_and_fresh(self):
        cause = ValueError("boom")
        err = ExecutionError(cause, GraphState())
        assert get_failure_evidence(None) == ()
        assert err.attempted_node_names == ()
        assert err.node_errors == {}
        assert err.node_failures == ()
        assert err.failure is None
        assert err.__cause__ is cause

    def test_constructor_params_are_stored(self):
        cause = ValueError("boom")
        state = GraphState(values={"x": 1})
        err = ExecutionError(
            cause,
            state,
            attempted_node_names=("a", "b"),
            node_errors={"b": cause},
            node_failures=(
                FailureEvidence(
                    node_name="b",
                    error=cause,
                    inputs={"x": 1},
                    superstep=2,
                    duration_ms=3.0,
                    graph_name="graph",
                    workflow_id="workflow",
                    item_index=None,
                ),
            ),
        )
        assert err.partial_state is state
        assert err.attempted_node_names == ("a", "b")
        assert err.node_errors == {"b": cause}
        assert err.failure is err.node_failures[0]
        assert err.failure.inputs == {"x": 1}
        assert get_failure_evidence(err) == err.node_failures

    def test_node_errors_default_is_not_shared(self):
        first = ExecutionError(ValueError("x"), GraphState())
        second = ExecutionError(ValueError("y"), GraphState())
        assert first.node_errors is not second.node_errors


class TestSuperstepErrorMetadataSync:
    def test_failing_superstep_surfaces_attempted_and_node_errors(self):
        graph = Graph([ok_node, bad_node])
        state = initialize_state(graph, {"x": 5})
        ready = get_ready_nodes(graph, state)
        assert {n.name for n in ready} == {"ok_node", "bad_node"}

        executor = SyncFunctionNodeExecutor()
        with pytest.raises(ExecutionError) as exc_info:
            run_superstep_sync(
                graph,
                state,
                ready,
                {"x": 5},
                {type(ok_node): executor},
                ExecutionContext(),
            )

        err = exc_info.value
        assert "bad_node" in err.attempted_node_names
        assert isinstance(err.node_errors["bad_node"], ValueError)
        assert "bad exploded" in str(err.node_errors["bad_node"])
        # The old monkey-patch channel must be gone.
        assert not hasattr(err, "_attempted_node_names")
        assert not hasattr(err, "_node_errors")


class TestSuperstepErrorMetadataAsync:
    async def test_failing_superstep_surfaces_attempted_and_node_errors(self):
        graph = Graph([ok_node, bad_node])
        state = initialize_state(graph, {"x": 5})
        ready = get_ready_nodes(graph, state)
        assert {n.name for n in ready} == {"ok_node", "bad_node"}

        executor = AsyncFunctionNodeExecutor()
        with pytest.raises(ExecutionError) as exc_info:
            await run_superstep_async(
                graph,
                state,
                ready,
                {"x": 5},
                {type(ok_node): executor},
                ExecutionContext(),
            )

        err = exc_info.value
        # Async attempts the full ready batch concurrently.
        assert set(err.attempted_node_names) == {"ok_node", "bad_node"}
        assert isinstance(err.node_errors["bad_node"], ValueError)
        assert "bad exploded" in str(err.node_errors["bad_node"])
        assert not hasattr(err, "_attempted_node_names")
        assert not hasattr(err, "_node_errors")


class TestAsyncSuperstepControlFlowPriority:
    async def test_cancelled_error_dominates_earlier_ordinary_exception(self):
        @node(output_name="ordinary_out")
        async def ordinary_failure(x: int) -> int:
            await asyncio.sleep(0)
            raise ValueError(f"ordinary failure: {x}")

        @node(output_name="cancelled_out")
        async def cancelled_failure(x: int) -> int:
            await asyncio.sleep(0)
            raise asyncio.CancelledError(f"cancelled: {x}")

        graph = Graph([ordinary_failure, cancelled_failure])
        state = initialize_state(graph, {"x": 5})
        ready = get_ready_nodes(graph, state)
        assert [node.name for node in ready] == ["ordinary_failure", "cancelled_failure"]

        with pytest.raises(asyncio.CancelledError):
            await run_superstep_async(
                graph,
                state,
                ready,
                {"x": 5},
                {type(ordinary_failure): AsyncFunctionNodeExecutor()},
                ExecutionContext(),
            )

    async def test_pause_dominates_earlier_ordinary_exception(self):
        pause = PauseExecution(PauseInfo(node_name="approval", value="draft", response_key="decision"))

        @node(output_name="ordinary_out")
        async def ordinary_failure(x: int) -> int:
            await asyncio.sleep(0)
            raise ValueError(f"ordinary failure: {x}")

        @node(output_name="decision")
        async def pause_execution(x: int) -> str:
            await asyncio.sleep(0)
            raise pause

        graph = Graph([ordinary_failure, pause_execution])
        state = initialize_state(graph, {"x": 5})
        ready = get_ready_nodes(graph, state)
        assert [node.name for node in ready] == ["ordinary_failure", "pause_execution"]

        with pytest.raises(PauseExecution) as exc_info:
            await run_superstep_async(
                graph,
                state,
                ready,
                {"x": 5},
                {type(ordinary_failure): AsyncFunctionNodeExecutor()},
                ExecutionContext(),
            )

        assert exc_info.value is pause


class TestAsyncSuperstepNestedExecutionError:
    async def test_existing_execution_error_is_wrapped_without_mutation(self):
        cause = ValueError("inner exploded")
        inner_state = GraphState(values={"inner_partial": 1})
        inner_error = ExecutionError(
            cause,
            inner_state,
            attempted_node_names=("inner_node",),
            node_errors={"inner_node": cause},
        )
        original_attempted = inner_error.attempted_node_names
        original_node_errors = dict(inner_error.node_errors)

        @node(output_name="bad_out")
        async def nested_failure(x: int) -> int:
            await asyncio.sleep(0)
            raise inner_error

        @node(output_name="good_out")
        async def successful_sibling(x: int) -> int:
            await asyncio.sleep(0)
            return x + 1

        graph = Graph([nested_failure, successful_sibling])
        state = initialize_state(graph, {"x": 5})
        ready = get_ready_nodes(graph, state)
        assert [node.name for node in ready] == ["nested_failure", "successful_sibling"]

        with pytest.raises(ExecutionError) as exc_info:
            await run_superstep_async(
                graph,
                state,
                ready,
                {"x": 5},
                {type(nested_failure): AsyncFunctionNodeExecutor()},
                ExecutionContext(),
            )

        outer_error = exc_info.value
        assert outer_error is not inner_error
        assert outer_error.__cause__ is inner_error
        assert outer_error.partial_state.values["good_out"] == 6
        assert outer_error.attempted_node_names == ("nested_failure", "successful_sibling")
        assert outer_error.node_errors == {"nested_failure": inner_error}
        assert outer_error not in outer_error.node_errors.values()
        assert inner_error.partial_state is inner_state
        assert inner_error.attempted_node_names == original_attempted
        assert inner_error.node_errors == original_node_errors
        assert inner_error.__cause__ is cause
        assert outer_error.failure is not None
        assert outer_error.failure.node_name == "nested_failure"
        assert outer_error.failure.error is inner_error
        assert outer_error.failure.inputs == {"x": 5}


class TestFailureEvidenceAttributionBoundaries:
    def test_sync_node_error_processor_failure_is_attempted_once(self):
        class RaisingProcessor(EventProcessor):
            def __init__(self) -> None:
                self.attempts = 0

            def on_event(self, event: Event) -> None:
                if isinstance(event, NodeErrorEvent):
                    self.attempts += 1
                    raise RuntimeError("processor failed")

        graph = Graph([bad_node])
        state = initialize_state(graph, {"x": 5})
        processor = RaisingProcessor()

        with pytest.raises(ExecutionError) as exc_info:
            run_superstep_sync(
                graph,
                state,
                get_ready_nodes(graph, state),
                {"x": 5},
                {type(bad_node): SyncFunctionNodeExecutor()},
                ExecutionContext(),
                dispatcher=EventDispatcher([processor], strict=True),
                run_id="run",
                run_span_id="span",
            )

        assert processor.attempts == 1
        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert str(exc_info.value.__cause__) == "processor failed"
        assert exc_info.value.node_failures == ()

    async def test_async_node_error_processor_failure_is_attempted_once(self):
        class RaisingProcessor(EventProcessor):
            def __init__(self) -> None:
                self.attempts = 0

            def on_event(self, event: Event) -> None:
                if isinstance(event, NodeErrorEvent):
                    self.attempts += 1
                    raise RuntimeError("processor failed")

        graph = Graph([bad_node])
        state = initialize_state(graph, {"x": 5})
        processor = RaisingProcessor()

        with pytest.raises(ExecutionError) as exc_info:
            await run_superstep_async(
                graph,
                state,
                get_ready_nodes(graph, state),
                {"x": 5},
                {type(bad_node): AsyncFunctionNodeExecutor()},
                ExecutionContext(),
                dispatcher=EventDispatcher([processor], strict=True),
                run_id="run",
                run_span_id="span",
            )

        assert processor.attempts == 1
        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert str(exc_info.value.__cause__) == "processor failed"
        assert exc_info.value.node_failures == ()

    def test_non_graph_node_ignores_stale_failure_context(self):
        stale_cause = ValueError("stale")
        stale_evidence = FailureEvidence(
            node_name="stale_node",
            error=stale_cause,
            inputs={"x": -1},
            superstep=9,
            duration_ms=1.0,
            graph_name="stale_graph",
            workflow_id=None,
            item_index=None,
        )
        stale_error = ExecutionError(
            stale_cause,
            GraphState(),
            node_failures=(stale_evidence,),
        )
        current_error = ValueError("current")
        current_error.__context__ = stale_error

        @node(output_name="bad_out")
        def current_failure(x: int) -> int:
            raise current_error

        result = SyncRunner().run(
            Graph([current_failure], name="current_graph"),
            {"x": 5},
            error_handling="continue",
        )

        assert result.failure is not None
        assert result.failure.node_name == "current_failure"
        assert result.failure.error is current_error
        assert result.failure.inputs == {"x": 5}

    def test_graph_node_qualifies_child_that_raises_execution_error(self):
        stale_cause = ValueError("stale")
        user_error = ExecutionError(
            stale_cause,
            GraphState(),
            node_failures=(
                FailureEvidence(
                    node_name="stale_node",
                    error=stale_cause,
                    inputs={"x": -1},
                    superstep=9,
                    duration_ms=1.0,
                    graph_name="stale_graph",
                    workflow_id=None,
                    item_index=None,
                ),
            ),
        )

        @node(output_name="bad_out")
        def current_failure(x: int) -> int:
            raise user_error

        child = Graph([current_failure], name="child")
        outer = Graph([child.as_node(name="child_node")], name="outer")

        result = SyncRunner().run(
            outer,
            {"x": 5},
            error_handling="continue",
        )

        assert result.error is user_error
        assert result.failure is not None
        assert result.failure.node_name == "child_node/current_failure"
        assert result.failure.error is user_error
        assert result.failure.inputs == {"x": 5}
        assert result.failure.graph_name == "child"

    def test_sync_graph_node_does_not_copy_raw_execution_error_evidence(self):
        cause = RuntimeError("custom graph executor failed")
        raw_error = ExecutionError(
            cause,
            GraphState(),
            node_failures=(
                FailureEvidence(
                    node_name="stale_node",
                    error=cause,
                    inputs={"secret": "TOP-SECRET"},
                    superstep=9,
                    duration_ms=1.0,
                    graph_name="stale_graph",
                    workflow_id=None,
                    item_index=None,
                ),
            ),
        )

        @node(output_name="child_out")
        def child_node(x: int) -> int:
            return x

        child = Graph([child_node], name="child")
        graph_node = child.as_node(name="custom_graph_node")
        runner = SyncRunner()

        def raise_raw_error(node, state, inputs, ctx):
            raise raw_error

        runner._executors[type(graph_node)] = raise_raw_error
        result = runner.run(
            Graph([graph_node], name="outer"),
            {"x": 5},
            error_handling="continue",
        )

        assert result.error is raw_error
        assert result.node_failures == ()
        assert result.failure is None
        assert "TOP-SECRET" not in repr(result)
        assert "TOP-SECRET" not in repr(result.to_dict())

    def test_sync_graph_node_does_not_replay_evidence_from_previous_run(self):
        reused_error = RuntimeError("reused across runs")

        @node(output_name="first_out")
        def first_failure(secret: str) -> str:
            raise reused_error

        with pytest.raises(RuntimeError) as exc_info:
            SyncRunner().run(Graph([first_failure], name="first"), {"secret": "TOP-SECRET"})

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

    async def test_async_graph_node_does_not_copy_raw_execution_error_evidence(self):
        cause = RuntimeError("custom async graph executor failed")
        raw_error = ExecutionError(
            cause,
            GraphState(),
            node_failures=(
                FailureEvidence(
                    node_name="stale_node",
                    error=cause,
                    inputs={"secret": "TOP-SECRET"},
                    superstep=9,
                    duration_ms=1.0,
                    graph_name="stale_graph",
                    workflow_id=None,
                    item_index=None,
                ),
            ),
        )

        @node(output_name="child_out")
        def child_node(x: int) -> int:
            return x

        child = Graph([child_node], name="child")
        graph_node = child.as_node(name="custom_graph_node")
        runner = AsyncRunner()

        async def raise_raw_error(node, state, inputs, ctx):
            raise raw_error

        runner._executors[type(graph_node)] = raise_raw_error
        result = await runner.run(
            Graph([graph_node], name="outer"),
            {"x": 5},
            error_handling="continue",
        )

        assert result.error is raw_error
        assert result.node_failures == ()
        assert result.failure is None
        assert "TOP-SECRET" not in repr(result)
        assert "TOP-SECRET" not in repr(result.to_dict())

    async def test_async_graph_node_does_not_replay_evidence_from_previous_run(self):
        reused_error = RuntimeError("reused across async runs")

        @node(output_name="first_out")
        async def first_failure(secret: str) -> str:
            raise reused_error

        with pytest.raises(RuntimeError) as exc_info:
            await AsyncRunner().run(Graph([first_failure], name="first"), {"secret": "TOP-SECRET"})

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

    def test_graph_node_does_not_attribute_child_infrastructure_failure(self):
        class CacheFailure(RuntimeError):
            pass

        class ExplodingCache:
            def get(self, key: str) -> tuple[bool, object]:
                raise CacheFailure(key)

            def set(self, key: str, value: object) -> None:
                raise AssertionError("cache set must not run")

        @node(output_name="cached_out", cache=True)
        def cached_child(x: int) -> int:
            raise AssertionError("executor must not run")

        child = Graph([cached_child], name="child")
        outer = Graph([child.as_node(name="child_node")], name="outer")

        result = SyncRunner(cache=ExplodingCache()).run(
            outer,
            {"x": 5},
            error_handling="continue",
        )

        assert isinstance(result.error, CacheFailure)
        assert result.node_failures == ()
        assert result.failure is None

    def test_cache_execution_error_cannot_smuggle_stale_evidence(self):
        cache_cause = RuntimeError("cache unavailable")
        stale_evidence = FailureEvidence(
            node_name="stale_node",
            error=cache_cause,
            inputs={"secret": object()},
            superstep=9,
            duration_ms=1.0,
            graph_name="stale_graph",
            workflow_id=None,
            item_index=None,
        )
        cache_error = ExecutionError(
            cache_cause,
            GraphState(),
            node_failures=(stale_evidence,),
        )

        class ExplodingCache:
            def get(self, key: str) -> tuple[bool, object]:
                raise cache_error

            def set(self, key: str, value: object) -> None:
                raise AssertionError("cache set must not run")

        @node(output_name="cached_out", cache=True)
        def cached_node(x: int) -> int:
            raise AssertionError("executor must not run")

        result = SyncRunner(cache=ExplodingCache()).run(
            Graph([cached_node], name="cache_graph"),
            {"x": 5},
            error_handling="continue",
        )

        assert result.error is cache_cause
        assert result.node_failures == ()
        assert result.failure is None

        with pytest.raises(RuntimeError) as exc_info:
            SyncRunner(cache=ExplodingCache()).run(
                Graph([cached_node], name="cache_graph"),
                {"x": 5},
            )

        assert exc_info.value is cache_cause
        assert get_failure_evidence(exc_info.value) == ()

    async def test_async_cache_execution_error_retains_identity_without_evidence(self):
        cache_cause = RuntimeError("async cache unavailable")
        stale_evidence = FailureEvidence(
            node_name="stale_node",
            error=cache_cause,
            inputs={"secret": object()},
            superstep=9,
            duration_ms=1.0,
            graph_name="stale_graph",
            workflow_id=None,
            item_index=None,
        )
        cache_error = ExecutionError(
            cache_cause,
            GraphState(),
            node_failures=(stale_evidence,),
        )

        class ExplodingCache:
            def get(self, key: str) -> tuple[bool, object]:
                raise cache_error

            def set(self, key: str, value: object) -> None:
                raise AssertionError("cache set must not run")

        @node(output_name="cached_out", cache=True)
        async def cached_node(x: int) -> int:
            raise AssertionError("executor must not run")

        graph = Graph([cached_node], name="async_cache_graph")
        result = await AsyncRunner(cache=ExplodingCache()).run(
            graph,
            {"x": 5},
            error_handling="continue",
        )

        assert result.error is cache_error
        assert result.node_failures == ()
        assert result.failure is None

        with pytest.raises(ExecutionError) as exc_info:
            await AsyncRunner(cache=ExplodingCache()).run(graph, {"x": 5})

        assert exc_info.value is cache_error
        assert get_failure_evidence(exc_info.value) == ()


# === GraphState stop fields (B1.1b) ===


class TestGraphStateStopFields:
    def test_defaults(self):
        state = GraphState()
        assert state.stopped is False
        assert state.stop_info is None

    def test_copy_carries_stop_fields(self):
        state = GraphState()
        state.stopped = True
        state.stop_info = {"reason": "user pressed stop"}
        copied = state.copy()
        assert copied.stopped is True
        assert copied.stop_info == {"reason": "user pressed stop"}

    def test_copy_default_stop_fields(self):
        copied = GraphState().copy()
        assert copied.stopped is False
        assert copied.stop_info is None


# === PauseExecution constructor params (B1.1c) ===


class TestPauseExecutionTypedFields:
    def _pause_info(self) -> PauseInfo:
        return PauseInfo(node_name="approval", value="draft", response_key="decision")

    def test_defaults(self):
        pause = PauseExecution(self._pause_info())
        assert pause.partial_state is None
        assert pause.stopped is False

    def test_constructor_params_are_stored(self):
        state = GraphState(values={"a": 1})
        pause = PauseExecution(self._pause_info(), partial_state=state, stopped=True)
        assert pause.partial_state is state
        assert pause.stopped is True
