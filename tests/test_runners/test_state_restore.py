"""Direct tests for canonical checkpoint state replay."""

from __future__ import annotations

from hypergraph import Graph, node
from hypergraph.checkpointers.types import StepRecord, StepStatus
from hypergraph.nodes.gate import END, route
from hypergraph.runners._shared.state_restore import initialize_state_with_checkpoint
from hypergraph.runners._shared.value_resolution import _state_value_satisfies_input


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


def test_compacted_replay_keeps_output_versions_on_the_state_counter() -> None:
    """A surviving producer's output version lands where the state counter is.

    Compaction folded the loop's earlier ``writer`` rows away. The rows that
    survive still carry the ORIGINAL run's absolute versions, so a recount of
    only the surviving producers can never reach them. ``output_versions`` has
    to come from the same counter ``state.versions`` is built from, or the
    explicit-edge producer check rejects the restored value and hands
    ``consumer`` its signature default instead.
    """

    @node(output_name="value")
    def writer(value: int = 0) -> int:
        return value + 10

    @route(targets=["writer", "consumer"])
    def gate(value: int = 0) -> str:
        return "consumer" if value >= 30 else "writer"

    @node(output_name="report")
    def consumer(value: int = 0) -> str:
        return f"value={value}"

    graph = Graph([writer, gate, consumer], entrypoint="writer")
    # Only the last turn survived: writer's third execution and the gate row
    # that consumed it. Both carry the original run's absolute versions.
    steps = [
        StepRecord(
            run_id="run",
            superstep=4,
            node_name="writer",
            index=8,
            status=StepStatus.COMPLETED,
            input_versions={"value": 2},
            values={"value": 30},
        ),
        StepRecord(
            run_id="run",
            superstep=5,
            node_name="gate",
            index=9,
            status=StepStatus.COMPLETED,
            input_versions={"value": 3},
            decision="consumer",
        ),
    ]

    state = initialize_state_with_checkpoint(
        graph=graph,
        checkpoint_values={"value": 30},
        runtime_values={},
        steps=steps,
    )

    assert state.versions["value"] == 3
    assert state.node_executions["writer"].output_versions == {"value": 3}
    assert state.node_executions["writer"].output_versions["value"] == state.versions["value"]

    # The consumer therefore resolves the restored value over its default.
    assert _state_value_satisfies_input("value", graph._nodes["consumer"], graph, state)
