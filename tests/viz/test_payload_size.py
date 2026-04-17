"""Regression tests that the rendered payload only contains the
(separate_outputs, show_inputs) variant requested by the caller.

This guards the notebook-size fix: before this change, render_graph would
emit nodesByState / edgesByState for every (sep, ext) combination, making
notebook cell output ~4x larger than needed. The JS side falls back to
initialData on toggle, and a Python re-render is the source of truth for
switching variants.
"""

from __future__ import annotations

from hypergraph import Graph, node
from hypergraph.viz.renderer import render_graph


@node(output_name="s0_out")
def _s0_step(x0: int) -> int:
    return x0


@node(output_name="s1_out")
def _s1_step(x1: int) -> int:
    return x1


@node(output_name="s2_out")
def _s2_step(x2: int) -> int:
    return x2


@node(output_name="s3_out")
def _s3_step(x3: int) -> int:
    return x3


@node(output_name="s4_out")
def _s4_step(x4: int) -> int:
    return x4


def _deeply_nested_graph() -> Graph:
    """Graph with several sibling subgraphs so every subgraph is an
    independent expandable container. N siblings -> 2**N valid expansion
    states, which is exactly where the old 4x multiplier would balloon
    payload size.
    """
    subs = [
        Graph(nodes=[_s0_step], name="sub_0"),
        Graph(nodes=[_s1_step], name="sub_1"),
        Graph(nodes=[_s2_step], name="sub_2"),
        Graph(nodes=[_s3_step], name="sub_3"),
        Graph(nodes=[_s4_step], name="sub_4"),
    ]
    return Graph(nodes=[sub.as_node() for sub in subs], name="root")


def _variant_keys_only(keys: set[str], sep: bool, ext: bool) -> bool:
    sep_tag = "sep:1" if sep else "sep:0"
    ext_tag = "ext:1" if ext else "ext:0"
    for key in keys:
        parts = key.split("|")
        # Legacy alias (only emitted when ext:1): "...|sep:X"
        if len(parts) == 2 and parts[1] == sep_tag and ext:
            continue
        # Full form: "...|sep:X|ext:Y" or "sep:X|ext:Y" when no expandables
        if len(parts) >= 2 and parts[-2] == sep_tag and parts[-1] == ext_tag:
            continue
        return False
    return True


def test_payload_contains_only_requested_variant_merged_with_inputs():
    graph = _deeply_nested_graph()
    data = render_graph(graph.to_flat_graph(), depth=0, separate_outputs=False, show_inputs=True)

    nodes_keys = set(data["meta"]["nodesByState"].keys())
    edges_keys = set(data["meta"]["edgesByState"].keys())

    assert nodes_keys == edges_keys
    assert _variant_keys_only(nodes_keys, sep=False, ext=True), nodes_keys
    # No keys from the other three (sep, ext) combinations should appear.
    for forbidden in ("sep:1|ext:1", "sep:0|ext:0", "sep:1|ext:0"):
        assert not any(key.endswith(forbidden) for key in nodes_keys)


def test_payload_contains_only_requested_variant_separate_without_inputs():
    graph = _deeply_nested_graph()
    data = render_graph(graph.to_flat_graph(), depth=0, separate_outputs=True, show_inputs=False)

    nodes_keys = set(data["meta"]["nodesByState"].keys())
    edges_keys = set(data["meta"]["edgesByState"].keys())

    assert nodes_keys == edges_keys
    assert _variant_keys_only(nodes_keys, sep=True, ext=False), nodes_keys
    # show_inputs=False must not emit the legacy key alias.
    assert not any(key.endswith("|sep:1") and "ext" not in key for key in nodes_keys)


def test_payload_state_count_matches_valid_expansion_states():
    """For ext:1 we emit one full-form key + one legacy alias per state."""
    from hypergraph.viz._common import enumerate_valid_expansion_states, get_expandable_nodes

    graph = _deeply_nested_graph()
    flat = graph.to_flat_graph()
    expandable = get_expandable_nodes(flat)
    valid = enumerate_valid_expansion_states(flat, expandable)

    data = render_graph(flat, depth=0, separate_outputs=False, show_inputs=True)
    nodes_keys = set(data["meta"]["nodesByState"].keys())

    # Each valid expansion state contributes 2 keys when show_inputs=True:
    # the full-form key and the legacy alias.
    assert len(nodes_keys) == 2 * len(valid)


def test_payload_size_vs_all_variants_baseline():
    """Directly measure that the payload is ~4x smaller than if we emitted
    every (sep, ext) combination. The comparison is vs a locally-constructed
    baseline so the test stays meaningful even if other payload fields grow.
    """
    import json

    from hypergraph.viz.renderer.precompute import precompute_all_edges, precompute_all_nodes

    graph = _deeply_nested_graph()
    flat = graph.to_flat_graph()
    input_spec = flat.graph.get("input_spec", {})

    single = render_graph(flat, depth=0, separate_outputs=False, show_inputs=True)
    single_bytes = len(json.dumps(single["meta"]["nodesByState"])) + len(json.dumps(single["meta"]["edgesByState"]))

    all_nodes: dict = {}
    all_edges: dict = {}
    for sep in (False, True):
        for ext in (False, True):
            n_by_state, _ = precompute_all_nodes(
                flat,
                input_spec,
                show_types=True,
                theme="auto",
                separate_outputs=sep,
                show_inputs=ext,
            )
            e_by_state, _ = precompute_all_edges(
                flat,
                input_spec,
                show_types=True,
                theme="auto",
                separate_outputs=sep,
                show_inputs=ext,
            )
            all_nodes.update(n_by_state)
            all_edges.update(e_by_state)

    all_bytes = len(json.dumps(all_nodes)) + len(json.dumps(all_edges))
    # Emitting all four (sep, ext) variants produces meaningfully more bytes.
    # Not quite 4x because ext:1 already emits a legacy-key alias, so the
    # single-variant payload already contains two keys per state.
    assert all_bytes > 2.5 * single_bytes, (single_bytes, all_bytes)
