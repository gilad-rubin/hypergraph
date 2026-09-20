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

    The child graph's ``inputs`` still advertise a name the graph BELOW it
    binds. A store written before #447 therefore holds a dead column for it;
    feeding that column back as a run value on a selective re-derive overrides
    the real binding and derives the row from ``None``: identity moves correctly
    while the data silently corrupts, which is strictly worse than the staleness
    this ticket set out to fix. A name bound anywhere in a node's subgraph is
    therefore never an input for it.
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


# --- #447: a child-bound name is recipe, so it is never a child column ---------


def _child_column_names(table: HyperTable) -> list[str]:
    table._ensure_analyzed()
    return [column.name for column in table._spec.children[0].columns]


def test_a_child_bound_name_never_becomes_a_child_source_column(tmp_path):
    """The column-side mirror of the run-side fix above.

    A name the child graph binds is recipe. It can never be provided at insert
    time and nothing ever writes a value into it, so a ``role="source"`` column
    for it is a lie: the row reports ``tag_value=None`` while ``tagged`` proves
    ``v1`` ran.
    """
    table = _mapped_table(tmp_path / "bound", "v1")
    assert "tag_value" not in _child_column_names(table)

    table.insert(doc_id="d1", text="alpha beta")
    rows = sorted(table.child("page").rows(), key=lambda row: row["page_id"])
    assert [set(row) for row in rows] == [{"page_id", "page_text", "tagged", "doc_id"}] * 2
    assert [row["tagged"] for row in rows] == ["v1:alpha", "v1:beta"]
    assert set(table.child("page").get("d1", "p1")) == {"page_id", "page_text", "tagged", "doc_id"}

    # Falsifier: drop the bind and tag_value is a genuine required input again,
    # so the source column must come back.
    unbound = Graph([tag], name="per_page").as_node(name="pages").map_over("pages", identity="page_id")
    plain = Graph([split, unbound]).as_table(identity="doc_id", store=LanceDBStore(str(tmp_path / "unbound")), runner=SyncRunner())
    assert "tag_value" in _child_column_names(plain)


def test_a_bind_two_levels_below_the_mapped_child_is_not_a_column_either(tmp_path):
    """``inputs.bound`` reports every depth, so one rule covers them all."""

    deepest = Graph([tag], name="tagger").bind(tag_value="d2v")
    mid = Graph([deepest.as_node(name="inner")], name="per_page")
    per_page = mid.as_node(name="pages").map_over("pages", identity="page_id")
    table = Graph([split, per_page]).as_table(identity="doc_id", store=LanceDBStore(str(tmp_path)), runner=SyncRunner())

    assert "tag_value" not in _child_column_names(table)
    table.insert(doc_id="d1", text="alpha beta")
    assert {row["tagged"] for row in table.child("page").rows()} == {"d2v:alpha", "d2v:beta"}


def test_a_child_input_with_a_default_and_no_bind_keeps_its_column(tmp_path):
    """The guard against over-fixing: a defaulted input is ``optional`` too, but
    it is fed from the item dict, so it is a real column with a real value."""

    @node(output_name="pages")
    def split_with_prefix(text: str) -> list[dict]:
        return [{"page_id": f"p{i}", "page_text": part, "prefix": part[0].upper()} for i, part in enumerate(text.split(), start=1)]

    @node(output_name="labeled")
    def label(page_text: str, prefix: str = "p") -> str:
        return f"{prefix}/{page_text}"

    inner = Graph([label], name="per_page_c")
    assert "prefix" in inner.inputs.optional and inner.inputs.bound == {}

    per_page = inner.as_node(name="pages").map_over("pages", identity="page_id")
    table = Graph([split_with_prefix, per_page]).as_table(identity="doc_id", store=LanceDBStore(str(tmp_path)), runner=SyncRunner())
    assert "prefix" in _child_column_names(table)

    table.insert(doc_id="d1", text="alpha beta")
    assert {(row["prefix"], row["labeled"]) for row in table.child("page").rows()} == {("A", "A/alpha"), ("B", "B/beta")}


def test_a_legacy_dead_column_is_hidden_on_read_and_left_alone_on_disk(tmp_path):
    """No migration: a store built before the fix keeps its physical column and
    its NULLs; the read path simply stops surfacing them."""
    import pyarrow as pa

    table = _mapped_table(tmp_path, "v1")
    table.insert(doc_id="d1", text="alpha beta")

    store = LanceDBStore(str(tmp_path))
    store.evolve_schema("page", {"tag_value": pa.utf8()})
    write_gen = store.max_write_gen("page") + 1
    for row in store.read_rows("page"):
        store.write_rows("page", [{**row, "tag_value": "LEGACY", "_write_gen": write_gen}])

    assert "tag_value" in store.column_names("page")
    assert "LEGACY" in {row.get("tag_value") for row in store.read_rows("page")}

    rows = _mapped_table(tmp_path, "v1").child("page").rows()
    assert all("tag_value" not in row for row in rows)
    assert {row["tagged"] for row in rows} == {"v1:alpha", "v1:beta"}
    # Still on disk, untouched: ignore-on-read, not drop-on-write.
    assert "tag_value" in LanceDBStore(str(tmp_path)).column_names("page")


def test_a_rebuild_pass_leaves_the_child_row_fingerprints_unchanged(tmp_path):
    """The dead column was not only cosmetic: ``rebuild_child_items`` recovered
    its NULL from the stored row, ``_insert_child_item`` folded it into the
    child inputs, and the same logical child row hashed differently depending on
    whether it came from a fresh fan-out or a RebuildChildren pass."""
    from hypergraph.materialization._commit import dedup_child_rows

    calls: list[str] = []

    @node(output_name="tagged")
    def counted_tag(page_text: str, tag_value: str) -> str:
        calls.append(page_text)
        return f"{tag_value}:{page_text}"

    @node(output_name="summary")
    def summarize(text: str, tone: str) -> str:
        return f"{tone}:{text}"

    def build(tone: str) -> HyperTable:
        inner = Graph([counted_tag], name="per_page").bind(tag_value="v1")
        per_page = inner.as_node(name="pages").map_over("pages", identity="page_id")
        return Graph([split, per_page, summarize]).bind(tone=tone).as_table(identity="doc_id", store=LanceDBStore(str(tmp_path)), runner=SyncRunner())

    def fingerprints() -> list[str]:
        rows = dedup_child_rows(LanceDBStore(str(tmp_path)).read_rows("page"), "page_id")
        return sorted(row["_row_fingerprint"] for row in rows)

    build("formal").insert(doc_id="d1", text="alpha beta")
    assert sorted(calls) == ["alpha", "beta"]
    fresh_fanout = fingerprints()

    calls.clear()
    # Only the ROOT bind moved; the child recipe and the child inputs are identical.
    build("casual").sync([{"doc_id": "d1", "text": "alpha beta"}])
    assert fingerprints() == fresh_fanout, "a RebuildChildren pass must not move the child fingerprint"


def test_setting_a_child_bound_name_is_refused_before_any_write(tmp_path):
    """Once the column leaves the spec it is no longer a content key, so without
    this refusal ``set()`` would quietly evolve a new physical column that the
    read filter then hides."""
    table = _mapped_table(tmp_path, "v1")
    table.insert(doc_id="d1", text="alpha beta")

    with pytest.raises(ValueError, match=r"(?s)bound on the child graph.*Fields: tag_value.*How to fix:.*bind\(\)"):
        table.child("page").set({"page_id": "p1"}, tag_value="x")

    assert "tag_value" not in LanceDBStore(str(tmp_path)).column_names("page")


@pytest.mark.asyncio
async def test_a_child_bound_name_is_not_a_column_on_the_async_runner(tmp_path):
    """Sync/async parity for the column-side fix."""
    table = _mapped_table(tmp_path, "v1", runner=AsyncRunner())
    await table.insert(doc_id="d1", text="alpha beta")

    assert "tag_value" not in _child_column_names(table)
    rows = sorted(table.child("page").rows(), key=lambda row: row["page_id"])
    assert [set(row) for row in rows] == [{"page_id", "page_text", "tagged", "doc_id"}] * 2
    assert [row["tagged"] for row in rows] == ["v1:alpha", "v1:beta"]
