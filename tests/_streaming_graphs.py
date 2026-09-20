"""Nested streaming graphs shared by the chunk-routing tests (issue #110, #451).

One definition, so `tests/test_stop_and_stream.py` and `tests/test_map_iter.py`
assert the same six-route invariant against the same graph instead of two
copies that can drift.
"""

from __future__ import annotations

from hypergraph import Graph, GraphNode, NodeContext, node


def drafting_graph() -> Graph:
    """Child graph that streams from two different nodes."""

    @node(output_name="draft")
    def streamer(topic: str, ctx: NodeContext) -> str:
        ctx.stream(f"drafting:streamer:{topic}")
        return topic

    @node(output_name="drafted")
    def polisher(draft: str, ctx: NodeContext) -> str:
        ctx.stream(f"drafting:polisher:{draft}")
        return draft

    return Graph([streamer, polisher], name="drafting")


def summary_graph() -> Graph:
    """Sibling child graph whose streaming node shares the local name 'streamer'."""

    @node(output_name="summarized")
    def streamer(topic: str, ctx: NodeContext) -> str:
        ctx.stream(f"summary:streamer:{topic}")
        return topic

    return Graph([streamer], name="summary")


def nested_streaming_graph() -> Graph:
    """Parent graph with two nested GraphNodes, both streaming."""
    return Graph(
        [
            GraphNode(drafting_graph(), name="left"),
            GraphNode(summary_graph(), name="right"),
        ],
        name="outer",
    )
