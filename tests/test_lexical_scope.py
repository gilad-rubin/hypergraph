"""Nested graph boundary addressing semantics."""

from __future__ import annotations

import pytest

from hypergraph import Graph, node
from hypergraph.exceptions import MissingInputError
from hypergraph.runners import SyncRunner


def test_flat_sibling_subgraphs_share_input_and_bind_by_default():
    @node(output_name="out_a")
    def use_a(overwrite: bool) -> str:
        return f"A:{overwrite}"

    @node(output_name="out_b")
    def use_b(overwrite: bool) -> str:
        return f"B:{overwrite}"

    inner_a = Graph([use_a], name="A").bind(overwrite=True)
    inner_b = Graph([use_b], name="B")
    outer = Graph([inner_a.as_node(), inner_b.as_node()], name="outer")

    assert outer.inputs.required == ()
    assert outer.inputs.bound == {"overwrite": True}

    result = SyncRunner().run(outer)
    assert result["out_a"] == "A:True"
    assert result["out_b"] == "B:True"


def test_namespaced_sibling_subgraphs_keep_same_input_separate():
    @node(output_name="out_a")
    def use_a(overwrite: bool) -> str:
        return f"A:{overwrite}"

    @node(output_name="out_b")
    def use_b(overwrite: bool) -> str:
        return f"B:{overwrite}"

    inner_a = Graph([use_a], name="A").bind(overwrite=True)
    inner_b = Graph([use_b], name="B")
    outer = Graph([inner_a.as_node(namespaced=True), inner_b.as_node(namespaced=True)], name="outer")

    assert outer.inputs.required == ("B.overwrite",)
    assert outer.inputs.bound == {"A.overwrite": True}

    result = SyncRunner().run(outer, {"B.overwrite": False})
    assert result["A.out_a"] == "A:True"
    assert result["B.out_b"] == "B:False"


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"A.overwrite": True, "B.overwrite": False}, id="port-address"),
        pytest.param({"A": {"overwrite": True}, "B": {"overwrite": False}}, id="nested-dict"),
    ],
)
def test_namespaced_runtime_values_accept_dot_path_or_nested_dict(values):
    @node(output_name="out_a")
    def use_a(overwrite: bool) -> str:
        return f"A:{overwrite}"

    @node(output_name="out_b")
    def use_b(overwrite: bool) -> str:
        return f"B:{overwrite}"

    inner_a = Graph([use_a], name="A")
    inner_b = Graph([use_b], name="B")
    outer = Graph([inner_a.as_node(namespaced=True), inner_b.as_node(namespaced=True)], name="outer")

    result = SyncRunner().run(outer, values)

    assert result["A.out_a"] == "A:True"
    assert result["B.out_b"] == "B:False"


def test_bare_name_satisfies_flat_nested_input():
    @node(output_name="out")
    def use_x(x: int) -> int:
        return x

    inner = Graph([use_x], name="inner")
    outer = Graph([inner.as_node()], name="outer")

    assert outer.inputs.required == ("x",)
    assert SyncRunner().run(outer, {"x": 5})["out"] == 5


def test_bare_name_does_not_satisfy_namespaced_nested_input():
    @node(output_name="out")
    def use_x(x: int) -> int:
        return x

    inner = Graph([use_x], name="inner")
    outer = Graph([inner.as_node(namespaced=True)], name="outer")

    assert outer.inputs.required == ("inner.x",)

    with pytest.warns(UserWarning, match="Not recognized"), pytest.raises(MissingInputError) as exc_info:
        SyncRunner().run(outer, {"x": 5})

    assert exc_info.value.missing == ["inner.x"]


def test_inner_bind_projects_flat_or_namespaced_by_boundary():
    @node(output_name="out")
    def use_x(x: int) -> int:
        return x

    inner = Graph([use_x], name="inner").bind(x=10)

    flat = Graph([inner.as_node()], name="flat")
    namespaced = Graph([inner.as_node(namespaced=True)], name="namespaced")

    assert flat.inputs.bound == {"x": 10}
    assert namespaced.inputs.bound == {"inner.x": 10}


def test_run_value_overriding_projected_bind_emits_warning():
    @node(output_name="out")
    def use_x(x: int) -> int:
        return x

    inner = Graph([use_x], name="inner").bind(x=10)
    outer = Graph([inner.as_node(namespaced=True)], name="outer")

    with pytest.warns(UserWarning, match=r"(?i)override.*inner\.x"):
        result = SyncRunner().run(outer, {"inner.x": 99})

    assert result["inner.out"] == 99


def test_rename_inputs_renames_local_name_then_boundary_projects_it():
    @node(output_name="inner_out")
    def consume_x(x: int) -> int:
        return x

    inner_graph = Graph([consume_x], name="inner")

    flat = Graph([inner_graph.as_node().rename_inputs(x="inner_x")], name="flat")
    namespaced = Graph([inner_graph.as_node(namespaced=True).rename_inputs(x="inner_x")], name="namespaced")

    assert flat.inputs.required == ("inner_x",)
    assert namespaced.inputs.required == ("inner.inner_x",)


def test_bind_with_dict_value_for_non_subgraph_key_passes_through_as_value():
    @node(output_name="out")
    def use_config(config: dict) -> dict:
        return config

    graph = Graph([use_config], name="g")
    bound = graph.bind(config={"key": "value"})

    assert bound.inputs.bound == {"config": {"key": "value"}}


def test_changed_namespaced_input_marks_graphnode_as_stale_on_replay():
    from hypergraph.runners._shared.helpers import _is_stale
    from hypergraph.runners._shared.types import GraphState, NodeExecution

    @node(output_name="doubled")
    def double(x: int) -> int:
        return x * 2

    inner = Graph([double], name="inner")
    outer = Graph([inner.as_node(name="embed", namespaced=True)], name="outer")
    embed_node = outer._nodes["embed"]

    state = GraphState()
    state.update_value("embed.x", 5)
    prior_exec = NodeExecution(
        node_name="embed",
        input_versions={"embed.x": 1},
        outputs={"embed.doubled": 10},
        output_versions={"embed.doubled": 1},
        wait_for_versions={},
    )
    state.node_executions["embed"] = prior_exec

    state.update_value("embed.x", 10)

    assert _is_stale(embed_node, outer, state, prior_exec) is True
