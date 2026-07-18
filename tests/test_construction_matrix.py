"""Construction-choice matrix (issue #208).

Graph construction makes two independent choices: whether data edges are
inferred from matching output/input names, and whether user-declared edges
are added. The four ``edges``/``shared`` combinations cover the matrix.
``edges=[]`` is the boundary case: it means "declared: no edges", not
"nothing declared", so it must disable inference exactly like a non-empty
declaration.

All assertions use the public graph surface (``nx_graph``, ``inputs``,
``has_explicit_edges``, ``explicit_predecessors``).
"""

import pytest

from hypergraph import Graph, GraphConfigError, node


@node(output_name=("a", "b"))
def produce(seed: int) -> tuple[int, int]:
    return seed, seed + 1


@node(output_name="marker")
def annotate(token: str) -> str:
    return token


@node(output_name="total")
def consume(a: int, b: int) -> int:
    return a + b


def _edges(graph: Graph) -> dict[tuple[str, str], tuple[str, tuple[str, ...]]]:
    """Snapshot of (source, target) -> (edge_type, value_names)."""
    return {(u, v): (d.get("edge_type"), tuple(d.get("value_names", []))) for u, v, d in graph.nx_graph.edges(data=True)}


class TestConstructionChoiceMatrix:
    """One test per edges/shared combination, plus the F1/F2 falsifiers."""

    def test_default_infers_name_matched_edges(self):
        """No edges, no shared: name matching wires producer to consumer."""
        g = Graph([produce, consume])
        assert _edges(g) == {("produce", "consume"): ("data", ("a", "b"))}
        assert g.inputs.required == ("seed",)
        assert g.has_explicit_edges is False
        assert g.explicit_predecessors == {}

    def test_empty_edges_disables_inference(self):
        """F1: same nodes, only edges=None -> edges=[] changes; inference must stop."""
        g = Graph([produce, consume], edges=[])
        assert _edges(g) == {}
        assert set(g.inputs.required) == {"seed", "a", "b"}
        assert g.has_explicit_edges is True

    def test_declared_edges_are_the_only_topology(self):
        """Explicit mode: declared edges land; name matches do not."""
        g = Graph([produce, annotate, consume], edges=[(annotate, consume)])
        assert _edges(g) == {("annotate", "consume"): ("ordering", ())}
        assert set(g.inputs.required) == {"seed", "token", "a", "b"}
        assert g.has_explicit_edges is True
        assert g.explicit_predecessors == {"consume": frozenset({"annotate"})}

    def test_shared_keeps_inference_for_non_shared_values(self):
        """Shared mode: inference stays on, shared values flow through state."""
        g = Graph([produce, consume], shared="b")
        assert _edges(g) == {("produce", "consume"): ("data", ("a",))}
        assert set(g.inputs.required) == {"seed", "b"}
        assert g.has_explicit_edges is False

    def test_shared_plus_declared_infers_and_adds(self):
        """F2: adding shared to a declared-edge graph turns inference back on."""
        g = Graph([produce, annotate, consume], shared="b", edges=[(annotate, consume)])
        assert _edges(g) == {
            ("produce", "consume"): ("data", ("a",)),
            ("annotate", "consume"): ("ordering", ()),
        }
        assert set(g.inputs.required) == {"seed", "token", "b"}
        assert g.has_explicit_edges is True
        assert g.explicit_predecessors == {"consume": frozenset({"annotate"})}


@node(output_name="x")
def left() -> int:
    return 1


@node(output_name="x")
def right() -> int:
    return 2


@node(output_name="done")
def use(x: int) -> int:
    return x


class TestConstructionChoiceDiagnostics:
    """F3: invalid graphs keep the exact per-mode diagnostics."""

    def test_unknown_source_exact_message(self):
        with pytest.raises(GraphConfigError, match=r"^Edge references unknown source node 'missing'$"):
            Graph([produce, consume], edges=[("missing", "consume")])

    def test_duplicate_producers_auto_diagnostic(self):
        with pytest.raises(GraphConfigError) as exc:
            Graph([left, right, use])
        assert str(exc.value) == (
            "Multiple nodes produce 'x'\n\n"
            "  -> left creates 'x'\n"
            "  -> right creates 'x'\n\n"
            "How to fix:\n"
            "  - Add ordering with emit/wait_for between the producers\n"
            "  - Or place them in exclusive gate branches"
        )

    def test_duplicate_producers_declared_diagnostic(self):
        with pytest.raises(GraphConfigError) as exc:
            Graph([left, right, use], edges=[(left, use), (right, use)])
        assert str(exc.value) == (
            "Multiple nodes produce 'x'\n\n"
            "  -> left creates 'x'\n"
            "  -> right creates 'x'\n\n"
            "How to fix:\n"
            "  - Add an edge between the producers to declare ordering\n"
            "  - Or place them in exclusive gate branches"
        )

    def test_shared_plus_declared_keeps_auto_conflict_diagnostic(self):
        """Declared edges do not select explicit conflict rules while inference is on."""
        with pytest.raises(GraphConfigError) as exc:
            Graph(
                [left, right, use],
                shared="done",
                edges=[(left, use), (right, use)],
            )
        assert str(exc.value) == (
            "Multiple nodes produce 'x'\n\n"
            "  -> left creates 'x'\n"
            "  -> right creates 'x'\n\n"
            "How to fix:\n"
            "  - Add ordering with emit/wait_for between the producers\n"
            "  - Or place them in exclusive gate branches"
        )
