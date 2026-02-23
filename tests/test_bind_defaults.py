"""Tests for bound parameters vs defaults in graph validation.

Tests that bound parameters are not incorrectly treated as defaults during
build-time validation, while still making parameters optional at runtime.
"""

import threading

import pytest

from hypergraph import Graph, SyncRunner, node
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


def test_bound_value_overrides_signature_default():
    """Edge case: binding a parameter that already has a signature default.

    When a graph has a signature default (x=10) and then binds it to a different
    value (x=20), the bound value takes precedence. From a validation perspective,
    the GraphNode no longer exposes a signature default (it's now bound/configured).

    This means if another node requires x=10 as a signature default, validation
    will correctly flag an inconsistency, forcing the user to either:
    1. Remove the binding (expose the signature default)
    2. Bind at the outer graph level to satisfy both nodes
    """
    @node(output_name="result1")
    def func_with_default(x: int = 10) -> int:
        return x

    # Bind x to a different value, overriding the signature default
    inner = Graph([func_with_default], name="inner").bind(x=20)

    @node(output_name="result2")
    def outer_func(result1: int, x: int = 10) -> int:
        return result1 + x

    # Should FAIL validation: inner has x bound (no signature default exposed),
    # outer_func has x=10 as signature default - inconsistent!
    with pytest.raises(GraphConfigError, match="Inconsistent defaults"):
        Graph([inner.as_node(), outer_func])

    # Verify the inner graph node correctly reports it has NO signature default
    # (because it's bound, even though the inner function has a default)
    inner_node = inner.as_node()
    assert inner_node.has_default_for("x") is True  # Runtime: has value (bound)
    assert inner_node.has_signature_default_for("x") is False  # Validation: no signature default (bound)

    # The bound value should be accessible at runtime
    assert inner_node.get_default_for("x") == 20

    # Solution 1: Don't use both together (incompatible)
    # Solution 2: Use inner WITHOUT binding, so signature default is exposed
    inner_unbound = Graph([func_with_default], name="inner")
    graph_ok = Graph([inner_unbound.as_node(), outer_func])
    assert set(graph_ok.inputs.optional) == {"x"}  # Both have x=10 default


def test_user_rag_example_with_non_copyable_embedder():
    """Reproduce and fix the exact bug from the user's notebook.

    This tests the runtime issue: when a graph with bound non-copyable objects
    (like Embedder with RLock) is used as a node, the runner should NOT attempt
    to deep-copy those bound values.
    """
    class Embedder:
        """Simplified Embedder with non-copyable RLock."""
        def __init__(self):
            self._lock = threading.RLock()

        def embed(self, text: str) -> list[float]:
            return [0.1, 0.2, 0.3]

    @node(output_name="embedding")
    def embed_query(query: str, embedder: Embedder) -> list[float]:
        return embedder.embed(query)

    @node(output_name="result")
    def process(query: str, embedding: list[float]) -> str:
        return f"Processed {query}"

    embedder = Embedder()

    # Bind embedder in inner graph
    inner_graph = Graph([embed_query], name="retrieval").bind(embedder=embedder)

    # Use as node in outer graph
    outer_graph = Graph([inner_graph.as_node(), process], name="rag")

    # Should work without deep-copy errors
    runner = SyncRunner()
    result = runner.run(outer_graph, {"query": "test"})

    assert result["result"] == "Processed test"


def test_three_level_nested_binding():
    """Test that bound values propagate through three levels of nesting.

    Regression test: bound values from deeply nested graphs must be accessible
    at runtime via graph.inputs.bound, not just categorized as optional.

    Before fix: embedder was optional but not in eval_graph.inputs.bound
    After fix: embedder is in eval_graph.inputs.bound (propagated from inner graph)
    """
    class Embedder:
        def embed(self, text: str) -> list[float]:
            return [0.1, 0.2]

    class VectorStore:
        def search(self, embedding: list[float]) -> list[str]:
            return ["doc1"]

    class LLM:
        def generate(self, docs: list[str], query: str) -> str:
            return f"Answer: {query}"

    @node(output_name="embedding")
    def embed(text: str, embedder: Embedder) -> list[float]:
        return embedder.embed(text)

    @node(output_name="docs")
    def retrieve(embedding: list[float], vector_store: VectorStore) -> list[str]:
        return vector_store.search(embedding)

    @node(output_name="answer")
    def generate(docs: list[str], query: str, llm: LLM) -> str:
        return llm.generate(docs, query)

    @node(output_name="judgment")
    def judge(query: str, answer: str, expected_answer: str, llm: LLM) -> str:
        return f"Judged: {answer}"

    embedder = Embedder()
    vector_store = VectorStore()
    llm = LLM()

    # Level 1: retrieval graph with bound dependencies
    retrieval_graph = Graph(
        [embed, retrieve],
        name="retrieval",
    ).bind(embedder=embedder, vector_store=vector_store)

    # Level 2: RAG graph using retrieval as nested node
    rag_graph = Graph(
        [retrieval_graph.as_node(name="retrieval"), generate],
        name="rag",
    ).bind(llm=llm)

    # Level 3: evaluation graph using RAG as nested node
    eval_graph = Graph(
        [rag_graph.as_node(name="rag"), judge],
        name="evaluation",
    ).bind(llm=llm)

    # Verify bound values propagate through all levels
    assert "embedder" in retrieval_graph.inputs.bound
    assert "vector_store" in retrieval_graph.inputs.bound

    # Key assertion: bound values from retrieval_graph must appear in rag_graph
    assert "embedder" in rag_graph.inputs.bound
    assert "vector_store" in rag_graph.inputs.bound
    assert "llm" in rag_graph.inputs.bound

    # Key assertion: bound values must propagate to eval_graph
    assert "embedder" in eval_graph.inputs.bound
    assert "vector_store" in eval_graph.inputs.bound
    assert "llm" in eval_graph.inputs.bound

    # Runtime execution should work
    runner = SyncRunner()
    result = runner.run(
        eval_graph,
        {"text": "test query", "query": "test query", "expected_answer": "expected"},
    )

    assert result["judgment"] == "Judged: Answer: test query"
