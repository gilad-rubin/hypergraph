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
