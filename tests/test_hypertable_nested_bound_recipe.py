"""Values bound INSIDE a nested graph are recipe, at every depth (#368).

``Graph.definition_hash`` deliberately excludes bindings ("runtime values, not
structure") and ``HyperTable`` only ever read the ROOT graph's ``_bound``, so a
value bound on a graph that is mounted as a ``GraphNode`` reached no
fingerprint at all: editing a profile prompt inside a nested graph left
``recipe_drift()`` reporting zero drift and ``sync()`` returning SKIPPED while
the run path happily used the NEW value. Identity said v1, execution used v2.

These tests pin the fix from the outside: four different inner-bound values are
four different recipes, the drift/re-derive loop closes end to end, recursion
holds two levels down, and the exclusion policy for objects with no stable
payload is the same inside a nested graph as it is at the root.
"""

from __future__ import annotations

import warnings

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import HyperTable
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.materialization._recipe_journal import KIND_BOUND_VALUE
from hypergraph.runners import AsyncRunner, SyncRunner


@node(output_name="pages")
def split(text: str) -> list[dict]:
    return [{"page_id": f"p{i}", "page_text": part} for i, part in enumerate(text.split(), start=1)]


@node(output_name="tagged")
def tag(page_text: str, tag_value: str) -> str:
    return f"{tag_value}:{page_text}"


def _mapped_table(tmp_path, tag_value: str, runner=None) -> HyperTable:
    """A mounted child table whose bind sits on the CHILD graph, not the root."""
    inner = Graph([tag], name="per_page").bind(tag_value=tag_value)
    per_page = inner.as_node(name="pages").map_over("pages", identity="page_id")
    return Graph([split, per_page]).as_table(
        identity="doc_id",
        store=LanceDBStore(str(tmp_path)),
        runner=runner or SyncRunner(),
    )


def _identities(table: HyperTable) -> tuple[str, str, str, str]:
    table._ensure_analyzed()
    policy = table._provenance_policy
    child_spec = table._spec.children[0]
    return (
        table.recipe_fingerprint(),
        policy.current_child_recipe_fingerprint(child_spec),
        policy.child_fingerprint({"page_text": "alpha"}, child_spec),
        policy.root_fingerprint({"text": "alpha beta"}),
    )


def test_four_inner_bound_values_are_four_distinct_recipes(tmp_path):
    """The issue's probe: four values bound on the CHILD graph must produce four
    distinct values of every recipe identity a HyperTable exposes — exactly as
    four values bound on the ROOT graph already do."""
    seen = [_identities(_mapped_table(tmp_path / v, v)) for v in ("v1", "v2", "v3", "v4")]

    for slot, label in enumerate(("recipe_fingerprint", "current_child_recipe_fingerprint", "child_fingerprint", "root_fingerprint")):
        assert len({identity[slot] for identity in seen}) == 4, f"{label} collapsed four inner-bound values"

    # And the same value twice is the same recipe (no per-process instability).
    assert _identities(_mapped_table(tmp_path / "again", "v1")) == seen[0]


def test_inner_bound_change_drifts_child_rows_and_sync_rederives(tmp_path):
    """End to end: insert under v1, rebuild under v2, drift reports it and sync
    re-derives (the receipt is not SKIPPED and the stored rows carry v2)."""
    v1 = _mapped_table(tmp_path, "v1")
    v1.insert(doc_id="d1", text="alpha beta")
    assert {row["tagged"] for row in LanceDBStore(str(tmp_path)).read_rows("page")} == {"v1:alpha", "v1:beta"}
    assert v1.recipe_drift().stale_total == 0

    v2 = _mapped_table(tmp_path, "v2")
    drift = v2.recipe_drift()
    assert drift.children[0].drifted == 2, f"child rows must read as drifted, got {drift}"
    assert drift.stale_total >= 2

    receipt = v2.sync([{"doc_id": "d1", "text": "alpha beta"}])
    assert [r.outcome.value for r in receipt.receipts] != ["skipped"], "sync must re-derive under the new inner bind"
    assert {row["tagged"] for row in LanceDBStore(str(tmp_path)).read_rows("page")} == {"v2:alpha", "v2:beta"}
    assert v2.recipe_drift().stale_total == 0


@pytest.mark.asyncio
async def test_inner_bound_change_rederives_on_the_async_runner(tmp_path):
    """Sync/async parity for the same loop."""
    v1 = _mapped_table(tmp_path, "v1", runner=AsyncRunner())
    await v1.insert(doc_id="d1", text="alpha beta")
    assert {row["tagged"] for row in LanceDBStore(str(tmp_path)).read_rows("page")} == {"v1:alpha", "v1:beta"}

    v2 = _mapped_table(tmp_path, "v2", runner=AsyncRunner())
    assert v2.recipe_drift().children[0].drifted == 2
    receipt = await v2.sync([{"doc_id": "d1", "text": "alpha beta"}])
    assert [r.outcome.value for r in receipt.receipts] != ["skipped"]
    assert {row["tagged"] for row in LanceDBStore(str(tmp_path)).read_rows("page")} == {"v2:alpha", "v2:beta"}


def test_plain_nested_graphnode_column_drifts_on_an_inner_bind(tmp_path):
    """Not only the map_over child: a plain nested GraphNode producing a ROOT
    column must drift when a value bound inside it changes."""

    @node(output_name="shout")
    def shout(text: str, suffix: str) -> str:
        return text.upper() + suffix

    def build(suffix: str) -> HyperTable:
        inner = Graph([shout], name="shouter").bind(suffix=suffix)
        return Graph([inner.as_node(name="shouting")]).as_table(identity="doc_id", store=LanceDBStore(str(tmp_path)), runner=SyncRunner())

    bang = build("!")
    bang.insert(doc_id="d1", text="hello")
    assert bang.get("d1")["shout"] == "HELLO!"
    assert bang.recipe_drift().stale_total == 0

    question = build("?")
    assert question.recipe_fingerprint() != bang.recipe_fingerprint()
    assert question.recipe_drift().drifted == 1

    question.sync([{"doc_id": "d1", "text": "hello"}])
    assert question.get("d1")["shout"] == "HELLO?"
    assert question.recipe_drift().stale_total == 0


def test_two_levels_of_nesting_inside_a_mounted_child_rederive_to_the_new_value(tmp_path):
    """The hard two-level shape: the bind sits a level BELOW the graph a
    ``map_over`` mounts as a child table.

    The child table's schema builds a source column from the child graph's
    ``inputs``, which still advertise a name the graph BELOW it binds — so that
    column exists and is permanently NULL. Feeding it back as a run value on a
    selective re-derive overrides the real binding and derives the row from
    ``None``: identity moves correctly while the data silently corrupts, which
    is strictly worse than the staleness this ticket set out to fix. A name
    bound anywhere in a node's subgraph is therefore never an input for it.
    """

    def build(tag_value: str) -> HyperTable:
        deepest = Graph([tag], name="tagger").bind(tag_value=tag_value)
        mid = Graph([deepest.as_node(name="inner")], name="per_page")
        per_page = mid.as_node(name="pages").map_over("pages", identity="page_id")
        return Graph([split, per_page]).as_table(identity="doc_id", store=LanceDBStore(str(tmp_path)), runner=SyncRunner())

    build("d1v").insert(doc_id="d1", text="alpha beta")
    assert {row["tagged"] for row in LanceDBStore(str(tmp_path)).read_rows("page")} == {"d1v:alpha", "d1v:beta"}

    v2 = build("d2v")
    assert v2.recipe_drift().children[0].drifted == 2

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        v2.sync([{"doc_id": "d1", "text": "alpha beta"}])
    overrides = [str(w.message) for w in caught if "overrides bound value" in str(w.message)]
    assert not overrides, f"the re-derive fed a stored NULL over the real binding: {overrides}"
    assert {row["tagged"] for row in LanceDBStore(str(tmp_path)).read_rows("page")} == {"d2v:alpha", "d2v:beta"}
    assert v2.recipe_drift().stale_total == 0


def test_a_root_bind_wins_over_the_child_value_it_shadows(tmp_path):
    """One precedence rule, identity and execution alike.

    When the same name is bound at the root AND on the mounted child graph, the
    root's value is what runs (it flattens down onto the child graph). Two
    tables differing only in the SHADOWED child value therefore derive identical
    data and must carry identical recipe identities — reporting drift for a
    value the run never honors would order a full re-derive that changes nothing.
    """

    def build(child_value: str, path) -> HyperTable:
        inner = Graph([tag], name="per_page").bind(tag_value=child_value)
        per_page = inner.as_node(name="pages").map_over("pages", identity="page_id")
        return Graph([split, per_page]).bind(tag_value="ROOT").as_table(identity="doc_id", store=LanceDBStore(str(path)), runner=SyncRunner())

    a, b = build("childA", tmp_path / "a"), build("childB", tmp_path / "b")
    assert a.recipe_fingerprint() == b.recipe_fingerprint()
    assert a._provenance_policy.root_fingerprint({"text": "alpha"}) == b._provenance_policy.root_fingerprint({"text": "alpha"})

    a.insert(doc_id="d1", text="alpha")
    assert [row["tagged"] for row in LanceDBStore(str(tmp_path / "a")).read_rows("page")] == ["ROOT:alpha"]

    # Same store, rebuilt with the other shadowed value: the run is identical.
    same = build("childB", tmp_path / "a")
    drift = same.recipe_drift()
    assert (drift.drifted, drift.children[0].drifted) == (0, 0)
    assert same.sync([{"doc_id": "d1", "text": "alpha"}]).receipts[0].outcome.value == "skipped"


def test_an_intermediate_bind_shadows_the_deeper_value_it_overrides(tmp_path):
    """Outermost-wins is the rule at EVERY hop, not only the first one.

    A binding on a middle graph overrides one on the graph nested inside it (the
    graph layer says so out loud: "Parent bind for X overrides nested bind from
    GraphNode Y"). Two tables differing only in that shadowed deepest value
    therefore derive identical data, so they must be one recipe — otherwise the
    shadow set stops growing after the first level and drift orders a re-derive
    that rewrites the same bytes.
    """

    @node(output_name="shout")
    def shout(text: str, suffix: str) -> str:
        return text.upper() + suffix

    def build(deepest_value: str, path) -> HyperTable:
        deepest = Graph([shout], name="deepest").bind(suffix=deepest_value)
        middle = Graph([deepest.as_node(name="inner")], name="middle").bind(suffix="MID")
        return Graph([middle.as_node(name="outer")]).as_table(identity="doc_id", store=LanceDBStore(str(path)), runner=SyncRunner())

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        bang, question = build("!", tmp_path / "a"), build("?", tmp_path / "b")
        bang.insert(doc_id="d1", text="hi")
        question.insert(doc_id="d1", text="hi")
        # The graph layer states the precedence out loud; identity must agree.
        assert any("overrides nested bind" in str(w.message) for w in caught)
        assert bang.get("d1")["shout"] == question.get("d1")["shout"] == "HIMID"
        assert bang.recipe_fingerprint() == question.recipe_fingerprint()

        # Same store, rebuilt with the other shadowed deepest value: nothing to do.
        same = build("?", tmp_path / "a")
        assert same.recipe_drift().drifted == 0
        assert same.sync([{"doc_id": "d1", "text": "hi"}]).receipts[0].outcome.value == "skipped"


def test_recursion_holds_at_two_levels_of_nesting(tmp_path):
    """A graph in a graph in a table: the deepest bind still moves the recipe."""

    @node(output_name="shout")
    def shout(text: str, suffix: str) -> str:
        return text.upper() + suffix

    def build(suffix: str) -> HyperTable:
        deepest = Graph([shout], name="deepest").bind(suffix=suffix)
        middle = Graph([deepest.as_node(name="inner")], name="middle")
        return Graph([middle.as_node(name="outer")]).as_table(identity="doc_id", store=LanceDBStore(str(tmp_path)), runner=SyncRunner())

    bang = build("!")
    bang.insert(doc_id="d1", text="hello")
    assert bang.get("d1")["shout"] == "HELLO!"

    question = build("?")
    assert question.recipe_fingerprint() != bang.recipe_fingerprint()
    assert question.recipe_drift().drifted == 1
    question.sync([{"doc_id": "d1", "text": "hello"}])
    assert question.get("d1")["shout"] == "HELLO?"


def test_inner_bound_object_without_a_stable_payload_stays_excluded(tmp_path):
    """The exclusion policy is identical at depth: an object with no
    ``__component_config__`` and no stable value payload is NOT hashed by repr,
    so two fresh instances of it are one recipe (not two)."""

    class OpaqueClient:
        """No _config, no __component_config__ — its repr carries an address."""

        def shout(self, text: str) -> str:
            return text.upper()

    @node(output_name="shout")
    def shout(text: str, client: OpaqueClient) -> str:
        return client.shout(text)

    def build(client: OpaqueClient) -> HyperTable:
        inner = Graph([shout], name="shouter").bind(client=client)
        return Graph([inner.as_node(name="shouting")]).as_table(identity="doc_id", store=LanceDBStore(str(tmp_path)), runner=SyncRunner())

    first, second = OpaqueClient(), OpaqueClient()
    assert repr(first) != repr(second)
    assert build(first).recipe_fingerprint() == build(second).recipe_fingerprint()


def test_recipe_journal_records_inner_bound_values(tmp_path):
    """A stamp must resolve back to the prompt text: an inner-bound value is
    journalled as KIND_BOUND_VALUE, exactly like a root-bound one."""
    table = _mapped_table(tmp_path, "the-vision-prompt")
    table.insert(doc_id="d1", text="alpha beta")

    bound = [row for row in table.journal_rows() if row["kind"] == KIND_BOUND_VALUE]
    assert any("the-vision-prompt" in row["payload"] for row in bound), f"inner-bound value never journalled: {bound}"


def test_a_wrong_inner_bind_is_refused_at_build_time_not_silently_stamped(tmp_path):
    """The failure path: binding a name the inner graph does not have is refused
    when the graph is built, naming the valid inputs — so a typo can never
    quietly produce a recipe that differs from what actually runs."""
    with pytest.raises(ValueError, match=r"Cannot bind 'tag_valeu'.*Valid inputs: \['page_text', 'tag_value'\]"):
        Graph([tag], name="per_page").bind(tag_valeu="v1")


def test_a_recipe_with_no_nested_binding_keeps_the_identity_it_already_had():
    """The blast radius: a node that wraps no graph — and a GraphNode whose inner
    graphs bind nothing — keep EXACTLY their construction-time definition hash,
    so no existing store re-derives just because nested bindings became recipe."""
    from hypergraph.materialization._fingerprint import (
        compute_node_definition_hash,
        compute_node_recipe_hash,
    )

    assert compute_node_recipe_hash(split) == compute_node_definition_hash(split)

    unbound = Graph([tag], name="per_page").as_node(name="pages")
    assert compute_node_recipe_hash(unbound) == compute_node_definition_hash(unbound)

    bound = Graph([tag], name="per_page").bind(tag_value="v1").as_node(name="pages")
    assert compute_node_recipe_hash(bound) != compute_node_definition_hash(bound)
