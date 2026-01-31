"""Tests for build-time validation of cache=True on disallowed node types."""

from __future__ import annotations

import pytest

from hypergraph import Graph, node, END
from hypergraph.graph.validation import GraphConfigError
from hypergraph.nodes.gate import route
from hypergraph.nodes.interrupt import InterruptNode


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
        graph = Graph([producer, gate])
        assert graph is not None


class TestCacheValidationOnInterruptNode:
    """InterruptNode.cache always returns False (not user-configurable)."""

    def test_interrupt_node_cache_is_false(self):
        @node(output_name="draft")
        def producer() -> str:
            return "hello"

        interrupt = InterruptNode(
            name="approval",
            input_param="draft",
            output_param="decision",
        )
        # InterruptNode.cache always returns False, so validation won't fire.
        # But we test that the property is indeed False.
        assert interrupt.cache is False


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
