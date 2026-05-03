"""Runtime coverage for mutex branch outputs with duplicate value names."""

from __future__ import annotations

import pytest

from hypergraph import AsyncRunner, Graph, RunStatus, SyncRunner, ifelse, node
from hypergraph.runners._shared.helpers import _has_input
from hypergraph.runners._shared.types import GraphState, NodeExecution


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
        (producers_and_consumer_in_separate_subgraphs, "producer_graph.x"),
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

    assert not _has_input("o1", consumer, graph, state)


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

    assert _has_input("o1", consumer, graph, state)


async def test_async_mutex_duplicate_outputs_feed_consumer_at_runtime() -> None:
    result = await AsyncRunner().run(producers_and_consumer_in_separate_subgraphs(), {"producer_graph.x": -1})

    assert result.status == RunStatus.COMPLETED
    assert result["final"] == "final:b:-1"


async def test_async_explicit_edge_missing_false_branch_does_not_leak_same_named_value() -> None:
    result = await AsyncRunner().run(explicit_edge_missing_false_branch(), {"x": -1})

    assert result.status == RunStatus.COMPLETED
    assert result["o1"] == "b:-1"
    assert "final" not in result.values
