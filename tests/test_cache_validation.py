"""Tests for build-time validation of cache=True on disallowed node types."""

from __future__ import annotations

from hypergraph import END, Graph, node
from hypergraph.nodes.gate import route
from hypergraph.nodes.interrupt import interrupt
from tests._interrupt_questions import StringQuestion


class TestCacheValidationOnGates:
    """cache=True is allowed on GateNode — routing functions are cacheable."""

    def test_route_with_cache_builds_successfully(self):
        @node(output_name="x")
        def producer() -> int:
            return 1

        @route(targets=["producer", END], cache=True)
        def gate(x: int) -> str:
            return END

        # Should build without error
        graph = Graph([producer, gate], entrypoint="producer")
        assert graph is not None


class TestCacheValidationOnInterruptNode:
    """InterruptNode cache defaults to False but is configurable."""

    def test_interrupt_node_cache_default_false(self):
        @interrupt(answer_name="decision")
        def approval(draft: str) -> StringQuestion:
            return StringQuestion(prompt="Approve?", evidence=(draft,))

        assert approval.cache is False

    def test_interrupt_node_cache_configurable(self):
        @interrupt(answer_name="decision", cache=True)
        def approval(draft: str) -> StringQuestion:
            return StringQuestion(prompt="Approve?", evidence=(draft,))

        assert approval.cache is True

    def test_interrupt_node_cache_in_graph(self):
        """InterruptNode with cache=True builds without error."""

        @node(output_name="draft")
        def producer() -> str:
            return "hello"

        @interrupt(answer_name="decision", cache=True)
        def approval(draft: str) -> StringQuestion:
            return StringQuestion(prompt="Approve?", evidence=(draft,))

        graph = Graph([producer, approval])
        assert graph is not None


class TestCacheValidationOnGraphNode:
    """GraphNode.cache always returns False (not user-configurable)."""

    def test_graph_node_cache_is_false(self):
        @node(output_name="x")
        def inner_node(a: int) -> int:
            return a

        inner = Graph([inner_node], name="inner")
        gn = inner.as_node()
        # GraphNode should not have cache=True
        assert gn.cache is False
