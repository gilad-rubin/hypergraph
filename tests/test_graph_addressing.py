"""Contract for ``hypergraph.graph.addressing`` (issue #246).

Input addressing used to live in ``graph/_helpers.py``, a module whose own
docstring claimed it was internal to ``core.py`` and ``input_spec.py`` while
``runners/_shared/`` imported it from four call sites. The three cross-package
functions now live in ``graph/addressing.py`` with a stated contract; this
module pins both halves of that move:

1. the boundary — nothing outside ``hypergraph.graph`` imports ``_helpers``;
2. the behavior — the user-visible strings these functions produce, which are
   inlined verbatim into missing-input errors, bind-override warnings, and
   nested-addressing failures.
"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path

import pytest

import hypergraph
from hypergraph import Graph, SyncRunner, node, route
from hypergraph.exceptions import MissingInputError
from hypergraph.graph import _helpers
from hypergraph.graph.addressing import (
    describe_addressed_input,
    flatten_subgraph_addressing,
    get_edge_produced_values,
)

SRC_ROOT = Path(hypergraph.__file__).parent
GRAPH_PACKAGE = SRC_ROOT / "graph"


def _helpers_importers() -> list[str]:
    """``file:line`` for every ``hypergraph.graph._helpers`` import outside ``graph/``."""
    offenders: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if GRAPH_PACKAGE in path.parents:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for stmt in ast.walk(tree):
            if isinstance(stmt, ast.ImportFrom):
                hit = (stmt.module or "") == "hypergraph.graph._helpers"
            elif isinstance(stmt, ast.Import):
                hit = any(alias.name == "hypergraph.graph._helpers" for alias in stmt.names)
            else:
                continue
            if hit:
                offenders.append(f"{path.relative_to(SRC_ROOT)}:{stmt.lineno}")
    return offenders


class TestPrivateHelpersStayPrivate:
    def test_no_module_outside_graph_imports_helpers(self):
        """Sensitivity: restoring any of the four pre-#246 ``runners/_shared``
        imports makes this fail naming that file and line."""
        offenders = _helpers_importers()
        assert offenders == [], (
            f"hypergraph.graph._helpers is imported from outside hypergraph/graph/ at {offenders}; "
            f"cross-package addressing helpers belong in hypergraph.graph.addressing (issue #246)."
        )

    def test_helpers_holds_only_the_graph_internal_function(self):
        """``_helpers`` is down to ``sources_of``, so its docstring's two-caller
        claim is now true. A promoted function reappearing here fails this."""
        public = {name for name in vars(_helpers) if not name.startswith("_") and callable(vars(_helpers)[name])}
        assert public == {"sources_of"}


class TestDescribeAddressedInput:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("x", "'x'"),
            ("inner.x", "'x' of subgraph 'inner'"),
            ("middle.inner.x", "'x' of subgraph 'middle.inner'"),
        ],
    )
    def test_format_is_byte_identical(self, path, expected):
        assert describe_addressed_input(path) == expected


class TestUserVisibleText:
    """The strings the three ``runners/_shared`` consumers build from these functions."""

    @staticmethod
    def _namespaced_outer() -> Graph:
        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        inner = Graph([double], name="inner")
        return Graph([inner.as_node(name="inner", namespaced=True)], name="outer")

    def test_missing_namespaced_input_names_its_subgraph(self):
        """``validation._build_missing_input_message`` consumer."""
        with pytest.raises(MissingInputError) as exc:
            SyncRunner().run(self._namespaced_outer())
        assert str(exc.value) == "Missing required inputs:\n  - 'x' of subgraph 'inner'  (address as 'inner.x')"

    def test_bind_override_warning_names_its_subgraph(self):
        """``value_resolution.warn_on_bind_overrides`` consumer."""
        bound = self._namespaced_outer().bind(**{"inner.x": 1})
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            SyncRunner().run(bound, values={"inner.x": 2})
        messages = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
        assert messages == ["Run value overrides bound value for 'x' of subgraph 'inner' (address 'inner.x'): 1 -> 2"]

    def test_unknown_nested_address_fails_identically_at_both_surfaces(self):
        """``input_normalization.normalize_inputs`` consumer, and the ``Graph.bind``
        surface it is documented to stay in lockstep with."""
        outer = self._namespaced_outer()
        expected = "Nested input 'inner.nope' is not a valid namespaced input address. Valid namespaced inputs: ['inner.x']"

        with pytest.raises(ValueError) as run_exc:
            SyncRunner().run(outer, values={"inner": {"nope": 3}})
        with pytest.raises(ValueError) as bind_exc:
            outer.bind(inner={"nope": 3})

        assert str(run_exc.value) == expected
        assert str(bind_exc.value) == expected

    def test_valid_nested_sugar_still_resolves_to_the_port_address(self):
        result = SyncRunner().run(self._namespaced_outer(), values={"inner": {"x": 3}})
        assert result.values == {"inner.doubled": 6}


class TestFlattenSubgraphAddressing:
    def test_flat_values_pass_through_untouched(self):
        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        graph = Graph([double])
        assert flatten_subgraph_addressing({"x": 1}, graph) == {"x": 1}


class TestGetEdgeProducedValues:
    def test_only_data_edges_contribute_values(self):
        """``validation.precompute_input_validation`` consumer: a gate's control
        edges define routing, not values, so they add nothing to the set."""

        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        @route(targets=["big", "small"])
        def pick(doubled: int) -> str:
            return "big" if doubled > 4 else "small"

        @node(output_name="label")
        def big(doubled: int) -> str:
            return "big"

        @node(output_name="other")
        def small(doubled: int) -> str:
            return "small"

        graph = Graph([double, pick, big, small])
        control_edges = [(u, v) for u, v, d in graph._nx_graph.edges(data=True) if d.get("edge_type") == "control"]
        assert control_edges, "graph must contain gate control edges for this test to mean anything"
        assert get_edge_produced_values(graph._nx_graph) == {"doubled"}
