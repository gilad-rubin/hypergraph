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

from hypergraph import Graph, node
from hypergraph.exceptions import ExecutionError
from hypergraph.runners._shared.readiness import get_ready_nodes
from hypergraph.runners._shared.state_restore import initialize_state
from hypergraph.runners._shared.types import (
    ExecutionContext,
    GraphState,
    PauseExecution,
    PauseInfo,
)
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
        assert err.attempted_node_names == ()
        assert err.node_errors == {}
        assert err.__cause__ is cause

    def test_constructor_params_are_stored(self):
        cause = ValueError("boom")
        state = GraphState(values={"x": 1})
        err = ExecutionError(
            cause,
            state,
            attempted_node_names=("a", "b"),
            node_errors={"b": cause},
        )
        assert err.partial_state is state
        assert err.attempted_node_names == ("a", "b")
        assert err.node_errors == {"b": cause}

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
        pause = PauseExecution(PauseInfo(node_name="approval", output_param="decision", value="draft"))

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
        return PauseInfo(node_name="approval", output_param="decision", value="draft")

    def test_defaults(self):
        pause = PauseExecution(self._pause_info())
        assert pause.partial_state is None
        assert pause.stopped is False

    def test_constructor_params_are_stored(self):
        state = GraphState(values={"a": 1})
        pause = PauseExecution(self._pause_info(), partial_state=state, stopped=True)
        assert pause.partial_state is state
        assert pause.stopped is True
