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

    # Inner graph (no bind on config -- with projected GraphNode boundaries, an inner bind on a
    # name the outer declares would be shadowed and is a build-time error).
    @node(output_name="result")
    def inner_func(x: int, config: str) -> str:
        return f"{x}:{config}"

    inner_graph = Graph([inner_func], name="inner")

    # Outer function with same param but NO default
    @node(output_name="final")
    def outer_func(result: str, config: str) -> str:
        return f"{result}:{config}"

    # Outer binds config at the scope where it's declared (auto-links into inner).
    outer_graph = Graph([inner_graph.as_node(), outer_func], name="outer").bind(config="bound_value")

    # x is projected as flat parent-facing key "x".
    assert set(outer_graph.inputs.required) == {"x"}
    assert "config" in outer_graph.inputs.bound


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
    # Both x and y are optional: x is bound inside inner, y has defaults.
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

    # RAG graph (no bind on llm -- judge_answer at eval also consumes llm, so a
    # bind here would be shadowed at the outer scope).
    rag_graph = Graph([rag_func], name="rag")

    # Bind llm once at eval scope where it's declared by both judge_answer and
    # (transitively) rag_func.
    eval_graph = Graph(
        [rag_graph.as_node(name="rag"), judge_answer],
        name="evaluation",
    ).bind(llm="llm_instance")

    # llm is now in bound (optional) at eval; required set excludes it.
    assert "llm" in eval_graph.inputs.bound
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

    # Level 2: RAG graph using retrieval as nested node. llm is declared at
    # rag (generate consumes it). We don't bind it here -- judge at eval also
    # consumes llm, so a bind here would be shadowed at the outer scope.
    rag_graph = Graph(
        [retrieval_graph.as_node(name="retrieval"), generate],
        name="rag",
    )

    # Level 3: evaluation graph binds llm once at the outermost scope where
    # both judge and (transitively) generate need it.
    eval_graph = Graph(
        [rag_graph.as_node(name="rag"), judge],
        name="evaluation",
    ).bind(llm=llm)

    # Verify bound values propagate through all levels
    assert "embedder" in retrieval_graph.inputs.bound
    assert "vector_store" in retrieval_graph.inputs.bound

    # Key assertion: bound values from retrieval_graph must appear in rag_graph
    # embedder/vector_store project through retrieval as flat parent-facing keys.
    assert "embedder" in rag_graph.inputs.bound
    assert "vector_store" in rag_graph.inputs.bound

    # Key assertion: bound values must propagate to eval_graph
    # embedder/vector_store continue projecting flat through rag.retrieval.
    assert "embedder" in eval_graph.inputs.bound
    assert "vector_store" in eval_graph.inputs.bound
    # llm bound at eval scope; auto-links to both judge.llm and rag.generate.llm.
    assert "llm" in eval_graph.inputs.bound

    # Runtime execution should work
    # text is projected through rag.retrieval (consumed by leaf 'embed' inside retrieval graph,
    # which is a GraphNode at rag scope and a sub-GraphNode at eval scope)
    runner = SyncRunner()
    result = runner.run(
        eval_graph,
        {"text": "test query", "query": "test query", "expected_answer": "expected"},
    )

    assert result["judgment"] == "Judged: Answer: test query"


def test_sibling_nested_bindings_do_not_leak():
    """Sibling GraphNodes should resolve their own bound values independently."""

    @node(output_name="out_a")
    def use_a(x: int, cfg: str) -> str:
        return f"A:{x}:{cfg}"

    @node(output_name="out_b")
    def use_b(x: int, cfg: str) -> str:
        return f"B:{x}:{cfg}"

    inner_a = Graph([use_a], name="inner_a").bind(cfg="CFG_A")
    inner_b = Graph([use_b], name="inner_b").bind(cfg="CFG_B")
    graph = Graph([inner_a.as_node(namespaced=True), inner_b.as_node(namespaced=True)], name="outer")

    assert "cfg" not in graph.inputs.bound
    assert graph.inputs.bound == {"inner_a.cfg": "CFG_A", "inner_b.cfg": "CFG_B"}

    result = SyncRunner().run(graph, {"inner_a.x": 1, "inner_b.x": 1})
    assert result["inner_a.out_a"] == "A:1:CFG_A"
    assert result["inner_b.out_b"] == "B:1:CFG_B"


def test_nested_bound_values_use_graphnode_public_input_names():
    """Outer InputSpec.bound should expose aliased GraphNode input names."""

    @node(output_name="result")
    def inner_func(x: int, cfg: str) -> str:
        return f"{x}:{cfg}"

    inner = Graph([inner_func], name="inner").bind(cfg="inner-cfg")
    nested = inner.as_node().with_inputs(cfg="public_cfg")
    outer = Graph([nested], name="outer")

    # public_cfg is projected to outer as flat parent-facing "public_cfg".
    assert "public_cfg" in outer.inputs.bound
    assert "cfg" not in outer.inputs.bound
    assert outer.inputs.bound["public_cfg"] == "inner-cfg"
