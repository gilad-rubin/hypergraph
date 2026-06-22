"""Sink + map_iter materialization path (PRD 0001): runner inheritance,
graph-derive, and sink output-port selection — at the DerivedTable public seam."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import ContentKey, Identity
from hypergraph.materialization._keys import compute_definition_hash
from hypergraph.materialization._store import clear_store_cache
from hypergraph.materialization._table import DerivedTable
from hypergraph.runners import SyncRunner


@dataclass(frozen=True)
class Utterance:
    utt_id: Annotated[str, Identity]
    text: Annotated[str, ContentKey]


@dataclass(frozen=True)
class Embedded:
    utt_id: str
    text: str
    vector: list[float]


@dataclass(frozen=True)
class Tagged:
    utt_id: str
    text: str
    n: int


class Embedder:
    def _config(self):
        return {"model": "test"}

    def embed(self, text: str) -> list[float]:
        return [float(len(text))]


def embed(utt: Utterance, embedder: Embedder) -> Embedded:
    return Embedded(utt_id=utt.utt_id, text=utt.text, vector=embedder.embed(utt.text))


def tag(emb: Embedded) -> Tagged:
    return Tagged(utt_id=emb.utt_id, text=emb.text, n=len(emb.vector))


@pytest.fixture(autouse=True)
def _clear_stores():
    clear_store_cache()
    yield
    clear_store_cache()


@pytest.fixture
def store_path(tmp_path):
    return str(tmp_path / "store")


def test_chained_table_inherits_root_runner(store_path):
    """The runner is set on the root and inherited by chained tables (ADR 0001)."""
    runner = SyncRunner()
    root = DerivedTable(
        source=Utterance,
        output=Embedded,
        derive=embed,
        components={"embedder": Embedder()},
        store=store_path,
        runner=runner,
    )
    chained = DerivedTable(source=root, output=Tagged, derive=tag, store=store_path)

    assert root._runner is runner
    assert chained._runner is runner  # inherited, not its own default

    # and it works end to end: insert at the root cascades through the chain
    root.insert([Utterance("u1", "hello")])
    assert chained.count() == 1
    assert chained.get(utt_id="u1").n == 1


def test_graph_derive_materializes(store_path):
    """A Graph derive (single output) materializes rows through the sink."""

    @node(output_name="result")
    def make(utt: Utterance, embedder: Embedder) -> Embedded:
        return Embedded(utt_id=utt.utt_id, text=utt.text, vector=embedder.embed(utt.text))

    graph = Graph([make], name="embed")
    table = DerivedTable(
        source=Utterance,
        output=Embedded,
        derive=graph,
        components={"embedder": Embedder()},
        store=store_path,
    )

    table.insert([Utterance("u1", "hello")])

    assert table.count() == 1
    assert table.get(utt_id="u1").vector == [5.0]


def test_graph_derive_multiple_outputs_requires_select(store_path):
    """A multi-output derive graph is a build-time error unless narrowed."""

    @node(output_name="cleaned")
    def clean(utt: Utterance) -> str:
        return utt.text.lower()

    @node(output_name="result")
    def build(utt: Utterance, cleaned: str, embedder: Embedder) -> Embedded:
        return Embedded(utt_id=utt.utt_id, text=cleaned, vector=embedder.embed(cleaned))

    graph = Graph([clean, build], name="embed")

    with pytest.raises(ValueError, match="multiple outputs"):
        DerivedTable(
            source=Utterance,
            output=Embedded,
            derive=graph,
            components={"embedder": Embedder()},
            store=store_path,
        )


def test_graph_derive_select_persists_only_row_output(store_path):
    """With the row output selected, only it is persisted; scaffolding stays out."""

    @node(output_name="cleaned")
    def clean(utt: Utterance) -> str:
        return utt.text.lower()

    @node(output_name="result")
    def build(utt: Utterance, cleaned: str, embedder: Embedder) -> Embedded:
        return Embedded(utt_id=utt.utt_id, text=cleaned, vector=embedder.embed(cleaned))

    graph = Graph([clean, build], name="embed").select("result")
    table = DerivedTable(
        source=Utterance,
        output=Embedded,
        derive=graph,
        components={"embedder": Embedder()},
        store=store_path,
    )

    table.insert([Utterance("u1", "HELLO")])

    assert table.count() == 1
    assert table.get(utt_id="u1").text == "hello"  # cleaned upstream, then built


class FixedEmbedder:
    def __init__(self, val: int):
        self.val = val

    def _config(self):
        return {"v": self.val}

    def embed(self, text: str) -> list[float]:
        return [float(self.val)]


def test_recompute_component_swap_reruns_with_new_component(store_path):
    """A swapped component is actually run on recompute, not just hashed."""
    table = DerivedTable(
        source=Utterance,
        output=Embedded,
        derive=embed,
        components={"embedder": FixedEmbedder(1)},
        store=store_path,
    )
    table.insert([Utterance("u1", "x")])
    assert table.get(utt_id="u1").vector == [1.0]

    table.recompute(components={"embedder": FixedEmbedder(2)})
    assert table.get(utt_id="u1").vector == [2.0]  # re-derived with the NEW embedder


def test_insert_duplicate_identities_last_wins(store_path):
    """A repeated identity in one insert batch collapses to a single upserted row."""
    table = DerivedTable(
        source=Utterance,
        output=Embedded,
        derive=embed,
        components={"embedder": Embedder()},
        store=store_path,
    )
    table.insert([Utterance("u1", "aa"), Utterance("u1", "bbbb")])  # same id, different content

    assert table.count() == 1
    assert table.get(utt_id="u1").text == "bbbb"  # last occurrence wins
    assert table.get(utt_id="u1").vector == [4.0]


def test_bad_chained_derive_does_not_poison_parent(store_path):
    """A chained table that fails to compile must not be registered on the parent."""
    root = DerivedTable(
        source=Utterance,
        output=Embedded,
        derive=embed,
        components={"embedder": Embedder()},
        store=store_path,
    )

    @node(output_name="a")
    def na(emb: Embedded) -> int:
        return 1

    @node(output_name="b")
    def nb(emb: Embedded) -> int:
        return 2

    bad_graph = Graph([na, nb], name="bad")
    with pytest.raises(ValueError, match="multiple outputs"):
        DerivedTable(source=root, output=Tagged, derive=bad_graph, store=store_path)

    # the parent is unharmed: insert + cascade still work (no broken dependent)
    assert root._dependents == []
    root.insert([Utterance("u1", "hello")])
    assert root.count() == 1


def test_graph_derive_hashes_by_graph_code(store_path):
    """Graph derives hash by graph code/config, not repr(), so changes invalidate."""

    @node(output_name="result")
    def make(utt: Utterance, embedder: Embedder) -> Embedded:
        return Embedded(utt_id=utt.utt_id, text=utt.text, vector=embedder.embed(utt.text))

    graph = Graph([make], name="embed")
    table = DerivedTable(
        source=Utterance,
        output=Embedded,
        derive=graph,
        components={"embedder": Embedder()},
        store=store_path,
    )
    # graph-aware hash, not the repr() fallback the plain function hasher would produce
    assert table._definition_hash != compute_definition_hash(graph)


def test_derive_function_with_optional_default(store_path):
    """A derive with an optional (defaulted) parameter still resolves one source param."""

    def derive_with_default(utt: Utterance, scale: int = 2) -> Embedded:
        return Embedded(utt_id=utt.utt_id, text=utt.text, vector=[float(len(utt.text) * scale)])

    table = DerivedTable(source=Utterance, output=Embedded, derive=derive_with_default, store=store_path)
    table.insert([Utterance("u1", "ab")])
    assert table.get(utt_id="u1").vector == [4.0]  # len("ab")=2 * default scale 2


def test_async_runner_rejected_at_construction(store_path):
    """A non-sync streaming runner is rejected at construction, not mid-insert."""
    from hypergraph.runners import AsyncRunner

    with pytest.raises(TypeError, match="sync"):
        DerivedTable(
            source=Utterance,
            output=Embedded,
            derive=embed,
            components={"embedder": Embedder()},
            store=store_path,
            runner=AsyncRunner(),
        )


def test_graph_derive_config_affects_definition_hash(tmp_path):
    """Graph bindings are part of the content key, so changing them re-derives."""

    @node(output_name="result")
    def make(utt: Utterance, scale: int, embedder: Embedder) -> Embedded:
        base = embedder.embed(utt.text)
        return Embedded(utt_id=utt.utt_id, text=utt.text, vector=[v * scale for v in base])

    g1 = Graph([make], name="m").bind(scale=1)
    g2 = Graph([make], name="m").bind(scale=2)
    t1 = DerivedTable(source=Utterance, output=Embedded, derive=g1, components={"embedder": Embedder()}, store=str(tmp_path / "s1"))
    t2 = DerivedTable(source=Utterance, output=Embedded, derive=g2, components={"embedder": Embedder()}, store=str(tmp_path / "s2"))

    assert t1._definition_hash != t2._definition_hash
