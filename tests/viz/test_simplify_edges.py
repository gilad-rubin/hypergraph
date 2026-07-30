"""Shortcut-edge simplification in the scene builders.

``simplify=True`` (the default) hides data edges that take a short way past a
route the diagram already draws: with ``fetch → parse → render``, a direct
``fetch → render`` is a shortcut. Not "redundant" — ``render`` really does read
``fetch``'s output, and hiding that costs the reader a real fact. Only the
*ordering* is already carried by the longer route.

These tests pin the invariants that make the reduction safe to have on by
default — cycles survive, control/ordering semantics survive, and nothing gets
disconnected — plus Python ↔ JS parity for the new option.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from hypergraph import END, Graph, node, route
from hypergraph.viz.renderer.ir_builder import build_graph_ir
from hypergraph.viz.scene_builder import build_initial_scene, simplify_transitive_edges
from hypergraph.viz.widget import visualize
from tests.viz.conftest import (
    HAS_PLAYWRIGHT,
    extract_debug_edges,
    make_chain_graph,
    make_shortcut_graph,
    make_workflow,
    scene_for_state,
    wait_for_debug_ready,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = Path(__file__).resolve().parent / "_parity_runner.js"
NODE = shutil.which("node")


def visible_pairs(scene: dict, edge_type: str | None = "data") -> set[tuple[str, str]]:
    """Visible ``(source, target)`` pairs, optionally filtered to one edge type."""
    return {
        (edge["source"], edge["target"])
        for edge in scene["edges"]
        if not edge["hidden"] and (edge_type is None or (edge.get("data") or {}).get("edgeType") == edge_type)
    }


class TestShortcutRemoval:
    def test_direct_edge_dropped_when_chain_implies_it(self) -> None:
        scene = scene_for_state(make_shortcut_graph(), simplify=True)
        assert visible_pairs(scene) == {("fetch", "parse"), ("parse", "render")}

    def test_direct_edge_kept_when_simplify_off(self) -> None:
        scene = scene_for_state(make_shortcut_graph(), simplify=False)
        assert visible_pairs(scene) == {("fetch", "parse"), ("fetch", "render"), ("parse", "render")}

    def test_simplify_defaults_to_on(self) -> None:
        ir = build_graph_ir(make_shortcut_graph().to_flat_graph())
        assert visible_pairs(build_initial_scene(ir)) == visible_pairs(build_initial_scene(ir, simplify=True))

    def test_chain_without_shortcut_is_untouched(self) -> None:
        """A pure chain has no shortcuts — simplify must be a no-op."""
        graph = make_chain_graph()
        assert visible_pairs(scene_for_state(graph, simplify=True)) == visible_pairs(scene_for_state(graph, simplify=False))

    def test_separate_outputs_reduces_from_the_data_node(self) -> None:
        """In separate-outputs mode the shortcut starts at the DATA pill, so
        the reduction drops ``data_fetch_raw → render``, not ``fetch → render``."""
        scene = scene_for_state(make_shortcut_graph(), simplify=True, separate_outputs=True)
        assert visible_pairs(scene) == {("data_fetch_raw", "parse"), ("data_parse_parsed", "render")}

    def test_producer_to_data_edges_survive(self) -> None:
        """``output`` edges are structural, never candidates for removal, so no
        DATA pill is left dangling."""
        scene = scene_for_state(make_shortcut_graph(), simplify=True, separate_outputs=True)
        assert ("fetch", "data_fetch_raw") in visible_pairs(scene, edge_type="output")

    def test_two_hop_shortcut_is_dropped(self) -> None:
        """A shortcut is a shortcut over any path length, not just 2 hops."""

        @node(output_name="a_out")
        def a(seed: int) -> int:
            return seed

        @node(output_name="b_out")
        def b(a_out: int) -> int:
            return a_out

        @node(output_name="c_out")
        def c(b_out: int) -> int:
            return b_out

        @node(output_name="d_out")
        def d(c_out: int, a_out: int) -> int:
            return c_out + a_out

        scene = scene_for_state(Graph(nodes=[a, b, c, d], name="long"), simplify=True)
        assert visible_pairs(scene) == {("a", "b"), ("b", "c"), ("c", "d")}


class TestSafetyInvariants:
    def test_cycle_survives(self) -> None:
        """Every edge in a cycle is reachable "the long way round"; excluding
        back edges from the path graph is what stops simplify eating the loop."""

        @node(output_name="x")
        def a(z: int, seed: int) -> int:
            return z

        @node(output_name="y")
        def b(x: int) -> int:
            return x

        @node(output_name="z")
        def c(y: int, x: int) -> int:
            return y

        graph = Graph(nodes=[a, b, c], name="cyc", entrypoint="a")
        assert visible_pairs(scene_for_state(graph, simplify=True)) == {("a", "b"), ("b", "c"), ("c", "a")}

    def test_gated_data_edges_survive_a_control_detour(self) -> None:
        """``intake → verify`` must survive: the only other route to ``verify``
        is a dotted control edge, which shows scheduling, not the value."""

        @node(output_name="signal")
        def intake(request: str) -> str:
            return request

        @node(output_name="checked")
        def verify(signal: str) -> str:
            return signal

        @route(targets=["verify", END])
        def dispatch(signal: str, checked: str) -> str:
            return "verify"

        graph = Graph(nodes=[intake, dispatch, verify], name="gated", entrypoint="intake")
        scene = scene_for_state(graph, simplify=True)
        assert ("dispatch", "verify") in visible_pairs(scene, edge_type="control")
        assert ("intake", "verify") in visible_pairs(scene, edge_type="data")

    def test_every_visible_node_keeps_its_connections(self) -> None:
        """The reduction preserves reachability, so no visible node that had
        an edge ends up isolated."""
        graph = make_shortcut_graph()
        full = scene_for_state(graph, simplify=False)
        reduced = scene_for_state(graph, simplify=True)

        def touched(scene: dict) -> set[str]:
            pairs = visible_pairs(scene, edge_type=None)
            return {n for pair in pairs for n in pair}

        assert touched(reduced) == touched(full)

    def test_hidden_edges_are_preserved(self) -> None:
        """Hidden edges belong to collapsed scopes and are neither path
        segments nor removal candidates — expansion must find them intact."""
        graph = make_workflow()
        full = scene_for_state(graph, simplify=False)
        reduced = scene_for_state(graph, simplify=True)
        hidden_ids = {e["id"] for e in full["edges"] if e["hidden"]}
        assert hidden_ids <= {e["id"] for e in reduced["edges"]}


class TestSimplifyTransitiveEdgesUnit:
    def _edge(self, source: str, target: str, **data) -> dict:
        payload = {"edgeType": "data"}
        payload.update(data)
        return {"id": f"{source}__{target}", "source": source, "target": target, "data": payload, "hidden": False}

    def test_exclusive_arms_survive(self) -> None:
        """Two mutex branch arms feeding one consumer are alternatives, not a
        chain plus a shortcut."""
        edges = [
            self._edge("a", "b"),
            self._edge("b", "sink", exclusive=True),
            self._edge("a", "sink", exclusive=True),
        ]
        assert len(simplify_transitive_edges(edges)) == 3

    def test_exclusive_arm_cannot_imply_away_an_unconditional_edge(self) -> None:
        """An arm carries its value only when its branch is taken, so it must
        be barred from the path graph, not merely from the candidates.

        ``A ⇢ B`` (arm) + ``B → C`` must not hide an unconditional ``A → C``:
        on the other branch that shortcut is the only route to C.
        """
        edges = [
            self._edge("A", "B", exclusive=True),
            self._edge("B", "C"),
            self._edge("A", "C"),
        ]
        kept = {(e["source"], e["target"]) for e in simplify_transitive_edges(edges)}
        assert kept == {("A", "B"), ("B", "C"), ("A", "C")}

    def test_self_loop_survives(self) -> None:
        assert simplify_transitive_edges([self._edge("acc", "acc")]) == [self._edge("acc", "acc")]

    def test_control_hop_does_not_justify_dropping_a_data_edge(self) -> None:
        """A gate's dotted ``gate ⇢ worker`` means "worker may run", not "worker
        receives this value" — so it cannot stand in for ``gate → sink``.
        Dropping it would leave ``sink`` with no visible data source at all."""
        edges = [
            self._edge("gate", "worker", edgeType="control"),
            self._edge("worker", "sink"),
            self._edge("gate", "sink"),
        ]
        kept = {(e["source"], e["target"]) for e in simplify_transitive_edges(edges)}
        assert kept == {("gate", "worker"), ("worker", "sink"), ("gate", "sink")}

    def test_output_hop_does_justify_dropping_a_data_edge(self) -> None:
        """``producer ─output▶ DATA ─data▶ consumer`` is the separate-outputs
        spine: it is real value flow, so it does imply the shortcut."""
        edges = [
            self._edge("a", "data_a_x", edgeType="output"),
            self._edge("data_a_x", "b"),
            self._edge("a", "b"),
        ]
        kept = {(e["source"], e["target"]) for e in simplify_transitive_edges(edges)}
        assert kept == {("a", "data_a_x"), ("data_a_x", "b")}


class TestMermaidAlignment:
    """`AGENTS.md` requires Mermaid and the interactive viz to stay aligned, so
    ``simplify`` must reduce the same edges in both pipelines."""

    def _arrows(self, graph, **kwargs) -> set[tuple[str, str]]:
        source = str(graph.to_mermaid(**kwargs))
        pairs = set()
        for line in source.splitlines():
            if "-->" not in line or "linkStyle" in line:
                continue
            left, right = line.split("-->", 1)
            pairs.add((left.strip(), right.split("|")[-1].strip()))
        return pairs

    def test_shortcut_dropped_by_default(self) -> None:
        assert self._arrows(make_shortcut_graph()) == {("input_url", "fetch"), ("fetch", "parse"), ("parse", "render")}

    def test_shortcut_kept_when_simplify_off(self) -> None:
        assert ("fetch", "render") in self._arrows(make_shortcut_graph(), simplify=False)

    def test_matches_the_interactive_scene(self) -> None:
        graph = make_shortcut_graph()
        scene = visible_pairs(scene_for_state(graph, simplify=True), edge_type=None)
        assert self._arrows(graph) == scene

    def test_producer_to_data_edges_survive(self) -> None:
        arrows = self._arrows(make_shortcut_graph(), separate_outputs=True)
        assert ("fetch", "data_fetch_raw") in arrows
        assert ("data_fetch_raw", "render") not in arrows

    def test_cycle_survives(self) -> None:
        """Mermaid has no ``is_back_edge`` flag of its own — it calls
        ``find_back_edges`` so the loop is excluded from the path graph."""

        @node(output_name="x")
        def a(z: int, seed: int) -> int:
            return z

        @node(output_name="y")
        def b(x: int) -> int:
            return x

        @node(output_name="z")
        def c(y: int, x: int) -> int:
            return y

        arrows = self._arrows(Graph(nodes=[a, b, c], name="cyc", entrypoint="a"))
        assert {("a", "b"), ("b", "c"), ("c", "a")} <= arrows
        assert ("a", "c") not in arrows


@pytest.mark.skipif(NODE is None, reason="Node.js not installed")
class TestJsHardening:
    """Failure modes the JS twin can have but the Python one structurally
    cannot, so the shared parity fixtures would never surface them."""

    def _run(self, edges: list[dict]) -> dict:
        script = (
            "const fs=require('fs');"
            f"eval(fs.readFileSync({str(REPO_ROOT / 'src/hypergraph/viz/assets/derivation.js')!r},'utf-8'));"
            f"eval(fs.readFileSync({str(REPO_ROOT / 'src/hypergraph/viz/assets/scene_builder.js')!r},'utf-8'));"
            f"const edges={json.dumps(edges)};"
            "const kept=globalThis.HypergraphSceneBuilder.simplifyTransitiveEdges(edges)"
            ".map(e=>[e.source,e.target]);"
            "process.stdout.write(JSON.stringify({kept, polluted: ({}).__hg_probe !== undefined}));"
        )
        proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=15)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def _edge(self, source: str, target: str, **data: object) -> dict:
        payload: dict = {"edgeType": "data"}
        payload.update(data)
        return {"id": f"{source}__{target}", "source": source, "target": target, "data": payload, "hidden": False}

    def test_node_named_proto_does_not_crash_or_pollute(self) -> None:
        """``__proto__`` is a legal Python identifier, so it can reach the JS
        side as a node id. On a plain object literal the adjacency write throws
        and blanks the canvas; null-prototype maps keep it an ordinary key."""
        result = self._run(
            [
                self._edge("__proto__", "b"),
                self._edge("b", "c"),
                self._edge("__proto__", "c"),
                self._edge("x", "__hg_probe"),
            ]
        )
        assert not result["polluted"]
        assert ["__proto__", "c"] not in result["kept"]  # still correctly reduced
        assert ["__proto__", "b"] in result["kept"]

    def test_exclusive_arm_cannot_imply_away_an_unconditional_edge(self) -> None:
        """The JS twin must bar exclusive arms from the path graph too."""
        result = self._run(
            [
                self._edge("A", "B", exclusive=True),
                self._edge("B", "C"),
                self._edge("A", "C"),
            ]
        )
        assert sorted(result["kept"]) == [["A", "B"], ["A", "C"], ["B", "C"]]


@pytest.mark.skipif(NODE is None, reason="Node.js not installed")
@pytest.mark.parametrize("simplify", [False, True])
@pytest.mark.parametrize("separate_outputs", [False, True])
def test_python_js_simplify_parity(simplify: bool, separate_outputs: bool) -> None:
    """``assets/scene_builder.js`` must reduce identically to its Python twin."""
    ir = build_graph_ir(make_shortcut_graph().to_flat_graph())
    payload = json.dumps(
        {
            "ir": asdict(ir),
            "opts": {"expansionState": {}, "separateOutputs": separate_outputs, "simplify": simplify},
        }
    )
    proc = subprocess.run(
        [NODE, str(RUNNER), str(REPO_ROOT)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr

    py_scene = build_initial_scene(ir, separate_outputs=separate_outputs, simplify=simplify)
    js_scene = json.loads(proc.stdout)
    assert visible_pairs(js_scene, edge_type=None) == visible_pairs(py_scene, edge_type=None)


# =============================================================================
# Live widget — the toolbar toggle
# =============================================================================


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestToolbarToggle:
    """The toolbar button is the user-facing half of the feature: the scene must
    actually re-derive on click, in both directions."""

    def _visible_data_edges(self, page) -> set[tuple[str, str]]:
        return {(edge["source"], edge["target"]) for edge in extract_debug_edges(page) if (edge.get("data") or {}).get("edgeType") == "data"}

    def _click_simplify(self, page) -> None:
        version = page.evaluate("window.__hypergraphVizDebug.version")
        # One stable accessible name in both states — the button is a toggle,
        # not two buttons, so the label names the thing rather than the next action.
        page.get_by_role("button", name="Simplify Graph").click()
        page.wait_for_function(
            f"window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > {version} && window.__hypergraphVizReady === true",
            timeout=10000,
        )

    def test_button_round_trips_the_shortcut_edge(self, page, temp_html_file) -> None:
        visualize(make_shortcut_graph(), simplify=True, filepath=temp_html_file)
        page.goto(f"file://{temp_html_file}")
        wait_for_debug_ready(page)

        assert ("fetch", "render") not in self._visible_data_edges(page)

        self._click_simplify(page)  # simplify off — the shortcut comes back
        assert ("fetch", "render") in self._visible_data_edges(page)

        self._click_simplify(page)  # and back on
        assert ("fetch", "render") not in self._visible_data_edges(page)

    def test_python_default_reaches_the_browser(self, page, temp_html_file) -> None:
        """``simplify=False`` in Python must survive the HTML round-trip rather
        than being re-defaulted to True by the JS side."""
        visualize(make_shortcut_graph(), simplify=False, filepath=temp_html_file)
        page.goto(f"file://{temp_html_file}")
        wait_for_debug_ready(page)

        assert ("fetch", "render") in self._visible_data_edges(page)
