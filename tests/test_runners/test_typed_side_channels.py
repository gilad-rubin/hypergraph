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

import pytest

from hypergraph import Graph, node
from hypergraph.exceptions import ExecutionError
from hypergraph.runners._shared.helpers import get_ready_nodes, initialize_state
from hypergraph.runners._shared.types import (
    ExecutionContext,
    GraphState,
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
