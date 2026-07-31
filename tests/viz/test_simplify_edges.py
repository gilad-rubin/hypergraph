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

from hypergraph import END, Graph, ifelse, node, route
from hypergraph.viz.renderer.ir_builder import build_graph_ir, compute_container_transits, resolve_boundary_ports
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


def _disconnected_container_graph() -> Graph:
    """``side`` consumes ``doc`` at one child and emits ``stats`` from another,
    and the two never touch — so ``load → render`` is the ONLY route carrying
    the document, collapsed or not."""

    @node(output_name="doc")
    def load(path: str) -> str:
        return path

    @node(output_name="thumb")
    def make_thumb(doc: str) -> str:
        return doc

    @node(output_name="stats")
    def count_words(corpus: str) -> int:
        return 0

    @node(output_name="page")
    def render(stats: int, doc: str) -> str:
        return ""

    side = Graph([make_thumb, count_words], name="side")
    return Graph([load, side.as_node(), render], name="doc_pipeline")


def _passthrough_container_graph() -> Graph:
    """``prep`` genuinely carries the value across (strip_tags → tokenize), so
    ``fetch → summarize`` really is a shortcut in every state."""

    @node(output_name="raw")
    def fetch(url: str) -> str:
        return url

    @node(output_name="clean")
    def strip_tags(raw: str) -> str:
        return raw

    @node(output_name="tokens")
    def tokenize(clean: str) -> list:
        return []

    @node(output_name="report")
    def summarize(tokens: list, raw: str) -> str:
        return ""

    prep = Graph([strip_tags, tokenize], name="prep")
    return Graph([fetch, prep.as_node(), summarize], name="pipe")


class TestCollapsedContainersAreNotAssumedTransparent:
    """A collapsed container is drawn as one box, which tempts the walk to join
    every in-edge to every out-edge. That is false when the box does two
    unrelated jobs, and hiding a real edge behind a route that does not exist
    is worse than drawing one extra line."""

    def test_disconnected_container_does_not_hide_the_only_route(self) -> None:
        graph = _disconnected_container_graph()
        collapsed = visible_pairs(scene_for_state(graph, expansion_state={"side": False}, simplify=True))
        assert ("load", "render") in collapsed

    def test_answer_does_not_change_when_the_box_opens(self) -> None:
        """The edge must not blink in and out as the container is toggled —
        that flicker was the user-visible symptom."""
        graph = _disconnected_container_graph()
        collapsed = visible_pairs(scene_for_state(graph, expansion_state={"side": False}, simplify=True))
        expanded = visible_pairs(scene_for_state(graph, expansion_state={"side": True}, simplify=True))
        assert ("load", "render") in collapsed and ("load", "render") in expanded

    def test_real_passthrough_still_simplifies_when_collapsed(self) -> None:
        """The precision cuts both ways: a container that DOES carry the value
        must still justify dropping the shortcut, or simplify stops working."""
        graph = _passthrough_container_graph()
        collapsed = visible_pairs(scene_for_state(graph, expansion_state={"prep": False}, simplify=True))
        assert ("fetch", "summarize") not in collapsed
        assert collapsed == {("fetch", "prep"), ("prep", "summarize")}

    def test_nested_container_entry_resolves_to_the_direct_child(self) -> None:
        """``target_when_expanded`` names the DEEPEST consumer, but transits are
        recorded between direct children. Without walking the port back up, a
        two-level nest never matches a transit and nothing simplifies."""

        @node(output_name="seed")
        def seed_fn(n: int) -> int:
            return n

        @node(output_name="a1")
        def inner_a(seed: int) -> int:
            return seed

        @node(output_name="a2")
        def inner_b(a1: int) -> int:
            return a1

        @node(output_name="mid_out")
        def mid_tail(a2: int) -> int:
            return a2

        @node(output_name="final")
        def sink(mid_out: int, seed: int) -> int:
            return 0

        deep = Graph([inner_a, inner_b], name="deep")
        mid = Graph([deep.as_node(), mid_tail], name="mid")
        graph = Graph([seed_fn, mid.as_node(), sink], name="outer")
        collapsed = visible_pairs(scene_for_state(graph, expansion_state={"mid": False}, simplify=True))
        assert ("seed_fn", "sink") not in collapsed

    def test_single_child_container_still_counts_as_a_pass_through(self) -> None:
        """The reflexive ``[entry, exit]`` pair is load-bearing: when one child
        both consumes at the boundary and produces at it, the container really
        does carry the value across. Drop reflexive pairs as "pointless" and
        every single-child container silently becomes impassable."""

        @node(output_name="raw")
        def fetch(url: str) -> str:
            return url

        @node(output_name="clean")
        def scrub(raw: str) -> str:
            return raw

        @node(output_name="out")
        def finish(clean: str, raw: str) -> str:
            return ""

        graph = Graph([fetch, Graph([scrub], name="one").as_node(), finish], name="reflexive")
        collapsed = visible_pairs(scene_for_state(graph, expansion_state={"one": False}, simplify=True))
        assert collapsed == {("fetch", "one"), ("one", "finish")}

    def test_conditional_internal_route_is_not_a_pass_through(self) -> None:
        """The unconditional-path rule applies INSIDE a container too. ``box``
        reaches its exit only through one arm of an ifelse, so it must not
        license hiding the unconditional ``load → render``.

        Third instance of this bug class: control edges, then mutex arms in the
        top-level path graph, now mutex arms inside a container.
        """

        @node(output_name="doc")
        def load(path: str) -> str:
            return path

        @node(output_name="mid")
        def head(doc: str) -> str:
            return doc

        @ifelse(when_true="left", when_false="right")
        def pick(mid: str) -> bool:
            return True

        @node(output_name="tail")
        def left(mid: str) -> str:
            return mid

        @node(output_name="tail")
        def right(mid: str) -> str:
            return mid

        @node(output_name="page")
        def render(tail: str, doc: str) -> str:
            return ""

        box = Graph([head, pick, left, right], name="box", entrypoint="head")
        graph = Graph([load, box.as_node(), render], name="conditional")
        assert compute_container_transits(graph.to_flat_graph()).get("box") is None
        collapsed = visible_pairs(scene_for_state(graph, expansion_state={"box": False}, simplify=True))
        assert ("load", "render") in collapsed

    def test_ambiguous_exit_is_unresolved_not_the_first_producer(self) -> None:
        """Several deepest producers means no single child definitely emits the
        value. Picking ``producers[0]`` would assert a route that may not run,
        and would disagree with the scene builders, which treat any tuple
        ``*_when_expanded`` as unresolved."""

        @node(output_name="doc")
        def load(path: str) -> str:
            return path

        @node(output_name="mid")
        def head(doc: str) -> str:
            return doc

        @ifelse(when_true="left", when_false="right")
        def pick(mid: str) -> bool:
            return True

        @node(output_name="tail")
        def left(mid: str) -> str:
            return mid

        @node(output_name="tail")
        def right(mid: str) -> str:
            return mid

        @node(output_name="page")
        def render(tail: str, doc: str) -> str:
            return ""

        box = Graph([head, pick, left, right], name="box", entrypoint="head")
        flat = Graph([load, box.as_node(), render], name="amb").to_flat_graph()
        exit_port, _entry = resolve_boundary_ports(flat, "box", "render", ["tail"])
        assert exit_port is None

    def test_mermaid_agrees_with_the_scene_on_a_collapsed_container(self) -> None:
        """Third implementation, same answer — the text export and the widget
        must never disagree about which edges exist."""
        graph = _disconnected_container_graph()
        arrows = {
            tuple(line.strip().split(" --> "))
            for line in str(graph.to_mermaid(depth=0, show_types=False)).splitlines()
            if "-->" in line and "input_" not in line
        }
        scene = visible_pairs(scene_for_state(graph, expansion_state={"side": False}, simplify=True))
        assert arrows == scene


@node(output_name="pages")
def select_pages(query: str) -> str:
    return query


@node(output_name="answer")
def generate(query: str, pages: str) -> str:
    return query + pages


@node(output_name="verdict")
def judge(query: str, answer: str) -> str:
    return query + answer


@node(output_name="mark")
def grade(query: str, verdict: str) -> str:
    return query + verdict


def make_input_fanout_graph() -> Graph:
    """One input feeding an entire chain — the panda review shape.

    ``query`` enters all four nodes, but the chain
    ``select_pages → generate → judge → grade`` already carries it forward:
    a reader following the earliest edge reaches every later consumer.
    """
    return Graph([select_pages, generate, judge, grade], name="review")


class TestInputEdgesSimplify:
    """``simplify`` applies to INPUT pill edges: only the EARLIEST consumer(s)
    keep their edge when every later consumer is reachable downstream."""

    def test_input_fanout_keeps_only_the_earliest_consumer(self) -> None:
        scene = scene_for_state(make_input_fanout_graph(), simplify=True)
        assert visible_pairs(scene, edge_type="input") == {("input_query", "select_pages")}

    def test_input_fanout_survives_with_simplify_off(self) -> None:
        scene = scene_for_state(make_input_fanout_graph(), simplify=False)
        assert visible_pairs(scene, edge_type="input") == {
            ("input_query", "select_pages"),
            ("input_query", "generate"),
            ("input_query", "judge"),
            ("input_query", "grade"),
        }

    def test_input_edge_into_a_collapsed_box_is_implied_by_a_delivering_route(self) -> None:
        """``query → box`` disappears when ``query → select → box`` already
        delivers into the box. Reaching ANY in-port of a collapsed box counts:
        the drawn edge ends at the hull, so the reader's question is only
        whether the value reaches the box, not which inner node it enters."""

        @node(output_name="context")
        def format_ctx(pages: str) -> str:
            return pages

        @node(output_name="messages")
        def assemble(query: str, context: str) -> str:
            return query + context

        inner = Graph([format_ctx, assemble], name="inner")
        outer = Graph([select_pages, inner.as_node(name="inner")], name="outer")

        on = scene_for_state(outer, expansion_state={}, simplify=True)
        off = scene_for_state(outer, expansion_state={}, simplify=False)
        assert visible_pairs(off, edge_type="input") == {("input_query", "select_pages"), ("input_query", "inner")}
        assert visible_pairs(on, edge_type="input") == {("input_query", "select_pages")}

    def test_sole_route_into_a_box_survives(self) -> None:
        """An input edge that is the pill's only route anywhere is never
        dropped, even when the box has other in-edges."""

        @node(output_name="context")
        def format_ctx(pages: str) -> str:
            return pages

        @node(output_name="messages")
        def assemble(secret: str, context: str) -> str:
            return secret + context

        inner = Graph([format_ctx, assemble], name="inner")
        outer = Graph([select_pages, inner.as_node(name="inner")], name="outer")

        scene = scene_for_state(outer, expansion_state={}, simplify=True)
        assert ("input_secret", "inner") in visible_pairs(scene, edge_type="input")

    def test_control_delivery_does_not_imply_an_input_edge(self) -> None:
        """A gate's dotted arrow into a node means "may run", never "receives
        the value" — it cannot justify dropping the input edge."""

        @ifelse(when_true="deliver", when_false="archive")
        def check(query: str) -> bool:
            return bool(query)

        @node(output_name="delivered")
        def deliver(query: str) -> str:
            return query

        @node(output_name="archived")
        def archive(query: str) -> str:
            return query

        graph = Graph([check, deliver, archive], name="gated")
        scene = scene_for_state(graph, simplify=True)
        pairs = visible_pairs(scene, edge_type="input")
        assert ("input_query", "deliver") in pairs
        assert ("input_query", "archive") in pairs

    def test_expanded_view_reduces_over_the_real_inner_routes(self) -> None:
        """Once the container opens, the same rule runs on the inner nodes:
        both inner consumers sit downstream of ``select_pages`` (its ``pages``
        edge re-routes to ``format_ctx``, which feeds ``assemble``), so only
        the genuinely earliest consumer keeps its edge."""

        @node(output_name="context")
        def format_ctx(pages: str, query: str) -> str:
            return pages + query

        @node(output_name="messages")
        def assemble(query: str, context: str) -> str:
            return query + context

        inner = Graph([format_ctx, assemble], name="inner")
        outer = Graph([select_pages, inner.as_node(name="inner")], name="outer")

        scene = scene_for_state(outer, expansion_state={"inner": True}, simplify=True)
        assert visible_pairs(scene, edge_type="input") == {("input_query", "select_pages")}

    def test_expanded_earliest_inner_consumer_keeps_its_edge(self) -> None:
        """An inner consumer NOT reachable from any other consumer keeps its
        edge after expansion — earliest is judged on the open topology."""

        @node(output_name="context")
        def prepare(query: str) -> str:
            return query

        @node(output_name="messages")
        def assemble(query: str, context: str) -> str:
            return query + context

        inner = Graph([prepare, assemble], name="inner")
        outer = Graph([inner.as_node(name="inner")], name="outer")

        scene = scene_for_state(outer, expansion_state={"inner": True}, simplify=True)
        assert visible_pairs(scene, edge_type="input") == {("input_query", "inner/prepare")}


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

    def test_input_fanout_reduces_like_the_scene(self) -> None:
        graph = make_input_fanout_graph()
        arrows = self._arrows(graph)
        input_arrows = {pair for pair in arrows if pair[0].startswith("input_")}
        assert input_arrows == {("input_query", "select_pages")}
        assert self._arrows(graph) == visible_pairs(scene_for_state(graph, simplify=True), edge_type=None)

    def test_input_edge_into_a_collapsed_box_reduces_like_the_scene(self) -> None:
        graph = make_input_fanout_into_box_graph()
        arrows = self._arrows(graph)
        input_arrows = {pair for pair in arrows if pair[0].startswith("input_")}
        assert input_arrows == {("input_query", "select_pages")}

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

    @pytest.mark.parametrize("name", ["constructor", "toString", "__proto__"])
    def test_transit_lookup_ignores_inherited_object_members(self, name: str) -> None:
        """``containerTransits`` arrives straight from ``JSON.parse``, so it
        still inherits from ``Object.prototype``. A container whose name is a
        prototype member and that has *no* recorded transit then reads one off
        the prototype: ``transits['constructor']`` is the ``Object`` function,
        whose ``length`` is 1, so the loop indexes ``pairs[0][0]`` on
        ``undefined`` and the whole canvas goes blank. Every such name is a
        legal Python identifier and so a legal node id."""
        edges = [self._edge("a", name), self._edge(name, "c"), self._edge("a", "c")]
        script = (
            "const fs=require('fs');"
            f"eval(fs.readFileSync({str(REPO_ROOT / 'src/hypergraph/viz/assets/derivation.js')!r},'utf-8'));"
            f"eval(fs.readFileSync({str(REPO_ROOT / 'src/hypergraph/viz/assets/scene_builder.js')!r},'utf-8'));"
            f"const edges={json.dumps(edges)};"
            "const ports=Object.create(null);"
            f"ports[{f'a__{name}'!r}]={{entry:'p/in'}}; ports[{f'{name}__c'!r}]={{exit:'p/out'}};"
            f"const collapsed=Object.create(null); collapsed[{name!r}]=true;"
            # No transit recorded for this container -- the payload is about someone else.
            'const transits=JSON.parse(\'{"other": [["q/in","q/out"]]}\');'
            "const kept=globalThis.HypergraphSceneBuilder.simplifyTransitiveEdges(edges,"
            "{containerTransits:transits, edgePorts:ports, collapsedContainers:collapsed})"
            ".map(e=>e.source+'->'+e.target);"
            "process.stdout.write(JSON.stringify(kept));"
        )
        proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=15)
        assert proc.returncode == 0, proc.stderr
        kept = json.loads(proc.stdout)
        # No transit means no verified route through the box, so nothing is hidden.
        assert sorted(kept) == sorted([f"a->{name}", f"{name}->c", "a->c"])

    def test_transits_for_a_container_named_proto_are_read(self) -> None:
        """The other half: re-keying must not *lose* a real entry. ``__proto__``
        survives ``JSON.parse`` as an own property, so its transit is real data
        and the container is a genuine pass-through."""
        edges = [self._edge("a", "__proto__"), self._edge("__proto__", "c"), self._edge("a", "c")]
        script = (
            "const fs=require('fs');"
            f"eval(fs.readFileSync({str(REPO_ROOT / 'src/hypergraph/viz/assets/derivation.js')!r},'utf-8'));"
            f"eval(fs.readFileSync({str(REPO_ROOT / 'src/hypergraph/viz/assets/scene_builder.js')!r},'utf-8'));"
            f"const edges={json.dumps(edges)};"
            "const ports=Object.create(null);"
            "ports['a____proto__']={entry:'p/in'}; ports['__proto____c']={exit:'p/out'};"
            "const collapsed=Object.create(null); collapsed['__proto__']=true;"
            'const transits=JSON.parse(\'{"__proto__": [["p/in","p/out"]]}\');'
            "const kept=globalThis.HypergraphSceneBuilder.simplifyTransitiveEdges(edges,"
            "{containerTransits:transits, edgePorts:ports, collapsedContainers:collapsed})"
            ".map(e=>e.source+'->'+e.target);"
            "process.stdout.write(JSON.stringify(kept));"
        )
        proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=15)
        assert proc.returncode == 0, proc.stderr
        kept = json.loads(proc.stdout)
        # The transit was read, so the box is a pass-through and a->c is a shortcut.
        assert "a->c" not in kept
        assert sorted(kept) == ["__proto__->c", "a->__proto__"]

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


def make_input_fanout_into_box_graph() -> Graph:
    """``query`` feeds ``select_pages`` AND a container that ``select_pages``
    delivers into — the collapsed-box input-candidacy shape."""

    @node(output_name="context")
    def format_ctx(pages: str) -> str:
        return pages

    @node(output_name="messages")
    def assemble(query: str, context: str) -> str:
        return query + context

    inner = Graph([format_ctx, assemble], name="inner")
    return Graph([select_pages, inner.as_node(name="inner")], name="outer")


@pytest.mark.skipif(NODE is None, reason="Node.js not installed")
@pytest.mark.parametrize("simplify", [False, True])
@pytest.mark.parametrize("separate_outputs", [False, True])
@pytest.mark.parametrize("make_graph", [make_shortcut_graph, make_input_fanout_graph, make_input_fanout_into_box_graph])
def test_python_js_simplify_parity(simplify: bool, separate_outputs: bool, make_graph) -> None:
    """``assets/scene_builder.js`` must reduce identically to its Python twin."""
    ir = build_graph_ir(make_graph().to_flat_graph())
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
    if simplify and make_graph is make_input_fanout_graph:
        # Not merely equal — both twins actually cleaned the fan-out.
        assert visible_pairs(js_scene, edge_type="input") == {("input_query", "select_pages")}


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
        page.get_by_role("button", name="Simplify Edges").click()
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
