"""Lexical scope semantics for nested subgraph inputs (issue #94).

Under lexical scope, an input name that no ancestor scope declares is private
to its subgraph; outer addresses it via the dot-path ``subgraph.name``.

Each test in this module exercises one observable behavior end-to-end through
the public Graph / Runner API. Implementation details (the scope tree, the
old lift/promote machinery) are not asserted here.
"""

from __future__ import annotations

import pytest

from hypergraph import Graph, node
from hypergraph.exceptions import MissingInputError
from hypergraph.graph.validation import GraphConfigError
from hypergraph.runners import SyncRunner


def test_sibling_subgraphs_private_inputs_appear_as_dot_paths():
    """Two siblings sharing an input name are private to each subgraph.

    A bind on one sibling does not leak into the other's required set, and the
    outer ``inputs.required`` / ``inputs.bound`` address each via dot-path.
    """

    @node(output_name="out_a")
    def use_a(overwrite: bool) -> bool:
        return overwrite

    @node(output_name="out_b")
    def use_b(overwrite: bool) -> bool:
        return overwrite

    inner_a = Graph([use_a], name="A").bind(overwrite=True)
    inner_b = Graph([use_b], name="B")
    outer = Graph([inner_a.as_node(), inner_b.as_node()], name="outer")

    assert outer.inputs.required == ("B.overwrite",)
    assert outer.inputs.bound == {"A.overwrite": True}
    assert "overwrite" not in outer.inputs.required
    assert "overwrite" not in outer.inputs.bound


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"A.overwrite": True, "B.overwrite": False}, id="dot-path"),
        pytest.param({"A": {"overwrite": True}, "B": {"overwrite": False}}, id="nested-dict"),
    ],
)
def test_run_addresses_private_inputs_by_dot_path_or_nested_dict(values):
    """Dot-path and nested-dict forms route a value to the right subgraph.

    Both forms must produce identical results -- they are two surfaces over the
    same canonical addressing.
    """

    @node(output_name="out_a")
    def use_a(overwrite: bool) -> str:
        return f"A:{overwrite}"

    @node(output_name="out_b")
    def use_b(overwrite: bool) -> str:
        return f"B:{overwrite}"

    inner_a = Graph([use_a], name="A")
    inner_b = Graph([use_b], name="B")
    outer = Graph([inner_a.as_node(), inner_b.as_node()], name="outer")

    result = SyncRunner().run(outer, values)

    assert result["out_a"] == "A:True"
    assert result["out_b"] == "B:False"


def test_bind_shadowed_by_ancestor_leaf_consumer_is_build_time_error():
    """A bind inside a subgraph errors at build time when an ancestor scope
    declares the same name.

    Without the check, the parent's value would silently override the bind at
    run time -- the bind would look like a lock but be one. The validator
    fires at the outermost graph that surfaces the conflict and reports both
    the bind path and the ancestor scope that shadowed it.
    """

    @node(output_name="inner_out")
    def consume_x_inner(x: int) -> int:
        return x * 2

    @node(output_name="outer_out")
    def consume_x_outer(x: int) -> int:
        return x + 1

    inner = Graph([consume_x_inner], name="inner").bind(x=10)
    with pytest.raises(GraphConfigError, match="(?i)bind.*shadow|shadow.*bind|bind.*conflict"):
        Graph([consume_x_outer, inner.as_node()], name="outer")


def test_bind_with_no_ancestor_declaration_builds_cleanly():
    """A bind on a name nobody else declares is just a normal default and is
    silent (no warning, no error)."""

    @node(output_name="inner_out")
    def consume_x(x: int) -> int:
        return x * 2

    @node(output_name="outer_out")
    def consume_y(y: int) -> int:  # outer leaf consumes y, NOT x -- no conflict
        return y + 1

    inner = Graph([consume_x], name="inner").bind(x=10)
    outer = Graph([consume_y, inner.as_node()], name="outer")

    assert outer.inputs.bound == {"inner.x": 10}


def test_run_value_overriding_primitive_bind_emits_warning_with_values():
    """A run value that overrides a bound primitive emits a warning showing
    both the bound and the new value."""

    @node(output_name="out")
    def use_x(x: int) -> int:
        return x

    graph = Graph([use_x], name="g").bind(x=10)

    with pytest.warns(UserWarning, match=r"(?i)override.*x.*10.*42|override.*x.*42.*10"):
        result = SyncRunner().run(graph, {"x": 42})

    assert result["out"] == 42


def test_run_value_overriding_opaque_bind_emits_generic_warning():
    """A run value that overrides a bound non-primitive value emits a generic
    warning without dumping the value text."""

    class Opaque:
        def __repr__(self) -> str:
            return "<should-not-appear-in-warning>"

    @node(output_name="out")
    def use_x(x) -> str:
        return "ok"

    graph = Graph([use_x], name="g").bind(x=Opaque())

    with pytest.warns(UserWarning) as records:
        SyncRunner().run(graph, {"x": Opaque()})

    msgs = [str(w.message) for w in records]
    assert any("override" in m.lower() and "x" in m for m in msgs)
    assert not any("should-not-appear-in-warning" in m for m in msgs)


def test_run_with_no_override_emits_no_warning():
    """A run that doesn't override anything emits no warnings."""

    @node(output_name="out")
    def use_x(x: int) -> int:
        return x

    graph = Graph([use_x], name="g").bind(x=10)

    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        result = SyncRunner().run(graph)

    assert result["out"] == 10


def test_run_value_overriding_dot_pathed_bind_emits_warning():
    """Override warning fires uniformly for dot-path overrides (not just flat)."""

    @node(output_name="out")
    def use_x(x: int) -> int:
        return x

    inner = Graph([use_x], name="inner").bind(x=10)

    @node(output_name="other_out")
    def consume_unrelated(y: int = 0) -> int:
        return y

    outer = Graph([inner.as_node(), consume_unrelated], name="outer")

    with pytest.warns(UserWarning, match=r"(?i)override.*inner\.x"):
        result = SyncRunner().run(outer, {"inner.x": 99})

    assert result["out"] == 99


def test_with_inputs_renames_leaf_label_only_not_scope():
    """with_inputs(...) changes only the leaf label of an input's path; it
    does not move the input out of the subgraph's scope.

    Before: a bare-name with_inputs implicitly "lifted" the input. Under
    lexical scope the renamed input stays under the subgraph (the path is
    still ``<graphnode>.<new_label>``), and a same-named ancestor input
    remains independent.
    """

    @node(output_name="inner_out")
    def consume_x(x: int) -> int:
        return x

    @node(output_name="outer_out")
    def consume_x_outer(x: int) -> int:  # outer also has x; declared at outer
        return x + 1000

    inner_graph = Graph([consume_x], name="inner")
    # Rename inner's "x" -> "inner_x" via with_inputs on the GraphNode.
    inner_node = inner_graph.as_node().with_inputs(x="inner_x")

    outer = Graph([inner_node, consume_x_outer], name="outer")

    # The renamed input stays under the subgraph -- new leaf label only.
    assert "inner.inner_x" in outer.inputs.required
    # Outer's own x is still an independent flat input.
    assert "x" in outer.inputs.required
    # Bare "inner_x" at outer is NOT auto-linked: rename didn't move scope.
    assert "inner_x" not in outer.inputs.required


def test_no_warning_or_error_on_silent_bind_with_no_ancestor_declaration():
    """A bind on a name no ancestor declares is a normal default and is silent."""
    import warnings as _warnings

    @node(output_name="out")
    def use_x(x: int) -> int:
        return x

    inner = Graph([use_x], name="inner").bind(x=10)

    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        outer = Graph([inner.as_node()], name="outer")
        # Building the graph emits no warnings/errors -- bind is private and silent.
        assert outer.inputs.bound == {"inner.x": 10}


def test_missing_input_error_names_subgraph_for_dot_pathed_inputs():
    """MissingInputError text annotates each dot-pathed missing input with
    its owning subgraph, so the user knows where the input belongs."""

    @node(output_name="out")
    def use_x(x: int) -> int:
        return x

    inner = Graph([use_x], name="inner")
    outer = Graph([inner.as_node()], name="outer")

    with pytest.raises(MissingInputError) as exc_info:
        SyncRunner().run(outer, {})

    msg = str(exc_info.value)
    # The new annotation: "input 'x' of subgraph 'inner'".
    assert "'x' of subgraph 'inner'" in msg
    # The bind() hint, irrelevant to a lexical-scope miss, is gone.
    assert "graph.bind(x=10)" not in msg


def test_bind_conflict_error_names_scope_and_shadowing_node():
    """The bind-conflict GraphConfigError names the scope (graph) and the
    specific shadowing node (a leaf at this scope) -- not just the bare
    leaf name -- so the user can navigate to the source of the conflict."""

    @node(output_name="inner_out")
    def consume_x(x: int) -> int:
        return x

    @node(output_name="judge_out")
    def judge_consumes_x(x: int) -> int:  # leaf at outer that shadows the bind
        return x

    inner = Graph([consume_x], name="inner").bind(x=10)
    with pytest.raises(GraphConfigError) as exc_info:
        Graph([inner.as_node(), judge_consumes_x], name="evaluation")

    msg = str(exc_info.value)
    # Scope (graph name) named explicitly.
    assert "evaluation" in msg
    # Shadowing leaf node named explicitly.
    assert "judge_consumes_x" in msg
    # Original bind path still present.
    assert "inner.x" in msg


def test_override_warning_annotates_subgraph_for_dot_pathed_address():
    """The override UserWarning annotates the dot-pathed override target with
    its owning subgraph, matching the missing-input message style."""

    @node(output_name="out")
    def use_x(x: int) -> int:
        return x

    inner = Graph([use_x], name="inner").bind(x=10)
    outer = Graph([inner.as_node()], name="outer")

    with pytest.warns(UserWarning) as records:
        SyncRunner().run(outer, {"inner.x": 99})

    msgs = [str(w.message) for w in records]
    assert any("'x' of subgraph 'inner'" in m for m in msgs), msgs


def test_changed_dot_pathed_input_marks_graphnode_as_stale_on_replay():
    """A GraphNode whose private dot-pathed input changed must be seen as stale
    against its previous execution record.

    This locks in the invariant that the version recorded for a GraphNode
    input at execution time uses the same key the staleness check reads.
    Without alignment, _is_stale silently classifies a changed dot-pathed
    input as unchanged (both record and read default to 0).
    """
    from hypergraph.runners._shared.helpers import _is_stale
    from hypergraph.runners._shared.types import GraphState, NodeExecution

    @node(output_name="doubled")
    def double(x: int) -> int:
        return x * 2

    inner = Graph([double], name="inner")
    outer = Graph([inner.as_node(name="embed")], name="outer")
    embed_node = outer._nodes["embed"]

    # Simulate a prior run that consumed embed.x=5 (state v1, recorded v1).
    state = GraphState()
    state.update_value("embed.x", 5)
    # Record what an aligned superstep WOULD record for embed's prior exec.
    # The address used here must match the staleness-check lookup.
    prior_exec = NodeExecution(
        node_name="embed",
        input_versions={"embed.x": 1},  # aligned key
        outputs={"doubled": 10},
        output_versions={"doubled": 1},
        wait_for_versions={},
    )
    state.node_executions["embed"] = prior_exec

    # User provides a new value -- state version advances for the dotted key.
    state.update_value("embed.x", 10)
    assert state.versions["embed.x"] == 2

    # The staleness check must see embed's prior input as stale (consumed v1, current v2).
    assert _is_stale(embed_node, outer, state, prior_exec) is True


def test_bare_name_at_outer_resolving_only_to_private_input_errors():
    """Strict mode: a bare name passed to runner.run() that doesn't resolve at
    the call's scope errors instead of silently smart-routing to a deeper
    private input. The error names the actual expected dot-path."""

    @node(output_name="out")
    def use_x(x: int) -> int:
        return x

    inner = Graph([use_x], name="inner")
    outer = Graph([inner.as_node()], name="outer")

    # x is private to inner (no leaf at outer declares it). Bare 'x' must
    # error -- the user has to address it as 'inner.x' (or via nested-dict).
    # The validator emits a warning naming the expected path AND raises a
    # MissingInputError; both surface the same fact.
    with pytest.warns(UserWarning, match=r"inner\.x"), pytest.raises(MissingInputError, match=r"inner\.x"):
        SyncRunner().run(outer, {"x": 5})

    # And the legitimate dot-path works.
    result = SyncRunner().run(outer, {"inner.x": 5})
    assert result["out"] == 5


def test_bind_conflict_walks_descendants_fires_at_outermost():
    """A deeply-nested bind whose leaf name matches an ancestor declaration
    triggers a build-time error at the outermost graph that surfaces it."""

    @node(output_name="out")
    def use_x(x: int) -> int:
        return x

    @node(output_name="root_out")
    def consume_x_at_root(x: int) -> int:
        return x

    # Three-level nest: bind x at the innermost.
    inner = Graph([use_x], name="inner").bind(x=10)
    middle = Graph([inner.as_node()], name="middle")  # builds clean -- no x at middle

    # Sanity: middle holds the bind dot-pathed and didn't error.
    assert middle.inputs.bound == {"inner.x": 10}

    # Outermost scope declares x via a leaf -- the bind's leaf name `x` is
    # shadowed even though the addressing chain is `inner.x`.
    with pytest.raises(GraphConfigError, match=r"(?i)bind.*shadow|shadow.*bind|bind.*conflict"):
        Graph([middle.as_node(), consume_x_at_root], name="root")
