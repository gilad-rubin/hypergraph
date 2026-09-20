"""Direct tests for canonical checkpoint state replay."""

from __future__ import annotations

from hypergraph import Graph, node
from hypergraph.checkpointers.types import StepRecord, StepStatus
from hypergraph.nodes.gate import END, route
from hypergraph.runners._shared.state_restore import initialize_state_with_checkpoint
from hypergraph.runners._shared.value_resolution import ValueSource, _state_value_satisfies_input, get_value_source


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


def test_compacted_replay_gives_every_last_producer_the_state_version() -> None:
    """The invariant holds even when the consumer row FOLLOWS the producer.

    ``accumulate`` re-reads ``total``, so its own row raises the counter before
    it produces. ``transform`` never reads ``doubled`` — the only surviving row
    that knows what version ``doubled`` is at is ``decide``, which replays
    after it. A number chosen per step therefore cannot be right for both; the
    last producer of a name only learns its version once the whole history has
    been walked.

    The rows below are the history ``SqliteCheckpointer`` actually persists for
    this graph under ``retention="latest"``.
    """

    @node(output_name="total")
    def accumulate(total: int = 0) -> int:
        return total + 1

    @node(output_name="doubled")
    def transform(total: int = 0) -> int:
        return total * 2

    @route(targets=["accumulate", END])
    def decide(total: int = 0, doubled: int = 0) -> str | list[str]:
        return "accumulate"

    graph = Graph([accumulate, transform, decide], entrypoint="accumulate")
    steps = [
        # The retention carrier: the folded turns, collapsed into one row.
        StepRecord(
            run_id="run",
            superstep=5,
            node_name="__retained_state__",
            index=0,
            status=StepStatus.COMPLETED,
            input_versions={},
            values={"total": 2, "doubled": 4, "_decide": "accumulate"},
            node_type="RetentionBaseline",
        ),
        StepRecord(
            run_id="run",
            superstep=6,
            node_name="accumulate",
            index=1,
            status=StepStatus.COMPLETED,
            input_versions={"total": 2},
            values={"total": 3},
        ),
        StepRecord(
            run_id="run",
            superstep=7,
            node_name="transform",
            index=2,
            status=StepStatus.COMPLETED,
            input_versions={"total": 3},
            values={"doubled": 6},
        ),
        StepRecord(
            run_id="run",
            superstep=8,
            node_name="decide",
            index=3,
            status=StepStatus.COMPLETED,
            input_versions={"total": 3, "doubled": 3},
            values={"_decide": "accumulate"},
            decision="accumulate",
        ),
    ]

    state = initialize_state_with_checkpoint(
        graph=graph,
        checkpoint_values={"total": 3, "doubled": 6},
        runtime_values={},
        steps=steps,
    )

    assert state.versions == {"total": 3, "doubled": 3, "_decide": 2}

    # Every name's LAST producer records the version the state ended up with.
    last_producers = {name: step.node_name for step in steps for name in (step.values or {}) if step.node_name != "__retained_state__"}
    assert last_producers == {"total": "accumulate", "doubled": "transform", "_decide": "decide"}
    for name, producer in last_producers.items():
        execution = state.node_executions[producer]
        assert execution.output_versions[name] == state.versions[name], name

    # The consumer of the derived value therefore resolves it over its default.
    assert _state_value_satisfies_input("doubled", graph._nodes["decide"], graph, state)


def test_carrier_only_history_still_resolves_the_restored_value() -> None:
    """The retention carrier is a fold marker, never a producer.

    Here EVERY ``writer`` row was folded away: the only row that still holds
    ``value`` is ``__retained_state__``, and the surviving ``gate`` row carries
    a higher absolute version than the carrier's own. ``writer`` is therefore
    absent from ``node_executions``, and ``_state_value_satisfies_input`` takes
    its "no surviving producer" escape — the restored value is all there is, so
    it is what the consumer gets.

    That escape is only reachable while the version-fixing post-pass leaves the
    carrier alone. Attribute the state's version to ``__retained_state__`` and
    ``_version_produced_by_any_node`` starts answering "yes, some node produced
    this version" for a row that executed nothing, the escape closes, and the
    consumer silently falls through to its default.
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
    steps = [
        StepRecord(
            run_id="run",
            superstep=4,
            node_name="__retained_state__",
            index=0,
            status=StepStatus.COMPLETED,
            input_versions={},
            values={"value": 30, "_gate": "consumer"},
            node_type="RetentionBaseline",
        ),
        StepRecord(
            run_id="run",
            superstep=5,
            node_name="gate",
            index=9,
            status=StepStatus.COMPLETED,
            input_versions={"value": 3},
            values={"_gate": "consumer"},
            decision="consumer",
        ),
    ]

    state = initialize_state_with_checkpoint(
        graph=graph,
        checkpoint_values={"value": 30},
        runtime_values={},
        steps=steps,
    )

    assert "writer" not in state.node_executions
    assert state.versions["value"] == 3

    # The consumer reads the restored value, not its signature default.
    assert _state_value_satisfies_input("value", graph._nodes["consumer"], graph, state)
    assert get_value_source("value", graph._nodes["consumer"], graph, state, {}) == (ValueSource.EDGE, 30)
    # ...because the carrier still holds the version it folded in at. Give it
    # the state's version instead and the line above returns (DEFAULT, 0).
    assert state.node_executions["__retained_state__"].output_versions["value"] == 1
