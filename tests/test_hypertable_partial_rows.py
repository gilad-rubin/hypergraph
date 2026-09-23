"""One failing node degrades its own column, not the whole row (#323)."""

from __future__ import annotations

from typing import TypedDict

import pytest

from hypergraph import Graph, node
from hypergraph.materialization import ChangeReason, RowStatus, WriteOutcome
from hypergraph.runners import AsyncRunner, SyncRunner
from tests.test_hypertable_on_error import MemoryStore

# ---------------------------------------------------------------------------
# A three-stage recipe: the expensive stages come first, the fragile one last.
# ---------------------------------------------------------------------------

calls: dict[str, int] = {}
failing: set[str] = set()


def _record(name: str) -> None:
    calls[name] = calls.get(name, 0) + 1
    if name in failing:
        raise RuntimeError(f"{name} service is down")


@node(output_name="extracted")
def extract(text: str) -> str:
    _record("extract")
    return text.upper()


@node(output_name="summary")
def summarize(extracted: str) -> str:
    _record("summarize")
    return extracted[:5]


@node(output_name="embedding")
def embed(summary: str) -> str:
    _record("embed")
    return f"vec({summary})"


# A branch the failure cannot reach, and a node that legitimately returns None.


@node(output_name="aside")
def aside(extracted: str) -> str:
    _record("aside")
    return f"aside({extracted})"


@node(output_name="maybe")
def maybe(extracted: str) -> str | None:
    _record("maybe")
    return None


@node(output_name="tail")
def tail(maybe: str | None) -> str:
    _record("tail")
    return f"tail({maybe})"


@pytest.fixture(autouse=True)
def _reset() -> None:
    calls.clear()
    failing.clear()


def _table(store: MemoryStore, runner=None, **kwargs):
    return Graph([extract, summarize, embed]).as_table(
        identity="doc_id",
        store=store,
        on_error="store",
        runner=runner or SyncRunner(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The record shape
# ---------------------------------------------------------------------------


def test_a_late_failure_keeps_the_earlier_columns_and_names_the_failed_one() -> None:
    table = _table(MemoryStore())
    failing.add("embed")

    receipt = table.insert(doc_id="d1", text="hello world")

    row = table.get("d1")
    assert row["extracted"] == "HELLO WORLD"
    assert row["summary"] == "HELLO"
    assert row["embedding"] is None
    assert receipt.status is RowStatus.PARTIAL
    assert not receipt.failed
    assert table.errors() == ()

    (partial,) = table.partial()
    assert partial.id == "d1"
    (change,) = partial.changes
    assert change.column == "embedding"
    assert change.reason is ChangeReason.NODE_ERROR
    assert change.node == "embed"
    assert change.error == "RuntimeError: embed service is down"


def test_a_column_the_failure_blocked_reads_as_an_upstream_error() -> None:
    table = _table(MemoryStore())
    failing.add("summarize")

    table.insert(doc_id="d1", text="hello world")

    (partial,) = table.partial()
    assert partial.row["extracted"] == "HELLO WORLD"
    by_column = {change.column: change for change in partial.changes}
    assert by_column["summary"].reason is ChangeReason.NODE_ERROR
    assert by_column["embedding"].reason is ChangeReason.UPSTREAM_ERROR
    assert by_column["embedding"].node == "embed"
    assert by_column["embedding"].error is None
    assert calls.get("embed") is None


def test_a_column_the_failure_cannot_reach_reads_as_not_run() -> None:
    """`aside` does not depend on the failed node — the run simply ended first."""
    store = MemoryStore()
    table = Graph([extract, summarize, embed, aside]).as_table(
        identity="doc_id",
        store=store,
        on_error="store",
        runner=SyncRunner(),
    )
    failing.add("summarize")

    table.insert(doc_id="d1", text="hello world")

    assert calls.get("aside") is None, "the superstep ended before aside was scheduled"
    (partial,) = table.partial()
    by_column = {change.column: change for change in partial.changes}
    assert by_column["summary"].reason is ChangeReason.NODE_ERROR
    assert by_column["embedding"].reason is ChangeReason.UPSTREAM_ERROR
    assert by_column["aside"].reason is ChangeReason.NOT_RUN
    assert by_column["aside"].node == "aside"
    assert by_column["aside"].error is None
    assert partial.row["extracted"] == "HELLO WORLD"


def test_a_node_that_returned_none_is_a_value_not_a_change() -> None:
    """`maybe` ran and returned None. Nothing failed on it, so nothing is recorded."""
    store = MemoryStore()
    table = Graph([extract, maybe, tail]).as_table(
        identity="doc_id",
        store=store,
        on_error="store",
        runner=SyncRunner(),
    )
    failing.add("tail")

    table.insert(doc_id="d1", text="hello world")

    assert calls["maybe"] == 1
    (partial,) = table.partial()
    assert [change.column for change in partial.changes] == ["tail"]
    assert partial.changes[0].reason is ChangeReason.NODE_ERROR
    assert partial.row["maybe"] is None
    assert partial.row["extracted"] == "HELLO WORLD"


def test_a_partial_row_reads_as_needing_heal() -> None:
    table = _table(MemoryStore())
    failing.add("embed")
    table.insert(doc_id="d1", text="hello world")

    status = table.status()

    assert status.total == 1
    assert status.fresh == 0
    assert status.stale == 1
    assert status.errored == 0
    assert status.stale_ids == ("d1",)
    assert dict(status.stale_columns)["embedding"] == 1
    assert not status.is_fresh


# ---------------------------------------------------------------------------
# The retry
# ---------------------------------------------------------------------------


def test_healing_a_partial_row_re_derives_only_the_nulled_column() -> None:
    table = _table(MemoryStore())
    failing.add("embed")
    table.insert(doc_id="d1", text="hello world")
    assert calls == {"extract": 1, "summarize": 1, "embed": 1}

    failing.clear()
    receipt = table.sync([{"doc_id": "d1", "text": "hello world"}])

    assert calls == {"extract": 1, "summarize": 1, "embed": 2}, "the expensive stages must not be paid twice"
    assert table.get("d1")["embedding"] == "vec(HELLO)"
    assert table.partial() == ()
    assert table.status().is_fresh
    assert receipt.completed


def test_a_second_failure_keeps_what_the_first_one_saved() -> None:
    table = _table(MemoryStore())
    failing.add("embed")
    table.insert(doc_id="d1", text="hello world")

    table.sync([{"doc_id": "d1", "text": "hello world"}])

    assert calls == {"extract": 1, "summarize": 1, "embed": 2}
    row = table.get("d1")
    assert row["extracted"] == "HELLO WORLD"
    assert row["summary"] == "HELLO"
    assert row["embedding"] is None
    assert table.errors() == ()
    (partial,) = table.partial()
    assert [change.column for change in partial.changes] == ["embedding"]


def test_a_source_change_still_re_derives_the_whole_row() -> None:
    table = _table(MemoryStore())
    failing.add("embed")
    table.insert(doc_id="d1", text="hello world")
    failing.clear()

    table.sync([{"doc_id": "d1", "text": "goodbye world"}])

    assert calls == {"extract": 2, "summarize": 2, "embed": 2}
    assert table.get("d1")["embedding"] == "vec(GOODB)"
    assert table.partial() == ()


# ---------------------------------------------------------------------------
# What a partial row refuses to claim
# ---------------------------------------------------------------------------


def test_a_first_node_failure_is_still_a_total_loss_error_row() -> None:
    """Nothing survived, so there is no column granularity to claim."""
    table = _table(MemoryStore())
    failing.add("extract")

    receipt = table.insert(doc_id="d1", text="hello world")

    assert receipt.failed
    assert receipt.status is RowStatus.ERROR
    assert table.partial() == ()
    assert table.errors()[0].id == "d1"
    assert "RuntimeError: extract service is down" in table.errors()[0].error
    assert table.get("d1")["extracted"] is None


def test_an_unattributable_failure_is_still_a_total_loss_error_row() -> None:
    """A plan-level failure names no node, so the row keeps its old shape."""
    store = MemoryStore()
    table = _table(store)

    receipt = table.insert(doc_id="d1")

    assert receipt.failed
    assert "MissingInputError" in receipt.error
    assert table.partial() == ()
    assert table.errors()[0].id == "d1"


@node
def notify(extracted: str) -> None:
    _record("notify")


def _notify_table(nodes, runner=None):
    return Graph(nodes).as_table(identity="doc_id", store=MemoryStore(), on_error="store", runner=runner or SyncRunner())


def test_a_failing_node_with_no_output_is_still_a_total_loss_error_row() -> None:
    """`notify` owns no stored column: no change entry can name it, and a partial
    row's column-scoped heal would never run it again — so the row stays ERROR
    and the retry re-runs it."""
    table = _notify_table([extract, summarize, notify])
    failing.add("notify")

    receipt = table.insert(doc_id="d1", text="hello world")

    assert receipt.status is RowStatus.ERROR
    assert table.partial() == ()

    failing.clear()
    table.sync([{"doc_id": "d1", "text": "hello world"}])

    assert calls["notify"] == 2, "the failed node must run again"
    assert table.status().is_fresh


def test_a_failed_no_output_node_beside_a_nulled_column_is_an_error_row_retried_in_full() -> None:
    """`notify` raised before `summarize` was scheduled. The nulled columns would
    make a partial row, but none of its entries would name `notify`."""
    table = _notify_table([extract, notify, summarize, embed])
    failing.add("notify")

    receipt = table.insert(doc_id="d1", text="hello world")

    assert calls.get("summarize") is None, "the run ended before summarize was scheduled"
    assert receipt.status is RowStatus.ERROR
    assert table.partial() == ()

    failing.clear()
    calls.clear()
    table.sync([{"doc_id": "d1", "text": "hello world"}])

    assert calls == {"extract": 1, "notify": 1, "summarize": 1, "embed": 1}
    assert table.status().is_fresh


def test_on_error_raise_still_raises_and_stores_nothing() -> None:
    store = MemoryStore()
    table = Graph([extract, summarize, embed]).as_table(identity="doc_id", store=store, runner=SyncRunner())
    failing.add("embed")

    with pytest.raises(RuntimeError, match="embed service is down"):
        table.insert(doc_id="d1", text="hello world")

    assert store.rows.get("doc", []) == []


# ---------------------------------------------------------------------------
# A failing fan-out boundary (#464)
#
# `split` produces the items a child table maps over. The parent stores no
# column for them, so a failure there has no stored column of its own to null.
# ---------------------------------------------------------------------------


class Word(TypedDict):
    word_id: str
    text: str


@node(output_name="words")
def split(extracted: str) -> list[Word]:
    _record("split")
    return [Word(word_id=f"w{i}", text=word) for i, word in enumerate(extracted.split())]


@node(output_name="tagged")
def tag(text: str) -> str:
    _record("tag")
    return f"tag:{text}"


def _word_node():
    return Graph([tag], name="word").as_node().map_over("words", identity="word_id")


def _fanout_table(store: MemoryStore, runner=None, *, on_error="store"):
    return Graph([extract, summarize, split, _word_node()], name="doc").as_table(
        identity="doc_id",
        store=store,
        on_error=on_error,
        runner=runner or SyncRunner(),
    )


def _words(table) -> list[tuple[str, str]]:
    return sorted((row["word_id"], row["tagged"]) for row in table.child("word").rows(parent="d1"))


ARMS = ("a fresh insert", "a re-sync on new text")
GAMMA = [{"doc_id": "d1", "text": "gamma delta"}]


def _fail_the_boundary(table, arm: str):
    """Write d1 on "gamma delta" while the boundary is down — as a new row (the
    full-derive arm) or over a healthy row on older text (the reconcile arm)."""
    if arm == "a re-sync on new text":
        table.sync([{"doc_id": "d1", "text": "alpha beta"}])
    failing.add("split")
    calls.clear()
    return table.sync(GAMMA)


def test_a_failing_fanout_boundary_keeps_the_columns_that_succeeded() -> None:
    table = _fanout_table(MemoryStore())

    receipt = _fail_the_boundary(table, "a re-sync on new text")

    (row_receipt,) = receipt.receipts
    assert row_receipt.status is RowStatus.PARTIAL
    assert row_receipt.error == "RuntimeError: split service is down"
    assert calls == {"extract": 1, "summarize": 1, "split": 1}
    row = table.get("d1")
    assert row["extracted"] == "GAMMA DELTA"
    assert row["summary"] == "GAMMA"
    assert table.errors() == ()
    status = table.status()
    assert (status.stale, status.errored) == (1, 0)
    assert status.stale_columns == (), "the parent stores no column for the fan-out"
    assert _words(table) == [("w0", "tag:ALPHA"), ("w1", "tag:BETA")], "the earlier child rows stay as they are"


@pytest.mark.parametrize("arm", ARMS)
def test_a_failing_fanout_boundary_names_the_fan_out_it_could_not_build(arm: str) -> None:
    table = _fanout_table(MemoryStore())

    _fail_the_boundary(table, arm)

    (partial,) = table.partial()
    (change,) = partial.changes
    assert change.column == "words", "the map_over input, not a stored column"
    assert change.reason is ChangeReason.NODE_ERROR
    assert change.node == "split"
    assert change.error == "RuntimeError: split service is down"
    assert partial.row["summary"] == "GAMMA"


@pytest.mark.parametrize("arm", ARMS)
def test_healing_a_failed_fanout_re_runs_only_the_boundary(arm: str) -> None:
    table = _fanout_table(MemoryStore())
    _fail_the_boundary(table, arm)

    failing.clear()
    calls.clear()
    receipt = table.sync(GAMMA)

    assert calls == {"split": 1, "tag": 2}, "extract and summarize must not be paid twice"
    assert _words(table) == [("w0", "tag:GAMMA"), ("w1", "tag:DELTA")]
    assert receipt.completed
    assert table.partial() == ()
    assert table.status().is_fresh

    calls.clear()
    again = table.sync(GAMMA)

    assert calls == {}, "a healed row converges"
    assert again.receipts[0].outcome is WriteOutcome.SKIPPED


@node(output_name="letters")
def count_letters(text: str) -> int:
    _record("count_letters")
    return len(text)


def test_two_child_tables_over_one_fan_out_get_one_entry() -> None:
    lengths = Graph([count_letters], name="word_length").as_node().map_over("words", identity="word_id")
    table = Graph([extract, summarize, split, _word_node(), lengths], name="doc").as_table(
        identity="doc_id",
        store=MemoryStore(),
        on_error="store",
        runner=SyncRunner(),
    )

    _fail_the_boundary(table, "a fresh insert")

    (partial,) = table.partial()
    assert [(change.column, change.reason, change.node) for change in partial.changes] == [
        ("words", ChangeReason.NODE_ERROR, "split"),
    ]


def test_a_fanout_boundary_failure_with_nothing_else_derived_is_still_an_error_row() -> None:
    """With `extracted` a source, the boundary is the only derived producer: nothing stood."""
    table = Graph([split, _word_node()], name="doc").as_table(
        identity="doc_id",
        store=MemoryStore(),
        on_error="store",
        runner=SyncRunner(),
    )
    failing.add("split")

    receipt = table.insert(doc_id="d1", extracted="gamma delta")

    assert receipt.status is RowStatus.ERROR
    assert table.partial() == ()
    assert table.errors()[0].error == "RuntimeError: split service is down"


def test_a_non_boundary_failure_in_a_fanout_graph_names_no_fan_out() -> None:
    table = _fanout_table(MemoryStore())
    table.sync([{"doc_id": "d1", "text": "alpha beta"}])
    failing.add("summarize")

    table.sync([{"doc_id": "d1", "text": "gamma delta"}])

    (partial,) = table.partial()
    columns = [change.column for change in partial.changes]
    assert "summary" in columns
    assert "words" not in columns


def test_a_failing_fanout_boundary_inside_a_mounted_graph_is_blamed_by_its_path() -> None:
    stage = Graph([split], name="split_stage").as_node(name="split_stage")
    table = Graph([extract, summarize, stage, _word_node()], name="doc").as_table(
        identity="doc_id",
        store=MemoryStore(),
        on_error="store",
        runner=SyncRunner(),
    )
    failing.add("split")

    receipt = table.insert(doc_id="d1", text="gamma delta")

    assert receipt.status is RowStatus.PARTIAL
    (partial,) = table.partial()
    (change,) = partial.changes
    assert (change.column, change.reason, change.node) == ("words", ChangeReason.NODE_ERROR, "split_stage/split")
    assert partial.row["summary"] == "GAMMA"


def test_on_error_raise_still_raises_on_a_failing_fanout_boundary_and_stores_nothing() -> None:
    store = MemoryStore()
    table = _fanout_table(store, on_error="raise")
    failing.add("split")

    with pytest.raises(RuntimeError, match="split service is down"):
        table.insert(doc_id="d1", text="gamma delta")

    assert store.rows.get("doc", []) == []
    assert store.rows.get("word", []) == []


# ---------------------------------------------------------------------------
# A real store
# ---------------------------------------------------------------------------


def _lance_table(path: str):
    from hypergraph.materialization import LanceDBStore

    return Graph([extract, summarize, embed]).as_table(
        identity="doc_id",
        store=LanceDBStore(path),
        on_error="store",
        runner=SyncRunner(),
    )


def test_a_partial_row_round_trips_through_lancedb_and_heals_there(tmp_path) -> None:
    path = str(tmp_path / "partial_store")
    failing.add("embed")
    _lance_table(path).insert(doc_id="d1", text="hello world")

    reader = _lance_table(path)
    (partial,) = reader.partial()
    assert partial.changes[0].column == "embedding"
    assert partial.changes[0].error == "RuntimeError: embed service is down"
    assert partial.row["summary"] == "HELLO"
    assert reader.status().stale_ids == ("d1",)

    failing.clear()
    _lance_table(path).sync([{"doc_id": "d1", "text": "hello world"}])

    healed = _lance_table(path)
    assert calls == {"extract": 1, "summarize": 1, "embed": 2}
    assert healed.get("d1")["embedding"] == "vec(HELLO)"
    assert healed.partial() == ()
    assert healed.status().is_fresh


def test_a_store_written_before_changes_existed_takes_a_partial_row(tmp_path) -> None:
    """The _changes column is additive: an existing table grows it on first use."""
    from dataclasses import replace

    from hypergraph.materialization import LanceDBStore
    from hypergraph.materialization._schema import CHANGES_COLUMN, analyze_table

    path = str(tmp_path / "legacy_store")
    spec = analyze_table(Graph([extract, summarize, embed]), "doc_id", {}, [])
    LanceDBStore(path).open(replace(spec, columns=[column for column in spec.columns if column.name != CHANGES_COLUMN]), [])
    assert CHANGES_COLUMN not in LanceDBStore(path).column_names("doc")

    failing.add("embed")
    receipt = _lance_table(path).insert(doc_id="d1", text="hello world")

    assert receipt.status is RowStatus.PARTIAL
    assert CHANGES_COLUMN in LanceDBStore(path).column_names("doc")
    (partial,) = _lance_table(path).partial()
    assert partial.changes[0].column == "embedding"


class KeyedWord(TypedDict):
    word_id: str
    length_id: str
    text: str


@node(output_name="words")
def split_keyed(extracted: str) -> list[KeyedWord]:
    _record("split")
    return [KeyedWord(word_id=f"w{i}", length_id=f"l{i}", text=word) for i, word in enumerate(extracted.split())]


def _real_store(kind: str, tmp_path):
    from hypergraph.materialization import LanceDBStore, SqliteTableStore

    return LanceDBStore(str(tmp_path / "lance")) if kind == "lance" else SqliteTableStore(str(tmp_path / "table.db"))


@pytest.mark.parametrize("arm", ARMS)
@pytest.mark.parametrize("kind", ["lance", "sqlite"])
def test_two_child_tables_over_one_fan_out_open_fail_and_heal_on_a_real_store(kind: str, arm: str, tmp_path) -> None:
    """Both child tables share one boundary stamp column, so the parent table opens on a real store."""
    lengths = Graph([count_letters], name="word_length").as_node().map_over("words", identity="length_id")
    table = Graph([extract, summarize, split_keyed, _word_node(), lengths], name="doc").as_table(
        identity="doc_id",
        store=_real_store(kind, tmp_path),
        on_error="store",
        runner=SyncRunner(),
    )

    receipt = _fail_the_boundary(table, arm)

    assert receipt.receipts[0].status is RowStatus.PARTIAL
    (partial,) = table.partial()
    assert [(change.column, change.reason, change.node) for change in partial.changes] == [
        ("words", ChangeReason.NODE_ERROR, "split_keyed"),
    ]

    failing.clear()
    calls.clear()
    healed = table.sync(GAMMA)

    # Loose on purpose: the heal runs the boundary once per child table over one
    # map_over input (split runs twice here), filed as its own issue (D45).
    assert calls.pop("split") >= 1
    assert calls == {"tag": 2, "count_letters": 2}, "extract and summarize must not be paid twice"
    assert healed.completed
    assert _words(table) == [("w0", "tag:GAMMA"), ("w1", "tag:DELTA")]
    assert sorted((row["length_id"], row["letters"]) for row in table.child("length").rows(parent="d1")) == [("l0", 5), ("l1", 5)]
    assert table.partial() == ()
    assert table.status().is_fresh

    calls.clear()
    again = table.sync(GAMMA)

    assert calls == {}, "a healed row converges"
    assert again.receipts[0].outcome is WriteOutcome.SKIPPED


# ---------------------------------------------------------------------------
# Receipts, nested graphs, async parity
# ---------------------------------------------------------------------------


def test_a_sync_receipt_separates_partial_rows_from_errors() -> None:
    table = _table(MemoryStore())
    failing.update({"embed"})

    receipt = table.sync([{"doc_id": "d1", "text": "hello world"}, {"doc_id": "d2", "text": "second doc"}])

    assert {row.id for row in receipt.partial} == {"d1", "d2"}
    assert receipt.errors == ()
    assert not receipt.completed
    assert not receipt.failed


def test_a_failure_inside_a_mounted_graph_is_blamed_on_its_column() -> None:
    store = MemoryStore()
    stage = Graph([embed], name="embedding_stage")
    table = Graph([extract, summarize, stage.as_node(name="embed_stage")]).as_table(
        identity="doc_id",
        store=store,
        on_error="store",
        runner=SyncRunner(),
    )
    failing.add("embed")

    table.insert(doc_id="d1", text="hello world")

    (partial,) = table.partial()
    (change,) = partial.changes
    assert change.column == "embedding"
    assert change.reason is ChangeReason.NODE_ERROR
    assert change.node == "embed_stage/embed"
    assert partial.row["summary"] == "HELLO"


@pytest.mark.asyncio
async def test_async_keeps_the_same_partial_row_and_heals_it_the_same_way() -> None:
    table = _table(MemoryStore(), runner=AsyncRunner())
    failing.add("embed")

    await table.insert(doc_id="d1", text="hello world")

    (partial,) = table.partial()
    assert partial.changes[0].column == "embedding"
    assert partial.row["summary"] == "HELLO"
    assert table.status().stale == 1

    failing.clear()
    await table.sync([{"doc_id": "d1", "text": "hello world"}])

    assert calls == {"extract": 1, "summarize": 1, "embed": 2}
    assert table.get("d1")["embedding"] == "vec(HELLO)"
    assert table.partial() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ARMS)
async def test_async_keeps_the_same_fanout_partial_row_and_heals_it_the_same_way(arm: str) -> None:
    table = _fanout_table(MemoryStore(), runner=AsyncRunner())
    if arm == "a re-sync on new text":
        await table.sync([{"doc_id": "d1", "text": "alpha beta"}])
    failing.add("split")
    calls.clear()

    receipt = await table.sync(GAMMA)

    assert receipt.receipts[0].status is RowStatus.PARTIAL
    assert table.get("d1")["summary"] == "GAMMA"
    (partial,) = table.partial()
    (change,) = partial.changes
    assert (change.column, change.reason, change.node, change.error) == (
        "words",
        ChangeReason.NODE_ERROR,
        "split",
        "RuntimeError: split service is down",
    )

    failing.clear()
    calls.clear()
    healed = await table.sync(GAMMA)

    assert calls == {"split": 1, "tag": 2}
    assert _words(table) == [("w0", "tag:GAMMA"), ("w1", "tag:DELTA")]
    assert healed.completed
    assert table.partial() == ()

    calls.clear()
    again = await table.sync(GAMMA)

    assert calls == {}, "a healed row converges"
    assert again.receipts[0].outcome is WriteOutcome.SKIPPED


@pytest.mark.asyncio
async def test_async_a_failed_no_output_node_beside_a_failed_boundary_is_an_error_row_retried_in_full() -> None:
    """`split` and `notify` raise in one superstep. The fan-out entry would name
    `split` only, so a partial row would drop `notify`'s failure for good."""
    table = _notify_table([extract, summarize, split, notify, _word_node()], runner=AsyncRunner())
    failing.update({"split", "notify"})

    receipt = await table.sync(GAMMA)

    assert (calls["split"], calls["notify"]) == (1, 1), "both raised"
    assert receipt.receipts[0].status is RowStatus.ERROR
    assert table.partial() == ()

    failing.clear()
    calls.clear()
    await table.sync(GAMMA)

    assert calls == {"extract": 1, "summarize": 1, "split": 1, "notify": 1, "tag": 2}
    assert table.status().is_fresh


@pytest.mark.asyncio
async def test_async_a_mounted_graph_whose_inner_nodes_both_raised_stays_partial() -> None:
    """Both failures belong to `stage`, which the heal re-runs whole — so the
    entries name `stage` once and still cover both."""
    stage = Graph([summarize, aside], name="stage").as_node(name="stage")
    table = _notify_table([extract, stage], runner=AsyncRunner())
    failing.update({"summarize", "aside"})

    receipt = await table.insert(doc_id="d1", text="hello world")

    assert (calls["summarize"], calls["aside"]) == (1, 1), "both raised"
    assert receipt.status is RowStatus.PARTIAL
    (partial,) = table.partial()
    assert {change.node.split("/", 1)[0] for change in partial.changes} == {"stage"}

    failing.clear()
    calls.clear()
    await table.sync([{"doc_id": "d1", "text": "hello world"}])

    assert calls == {"summarize": 1, "aside": 1}, "only the mounted graph re-runs"
    assert table.status().is_fresh


@pytest.mark.asyncio
async def test_async_a_failed_no_output_node_beside_a_nulled_column_is_an_error_row_retried_in_full() -> None:
    table = _notify_table([extract, notify, summarize, embed], runner=AsyncRunner())
    failing.add("notify")

    receipt = await table.insert(doc_id="d1", text="hello world")

    assert receipt.status is RowStatus.ERROR
    assert table.partial() == ()

    failing.clear()
    calls.clear()
    await table.sync([{"doc_id": "d1", "text": "hello world"}])

    assert calls == {"extract": 1, "notify": 1, "summarize": 1, "embed": 1}
    assert table.status().is_fresh
