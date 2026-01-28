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


def test_edges_by_state_expanded_skips_container_data_nodes():
    """Expanded edgesByState should not reference container DATA nodes."""
    graph = make_outer()

    base = render_graph(graph.to_flat_graph(), depth=0, separate_outputs=True)
    edges_by_state = base["meta"]["edgesByState"]
    expandable = base["meta"]["expandableNodes"]

    expanded_key = _state_key(expandable, expanded=True, separate_outputs=True)
    expanded_edges = edges_by_state[expanded_key]

    container_ids = {
        n["id"]
        for n in base["nodes"]
        if n.get("data", {}).get("nodeType") == "PIPELINE"
    }
    container_data_nodes = {
        n["id"]
        for n in base["nodes"]
        if n.get("data", {}).get("nodeType") == "DATA"
        and n.get("data", {}).get("sourceId") in container_ids
    }

    assert container_data_nodes, "Expected container DATA nodes in base render"
    for edge in expanded_edges:
        assert edge.get("source") not in container_data_nodes
        assert edge.get("target") not in container_data_nodes


def test_nodes_by_state_hides_container_data_when_expanded():
    """Expanded nodesByState should hide container DATA nodes."""
    graph = make_outer()

    base = render_graph(graph.to_flat_graph(), depth=0, separate_outputs=True)
    nodes_by_state = base["meta"]["nodesByState"]
    expandable = base["meta"]["expandableNodes"]

    expanded_key = _state_key(expandable, expanded=True, separate_outputs=True)
    expanded_nodes = nodes_by_state[expanded_key]

    container_ids = {
        n["id"]
        for n in expanded_nodes
        if n.get("data", {}).get("nodeType") == "PIPELINE"
    }
    container_data_nodes = [
        n for n in expanded_nodes
        if n.get("data", {}).get("nodeType") == "DATA"
        and n.get("data", {}).get("sourceId") in container_ids
    ]

    assert container_data_nodes, "Expected container DATA nodes in expanded state"
    assert all(n.get("hidden") is True for n in container_data_nodes)


def test_nodes_by_state_hides_data_nodes_in_merged_mode():
    """Merged mode nodesByState should hide DATA nodes."""
    graph = make_outer()

    base = render_graph(graph.to_flat_graph(), depth=0, separate_outputs=False)
    nodes_by_state = base["meta"]["nodesByState"]
    expandable = base["meta"]["expandableNodes"]

    collapsed_key = _state_key(expandable, expanded=False, separate_outputs=False)
    collapsed_nodes = nodes_by_state[collapsed_key]

    data_nodes = [
        n for n in collapsed_nodes
        if n.get("data", {}).get("nodeType") == "DATA"
    ]

    assert data_nodes, "Expected DATA nodes in merged mode state"
    assert all(n.get("hidden") is True for n in data_nodes)
