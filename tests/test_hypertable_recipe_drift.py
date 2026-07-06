"""Pin the per-row recipe stamp + zero-content-read drift count (PRD 0027 F4).

Every derived row additionally stamps a RECIPE-ONLY fingerprint (node code +
component configs + bound plain values — NO input values) in the
``_recipe_fingerprint`` column at derive time. "Does this row match today's
recipe" then becomes a cheap stored-column comparison:

- a bound-value change (e.g. a segmentation mode) flips the stamp for new
  derives, and ``recipe_drift()`` counts the old rows as drifted;
- the drift count reads ONLY identity/reserved columns (column projection is
  pushed down) — content bytes never leave the disk;
- rows written before the stamp existed read as UNKNOWN (drifted-unknown),
  honestly, never as current and never as a crash.

The stamp is additive: physical table names, existing column names, and
manifest keys are untouched; old stores gain the column via idempotent
schema evolution the first time a stamped row is written.
"""

from __future__ import annotations

from hypergraph import node
from hypergraph.materialization import HyperTable, RecipeDrift
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.materialization._schema import RECIPE_COLUMN
from hypergraph.runners import SyncRunner


@node(output_name="upper")
def to_upper(text: str, mode: str) -> str:
    return text.upper() if mode == "loud" else text


def _table(tmp_path, mode: str) -> HyperTable:
    return HyperTable([to_upper], identity="doc_id", store=LanceDBStore(str(tmp_path))).bind(mode=mode).with_runner(SyncRunner())


class RecordingStore(LanceDBStore):
    """Records every read's projection so a test can prove content never loads."""

    def __init__(self, path: str):
        super().__init__(path)
        self.read_projections: list[tuple[str, list[str] | None]] = []

    def read_rows(self, table_name, where=None, *, limit=None, columns=None):
        self.read_projections.append((table_name, columns))
        return super().read_rows(table_name, where, limit=limit, columns=columns)


def test_derived_rows_stamp_the_recipe_fingerprint(tmp_path):
    table = _table(tmp_path, "loud")
    table.insert(doc_id="d1", text="hello")

    raw = LanceDBStore(str(tmp_path)).read_rows("doc")
    assert len(raw) == 1
    stamp = raw[0][RECIPE_COLUMN]
    assert isinstance(stamp, str) and len(stamp) == 64  # a sha256 hex digest

    # The stamp is recipe-only: a second row with DIFFERENT input values
    # carries the SAME stamp (input values are not recipe).
    table.insert(doc_id="d2", text="other words entirely")
    raw = LanceDBStore(str(tmp_path)).read_rows("doc")
    assert {row[RECIPE_COLUMN] for row in raw} == {stamp}


def test_stamp_is_internal_and_stripped_from_public_rows(tmp_path):
    table = _table(tmp_path, "loud")
    table.insert(doc_id="d1", text="hello")
    assert RECIPE_COLUMN not in table.get("d1")


def test_bound_value_change_flips_drift_and_rederive_clears_it(tmp_path):
    v1 = _table(tmp_path, "loud")
    v1.insert(doc_id="d1", text="hello")
    v1.insert(doc_id="d2", text="world")
    drift = v1.recipe_drift()
    assert isinstance(drift, RecipeDrift)
    assert (drift.total, drift.current, drift.drifted, drift.unknown) == (2, 2, 0, 0)
    assert drift.stale_total == 0

    # The recipe changes: same nodes, different bound value.
    v2 = _table(tmp_path, "quiet")
    drift = v2.recipe_drift()
    assert (drift.total, drift.current, drift.drifted, drift.unknown) == (2, 0, 2, 0)
    assert drift.stale_total == 2

    # Re-deriving under the new recipe clears the drift.
    v2.insert(doc_id="d1", text="hello")
    v2.insert(doc_id="d2", text="world")
    drift = v2.recipe_drift()
    assert (drift.total, drift.current, drift.drifted, drift.unknown) == (2, 2, 0, 0)


def test_drift_count_reads_no_content_columns(tmp_path):
    store = RecordingStore(str(tmp_path))
    table = HyperTable([to_upper], identity="doc_id", store=store).bind(mode="loud").with_runner(SyncRunner())
    table.insert(doc_id="d1", text="a very large body of content" * 100)

    store.read_projections.clear()
    table.recipe_drift()

    assert store.read_projections, "recipe_drift must read through the store"
    for table_name, columns in store.read_projections:
        assert columns is not None, f"unprojected (full-row) read of {table_name!r} during recipe_drift"
        assert "text" not in columns and "upper" not in columns, f"recipe_drift read content column(s) from {table_name!r}: {columns}"


def test_rows_without_a_stamp_read_as_unknown_not_current(tmp_path):
    table = _table(tmp_path, "loud")
    table.insert(doc_id="d1", text="hello")

    # Simulate a pre-stamp row: written straight through the store, the stamp
    # column NULL — exactly how a pre-0027 row reads after schema evolution.
    store = LanceDBStore(str(tmp_path))
    spec_row = {
        "doc_id": "old1",
        "text": "legacy",
        "upper": "LEGACY",
        "_row_fingerprint": "legacy-fp",
        "_write_gen": 99,
        "_status": "complete",
        "_error": None,
    }
    store.read_rows("doc")  # opens the table handle write_rows requires
    store.write_rows("doc", [spec_row])

    drift = table.recipe_drift()
    assert (drift.total, drift.current, drift.drifted, drift.unknown) == (2, 1, 0, 1)
    assert drift.stale_total == 1


def test_child_rows_stamp_their_own_child_recipe(tmp_path):
    from hypergraph import Graph

    @node(output_name="pages")
    def split(text: str) -> list[dict]:
        return [{"page_id": f"p{i}", "page_text": part} for i, part in enumerate(text.split(), start=1)]

    @node(output_name="tagged")
    def tag(page_text: str, tag_value: str) -> str:
        return f"{tag_value}:{page_text}"

    per_page = Graph([tag], name="per_page").as_node(name="pages").map_over("pages", identity="page_id")

    def build(tag_value: str) -> HyperTable:
        return HyperTable([split, per_page], identity="doc_id", store=LanceDBStore(str(tmp_path))).bind(tag_value=tag_value).with_runner(SyncRunner())

    v1 = build("v1")
    v1.insert(doc_id="d1", text="alpha beta")
    drift = v1.recipe_drift()
    assert drift.stale_total == 0
    assert len(drift.children) == 1
    assert (drift.children[0].total, drift.children[0].current) == (2, 2)

    # Changing the bound value consumed ONLY by the child graph drifts the
    # child rows; drift aggregates through stale_total.
    v2 = build("v2")
    drift = v2.recipe_drift()
    assert drift.children[0].drifted == 2
    assert drift.stale_total >= 2


def test_sync_stamps_unstamped_rows_it_proves_current_without_rederiving(tmp_path):
    """A pre-stamp row whose fingerprint matches today's recipe gets STAMPED on
    the next sync, not re-derived and not left unknown forever.

    The row fingerprint embeds the whole recipe (inputs + node code + component
    hashes), so a fingerprint-skip PROVES the row is current under today's
    recipe — the recipe-only stamp can be written truthfully without running a
    single node. Without this, a store written before stamping existed reads
    "N rows derived under an older recipe" forever and Sync is a visible no-op.
    """
    calls: list[str] = []

    @node(output_name="shout")
    def shout(text: str, suffix: str) -> str:
        calls.append(text)
        return text + suffix

    def build() -> HyperTable:
        return HyperTable([shout], identity="doc_id", store=LanceDBStore(str(tmp_path))).bind(suffix="!").with_runner(SyncRunner())

    table = build()
    table.insert(doc_id="d1", text="hello")

    # Simulate the pre-stamp store: physically NULL the stamp on the stored row.
    store = LanceDBStore(str(tmp_path))
    row = store.read_rows("doc")[0]
    row[RECIPE_COLUMN] = None
    row["_write_gen"] = int(row["_write_gen"]) + 1
    store.write_rows("doc", [row])
    store.delete_rows("doc", [("doc_id", "eq", "d1"), ("_write_gen", "lt", row["_write_gen"])])
    assert build().recipe_drift().unknown == 1

    calls.clear()
    result = build().sync([{"doc_id": "d1", "text": "hello"}])
    assert result.skipped == 1  # fingerprint-skip: nothing re-derived
    assert calls == []  # the node never ran
    drift = build().recipe_drift()
    assert (drift.current, drift.unknown) == (1, 0)


def test_sync_stamps_unstamped_child_rows_via_the_bump_path(tmp_path):
    from hypergraph import Graph

    @node(output_name="pages")
    def split(text: str) -> list[dict]:
        return [{"page_id": f"p{i}", "page_text": part} for i, part in enumerate(text.split(), start=1)]

    @node(output_name="tagged")
    def tag(page_text: str) -> str:
        return f"t:{page_text}"

    per_page = Graph([tag], name="per_page").as_node(name="pages").map_over("pages", identity="page_id")

    def build() -> HyperTable:
        return HyperTable([split, per_page], identity="doc_id", store=LanceDBStore(str(tmp_path))).with_runner(SyncRunner())

    table = build()
    table.insert(doc_id="d1", text="alpha beta")

    # NULL the stamps on parent + children (the pre-stamp shape).
    store = LanceDBStore(str(tmp_path))
    for table_name in ("doc", "page"):
        for row in store.read_rows(table_name):
            row[RECIPE_COLUMN] = None
            row["_write_gen"] = int(row["_write_gen"]) + 1
            store.write_rows(table_name, [row])
    for row in list(store.read_rows("doc")):
        store.delete_rows("doc", [("doc_id", "eq", row["doc_id"]), ("_write_gen", "lt", row["_write_gen"])])
    drift = build().recipe_drift()
    assert drift.unknown == 1 and drift.children[0].unknown == 2

    build().sync([{"doc_id": "d1", "text": "alpha beta"}])
    drift = build().recipe_drift()
    assert drift.stale_total == 0, f"sync must stamp what it proves current, got {drift}"
