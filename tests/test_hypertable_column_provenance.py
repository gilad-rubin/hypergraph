"""Tests for config-aware per-column provenance and column-scoped re-derivation.

A column's provenance = hash(producing node's code + configs of the components
it consumes + values of its direct inputs). Direct inputs are stored columns,
so transitivity is value-based: an upstream code change that produces the same
value stops the cascade; a changed value propagates.
"""

from __future__ import annotations

import pytest

from hypergraph import node
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.runners import SyncRunner

# ---------------------------------------------------------------------------
# Test nodes and components (with execution counters)
# ---------------------------------------------------------------------------

CALLS = {"clean": 0, "clean_same": 0, "clean_diff": 0, "embed": 0, "embed_v2": 0, "shape": 0}


class Embedder:
    def __init__(self, model_name: str = "embed-a"):
        self.model_name = model_name

    def _config(self):
        return {"model": self.model_name}

    def embed(self, text: str) -> list[float]:
        return [float(len(self.model_name)), float(len(text))]


@node(output_name="clean_text")
def clean(text: str) -> str:
    CALLS["clean"] += 1
    return text.strip().lower()


@node(output_name="clean_text")
def clean_same_value(text: str) -> str:
    CALLS["clean_same"] += 1
    result = text.strip().lower()
    return result  # different code, same value


@node(output_name="clean_text")
def clean_diff_value(text: str) -> str:
    CALLS["clean_diff"] += 1
    return text.strip().lower() + "!"


@node(output_name="vector")
def embed_text(clean_text: str, embedder: Embedder) -> list[float]:
    CALLS["embed"] += 1
    return embedder.embed(clean_text)


@node(output_name="vector_v2")
def embed_text_v2(clean_text: str, embedder_v2: Embedder) -> list[float]:
    CALLS["embed_v2"] += 1
    return embedder_v2.embed(clean_text)


@node(output_name="shaped_text")
def shape_text(text: str, mode: str) -> str:
    CALLS["shape"] += 1
    return text.upper() if mode == "upper" else text.lower()


@pytest.fixture(autouse=True)
def reset_calls():
    for key in CALLS:
        CALLS[key] = 0


@pytest.fixture
def store(tmp_path):
    return LanceDBStore(str(tmp_path / "prov_store"))


def make_table(store, embedder, nodes=None):
    from hypergraph.materialization import HyperTable

    return HyperTable(nodes or [clean, embed_text], identity="doc_id", store=store).bind(embedder=embedder).with_runner(SyncRunner())


DOCS = [{"doc_id": "d1", "text": "Chest pain"}, {"doc_id": "d2", "text": "Stroke triage"}]


# ---------------------------------------------------------------------------
# Column-scoped re-derivation (value-chaining)
# ---------------------------------------------------------------------------


class TestComponentSwap:
    def test_component_swap_rederives_only_consuming_column(self, store):
        table = make_table(store, Embedder("embed-a"))
        table.insert(DOCS)
        assert (CALLS["clean"], CALLS["embed"]) == (2, 2)

        rebound = make_table(store, Embedder("embed-b-longer"))
        rebound.sync(DOCS)

        assert CALLS["clean"] == 2, "clean() must not re-run on an embedder swap"
        assert CALLS["embed"] == 4, "embed must re-run for both rows"
        assert rebound.get("d1")["vector"][0] == float(len("embed-b-longer"))
        assert rebound.status().is_fresh

    def test_unconsumed_component_swap_heals_without_execution(self, store):
        table = make_table(store, Embedder("embed-a")).bind(unused=Embedder("x")).with_runner(SyncRunner())
        table.insert(DOCS)
        calls_before = dict(CALLS)

        rebound = make_table(store, Embedder("embed-a")).bind(unused=Embedder("y")).with_runner(SyncRunner())
        assert rebound.status().stale == 2
        assert rebound.status().stale_columns == ()

        rebound.sync(DOCS)
        assert dict(CALLS) == calls_before, "no node may execute for an unconsumed component swap"
        assert rebound.status().is_fresh


class TestValueChaining:
    def test_code_change_with_same_value_stops_cascade(self, store):
        make_table(store, Embedder()).insert(DOCS)
        embed_before = CALLS["embed"]

        variant = make_table(store, Embedder(), nodes=[clean_same_value, embed_text])
        variant.sync(DOCS)

        assert CALLS["clean_same"] == 2, "changed node must re-run"
        assert CALLS["embed"] == embed_before, "same upstream value must stop the cascade"
        assert variant.status().is_fresh

    def test_code_change_with_new_value_cascades(self, store):
        make_table(store, Embedder()).insert(DOCS)
        embed_before = CALLS["embed"]

        variant = make_table(store, Embedder(), nodes=[clean_diff_value, embed_text])
        variant.sync(DOCS)

        assert CALLS["clean_diff"] == 2
        assert CALLS["embed"] == embed_before + 2, "changed upstream value must cascade"
        assert variant.get("d1")["clean_text"] == "chest pain!"
        assert variant.status().is_fresh


# ---------------------------------------------------------------------------
# Bound plain values are recipe
# ---------------------------------------------------------------------------


def make_bound_value_table(store, mode: str):
    """A table binding a plain scalar (segment_semantics-style) beside a component."""
    from hypergraph.materialization import HyperTable

    return (
        HyperTable([shape_text, clean, embed_text], identity="doc_id", store=store)
        .bind(embedder=Embedder("embed-a"), mode=mode)
        .with_runner(SyncRunner())
    )


class TestBoundValueChange:
    """A bound non-component scalar parameterizes derivation, so it is recipe.

    The fingerprint docstring promises hash(source values + node definitions +
    component configs); a bound plain value that a node consumes must count as
    recipe the same way a component config does — changing it stales exactly
    the consuming column, and columns that do not consume it stay fresh.
    """

    def test_bound_value_change_stales_only_the_consuming_column(self, store):
        make_bound_value_table(store, mode="lower").insert(DOCS)
        assert (CALLS["shape"], CALLS["clean"], CALLS["embed"]) == (2, 2, 2)

        rebound = make_bound_value_table(store, mode="upper")
        report = rebound.status()
        assert report.stale == 2, "a changed bound value must stale its consuming rows"
        assert report.stale_columns == (("shaped_text", 2),)

        rebound.sync(DOCS)
        assert CALLS["shape"] == 4, "shape_text consumes the bound value; it must re-run"
        assert CALLS["clean"] == 2, "clean() does not consume the bound value; it must not re-run"
        assert CALLS["embed"] == 2, "embed's input value is unchanged; the cascade must stop"
        assert rebound.get("d1")["shaped_text"] == "CHEST PAIN"
        assert rebound.status().is_fresh

    def test_unchanged_bound_value_keeps_rows_fresh(self, store):
        make_bound_value_table(store, mode="lower").insert(DOCS)
        calls_before = dict(CALLS)

        same = make_bound_value_table(store, mode="lower")
        assert same.status().is_fresh
        same.sync(DOCS)
        assert dict(CALLS) == calls_before, "an unchanged bound value must not re-derive anything"


# ---------------------------------------------------------------------------
# status() column breakdown
# ---------------------------------------------------------------------------


class TestStatusColumns:
    def test_status_reports_stale_columns(self, store):
        make_table(store, Embedder("embed-a")).insert(DOCS)

        report = make_table(store, Embedder("embed-b")).status()
        assert report.stale == 2
        assert report.stale_columns == (("vector", 2),)

    def test_fresh_table_has_no_stale_columns(self, store):
        table = make_table(store, Embedder())
        table.insert(DOCS)
        assert table.status().stale_columns == ()


# ---------------------------------------------------------------------------
# Column-scoped recompute / backfill
# ---------------------------------------------------------------------------


class TestRecomputeBackfill:
    def test_recompute_is_column_scoped_and_converges(self, store):
        make_table(store, Embedder("embed-a")).insert(DOCS)
        clean_before = CALLS["clean"]

        rebound = make_table(store, Embedder("embed-b"))
        rebound.recompute("vector")

        assert CALLS["clean"] == clean_before, "recompute('vector') must not run clean()"
        assert CALLS["embed"] == 4
        assert rebound.status().is_fresh, "converged recompute must refresh freshness"

    def test_backfill_new_column_executes_only_its_node(self, store):
        make_table(store, Embedder("embed-a")).insert(DOCS)
        calls_before = dict(CALLS)

        extended = (
            make_table(store, Embedder("embed-a"), nodes=[clean, embed_text, embed_text_v2])
            .bind(embedder_v2=Embedder("embed-v2"))
            .with_runner(SyncRunner())
        )
        extended.backfill("vector_v2")

        assert CALLS["clean"] == calls_before["clean"], "backfill must not re-run clean()"
        assert CALLS["embed"] == calls_before["embed"], "backfill must not re-run embed()"
        assert CALLS["embed_v2"] == 2
        assert extended.get("d1")["vector_v2"][0] == float(len("embed-v2"))
        assert extended.status().is_fresh, "backfilled table with unchanged siblings must be fresh"
