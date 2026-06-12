"""Semantic parity harness — Python ``scene_builder.py`` vs JS ``scene_builder.js``.

For each fixture × expansion state × variant, both implementations must
produce the same node and edge signatures (after normalization). This is
the merge gate that catches structural drift before pixel parity (Stage 5)
is even worth running.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict
from itertools import product
from pathlib import Path
from typing import Any

import pytest

from hypergraph.viz.renderer.ir_builder import build_graph_ir
from hypergraph.viz.scene_builder import build_initial_scene
from tests.viz.conftest import (
    make_chain_graph,
    make_mapped_gate_graph,
    make_outer,
    make_simple_graph,
    make_workflow,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = Path(__file__).resolve().parent / "_parity_runner.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js not installed")


def _node_scene(ir: dict, opts: dict) -> dict:
    """Run the JS scene_builder via Node and return the resulting scene."""
    payload = json.dumps({"ir": ir, "opts": opts})
    proc = subprocess.run(
        [NODE, str(RUNNER), str(REPO_ROOT)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Node parity runner failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _node_signature(node: dict) -> tuple:
    data = node.get("data", {})
    return (
        node["id"],
        data.get("nodeType"),
        bool(node.get("hidden")),
        node.get("parentNode"),
        _semantic_node_data(data),
    )


def _semantic_node_data(data: dict) -> tuple:
    node_type = data.get("nodeType")
    if node_type == "INPUT":
        return (
            data.get("label"),
            data.get("typeHint"),
            data.get("isBound"),
            data.get("ownerContainer"),
            data.get("deepestOwnerContainer"),
            tuple(sorted(data.get("actualTargets") or ())),
        )
    if node_type == "INPUT_GROUP":
        return (
            tuple(data.get("params") or ()),
            tuple(data.get("paramTypes") or ()),
            data.get("isBound"),
            data.get("ownerContainer"),
            data.get("deepestOwnerContainer"),
            tuple(sorted(data.get("actualTargets") or ())),
        )
    return ()


def _edge_signature(edge: dict) -> tuple:
    data = edge.get("data", {})
    return (
        edge["id"],
        edge["source"],
        edge["target"],
        data.get("edgeType"),
        data.get("valueName"),
        data.get("label"),
        bool(data.get("exclusive")),
        bool(data.get("forceFeedback")),
        bool(edge.get("hidden")),
    )


def _project(scene: dict) -> tuple[set, set]:
    nodes = {_node_signature(n) for n in scene["nodes"]}
    edges = {_edge_signature(e) for e in scene["edges"]}
    return nodes, edges


def test_python_js_external_input_owner_container_matches() -> None:
    graph = make_workflow()
    ir = build_graph_ir(graph.to_flat_graph())
    expansion_state = {"preprocess": True}

    py_scene = build_initial_scene(ir, expansion_state=expansion_state)
    js_scene = _node_scene(
        asdict(ir),
        {
            "expansionState": expansion_state,
            "separateOutputs": False,
            "showInputs": True,
            "showBoundedInputs": False,
        },
    )

    py_input = next(n for n in py_scene["nodes"] if n["id"] == "input_text")
    js_input = next(n for n in js_scene["nodes"] if n["id"] == "input_text")

    assert js_input["data"].get("ownerContainer") == py_input["data"]["ownerContainer"] == "preprocess"


def _all_expansion_states(ir_dict: dict) -> list[dict]:
    """Enumerate the relevant subset of expansion states.

    Full Cartesian on `expandable_nodes` is fine for our test fixtures —
    they top out at 3 expandable containers (8 states). We rely on
    test_parity_smoke skipping the largest fixtures if needed.
    """
    expandable = list(ir_dict.get("expandable_nodes", []))
    states: list[dict] = []
    for bits in product([False, True], repeat=len(expandable)):
        states.append(dict(zip(expandable, bits, strict=True)))
    if not states:
        states = [{}]
    return states


def make_bound_graph():
    """A 2-node graph where one external param is bound at the graph level —
    exercises the ``ext.is_bound`` branch in both scene builders so the
    ``show_bounded_inputs`` flag has something to filter."""
    from hypergraph import Graph, node

    @node(output_name="scaled")
    def scale(x: int, factor: int) -> int:
        return x * factor

    @node(output_name="result")
    def report(scaled: int) -> int:
        return scaled

    return Graph(nodes=[scale, report]).bind(factor=10)


def make_unordered_nested_entrypoint_graph():
    """Nested entrypoint fixture whose real source is not first in child order."""
    from hypergraph import Graph, node

    @node(output_name="done")
    def downstream(started: int) -> int:
        return started + 1

    @node(output_name="started")
    def upstream(x: int) -> int:
        return x

    inner = Graph(nodes=[downstream, upstream], name="inner")
    return Graph(nodes=[inner.as_node()], entrypoint="inner")


FIXTURES = {
    "simple": make_simple_graph,
    "chain": make_chain_graph,
    "workflow": make_workflow,
    "outer": make_outer,
    "bound": make_bound_graph,
    "unordered_entrypoint": make_unordered_nested_entrypoint_graph,
    "mapped_gate": make_mapped_gate_graph,
}


@pytest.mark.parametrize("fixture_name", list(FIXTURES.keys()))
@pytest.mark.parametrize("separate_outputs", [False, True])
@pytest.mark.parametrize("show_inputs", [False, True])
@pytest.mark.parametrize("show_bounded_inputs", [False, True])
def test_python_js_scenes_match(fixture_name: str, separate_outputs: bool, show_inputs: bool, show_bounded_inputs: bool) -> None:
    graph = FIXTURES[fixture_name]()
    flat_graph = graph.to_flat_graph()
    ir = build_graph_ir(flat_graph)
    ir_dict: dict[str, Any] = asdict(ir)

    for expansion_state in _all_expansion_states(ir_dict):
        py_scene = build_initial_scene(
            ir,
            expansion_state=expansion_state,
            separate_outputs=separate_outputs,
            show_inputs=show_inputs,
            show_bounded_inputs=show_bounded_inputs,
        )
        js_scene = _node_scene(
            ir_dict,
            {
                "expansionState": expansion_state,
                "separateOutputs": separate_outputs,
                "showInputs": show_inputs,
                "showBoundedInputs": show_bounded_inputs,
            },
        )

        py_nodes, py_edges = _project(py_scene)
        js_nodes, js_edges = _project(js_scene)

        ctx = f"fixture={fixture_name} state={expansion_state} sep={separate_outputs} inputs={show_inputs} bounded={show_bounded_inputs}"
        assert py_nodes == js_nodes, (
            f"Node-set drift for {ctx}\nOnly in Python: {sorted(py_nodes - js_nodes)}\nOnly in JS:     {sorted(js_nodes - py_nodes)}"
        )
        assert py_edges == js_edges, (
            f"Edge-set drift for {ctx}\nOnly in Python: {sorted(py_edges - js_edges)}\nOnly in JS:     {sorted(js_edges - py_edges)}"
        )
