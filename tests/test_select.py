"""Tests for Graph.select() — default output selection."""

import pytest

from hypergraph.graph import Graph
from hypergraph.nodes.function import node
from hypergraph.runners.sync.runner import SyncRunner


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="sum")
def add(doubled: int, b: int) -> int:
    return doubled + b


@node(output_name="final")
def finalize(sum: int) -> str:
    return f"result={sum}"


def _build_graph():
    return Graph([double, add, finalize])


class TestSelectGraph:
    """Graph-level select behavior."""

    def test_selected_is_none_by_default(self):
        g = _build_graph()
        assert g.selected is None

    def test_select_returns_new_graph(self):
        g = _build_graph()
        g2 = g.select("final")
        assert g2 is not g
        assert g.selected is None
        assert g2.selected == ("final",)

    def test_select_validates_names(self):
        g = _build_graph()
        with pytest.raises(ValueError, match="not graph outputs"):
            g.select("nonexistent")

    def test_select_rejects_duplicates(self):
        g = _build_graph()
        with pytest.raises(ValueError, match="unique output names"):
            g.select("final", "final")

    def test_select_multiple_outputs(self):
        g = _build_graph().select("doubled", "final")
        assert g.selected == ("doubled", "final")

    def test_chained_select_last_wins(self):
        g = _build_graph().select("doubled").select("final")
        assert g.selected == ("final",)

    def test_outputs_property_unchanged(self):
        """select does not alter the outputs property."""
        g = _build_graph()
        g2 = g.select("final")
        assert g2.outputs == g.outputs


class TestSelectRunner:
    """Runner respects graph-level selection."""

    def test_run_returns_only_selected(self):
        g = _build_graph().select("final")
        result = SyncRunner().run(g, {"x": 5, "b": 3})
        assert set(result.values.keys()) == {"final"}

    def test_runtime_select_overrides_graph_default(self):
        g = _build_graph().select("final")
        result = SyncRunner().run(g, {"x": 5, "b": 3}, select=["doubled", "sum"])
        assert set(result.values.keys()) == {"doubled", "sum"}

    def test_no_select_returns_all(self):
        g = _build_graph()
        result = SyncRunner().run(g, {"x": 5, "b": 3})
        assert set(result.values.keys()) == {"doubled", "sum", "final"}


class TestSelectNested:
    """select controls what a GraphNode exposes to the parent graph."""

    def test_graph_node_exposes_only_selected(self):
        inner = Graph([double, add], name="inner").select("sum")
        gn = inner.as_node()
        assert gn.outputs == ("sum",)

    def test_graph_node_exposes_all_without_select(self):
        inner = Graph([double, add], name="inner")
        gn = inner.as_node()
        assert set(gn.outputs) == {"doubled", "sum"}

    def test_nested_graph_runs_select(self):
        inner = Graph([double, add], name="inner").select("sum")

        @node(output_name="formatted")
        def fmt(sum: int) -> str:
            return f"val={sum}"

        outer = Graph([inner.as_node(), fmt])
        result = SyncRunner().run(outer, {"x": 5, "b": 3})
        assert result.values["formatted"] == "val=13"

    def test_nested_graph_hides_unselected_from_parent(self):
        """Parent can't wire to an output the inner graph didn't select."""
        inner = Graph([double, add], name="inner").select("sum")

        @node(output_name="oops")
        def use_doubled(doubled: int) -> int:
            return doubled + 1

        # "doubled" is not exposed by inner — so use_doubled gets no edge.
        # It becomes a required input of the outer graph.
        outer = Graph([inner.as_node(), use_doubled])
        assert "doubled" in outer.inputs.required
