"""A stored child error row is damage, and sync() repairs it (#314).

With ``on_error="store"`` a child whose derivation raises is kept as an error
row so its siblings can land. That row is a row, so the unchanged-parent
completeness probe used to count it as PRESENT and ``sync()`` skipped the
parent forever — ``insert()`` was the only repair path. Root rows got the
retry contract in #205; this is the same contract one level down:

- unchanged parent, every child ``complete`` -> today's fast path: ``skipped``,
  zero derivation executions, zero writes;
- unchanged parent, some child ``error``    -> the stored item list is reused,
  ONLY the failed child runs the child graph, the parent and its complete
  children are not re-derived, and the receipt reports ``healed``;
- the retry fails again                      -> the row stays in error and is
  still there to retry next time.

Under ``on_error="raise"`` nothing is stored, so there is nothing to retry.

When the fan-out boundary also produces a stored parent column (#468), the
repair cannot be column-scoped: sync() takes the same whole-graph repair
insert() takes, with the same receipt and the same execution counts. The
parent row is rewritten only when its recorded boundary stamp changed, so a
stale count is corrected once and the next sync() settles. Healthy children
keep the zero-write skip.
"""

from __future__ import annotations

from typing import Any, TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import TableStore
from hypergraph.runners import AsyncRunner, SyncRunner


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
    appended_words.clear()
    executions["clean"] = executions["split"] = executions["child"] = 0


def _table(store, *, runner=None, on_error="store"):
    return Graph(
        [clean, split_words, process_word.as_node().map_over("utterances", identity="utterance_id")],
        name="doc",
    ).as_table(identity="doc_id", store=store, on_error=on_error, runner=runner or SyncRunner())


def _child_state(store):
    return {row["utterance_id"]: (row["_status"], row.get("clean_word")) for row in store.rows["utterance"]}


def _reset_counters():
    executions["clean"] = executions["split"] = executions["child"] = 0


# ---------------------------------------------------------------------------
# 1. RED: the stored child error row is retried by the next sync()
# ---------------------------------------------------------------------------


def test_sync_retries_stored_child_error_row():
    """On master the completeness probe counts the error row as present, so the
    fast path skips the parent and the child is never retried (zero node calls).
    After the fix the same sync() re-runs exactly that child and repairs it."""

    store = MemoryStore()
    fail_on_word.add("beta")
    _table(store).insert(doc_id="d1", text="alpha beta")
    assert _child_state(store) == {"u0": ("complete", "t:alpha"), "u1": ("error", None)}

    fail_on_word.discard("beta")
    _reset_counters()

    # A FRESH handle over the same physical rows: the retry must be decided from
    # the store, not from same-handle caches.
    fresh = _table(MemoryStore(store.rows))
    receipt = fresh.sync([{"doc_id": "d1", "text": "alpha beta"}])

    assert _child_state(store) == {"u0": ("complete", "t:alpha"), "u1": ("complete", "t:beta")}
    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("healed", "complete")]
    assert fresh.child("utterance").errors() == ()
    assert executions["child"] == 1, "only the failed child may re-derive"
    assert executions["split"] == 0, "the stored item list is intact — the boundary must not re-run"
    assert executions["clean"] == 0, "parent derived columns must not re-derive"


def test_failed_retry_leaves_the_row_in_error_to_retry_again():
    """The falsifier for the test above: when the retry fails too, the child row
    stays in error (with the fresh message) and the next sync tries again."""

    store = MemoryStore()
    fail_on_word.add("beta")
    _table(store).insert(doc_id="d1", text="alpha beta")

    second = _table(MemoryStore(store.rows))
    second.sync([{"doc_id": "d1", "text": "alpha beta"}])

    assert _child_state(store) == {"u0": ("complete", "t:alpha"), "u1": ("error", None)}
    errors = second.child("utterance").errors()
    assert [row.id for row in errors] == ["u1"]
    assert "RuntimeError: boom on beta" in errors[0].error

    # Still retried after the failure is fixed — the error row was not poisoned.
    fail_on_word.discard("beta")
    third = _table(MemoryStore(store.rows))
    third.sync([{"doc_id": "d1", "text": "alpha beta"}])
    assert _child_state(store) == {"u0": ("complete", "t:alpha"), "u1": ("complete", "t:beta")}


# ---------------------------------------------------------------------------
# 2. The zero-execution skip is preserved for non-error children
# ---------------------------------------------------------------------------


def test_one_error_child_among_nine_complete_runs_the_child_graph_once():
    store = MemoryStore()
    text = "w0 w1 w2 w3 w4 w5 w6 w7 w8 w9"
    fail_on_word.add("w4")
    _table(store).insert(doc_id="d1", text=text)
    assert sum(row["_status"] == "error" for row in store.rows["utterance"]) == 1

    fail_on_word.discard("w4")
    _reset_counters()

    receipt = _table(MemoryStore(store.rows)).sync([{"doc_id": "d1", "text": text}])

    assert executions["child"] == 1, "nine present children must not re-derive"
    assert receipt.healed == 1
    assert {row["utterance_id"]: row["clean_word"] for row in store.rows["utterance"]} == {f"u{i}": f"t:w{i}" for i in range(10)}


def test_all_children_complete_stays_a_zero_write_skip():
    store = MemoryStore()
    table = _table(store)
    table.sync([{"doc_id": "d1", "text": "alpha beta"}])
    snapshot = {name: [row.copy() for row in rows] for name, rows in store.rows.items()}
    _reset_counters()

    receipt = table.sync([{"doc_id": "d1", "text": "alpha beta"}])

    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("skipped", "complete")]
    assert (executions["clean"], executions["split"], executions["child"]) == (0, 0, 0)
    assert store.rows == snapshot, "the fast path must stay zero-write"


# ---------------------------------------------------------------------------
# 3. on_error matrix: "raise" stores nothing, so there is nothing to retry
# ---------------------------------------------------------------------------


def test_on_error_raise_still_raises_out_of_sync():
    store = MemoryStore()
    fail_on_word.add("beta")

    with pytest.raises(RuntimeError, match="boom on beta"):
        _table(store, on_error="raise").sync([{"doc_id": "d1", "text": "alpha beta"}])

    assert [row for row in store.rows["utterance"] if row["_status"] == "error"] == [], "on_error='raise' stores no error row to retry"


# ---------------------------------------------------------------------------
# 4. Generation ordering (#311 contract) and fresh-handle visibility
# ---------------------------------------------------------------------------


def test_repaired_child_lands_above_the_error_row_and_the_error_row_is_gone():
    store = MemoryStore()
    fail_on_word.add("beta")
    _table(store).insert(doc_id="d1", text="alpha beta")
    error_gen = next(row["_write_gen"] for row in store.rows["utterance"] if row["utterance_id"] == "u1")

    fail_on_word.discard("beta")
    _table(MemoryStore(store.rows)).sync([{"doc_id": "d1", "text": "alpha beta"}])

    physical = [row for row in store.rows["utterance"] if row["utterance_id"] == "u1"]
    assert len(physical) == 1, "the superseded error row must be cleaned up, not left to tie"
    assert physical[0]["_write_gen"] > error_gen
    assert physical[0]["_status"] == "complete"

    reader = _table(MemoryStore(store.rows))
    assert [child["clean_word"] for child in reader.child("utterance").rows(parent="d1")] == ["t:alpha", "t:beta"]


def test_sync_retries_stored_child_error_row_lancedb_fresh_handles(tmp_path):
    from hypergraph.materialization import LanceDBStore

    path = str(tmp_path / "retry_store")
    fail_on_word.add("beta")
    _table(LanceDBStore(path)).insert(doc_id="d1", text="alpha beta")

    inspect_store = LanceDBStore(path)
    error_gen = next(row["_write_gen"] for row in inspect_store.read_rows("utterance") if row["utterance_id"] == "u1")

    fail_on_word.discard("beta")
    _reset_counters()
    receipt = _table(LanceDBStore(path)).sync([{"doc_id": "d1", "text": "alpha beta"}])
    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("healed", "complete")]
    assert executions["child"] == 1

    read_store = LanceDBStore(path)
    physical = [row for row in read_store.read_rows("utterance") if row["utterance_id"] == "u1"]
    assert len(physical) == 1
    assert physical[0]["_write_gen"] > error_gen
    inspector = _table(read_store)
    assert inspector.child("utterance").errors() == ()
    assert [child["clean_word"] for child in inspector.child("utterance").rows(parent="d1")] == ["t:alpha", "t:beta"]


# ---------------------------------------------------------------------------
# 5. Async parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_sync_retries_stored_child_error_row():
    store = MemoryStore()
    fail_on_word.add("beta")
    await _table(store, runner=AsyncRunner()).insert(doc_id="d1", text="alpha beta")
    assert _child_state(store) == {"u0": ("complete", "t:alpha"), "u1": ("error", None)}

    fail_on_word.discard("beta")
    _reset_counters()

    fresh = _table(MemoryStore(store.rows), runner=AsyncRunner())
    receipt = await fresh.sync([{"doc_id": "d1", "text": "alpha beta"}])

    assert _child_state(store) == {"u0": ("complete", "t:alpha"), "u1": ("complete", "t:beta")}
    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("healed", "complete")]
    assert (executions["clean"], executions["split"], executions["child"]) == (0, 0, 1)


@pytest.mark.asyncio
async def test_async_all_children_complete_stays_a_zero_write_skip():
    store = MemoryStore()
    table = _table(store, runner=AsyncRunner())
    await table.sync([{"doc_id": "d1", "text": "alpha beta"}])
    snapshot = {name: [row.copy() for row in rows] for name, rows in store.rows.items()}
    _reset_counters()

    receipt = await table.sync([{"doc_id": "d1", "text": "alpha beta"}])

    assert [(row.outcome.value, row.status.value) for row in receipt.receipts] == [("skipped", "complete")]
    assert (executions["clean"], executions["split"], executions["child"]) == (0, 0, 0)
    assert store.rows == snapshot


# ---------------------------------------------------------------------------
# 6. A fan-out boundary that also produces a stored parent column (#468)
# ---------------------------------------------------------------------------


appended_words: list[str] = []  # lets one input split differently between runs (an LLM splitter, say)


@node(output_name=("utterances", "word_count"))
def split_words_counting(text: str) -> tuple[list[Utterance], int]:
    """The fan-out boundary ALSO produces the stored parent column ``word_count``."""
    executions["split"] += 1
    words = text.split() + appended_words
    return [Utterance(utterance_id=f"u{i}", text=word) for i, word in enumerate(words)], len(words)


def _counting_table(store, *, runner=None, on_error="store"):
    return Graph(
        [split_words_counting, process_word.as_node().map_over("utterances", identity="utterance_id")],
        name="doc",
    ).as_table(identity="doc_id", store=store, on_error=on_error, runner=runner or SyncRunner())


def _damaged_counting_store() -> MemoryStore:
    store = MemoryStore()
    fail_on_word.add("beta")
    _counting_table(store).insert(doc_id="d1", text="alpha beta")
    assert _child_state(store) == {"u0": ("complete", "t:alpha"), "u1": ("error", None)}
    fail_on_word.discard("beta")
    _reset_counters()
    return store


def _outcomes(receipt) -> list[tuple[str, str]]:
    return [(row.outcome.value, row.status.value) for row in receipt.receipts]


def test_sync_heals_a_child_error_under_a_counting_boundary():
    """On master the unchanged-parent probe skipped this child spec outright, so
    sync() reported SKIPPED forever and status() never became fresh. The same
    damage is now repaired the way insert() repairs it: the graph runs once for
    the row, the failed child is rebuilt, the parent row is not rewritten."""

    store = _damaged_counting_store()
    parent_rows = [row.copy() for row in store.rows["doc"]]
    fresh = _counting_table(MemoryStore(store.rows))
    assert fresh.status().is_fresh is False

    receipt = fresh.sync([{"doc_id": "d1", "text": "alpha beta"}])

    assert _outcomes(receipt) == [("healed", "complete")]
    assert (receipt.healed, receipt.skipped) == (1, 0)
    assert _child_state(store) == {"u0": ("complete", "t:alpha"), "u1": ("complete", "t:beta")}
    assert fresh.child("utterance").errors() == ()
    assert executions == {"clean": 0, "split": 1, "child": 1}
    assert store.rows["doc"] == parent_rows, "the parent row must not be rewritten"
    assert fresh.status().is_fresh is True


def test_sync_and_insert_agree_under_a_counting_boundary():
    """One damage, one answer: insert() and sync() over the identical store
    produce the same receipt, child state, stored errors and execution counts."""

    observed = {}
    for verb in ("insert", "sync"):
        store = _damaged_counting_store()
        table = _counting_table(MemoryStore(store.rows))
        receipt = getattr(table, verb)([{"doc_id": "d1", "text": "alpha beta"}])
        observed[verb] = (
            _outcomes(receipt),
            _child_state(store),
            [row.id for row in table.child("utterance").errors()],
            dict(executions),
        )

    assert observed["sync"] == observed["insert"]
    assert observed["insert"][0] == [("healed", "complete")], "the shared answer is a heal"


def test_counting_boundary_healthy_children_stay_a_zero_write_skip():
    """The falsifier: with every child complete the probe finds no damage, so
    the boundary never runs and nothing is written."""

    store = MemoryStore()
    _counting_table(store).sync([{"doc_id": "d1", "text": "alpha beta"}])
    snapshot = {name: [row.copy() for row in rows] for name, rows in store.rows.items()}
    _reset_counters()

    receipt = _counting_table(MemoryStore(store.rows)).sync([{"doc_id": "d1", "text": "alpha beta"}])

    assert _outcomes(receipt) == [("skipped", "complete")]
    assert executions == {"clean": 0, "split": 0, "child": 0}
    assert store.rows == snapshot, "the fast path must stay zero-write"


def test_counting_boundary_retry_that_fails_again_reports_updated():
    """R13: a repair whose retry fails again healed nothing, so it reports
    UPDATED, the child stays in error, and the next sync() after the fix heals."""

    store = MemoryStore()
    fail_on_word.add("beta")
    _counting_table(store).insert(doc_id="d1", text="alpha beta")

    second = _counting_table(MemoryStore(store.rows))
    receipt = second.sync([{"doc_id": "d1", "text": "alpha beta"}])

    assert _outcomes(receipt) == [("updated", "complete")]
    assert (receipt.healed, receipt.skipped) == (0, 0)
    assert _child_state(store) == {"u0": ("complete", "t:alpha"), "u1": ("error", None)}
    assert [row.id for row in second.child("utterance").errors()] == ["u1"]

    fail_on_word.discard("beta")
    assert _counting_table(MemoryStore(store.rows)).sync([{"doc_id": "d1", "text": "alpha beta"}]).healed == 1


@pytest.mark.asyncio
async def test_async_sync_heals_a_child_error_under_a_counting_boundary():
    store = MemoryStore()
    fail_on_word.add("beta")
    await _counting_table(store, runner=AsyncRunner()).insert(doc_id="d1", text="alpha beta")
    assert _child_state(store) == {"u0": ("complete", "t:alpha"), "u1": ("error", None)}

    fail_on_word.discard("beta")
    _reset_counters()

    fresh = _counting_table(MemoryStore(store.rows), runner=AsyncRunner())
    receipt = await fresh.sync([{"doc_id": "d1", "text": "alpha beta"}])

    assert _outcomes(receipt) == [("healed", "complete")]
    assert _child_state(store) == {"u0": ("complete", "t:alpha"), "u1": ("complete", "t:beta")}
    assert executions == {"clean": 0, "split": 1, "child": 1}


# ---------------------------------------------------------------------------
# 7. The repair corrects the parent's recorded fan-out count, so it settles
# ---------------------------------------------------------------------------
#
# The probe compares the parent's ``<provenance>#<count>`` stamp against the
# child rows present. Under a counting boundary the repair runs the graph; if
# it did not restamp the parent, a stamp that disagrees with healthy children
# would send every later sync() back through the graph (review of #468, D27).

DOC = {"doc_id": "d1", "text": "alpha beta"}


def _parent(store) -> dict[str, Any]:
    assert len(store.rows["doc"]) == 1, "a restamped parent row retires the one it replaces"
    return store.rows["doc"][0]


def _stamp_count(store) -> int:
    return int(_parent(store)["_provenance_utterances"].rpartition("#")[2])


def _snapshot(store) -> dict[str, list[dict[str, Any]]]:
    return {name: [row.copy() for row in rows] for name, rows in store.rows.items()}


def _stale_counting_store() -> MemoryStore:
    """Healthy children, but the parent records one item more than exists (a stale or legacy stamp)."""
    store = MemoryStore()
    _counting_table(store).sync([DOC])
    provenance, _, count = _parent(store)["_provenance_utterances"].rpartition("#")
    _parent(store)["_provenance_utterances"] = f"{provenance}#{int(count) + 1}"
    _reset_counters()
    return store


@pytest.mark.parametrize("verb", ["sync", "insert"])
def test_counting_boundary_stale_stamp_is_corrected_once_then_skips(verb):
    store = _stale_counting_store()
    assert _stamp_count(store) == 3

    getattr(_counting_table(MemoryStore(store.rows)), verb)([DOC])

    assert executions == {"clean": 0, "split": 1, "child": 0}, "the boundary re-runs once; no child re-derives"
    assert _stamp_count(store) == 2, "the parent's recorded count now matches the children present"
    assert _child_state(store) == {"u0": ("complete", "t:alpha"), "u1": ("complete", "t:beta")}
    snapshot = _snapshot(store)
    _reset_counters()

    receipt = _counting_table(MemoryStore(store.rows)).sync([DOC])

    assert _outcomes(receipt) == [("skipped", "complete")]
    assert executions == {"clean": 0, "split": 0, "child": 0}
    assert store.rows == snapshot, "settled: the next sync() writes nothing"


def test_counting_boundary_whose_item_count_changed_is_restamped_by_the_heal():
    """The boundary now yields three items for the same input. The heal writes
    the new child and rewrites the parent from this run's outputs, so the
    recorded count and ``word_count`` agree with the three children present."""

    store = _damaged_counting_store()
    appended_words.append("gamma")

    receipt = _counting_table(MemoryStore(store.rows)).sync([DOC])

    assert _outcomes(receipt) == [("healed", "complete")]
    assert _child_state(store) == {"u0": ("complete", "t:alpha"), "u1": ("complete", "t:beta"), "u2": ("complete", "t:gamma")}
    assert (_stamp_count(store), _parent(store)["word_count"]) == (3, 3)
    snapshot = _snapshot(store)
    _reset_counters()

    receipt = _counting_table(MemoryStore(store.rows)).sync([DOC])

    assert _outcomes(receipt) == [("skipped", "complete")]
    assert executions == {"clean": 0, "split": 0, "child": 0}
    assert store.rows == snapshot


@pytest.mark.asyncio
async def test_async_counting_boundary_stale_stamp_is_corrected_once_then_skips():
    store = _stale_counting_store()

    await _counting_table(MemoryStore(store.rows), runner=AsyncRunner()).sync([DOC])

    assert executions == {"clean": 0, "split": 1, "child": 0}
    assert _stamp_count(store) == 2
    snapshot = _snapshot(store)
    _reset_counters()

    receipt = await _counting_table(MemoryStore(store.rows), runner=AsyncRunner()).sync([DOC])

    assert _outcomes(receipt) == [("skipped", "complete")]
    assert executions == {"clean": 0, "split": 0, "child": 0}
    assert store.rows == snapshot


@pytest.mark.asyncio
async def test_async_counting_boundary_whose_item_count_changed_is_restamped_by_the_heal():
    store = _damaged_counting_store()
    appended_words.append("gamma")

    receipt = await _counting_table(MemoryStore(store.rows), runner=AsyncRunner()).sync([DOC])

    assert _outcomes(receipt) == [("healed", "complete")]
    assert (_stamp_count(store), _parent(store)["word_count"]) == (3, 3)
    snapshot = _snapshot(store)
    _reset_counters()

    receipt = await _counting_table(MemoryStore(store.rows), runner=AsyncRunner()).sync([DOC])

    assert _outcomes(receipt) == [("skipped", "complete")]
    assert executions == {"clean": 0, "split": 0, "child": 0}
    assert store.rows == snapshot


@pytest.mark.asyncio
async def test_async_counting_boundary_healthy_children_stay_a_zero_write_skip():
    store = MemoryStore()
    await _counting_table(store, runner=AsyncRunner()).sync([DOC])
    snapshot = _snapshot(store)
    _reset_counters()

    receipt = await _counting_table(MemoryStore(store.rows), runner=AsyncRunner()).sync([DOC])

    assert _outcomes(receipt) == [("skipped", "complete")]
    assert executions == {"clean": 0, "split": 0, "child": 0}
    assert store.rows == snapshot, "the fast path must stay zero-write"
