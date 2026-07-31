"""The scene must survive the REAL dagre layout, not merely look well-formed.

Every other viz test asserts on the scene payload. A scene can be perfectly
well-formed there — every edge endpoint present, every parent visible — and
still kill the widget the moment dagre runs, because dagre cannot route an
edge to a compound (parent) node: it throws ``Cannot set properties of
undefined (setting 'rank')`` and the diagram renders nothing.

That is exactly what a mounted HyperTable hit once it began expanding into
its recipe: the table's identity column is consumed by the CONTAINER rather
than by any inner node, so its input edge pointed at the container — fine
while collapsed, fatal once expanded. This module runs the vendored dagre
over real scenes so that class of break cannot reach a notebook again.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hypergraph import Graph, SyncRunner, node
from hypergraph.viz._common import build_expansion_state
from hypergraph.viz.renderer import render_graph

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = Path(__file__).resolve().parent / "_layout_runner.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js not installed")


def _layout(flat: Any, depth: int) -> dict[str, Any]:
    """Run performCompoundLayout over the scene at ``depth``."""
    scene = render_graph(flat, depth=depth, separate_outputs=False)
    payload = json.dumps(
        {
            "scene": {"nodes": scene["nodes"], "edges": scene["edges"]},
            "expansion": {key: bool(value) for key, value in build_expansion_state(flat, depth).items()},
        }
    )
    proc = subprocess.run(
        [NODE, str(RUNNER), str(REPO_ROOT)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"layout runner crashed: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout)


def _assert_lays_out(flat: Any, depth: int) -> None:
    result = _layout(flat, depth)
    assert result["ok"], f"dagre failed at depth={depth}: {result.get('error')}"
    assert not result["unpositioned"], f"nodes never got a position at depth={depth}: {result['unpositioned']}"
    assert result["laidOut"] > 0


# --- fixtures -------------------------------------------------------------


@node(output_name="clean_text")
def clean(text: str) -> str:
    return text.strip().lower()


@node(output_name="word_count")
def count_words(clean_text: str) -> int:
    return len(clean_text.split())


@node(output_name="summary")
def summarize(word_count: int) -> str:
    return f"{word_count} words"


def _table():
    from tests.test_materialization_node_viz import MemoryStore as Store

    return Graph([clean, count_words], name="recipe").as_table(
        identity="doc_id",
        store=Store(),
        runner=SyncRunner(),
    )


@pytest.mark.parametrize("depth", [0, 1, 2])
def test_mounted_table_lays_out(depth):
    """The #364 regression: expanding a table crashed the layout."""
    flat = Graph([_table().as_node(name="materialize_docs")], name="ingest").to_flat_graph()
    _assert_lays_out(flat, depth)


@pytest.mark.parametrize("depth", [0, 1, 2])
def test_mounted_table_beside_a_sibling_lays_out(depth):
    """The shape panda actually renders: a table plus downstream work."""

    @node(output_name="published")
    def publish(receipt: object) -> str:
        return str(receipt)

    flat = Graph([_table().as_node(name="materialize_docs"), publish], name="ingest").to_flat_graph()
    _assert_lays_out(flat, depth)


@pytest.mark.parametrize("depth", [0, 1, 2])
def test_nested_graph_container_still_lays_out(depth):
    """Guards the fix itself: ordinary containers must not regress."""
    inner = Graph([clean, count_words], name="inner")
    flat = Graph([inner.as_node(name="inner"), summarize], name="outer").to_flat_graph()
    _assert_lays_out(flat, depth)


@pytest.mark.parametrize("depth", [0, 1])
def test_flat_graph_lays_out(depth):
    """The control: a graph with no containers was never affected."""
    flat = Graph([clean, count_words, summarize], name="flat").to_flat_graph()
    _assert_lays_out(flat, depth)


def test_no_edge_targets_an_expanded_container():
    """The invariant behind the crash, asserted directly on the scene.

    dagre treats a node with children as a cluster and refuses edges to it,
    so no scene edge may point at a container that is currently expanded.
    """
    flat = Graph([_table().as_node(name="materialize_docs")], name="ingest").to_flat_graph()
    scene = render_graph(flat, depth=2, separate_outputs=False)
    expansion = build_expansion_state(flat, 2)
    parents = {n["id"]: n.get("parentNode") for n in scene["nodes"]}
    containers = {parent for parent in parents.values() if parent}
    expanded_containers = {c for c in containers if expansion.get(c)}

    offenders = [(edge["source"], edge["target"]) for edge in scene["edges"] if edge["target"] in expanded_containers and not edge["hidden"]]
    assert not offenders, f"edges point at expanded containers, which dagre cannot rank: {offenders}"


def test_merge_tolerates_ir_without_type_hints():
    """``type_hints`` has no length invariant; Python must not raise where JS renders.

    ``IRExternalInput.type_hints`` defaults to an empty tuple, and the JS twin
    reads a missing hint as null. Python raising on the same payload would be
    a divergence the parity harness cannot catch, because it only compares
    payloads both sides survive.
    """
    from hypergraph.viz.ir_schema import IRExternalInput
    from hypergraph.viz.scene_builder import _merge_inputs_for_state

    class _IR:
        external_inputs = (IRExternalInput(params=("alpha", "beta")),)
        container_entrypoints: dict = {}

    merged = _merge_inputs_for_state(_IR(), {}, {}, set(), show_bounded_inputs=True)

    assert len(merged) == 1
    assert merged[0]["params"] == ["alpha", "beta"]
    assert merged[0]["type_hints"] == [None, None]
    assert merged[0]["id"] == "input_group_alpha_beta"
