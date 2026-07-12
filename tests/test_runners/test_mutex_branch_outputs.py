"""Runtime coverage for mutex branch outputs with duplicate value names."""

from __future__ import annotations

import pytest

from hypergraph import AsyncRunner, Graph, RunStatus, SyncRunner, ifelse, node
from hypergraph.checkpointers.types import Checkpoint, StepRecord, StepStatus
from hypergraph.runners._shared.readiness import apply_node_result
from hypergraph.runners._shared.state import GraphState, NodeExecution
from hypergraph.runners._shared.state_restore import initialize_state
from hypergraph.runners._shared.value_resolution import _unversioned_execution_can_own_value, has_input


def same_graph() -> Graph:
    @ifelse(when_true="path_a", when_false="path_b")
    def decide(x: int) -> bool:
        return x > 0

    @node(output_name="o1")
    def path_a(x: int) -> str:
        return f"a:{x}"

    @node(output_name="o1")
    def path_b(x: int) -> str:
        return f"b:{x}"

    @node(output_name="final")
    def consumer(o1: str) -> str:
        return f"final:{o1}"

    return Graph([decide, path_a, path_b, consumer])


def consumer_in_subgraph() -> Graph:
    @ifelse(when_true="path_a", when_false="path_b")
    def decide(x: int) -> bool:
        return x > 0

    @node(output_name="o1")
    def path_a(x: int) -> str:
        return f"a:{x}"

    @node(output_name="o1")
    def path_b(x: int) -> str:
        return f"b:{x}"

    @node(output_name="final")
    def consumer(o1: str) -> str:
        return f"final:{o1}"

    consumer_graph = Graph([consumer], name="consumer_graph")
    return Graph([decide, path_a, path_b, consumer_graph.as_node()])


def producers_and_consumer_in_separate_subgraphs() -> Graph:
    @ifelse(when_true="path_a", when_false="path_b")
    def decide(x: int) -> bool:
        return x > 0

    @node(output_name="o1")
    def path_a(x: int) -> str:
        return f"a:{x}"

    @node(output_name="o1")
    def path_b(x: int) -> str:
        return f"b:{x}"

    producer_graph = Graph([decide, path_a, path_b], name="producer_graph").select("o1")

    @node(output_name="final")
    def consumer(o1: str) -> str:
        return f"final:{o1}"

    consumer_graph = Graph([consumer], name="consumer_graph")
    return Graph([producer_graph.as_node(), consumer_graph.as_node()])


def explicit_edges_both_branches() -> Graph:
    @ifelse(when_true="path_a", when_false="path_b")
    def decide(x: int) -> bool:
        return x > 0

    @node(output_name="o1")
    def path_a(x: int) -> str:
        return f"a:{x}"

    @node(output_name="o1")
    def path_b(x: int) -> str:
        return f"b:{x}"

    @node(output_name="final")
    def consumer(o1: str) -> str:
        return f"final:{o1}"

    return Graph([decide, path_a, path_b, consumer], edges=[(path_a, consumer), (path_b, consumer)])


def explicit_edge_missing_false_branch() -> Graph:
    @ifelse(when_true="path_a", when_false="path_b")
    def decide(x: int) -> bool:
        return x > 0

    @node(output_name="o1")
    def path_a(x: int) -> str:
        return f"a:{x}"

    @node(output_name="o1")
    def path_b(x: int) -> str:
        return f"b:{x}"

    @node(output_name="final")
    def consumer(o1: str) -> str:
        return f"final:{o1}"

    return Graph([decide, path_a, path_b, consumer], edges=[(path_a, consumer)])


@pytest.mark.parametrize(
    ("builder", "input_key"),
    [
        (same_graph, "x"),
        (consumer_in_subgraph, "x"),
        (producers_and_consumer_in_separate_subgraphs, "x"),
        (explicit_edges_both_branches, "x"),
    ],
)
@pytest.mark.parametrize(("x", "expected"), [(1, "final:a:1"), (-1, "final:b:-1")])
def test_mutex_duplicate_outputs_feed_consumer_at_runtime(builder, input_key: str, x: int, expected: str) -> None:
    result = SyncRunner().run(builder(), {input_key: x})

    assert result.status == RunStatus.COMPLETED
    assert result["final"] == expected


def test_explicit_edge_missing_false_branch_does_not_leak_same_named_value() -> None:
    result = SyncRunner().run(explicit_edge_missing_false_branch(), {"x": -1})

    assert result.status == RunStatus.COMPLETED
    assert result["o1"] == "b:-1"
    assert "final" not in result.values


def test_explicit_edge_missing_false_branch_still_runs_wired_branch() -> None:
    result = SyncRunner().run(explicit_edge_missing_false_branch(), {"x": 1})

    assert result.status == RunStatus.COMPLETED
    assert result["final"] == "final:a:1"


def test_missing_output_versions_do_not_accept_later_undeclared_writer() -> None:
    graph = explicit_edge_missing_false_branch()
    consumer = graph._nodes["consumer"]
    state = GraphState(
        values={"o1": "b:-1"},
        versions={"o1": 2},
        node_executions={
            "path_a": NodeExecution(
                node_name="path_a",
                input_versions={"x": 1},
                outputs={"o1": "a:1"},
                output_versions={},
            ),
            "path_b": NodeExecution(
                node_name="path_b",
                input_versions={"x": 1},
                outputs={"o1": "b:-1"},
                output_versions={},
            ),
        },
    )

    assert not has_input("o1", consumer, graph, state)


def test_missing_output_versions_still_accept_current_declared_writer() -> None:
    graph = explicit_edge_missing_false_branch()
    consumer = graph._nodes["consumer"]
    state = GraphState(
        values={"o1": "a:1"},
        versions={"o1": 1},
        node_executions={
            "path_a": NodeExecution(
                node_name="path_a",
                input_versions={"x": 1},
                outputs={"o1": "a:1"},
                output_versions={},
            ),
        },
    )

    assert has_input("o1", consumer, graph, state)


def test_unversioned_owner_uses_explicit_execution_sequence() -> None:
    state = GraphState(
        node_executions={
            # Deliberately opposite execution order: replacement/re-keying must
            # not change which legacy unversioned writer owns ``shared``.
            "newer": NodeExecution(
                node_name="newer",
                input_versions={},
                outputs={"shared": "new"},
                sequence=2,
            ),
            "older": NodeExecution(
                node_name="older",
                input_versions={},
                outputs={"shared": "old"},
                sequence=1,
            ),
        }
    )

    assert _unversioned_execution_can_own_value("shared", "newer", state)
    assert not _unversioned_execution_can_own_value("shared", "older", state)


def test_explicit_execution_sequence_outranks_legacy_sentinel() -> None:
    state = GraphState(
        node_executions={
            "sequenced": NodeExecution(
                node_name="sequenced",
                input_versions={},
                outputs={"shared": "new"},
                sequence=4,
            ),
            "legacy": NodeExecution(
                node_name="legacy",
                input_versions={},
                outputs={"shared": "unknown"},
            ),
        }
    )

    assert _unversioned_execution_can_own_value("shared", "sequenced", state)
    assert not _unversioned_execution_can_own_value("shared", "legacy", state)


def test_reexecuted_node_gets_sequence_after_existing_maximum() -> None:
    @node(output_name="shared")
    def writer(x: int) -> int:
        return x

    graph = Graph([writer])
    state = GraphState(
        node_executions={
            "writer": NodeExecution(
                node_name="writer",
                input_versions={"x": 1},
                outputs={"shared": 1},
                sequence=1,
            ),
            "other": NodeExecution(
                node_name="other",
                input_versions={},
                outputs={"other": 1},
                sequence=5,
            ),
        }
    )

    apply_node_result(
        graph,
        state,
        writer,
        {"shared": 2},
        {"x": 2},
        {},
        duration_ms=0.0,
        cached=False,
    )

    assert list(state.node_executions) == ["writer", "other"]
    assert state.node_executions["writer"].sequence == 6


def test_checkpoint_replay_restores_durable_sequence_for_next_execution() -> None:
    @node(output_name="shared")
    def writer(x: int) -> int:
        return x

    graph = Graph([writer])
    checkpoint = Checkpoint(
        values={"x": 1, "shared": 2},
        steps=[
            StepRecord(
                run_id="workflow",
                superstep=1,
                node_name="writer",
                index=7,
                status=StepStatus.COMPLETED,
                input_versions={"x": 1},
                values={"shared": 2},
            ),
            StepRecord(
                run_id="workflow",
                superstep=0,
                node_name="writer",
                index=2,
                status=StepStatus.COMPLETED,
                input_versions={"x": 1},
                values={"shared": 1},
            ),
        ],
    )

    state = initialize_state(graph, {}, checkpoint=checkpoint)

    assert state.node_executions["writer"].sequence == 7

    apply_node_result(
        graph,
        state,
        writer,
        {"shared": 3},
        {"x": 1},
        {},
        duration_ms=0.0,
        cached=False,
    )

    assert state.node_executions["writer"].sequence == 8


async def test_async_mutex_duplicate_outputs_feed_consumer_at_runtime() -> None:
    result = await AsyncRunner().run(producers_and_consumer_in_separate_subgraphs(), {"x": -1})

    assert result.status == RunStatus.COMPLETED
    assert result["final"] == "final:b:-1"


async def test_async_explicit_edge_missing_false_branch_does_not_leak_same_named_value() -> None:
    result = await AsyncRunner().run(explicit_edge_missing_false_branch(), {"x": -1})

    assert result.status == RunStatus.COMPLETED
    assert result["o1"] == "b:-1"
    assert "final" not in result.values
