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
