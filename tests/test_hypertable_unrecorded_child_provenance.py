"""A child row that recorded no provenance for a step is judged by its inputs.

A child row derived before its graph's own bound values entered provenance
recorded none for the steps that read them, so every later reconcile re-ran
those steps however little changed. Such a row's value is trusted exactly as
its fingerprint skip already trusts it: only on a row stamped with today's
child recipe, and only when the step's inputs, read back from the row, are
today's.
"""

from __future__ import annotations

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import SqliteTableStore
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.materialization._schema import RECIPE_COLUMN

CALLS = {"phrase": 0, "double": 0}


@node(output_name="pages")
def split_pages(text: str, n: int) -> list[dict]:
    return [{"page_id": f"p{i}", "page_text": part, "n": n} for i, part in enumerate(text.split("|"))]


@node(output_name="phrased")
def phrase(page_text: str, prompt: str) -> str:
    CALLS["phrase"] += 1
    return f"{prompt}:{page_text}"


@node(output_name="doubled")
def double(n: int) -> int:
    CALLS["double"] += 1
    return n * 2


@pytest.fixture(params=["lance", "sqlite"])
def store(request, tmp_path):
    if request.param == "sqlite":
        s = SqliteTableStore()
        yield s
        s.close()
    else:
        yield LanceDBStore(str(tmp_path / "store"))


def _table(store):
    page = Graph([phrase, double], name="page").bind(prompt="P")
    return Graph([split_pages, page.as_node().map_over("pages", identity="page_id")]).as_table(identity="doc_id", store=store)


def _as_derived_before(store, **overrides):
    """Rewrite every stored page row as a row derived before `phrase` recorded a provenance."""
    rows = store.read_rows("page")
    gen = store.max_write_gen("page") + 1
    store.write_rows("page", [{**row, "_provenance_phrased": None, "_write_gen": gen, **overrides} for row in rows])
    store.delete_rows("page", [("_write_gen", "lt", gen)])


def _counts_after(table, **changes):
    CALLS.update(phrase=0, double=0)
    table.update("d1", **changes)
    return dict(CALLS)


def test_an_unrecorded_step_whose_inputs_match_is_kept(store):
    table = _table(store)
    table.insert(doc_id="d1", text="alpha|beta", n=1)
    _as_derived_before(store)

    assert _counts_after(table, n=2) == {"phrase": 0, "double": 2}
    rows = {row["page_id"]: row for row in store.read_rows("page")}
    assert rows["p0"]["phrased"] == "P:alpha"
    assert rows["p0"]["doubled"] == 4
    # The re-derived row records the provenance it was missing.
    assert all(row["_provenance_phrased"] for row in rows.values())


def test_an_unrecorded_step_whose_inputs_changed_runs(store):
    table = _table(store)
    table.insert(doc_id="d1", text="alpha|beta", n=1)
    _as_derived_before(store)

    assert _counts_after(table, text="alpha|gamma", n=2) == {"phrase": 1, "double": 2}
    assert {row["page_id"]: row["phrased"] for row in table.child("page").rows()} == {"p0": "P:alpha", "p1": "P:gamma"}


def test_an_unrecorded_step_on_a_row_of_another_recipe_runs(store):
    table = _table(store)
    table.insert(doc_id="d1", text="alpha|beta", n=1)
    _as_derived_before(store, **{RECIPE_COLUMN: "an-older-recipe"})

    assert _counts_after(table, n=2) == {"phrase": 2, "double": 2}
