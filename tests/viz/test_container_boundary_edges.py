"""Boundary fidelity: expanding or collapsing a container must never LOSE an edge.

Two real losses, both first seen on panda's review_generation graph (a nested
generation graph whose ``@ifelse`` guard and nested messages graph BOTH consume
the same incoming ``document``):

1. An edge into an expanded container was re-routed to only ONE internal
   consumer — the deepest — so every other internal consumer (the guard)
   rendered with no incoming edge at any depth.
2. The single re-route target could itself sit inside a still-collapsed inner
   container; the rewritten edge then pointed at a hidden node and vanished
   entirely, so the expanded view showed LESS than the collapsed one.

An outer input consumed only inside a collapsed container has the same
obligation from the pill side: the pill hoists to the deepest visible ancestor
and its edge aggregates to the container boundary — it must not vanish with
the container's internals.
"""

from __future__ import annotations

from hypergraph import Graph, ifelse, node
from tests.viz.conftest import scene_for_state


@node(output_name="document")
def pick(raw: str) -> str:
    return raw


@node(output_name="context")
def format_ctx(document: str) -> str:
    return document


@node(output_name="messages")
def assemble(context: str, query: str) -> list[str]:
    return [context, query]


@node(output_name="answer")
def respond(messages: list[str]) -> str:
    return messages[0]


@node(output_name="answer")
def canned(note: str = "see figures") -> str:
    return note


@ifelse(when_true="canned", when_false="build_messages")
def gate(document: str) -> bool:
    return len(document) > 3


def _generation() -> Graph:
    msgs = Graph([format_ctx, assemble], name="msgs")
    gen = Graph([gate, canned, msgs.as_node(name="build_messages"), respond], name="gen").select("answer")
    return Graph([pick, gen.as_node(name="gen")], name="outer")


def _visible(scene: dict) -> list[tuple[str, str]]:
    return [(e["source"], e["target"]) for e in scene["edges"] if not e["hidden"]]


def _pill_for(scene: dict, param: str) -> str | None:
    """The visible INPUT/INPUT_GROUP pill carrying ``param``, whatever the
    per-state grouping merged it with."""
    for scene_node in scene["nodes"]:
        if scene_node["hidden"]:
            continue
        data = scene_node["data"]
        if data.get("nodeType") == "INPUT" and data.get("label") == param:
            return scene_node["id"]
        if data.get("nodeType") == "INPUT_GROUP" and param in data.get("params", ()):
            return scene_node["id"]
    return None


class TestEdgeIntoExpandedContainerReachesEveryConsumer:
    def test_gate_receives_the_boundary_value_when_expanded(self):
        """The E.2 shape: the @ifelse guard consumed ``document`` and drew nothing."""
        scene = scene_for_state(_generation(), expansion_state={"gen": True}, simplify=False)

        assert ("pick", "gen/gate") in _visible(scene), f"the guard consumes 'document'; visible: {sorted(_visible(scene))}"

    def test_rewritten_edge_resolves_to_the_collapsed_inner_container(self):
        """The deepest consumer sits inside collapsed ``build_messages``; the
        edge must aggregate to that boundary, not vanish with it."""
        scene = scene_for_state(_generation(), expansion_state={"gen": True}, simplify=False)

        assert ("pick", "gen/build_messages") in _visible(scene), f"'document' also feeds the messages graph; visible: {sorted(_visible(scene))}"

    def test_fully_expanded_reaches_the_deepest_consumer(self):
        scene = scene_for_state(_generation(), expansion_state={"gen": True, "gen/build_messages": True}, simplify=False)

        visible = _visible(scene)
        assert ("pick", "gen/gate") in visible
        assert ("pick", "gen/build_messages/format_ctx") in visible

    def test_collapsed_view_is_unchanged(self):
        scene = scene_for_state(_generation(), expansion_state={}, simplify=False)

        assert ("pick", "gen") in _visible(scene)

    def test_no_duplicate_edges_when_consumers_share_a_visible_ancestor(self):
        """Two deep consumers resolving to one collapsed box = one edge."""

        @node(output_name="context2")
        def also_reads(document: str) -> str:
            return document

        msgs = Graph([format_ctx, also_reads, assemble], name="msgs")
        gen = Graph([gate, canned, msgs.as_node(name="build_messages"), respond], name="gen").select("answer")
        outer = Graph([pick, gen.as_node(name="gen")], name="outer")

        scene = scene_for_state(outer, expansion_state={"gen": True}, simplify=False)
        ids = [e["id"] for e in scene["edges"] if not e["hidden"]]
        assert len(ids) == len(set(ids)), f"duplicate edge ids: {sorted(ids)}"
        hits = [pair for pair in _visible(scene) if pair == ("pick", "gen/build_messages")]
        assert len(hits) == 1, f"expected exactly one aggregated edge, got {len(hits)}"


class TestMermaidTwinAgrees:
    """The text export resolves the same boundaries as the widget."""

    def test_mermaid_fans_out_to_every_consumer(self):
        from hypergraph.viz.mermaid import to_mermaid

        text = str(to_mermaid(_generation().to_flat_graph(), depth=1, simplify=False))
        assert "pick --> gen__gate" in text
        assert "pick --> gen__build_messages" in text

    def test_mermaid_fully_expanded_reaches_the_deepest_consumer(self):
        from hypergraph.viz.mermaid import to_mermaid

        text = str(to_mermaid(_generation().to_flat_graph(), depth=2, simplify=False))
        assert "pick --> gen__gate" in text
        assert "pick --> gen__build_messages__format_ctx" in text


class TestInternallyFedConsumersAreExcluded:
    """A consumer already fed by an INTERNAL producer of the same name must
    not receive the boundary edge — in the IR and in Mermaid alike."""

    @staticmethod
    def _mixed_flat_graph():
        import networkx as nx

        flat = nx.DiGraph()
        flat.add_node("pick", node_type="FUNCTION", outputs=("document",), inputs=("raw",))
        flat.add_node("box", node_type="GRAPH", outputs=(), inputs=("document",))
        flat.add_node("box/gate", node_type="FUNCTION", parent="box", inputs=("document",), outputs=("ok",))
        flat.add_node("box/maker", node_type="FUNCTION", parent="box", inputs=(), outputs=("document",))
        flat.add_node("box/user", node_type="FUNCTION", parent="box", inputs=("document",), outputs=("used",))
        flat.add_edge("pick", "box", edge_type="data", value_names=["document"])
        flat.add_edge("box/maker", "box/user", edge_type="data", value_names=["document"])
        return flat

    def test_ir_builder_excludes_the_internally_fed_consumer(self):
        from hypergraph.viz.renderer.ir_builder import find_internal_consumers

        assert find_internal_consumers("box", "document", self._mixed_flat_graph()) == ("box/gate",)

    def test_mermaid_excludes_the_internally_fed_consumer(self):
        from hypergraph.viz.mermaid import _resolve_data_targets

        targets = _resolve_data_targets(
            "box",
            "document",
            self._mixed_flat_graph(),
            {"box": True},
            {},
        )
        assert targets == ["box/gate"], f"the boundary value must not be drawn into the internally fed consumer: {targets}"


class TestHiddenNodesDoNotAggregate:
    """Only collapse-hiding aggregates to a boundary. A ``hide=True`` node's
    visible ancestor is an EXPANDED container — never a legitimate edge
    target (dagre cannot rank an edge into a compound node) — so its edges
    stay hidden, exactly as before endpoints were resolved at all."""

    def test_edge_to_a_hidden_consumer_stays_hidden_when_expanded(self):
        from tests.viz.conftest import make_hidden_source_data_dependency_graph

        scene = scene_for_state(make_hidden_source_data_dependency_graph(), expansion_state={"box": True}, simplify=False)

        offenders = [pair for pair in _visible(scene) if pair[1] == "box"]
        assert not offenders, f"no edge may target the expanded 'box' hull: {offenders}"


class TestOuterInputAggregatesToTheCollapsedBoundary:
    def test_pill_and_edge_survive_the_collapse(self):
        """The E.1 shape: an unbound outer input consumed only inside a
        collapsed container lost both its pill and its edge."""
        scene = scene_for_state(_generation(), expansion_state={})

        pill = _pill_for(scene, "query")
        assert pill is not None, "'query' is required by the outer graph; its pill must survive the collapse"
        assert (pill, "gen") in _visible(scene)

    def test_pill_follows_the_deepest_visible_ancestor(self):
        """One level open: the pill's edge lands on the still-collapsed inner box."""
        scene = scene_for_state(_generation(), expansion_state={"gen": True})

        pill = _pill_for(scene, "query")
        assert pill is not None
        assert (pill, "gen/build_messages") in _visible(scene)

    def test_fully_expanded_reaches_the_real_consumer(self):
        scene = scene_for_state(_generation(), expansion_state={"gen": True, "gen/build_messages": True})

        pill = _pill_for(scene, "query")
        assert pill is not None
        assert (pill, "gen/build_messages/assemble") in _visible(scene)
