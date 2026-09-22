"""A receipt states the physical effect of a write, not a hopeful summary (#248).

``WriteOutcome`` documents itself as "Physical effect of a row write", so
``SKIPPED`` may only be reported by a path that wrote nothing and ``HEALED``
only by one that actually repaired damage. Three claims had to become true
rather than merely plausible:

- ``insert()`` repairing a stored child error row wrote rows, so it reports
  ``HEALED`` — #314 made the repair happen, the receipt still said ``SKIPPED``;
- a repair whose retry fails again healed nothing, so it reports ``UPDATED``
  and the stored error row is still there to find and retry;
- a metadata-only ``update()`` writes a row, so it reports ``UPDATED``.

The last two cases cover the decoder all of those paths read ``_status``
through: one codec, which keeps the undocumented pre-``_status`` legacy row
readable and refuses a value it does not know instead of quietly reading it as
"not an error".
"""

from __future__ import annotations

from typing import Any, TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import TableStore, WriteOutcome
from hypergraph.materialization._provenance import split_boundary_provenance
from hypergraph.runners import SyncRunner


class MemoryStore(TableStore):
    def __init__(self, rows: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = rows if rows is not None else {}

    def open(self, spec, children):
        self.rows.setdefault(spec.name, [])
        for child in children:
            self.rows.setdefault(child.name, [])
        return {name: list(rows[0].keys()) if rows else [] for name, rows in self.rows.items()}

    def count(self, table_name):
        return len(self.rows.get(table_name, []))

    def read_rows(self, table_name, where=None, *, limit=None):
        rows = [row.copy() for row in self.rows.get(table_name, []) if _matches(row, where or [])]
        return rows[:limit] if limit is not None else rows

    def read_one(self, table_name, identity_column, identity_value):
        rows = self.read_rows(table_name, [(identity_column, "eq", identity_value)])
        if not rows:
            return None
        return max(rows, key=lambda row: row.get("_write_gen", 0))

    def write_rows(self, table_name, rows):
        self.rows.setdefault(table_name, []).extend(row.copy() for row in rows)

    def delete_rows(self, table_name, where):
        existing = self.rows.get(table_name, [])
        keep = [row for row in existing if not _matches(row, where)]
        self.rows[table_name] = keep
        return len(existing) - len(keep)

    def max_write_gen(self, table_name):
        return max((row.get("_write_gen", 0) for row in self.rows.get(table_name, [])), default=0)

    def evolve_schema(self, table_name, new_columns):
        return []


def _matches(row: dict[str, Any], where) -> bool:
    for col, op, value in where:
        current = row.get(col)
        if op == "eq" and current != value:
            return False
        if op == "lt" and not (current is not None and current < value):
            return False
        if op == "in" and current not in value:
            return False
    return True


class Utterance(TypedDict):
    utterance_id: str
    text: str


fail_on_word: set[str] = set()
executions = {"clean": 0, "split": 0, "child": 0}


@node(output_name="clean_text")
def clean(text: str) -> str:
    executions["clean"] += 1
    return text.upper()


@node(output_name="utterances")
def split_words(text: str) -> list[Utterance]:
    executions["split"] += 1
    return [Utterance(utterance_id=f"u{i}", text=word) for i, word in enumerate(text.split())]


@node(output_name="clean_word")
def clean_word(text: str) -> str:
    executions["child"] += 1
    if text in fail_on_word:
        raise RuntimeError(f"boom on {text}")
    return f"t:{text}"


process_word = Graph([clean_word], name="process_word")


@pytest.fixture(autouse=True)
def _reset():
    fail_on_word.clear()
    executions["clean"] = executions["split"] = executions["child"] = 0


def _fanout_table(store, *, on_error="store"):
    return Graph(
        [clean, split_words, process_word.as_node().map_over("utterances", identity="utterance_id")],
        name="doc",
    ).as_table(identity="doc_id", store=store, on_error=on_error, runner=SyncRunner())


def _flat_table(store):
    return Graph([clean], name="doc").as_table(identity="doc_id", store=store, runner=SyncRunner())


def _child_state(store):
    return {row["utterance_id"]: (row["_status"], row.get("clean_word")) for row in store.rows["utterance"]}


def _reset_counters():
    executions["clean"] = executions["split"] = executions["child"] = 0


# ---------------------------------------------------------------------------
# 1. insert() that repairs a child reports the repair, not a skip
# ---------------------------------------------------------------------------


def test_insert_repairing_a_child_error_row_reports_healed():
    """#314 made insert() repair a stored child error row under an unchanged
    parent; the receipt still called that write SKIPPED. Rows were written, so
    the honest outcome is the one sync() already used for the same repair."""

    store = MemoryStore()
    fail_on_word.add("beta")
    _fanout_table(store).insert(doc_id="d1", text="alpha beta")
    assert _child_state(store) == {"u0": ("complete", "t:alpha"), "u1": ("error", None)}

    fail_on_word.discard("beta")
    receipt = _fanout_table(MemoryStore(store.rows)).insert([{"doc_id": "d1", "text": "alpha beta"}])

    assert _child_state(store) == {"u0": ("complete", "t:alpha"), "u1": ("complete", "t:beta")}
    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("healed", "complete")]
    assert receipt.skipped == 0, "a path that wrote child rows must not report a skip"


def test_insert_over_healthy_children_still_reports_skipped():
    """The falsifier: when there is nothing to repair, the unchanged parent is
    still a SKIPPED receipt — the new outcome tracks repairs, not re-inserts."""

    store = MemoryStore()
    table = _fanout_table(store)
    table.insert(doc_id="d1", text="alpha beta")
    executions["child"] = 0

    receipt = _fanout_table(MemoryStore(store.rows)).insert([{"doc_id": "d1", "text": "alpha beta"}])

    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("skipped", "complete")]
    assert executions["child"] == 0


def test_an_extra_child_row_is_a_repair_the_receipt_reports():
    """The other shape of child damage: an interrupted write left one child row
    too many. The recorded fan-out count no longer matches what is physically
    there, so the boundary re-runs to rebuild the item list and the orphan is
    retired. Nothing new is derived per child — every surviving row re-stamps
    unchanged — but re-running the fan-out boundary to repair the child set is
    derivation, so this is a repair and not a skip."""

    store = MemoryStore()
    table = _fanout_table(store)
    table.sync([{"doc_id": "d1", "text": "alpha beta"}])
    orphan = dict(store.rows["utterance"][0])
    orphan["utterance_id"] = "u9"
    store.rows["utterance"].append(orphan)
    _reset_counters()

    fresh = _fanout_table(MemoryStore(store.rows))
    receipt = fresh.sync([{"doc_id": "d1", "text": "alpha beta"}])

    assert sorted(row["utterance_id"] for row in store.rows["utterance"]) == ["u0", "u1"], "the orphan must be retired"
    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("healed", "complete")]
    assert executions["split"] == 1, "the fan-out boundary re-ran to rebuild the item list"
    assert executions["child"] == 0, "no child row had to be re-derived"
    assert executions["clean"] == 0, "parent derived columns must not re-derive"

    # insert() repairs the same damage the same way: base reported `skipped`
    # here while sync() reported `healed` for identical work.
    orphan = dict(store.rows["utterance"][0])
    orphan["utterance_id"] = "u8"
    store.rows["utterance"].append(orphan)
    _reset_counters()

    receipt = _fanout_table(MemoryStore(store.rows)).insert([{"doc_id": "d1", "text": "alpha beta"}])

    assert sorted(row["utterance_id"] for row in store.rows["utterance"]) == ["u0", "u1"]
    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("healed", "complete")]
    assert executions["split"] == 1


# ---------------------------------------------------------------------------
# 2. A repair that failed again healed nothing
# ---------------------------------------------------------------------------


def test_a_repair_whose_retry_fails_again_does_not_claim_healed():
    """sync() reported ("healed", "complete") for a retry that re-stored the
    same child error row. Rows were written, so it is not a skip either: the
    receipt says UPDATED and the damage is still discoverable."""

    store = MemoryStore()
    fail_on_word.add("beta")
    _fanout_table(store).insert(doc_id="d1", text="alpha beta")

    second = _fanout_table(MemoryStore(store.rows))
    receipt = second.sync([{"doc_id": "d1", "text": "alpha beta"}])

    assert [row.outcome.value for row in receipt.receipts] == ["updated"]
    assert receipt.healed == 0, "nothing was healed — the child is still stored in error"
    assert receipt.skipped == 0, "the retry wrote a row"
    assert _child_state(store) == {"u0": ("complete", "t:alpha"), "u1": ("error", None)}
    assert [row.id for row in second.child("utterance").errors()] == ["u1"]

    # And the next attempt, with the failure fixed, is a real heal.
    fail_on_word.discard("beta")
    third = _fanout_table(MemoryStore(store.rows))
    assert third.sync([{"doc_id": "d1", "text": "alpha beta"}]).healed == 1


# ---------------------------------------------------------------------------
# 3. A metadata-only update writes a row
# ---------------------------------------------------------------------------


def test_metadata_only_update_reports_updated_not_skipped():
    """No derivation runs, but the stored row is rewritten with the new value,
    so the receipt reports the write it made."""

    store = MemoryStore()
    table = _flat_table(store)
    table.insert(doc_id="d1", text="hello", title="Old Title")
    executions["clean"] = 0

    receipt = table.update("d1", title="New Title")

    assert receipt.outcome is WriteOutcome.UPDATED
    assert table.get("d1")["title"] == "New Title"
    assert executions["clean"] == 0, "a metadata-only update must still derive nothing"


# ---------------------------------------------------------------------------
# 4. One decoder for the stored _status column
# ---------------------------------------------------------------------------


def test_a_row_stored_without_a_status_column_reads_as_complete():
    """Rows written before ``_status`` existed carry no value there; every raw
    comparison the codec replaces had to spell that legacy state out by hand."""

    store = MemoryStore()
    table = _flat_table(store)
    table.insert(doc_id="d1", text="hello")
    for row in store.rows["doc"]:
        row.pop("_status", None)

    reader = _flat_table(MemoryStore(store.rows))
    assert reader.status().errored == 0
    assert reader.status().is_fresh
    receipt = reader.sync([{"doc_id": "d1", "text": "hello"}])
    assert [row.outcome.value for row in receipt.receipts] == ["skipped"]


def test_an_unknown_stored_status_is_refused_with_a_clear_message():
    """A store that does not preserve ``_status`` used to read as "not an
    error" at ten separate comparisons. The codec refuses the value instead."""

    store = MemoryStore()
    table = _flat_table(store)
    table.insert(doc_id="d1", text="hello")
    for row in store.rows["doc"]:
        row["_status"] = "finished"

    reader = _flat_table(MemoryStore(store.rows))
    with pytest.raises(ValueError, match="unknown _status value"):
        reader.status()
    with pytest.raises(ValueError, match="How to fix: preserve the HyperTable-managed _status value"):
        reader.sync([{"doc_id": "d1", "text": "hello"}])


# ---------------------------------------------------------------------------
# 5. Items that share a child identity occupy one child row (#470)
# ---------------------------------------------------------------------------


@node(output_name="utterances")
def split_words_by_content(text: str) -> list[Utterance]:
    executions["split"] += 1
    return [Utterance(utterance_id=word, text=word) for word in text.split()]


@node(output_name="utterances")
def split_words_without_identity(text: str) -> list[dict[str, str]]:
    """The first item carries an explicit empty identity; the rest carry none."""
    executions["split"] += 1
    first, *rest = text.split()
    return [{"utterance_id": "", "text": first}, *({"text": word} for word in rest)]


def _content_identity_table(store, splitter=split_words_by_content):
    return Graph(
        [splitter, process_word.as_node().map_over("utterances", identity="utterance_id")],
        name="doc",
    ).as_table(identity="doc_id", store=store, on_error="store", runner=SyncRunner())


def _parent_row(store) -> dict[str, Any]:
    return max(store.rows["doc"], key=lambda row: row["_write_gen"])


def _recorded_child_count(store) -> int | None:
    return split_boundary_provenance(_parent_row(store)["_provenance_utterances"])[1]


def _snapshot(store) -> dict[str, list[dict[str, Any]]]:
    return {name: [row.copy() for row in rows] for name, rows in store.rows.items()}


def test_a_repeated_child_identity_settles_to_a_zero_work_skip():
    """Two mapped items with the same identity occupy ONE child row, because
    the logical child key is (parent identity, child identity). The parent row
    used to record the raw item count (3) while every freshness check counted
    deduplicated child rows (2), so the two could never agree: every write
    re-ran the fan-out boundary and reported HEALED forever, with nothing
    damaged and nothing derived. The recorded count is now the number of
    distinct child identities, so an untouched row is a SKIPPED that runs
    nothing and writes nothing."""

    store = MemoryStore()
    _content_identity_table(store).insert(doc_id="d1", text="alpha beta alpha")

    assert _recorded_child_count(store) == 2, "the stamp records distinct child identities, not items"

    for attempt in range(4):
        _reset_counters()
        before = _snapshot(store)
        receipt = _content_identity_table(MemoryStore(store.rows)).sync([{"doc_id": "d1", "text": "alpha beta alpha"}])

        assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("skipped", "complete")], f"sync #{attempt + 1}"
        assert (executions["split"], executions["child"]) == (0, 0), f"sync #{attempt + 1} must not re-run the boundary"
        assert store.rows == before, f"sync #{attempt + 1} must not write"

    _reset_counters()
    receipt = _content_identity_table(MemoryStore(store.rows)).insert([{"doc_id": "d1", "text": "alpha beta alpha"}])

    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("skipped", "complete")]
    assert (executions["split"], executions["child"]) == (0, 0)
    assert _content_identity_table(MemoryStore(store.rows)).status().is_fresh


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("alpha beta gamma", 3),
        ("alpha beta alpha", 2),
        ("alpha beta alpha alpha", 2),
    ],
)
def test_the_recorded_child_count_tracks_distinct_identities(text, expected):
    """Falsifier: the stamp is not a constant. A unique third word is a third
    child row; a third repeat of an existing word is not."""

    store = MemoryStore()
    _content_identity_table(store).insert(doc_id="d1", text=text)

    assert _recorded_child_count(store) == expected
    assert _recorded_child_count(store) == len({row["utterance_id"] for row in store.rows["utterance"]})


def test_an_item_without_the_identity_field_counts_as_the_empty_identity():
    """Failure path: an item that does not carry the child identity is stored
    under the empty identity, so it shares one child row with an item that
    carries ``""`` explicitly. The recorded count keys a missing field as
    ``""`` exactly as the deduplicating read does, so the two agree and
    nothing raises."""

    store = MemoryStore()
    _content_identity_table(store, split_words_without_identity).insert(doc_id="d1", text="alpha beta")

    assert _recorded_child_count(store) == 1
    _reset_counters()
    receipt = _content_identity_table(MemoryStore(store.rows), split_words_without_identity).sync([{"doc_id": "d1", "text": "alpha beta"}])

    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("skipped", "complete")]
    assert (executions["split"], executions["child"]) == (0, 0)


@node(output_name=("utterances", "word_count"))
def split_words_by_content_counting(text: str) -> tuple[list[Utterance], int]:
    """The fan-out boundary ALSO produces the stored parent column ``word_count``."""
    executions["split"] += 1
    words = text.split()
    return [Utterance(utterance_id=word, text=word) for word in words], len(words)


@pytest.mark.parametrize(
    "splitter",
    [
        pytest.param(split_words_by_content, id="plain-boundary"),
        pytest.param(split_words_by_content_counting, id="counting-boundary"),
    ],
)
def test_a_row_stamped_with_the_raw_item_count_resettles_after_one_boundary_run(splitter):
    """Migration: a row written before #470 recorded the raw item count. Over a
    colliding child set that stamp disagrees with the store once, so the next
    sync() re-runs the boundary exactly once and re-stamps the distinct count;
    every sync() after that is a zero-execution, zero-write SKIPPED. Both
    boundary shapes converge: the plain one through the reconcile path, the one
    that also produces a stored parent column through the whole-graph repair."""

    store = MemoryStore()
    _content_identity_table(store, splitter).insert(doc_id="d1", text="alpha beta alpha")
    parent = _parent_row(store)
    provenance, _ = split_boundary_provenance(parent["_provenance_utterances"])
    parent["_provenance_utterances"] = f"{provenance}#3"
    _reset_counters()

    _content_identity_table(MemoryStore(store.rows), splitter).sync([{"doc_id": "d1", "text": "alpha beta alpha"}])

    assert executions["split"] == 1, "the old stamp re-runs the boundary once"
    assert executions["child"] == 0, "no child row is re-derived"
    assert _recorded_child_count(store) == 2, "the migration pass re-stamps the distinct count"

    for attempt in range(2):
        _reset_counters()
        before = _snapshot(store)
        receipt = _content_identity_table(MemoryStore(store.rows), splitter).sync([{"doc_id": "d1", "text": "alpha beta alpha"}])

        assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("skipped", "complete")], f"sync #{attempt + 1}"
        assert (executions["split"], executions["child"]) == (0, 0), f"sync #{attempt + 1} must not re-run the boundary"
        assert store.rows == before, f"sync #{attempt + 1} must not write"
