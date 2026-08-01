"""A routed interrupt's answer edge reads as "the answer comes back".

Panda's duplicate-review gate is the canonical shape: an ``@route`` gate sends
an unanswered case to an ``@interrupt``, and the declared edge
``(interrupt, gate)`` orders the cycle while the answer itself travels via
shared state. That edge is real and correct — the presentation was not: it
rendered as an anonymous dashed orbit, carrying no hint that it exists to
deliver ``duplicate_decision`` back to the gate that asked.

The rule under test: an ordering back edge whose source outputs ∩ target
inputs ∩ shared-state is non-empty is labeled with that value name, in the IR
(scene builders inherit it) and in the Mermaid export.
"""

from __future__ import annotations

from hypergraph import Graph, interrupt, node, route
from hypergraph.viz.renderer.ir_builder import build_graph_ir, shared_answer_label
from tests._interrupt_questions import StringQuestion

from .conftest import scene_for_state


@node(output_name="duplicates")
def find_duplicates(doc: str) -> list[int]:
    return []


@route(targets=["review", "ingest"])
def gate(duplicates: list[int], decision: str | None = None) -> str:
    return "review" if duplicates and decision is None else "ingest"


@interrupt(answer_name="decision")
def review(duplicates: list[int]) -> StringQuestion:
    return StringQuestion(prompt="Duplicate — proceed?")


@node(output_name="ingested")
def ingest(doc: str) -> str:
    return doc


def make_cycle_graph() -> Graph:
    return Graph(
        [find_duplicates, gate, review, ingest],
        edges=[(find_duplicates, gate), (review, gate)],
        shared=["decision"],
        name="dup_review",
        entrypoint="find_duplicates",
    )


def test_answer_back_edge_is_labeled_in_the_ir() -> None:
    ir = build_graph_ir(make_cycle_graph().to_flat_graph())

    (back,) = [e for e in ir.edges if e.source == "review" and e.target == "gate"]
    assert back.edge_type == "ordering"
    assert back.is_back_edge
    assert back.label == "decision"


def test_answer_label_reaches_the_scene_edge() -> None:
    scene = scene_for_state(make_cycle_graph())

    (back,) = [e for e in scene["edges"] if e["source"] == "review" and e["target"] == "gate"]
    assert back["data"]["label"] == "decision"
    assert back["data"]["forceFeedback"] is True


def test_answer_label_reaches_the_mermaid_export() -> None:
    mermaid = str(make_cycle_graph().to_mermaid())
    lines = [line.strip() for line in mermaid.splitlines()]

    assert "review -.->|decision| gate" in lines


def test_a_plain_ordering_back_edge_stays_unlabeled() -> None:
    """No shared answer between the two endpoints — nothing to claim."""

    @node(output_name="left")
    def a(x: str) -> str:
        return x

    @node(output_name="right")
    def b(left: str) -> str:
        return left

    graph = Graph([a, b], edges=[(a, b), (b, a)], name="plain_cycle", entrypoint="a")
    flat = graph.to_flat_graph()
    assert shared_answer_label("b", "a", flat) is None

    ir = build_graph_ir(flat)
    back_edges = [e for e in ir.edges if e.is_back_edge]
    assert back_edges and all(e.label is None for e in back_edges)
