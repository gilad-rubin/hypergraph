"""Tests for the Daft columnar execution engine."""

from __future__ import annotations

import pytest

pytest.importorskip("daft")

from hypergraph import Graph, node
from hypergraph.runners.daft.engine import (
    build_execution_plan,
    build_input_dataframe,
    execute_plan,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@node(output_name="y")
def add_one(x: int) -> int:
    return x + 1


@node(output_name="z")
def double_y(y: int) -> int:
    return y * 2


@node(output_name="w")
def add_yz(y: int, z: int) -> int:
    return y + z


@node(output_name="b")
def branch_a(x: int) -> int:
    return x + 10


@node(output_name="c")
def branch_b(x: int) -> int:
    return x + 20


# ---------------------------------------------------------------------------
# build_execution_plan
# ---------------------------------------------------------------------------


class TestBuildExecutionPlan:
    def test_linear_dag(self):
        """A → B → C produces 2 operations in order."""
        graph = Graph([add_one, double_y], name="linear")
        plan = build_execution_plan(graph, bound_values={})
        assert len(plan) == 2
        # First op processes 'add_one', second processes 'double_y'
        assert plan[0].node.name == "add_one"
        assert plan[1].node.name == "double_y"

    def test_diamond_dag(self):
        """A → [B, C] → D produces 3 operations."""
        graph = Graph([add_one, double_y, add_yz], name="diamond")
        plan = build_execution_plan(graph, bound_values={})
        assert len(plan) == 3
        # add_one must come before both double_y and add_yz
        names = [op.node.name for op in plan]
        assert names.index("add_one") < names.index("double_y")
        assert names.index("add_one") < names.index("add_yz")

    def test_parallel_branches(self):
        """Two independent branches from same input."""
        graph = Graph([branch_a, branch_b], name="parallel")
        plan = build_execution_plan(graph, bound_values={})
        assert len(plan) == 2


# ---------------------------------------------------------------------------
# execute_plan
# ---------------------------------------------------------------------------


class TestExecutePlan:
    def test_linear_produces_correct_columns(self):
        graph = Graph([add_one, double_y], name="linear")
        plan = build_execution_plan(graph, bound_values={})
        df = build_input_dataframe([{"x": 5}], ["x"])
        result_df = execute_plan(df, plan).collect()
        result = result_df.to_pydict()
        assert result["y"] == [6]
        assert result["z"] == [12]

    def test_diamond_produces_correct_columns(self):
        graph = Graph([add_one, double_y, add_yz], name="diamond")
        plan = build_execution_plan(graph, bound_values={})
        df = build_input_dataframe([{"x": 3}], ["x"])
        result_df = execute_plan(df, plan).collect()
        result = result_df.to_pydict()
        assert result["y"] == [4]
        assert result["z"] == [8]
        assert result["w"] == [12]  # 4 + 8


# ---------------------------------------------------------------------------
# build_input_dataframe
# ---------------------------------------------------------------------------


class TestBuildInputDataframe:
    def test_multiple_rows(self):
        variations = [{"x": 1, "y": "a"}, {"x": 2, "y": "b"}, {"x": 3, "y": "c"}]
        df = build_input_dataframe(variations, ["x", "y"]).collect()
        result = df.to_pydict()
        assert result["x"] == [1, 2, 3]
        assert result["y"] == ["a", "b", "c"]

    def test_single_row(self):
        df = build_input_dataframe([{"x": 42}], ["x"]).collect()
        result = df.to_pydict()
        assert result["x"] == [42]

    def test_empty_variations(self):
        df = build_input_dataframe([], ["x"]).collect()
        result = df.to_pydict()
        assert result["x"] == []
