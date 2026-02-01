"""Tests for bound parameters vs defaults in graph validation.

Tests that bound parameters are not incorrectly treated as defaults during
build-time validation, while still making parameters optional at runtime.
"""

import pytest
from hypergraph import Graph, node
from hypergraph.graph.validation import GraphConfigError


def test_bound_params_not_treated_as_defaults():
    """Bound parameters should not be treated as defaults during validation.

    When a graph is bound and used as a node, validation should not consider
    the bound parameters as having defaults. Only actual function signature
    defaults should trigger the consistency check.

    The key fix: A bound parameter in an inner graph is NOT a signature default,
    so it shouldn't trigger the "inconsistent defaults" validation error.
    """
    # Inner graph with bound parameter
    @node(output_name="result")
    def inner_func(x: int, config: str) -> str:
        return f"{x}:{config}"

    inner_graph = Graph([inner_func], name="inner").bind(config="bound_value")

    # Outer function with same param but NO default
    @node(output_name="final")
    def outer_func(result: str, config: str) -> str:
        return f"{result}:{config}"

    # Should NOT raise GraphConfigError about inconsistent defaults
    # config is bound in inner_graph, but that's not a "default"
    # This is the main test - it should not raise during graph construction
    outer_graph = Graph([inner_graph.as_node(), outer_func], name="outer")

    # config is now optional because the GraphNode has it bound (has_default_for returns True)
    # This allows the outer graph to optionally provide config for outer_func
    assert set(outer_graph.inputs.required) == {"x"}
    assert set(outer_graph.inputs.optional) == {"config"}

    # If we bind config at the outer level, it's still in bound dict
    outer_bound = outer_graph.bind(config="outer_config")
    assert set(outer_bound.inputs.required) == {"x"}
    assert "config" in outer_bound.inputs.bound


def test_actual_defaults_still_validated():
    """Validation should still catch inconsistent actual defaults."""
    @node(output_name="a")
    def node_with_default(x: int = 10) -> int:
        return x

    @node(output_name="b")
    def node_without_default(x: int) -> int:
        return x

    # Should still raise error for inconsistent actual defaults
    with pytest.raises(GraphConfigError, match="Inconsistent defaults"):
        Graph([node_with_default, node_without_default])


def test_nested_graph_with_bound_and_defaults():
    """Complex case: nested graph with both bound params and actual defaults."""
    @node(output_name="result")
    def func_with_default(x: int, y: int = 5) -> int:
        return x + y

    # Bind a parameter that has NO default in the function
    inner = Graph([func_with_default], name="inner").bind(x=10)

    @node(output_name="final")
    def outer_func(result: int, y: int = 5) -> int:  # y has same default
        return result + y

    # Should work: y has consistent defaults, x is bound (not a default)
    graph = Graph([inner.as_node(), outer_func])
    # Both x and y are optional: x is bound in inner, y has defaults
    assert set(graph.inputs.optional) == {"x", "y"}
    assert set(graph.inputs.required) == set()


def test_user_rag_example():
    """Reproduce the exact bug from the user's notebook example."""
    # Simplified version of the user's RAG example
    @node(output_name="answer")
    def rag_func(query: str, llm) -> str:
        return f"RAG answer for {query}"

    @node(output_name="judgment")
    def judge_answer(query: str, answer: str, expected_answer: str, llm) -> dict:
        return {"judgment": "good"}

    # RAG graph with bound llm
    rag_graph = Graph([rag_func], name="rag").bind(llm="llm_instance")

    # Evaluation graph - should NOT raise error
    # llm is bound in rag_graph (not a default), and judge_answer has no default for llm
    eval_graph = Graph(
        [rag_graph.as_node(name="rag"), judge_answer],
        name="evaluation",
    )

    # llm is optional because the rag GraphNode has it bound
    # The user can provide llm to override both nodes, or omit it to use rag's binding
    assert "llm" in eval_graph.inputs.optional
    assert "llm" not in eval_graph.inputs.required
