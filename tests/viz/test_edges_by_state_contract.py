"""Contract tests for edgesByState selection."""

from hypergraph.viz.renderer import render_graph
from tests.viz.conftest import make_outer


def _edge_signature(edge: dict) -> tuple[str, str, str, str]:
    data = edge.get("data", {})
    return (
        edge.get("source"),
        edge.get("target"),
        data.get("edgeType", ""),
        data.get("valueName", ""),
    )


def _state_key(expandable_nodes: list[str], expanded: bool, separate_outputs: bool) -> str:
    sep_key = "sep:1" if separate_outputs else "sep:0"
    if not expandable_nodes:
        return sep_key
    parts = [f"{node_id}:{1 if expanded else 0}" for node_id in expandable_nodes]
    return f"{','.join(parts)}|{sep_key}"


def test_edges_by_state_matches_depth_expanded_and_collapsed():
    """edgesByState should match render_graph outputs for depth 0 and full expansion."""
    graph = make_outer()

    for separate in (False, True):
        base = render_graph(graph.to_flat_graph(), depth=0, separate_outputs=separate)
        edges_by_state = base["meta"]["edgesByState"]
        expandable = base["meta"]["expandableNodes"]

        collapsed_key = _state_key(expandable, expanded=False, separate_outputs=separate)
        expanded_key = _state_key(expandable, expanded=True, separate_outputs=separate)

        collapsed_edges = edges_by_state[collapsed_key]
        expanded_edges = edges_by_state[expanded_key]

        collapsed_render = render_graph(graph.to_flat_graph(), depth=0, separate_outputs=separate)["edges"]
        expanded_render = render_graph(graph.to_flat_graph(), depth=2, separate_outputs=separate)["edges"]

        assert {_edge_signature(e) for e in collapsed_edges} == {
            _edge_signature(e) for e in collapsed_render
        }
        assert {_edge_signature(e) for e in expanded_edges} == {
            _edge_signature(e) for e in expanded_render
        }
