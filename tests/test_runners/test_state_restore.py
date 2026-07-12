"""Direct tests for canonical checkpoint state replay."""

from __future__ import annotations

from hypergraph import Graph, node
from hypergraph.checkpointers.types import StepRecord, StepStatus
from hypergraph.nodes.gate import END, route
from hypergraph.runners._shared.state_restore import initialize_state_with_checkpoint


def test_checkpoint_replay_is_ordered_single_pass_with_exact_state() -> None:
    @node(output_name="value")
    def writer(seed: int) -> int:
        return seed

    @route(targets=["writer", END])
    def choose(value: int) -> str | list[str]:
        return "writer"

    graph = Graph([writer, choose], entrypoint="writer")
    steps = [
        StepRecord(
            run_id="run",
            superstep=2,
            node_name="writer",
            index=7,
            status=StepStatus.COMPLETED,
            input_versions={"seed": 3},
            values={"value": 20},
        ),
        StepRecord(
            run_id="run",
            superstep=3,
            node_name="ignored_failure",
            index=9,
            status=StepStatus.FAILED,
            input_versions={"value": 99},
            values={"value": 999},
            decision="END",
        ),
        StepRecord(
            run_id="run",
            superstep=1,
            node_name="choose",
            index=4,
            status=StepStatus.COMPLETED,
            input_versions={"value": 1},
            decision=["writer", "END"],
        ),
        StepRecord(
            run_id="run",
            superstep=0,
            node_name="writer",
            index=2,
            status=StepStatus.COMPLETED,
            input_versions={"seed": 1},
            values={"value": 10},
        ),
    ]

    state = initialize_state_with_checkpoint(
        graph=graph,
        checkpoint_values={"seed": 1, "value": 20},
        runtime_values={"seed": 9},
        steps=steps,
    )

    assert state.values == {"seed": 9, "value": 20}
    assert state.versions == {"seed": 4, "value": 2}
    assert state.routing_decisions == {"choose": ["writer", END]}
    assert "ignored_failure" not in state.node_executions

    latest_writer = state.node_executions["writer"]
    assert latest_writer.outputs == {"value": 20}
    assert latest_writer.input_versions == {"seed": 3}
    assert latest_writer.output_versions == {"value": 2}
    assert latest_writer.sequence == 7

    routing_execution = state.node_executions["choose"]
    assert routing_execution.output_versions == {}
    assert routing_execution.sequence == 4
