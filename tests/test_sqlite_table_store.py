"""``SqliteTableStore``: a stdlib-sqlite3 ``TableStore``, so Table and HyperTable run without lancedb.

``check_store_conformance`` does not pin several behaviors HyperTable relies on
(compare-and-set, concurrent writers, limit after the filter, unknown operators,
type fidelity, tie-breaking, row order, construction I/O). They are pinned here,
against the reference ``LanceDBStore`` behavior where one exists. The harness
itself runs in ``test_store_conformance.py``.
"""

from __future__ import annotations

import json
import math
import multiprocessing
import sqlite3
import threading
from pathlib import Path
from typing import TypedDict

import pyarrow as pa
import pytest

from hypergraph import Graph, node
from hypergraph.materialization import SqliteTableStore, Table
from hypergraph.materialization._schema import ColumnSpec, TableSpec
from hypergraph.materialization._sqlite_store import _where
from hypergraph.runners import AsyncRunner, SyncRunner

_INTERNAL = [
    ColumnSpec("_row_fingerprint", role="internal", arrow_type=pa.utf8()),
    ColumnSpec("_write_gen", role="internal", arrow_type=pa.int64()),
    ColumnSpec("_status", role="internal", arrow_type=pa.utf8()),
    ColumnSpec("_error", role="internal", arrow_type=pa.utf8()),
]


def _spec(name: str = "t", *extra: ColumnSpec) -> TableSpec:
    return TableSpec(name=name, identity="cid", columns=[ColumnSpec("cid", role="identity", arrow_type=pa.utf8()), *extra, *_INTERNAL])


def _typed_spec() -> TableSpec:
    return _spec(
        "t",
        ColumnSpec("n", role="source", arrow_type=pa.int64()),
        ColumnSpec("f", role="source", arrow_type=pa.float64()),
        ColumnSpec("b", role="source", arrow_type=pa.bool_()),
        ColumnSpec("vec", role="derived", arrow_type=pa.list_(pa.float32())),
        ColumnSpec("tags", role="derived", arrow_type=pa.list_(pa.utf8())),
        ColumnSpec("blob", role="derived", arrow_type=pa.large_binary()),
    )


@pytest.fixture
def store():
    s = SqliteTableStore()
    yield s
    s.close()


@pytest.fixture
def typed(store):
    store.open(_typed_spec(), [])
    return store


@pytest.fixture
def numbers(store):
    """Rows a..e with n = 1..5, written in that order."""
    store.open(_spec("t", ColumnSpec("n", role="source", arrow_type=pa.int64())), [])
    store.write_rows("t", [{"cid": cid, "n": n, "_write_gen": 1} for n, cid in enumerate("abcde", start=1)])
    return store


# --- conformance and capabilities ---


def test_capabilities(store):
    """Conformance itself runs in test_store_conformance.py, in memory and on a file."""
    assert store.supports_column_projection() is True
    assert store.supports_manifests() is False
    assert SqliteTableStore.thread_safe is True


# --- types ---


def test_every_column_type_round_trips_exactly(typed):
    typed.write_rows("t", [{"cid": "a", "n": 2, "f": 0.1, "b": True, "vec": [0.1, 0.2], "tags": ["x"], "blob": b"\x00\xff", "_write_gen": 1}])

    row = typed.read_one("t", "cid", "a")

    assert {k: row[k] for k in ("n", "f", "b", "vec", "tags", "blob")} == {
        "n": 2,
        "f": 0.1,
        "b": True,
        "vec": [0.1, 0.2],
        "tags": ["x"],
        "blob": b"\x00\xff",
    }
    assert type(row["b"]) is bool
    assert type(row["blob"]) is bytes


def test_column_kinds_are_read_back_by_a_fresh_handle(tmp_path):
    """The kind lives in the file, so another process decodes bool and JSON the same way."""
    path = tmp_path / "kinds.db"
    writer = SqliteTableStore(path)
    reader = SqliteTableStore(path)
    try:
        writer.open(_typed_spec(), [])
        writer.write_rows("t", [{"cid": "a", "b": False, "tags": ["x", "y"], "_write_gen": 1}])
        row = reader.read_one("t", "cid", "a")
    finally:
        writer.close()
        reader.close()
    assert row["b"] is False
    assert row["tags"] == ["x", "y"]


def test_a_sparse_row_reads_every_physical_column(typed):
    typed.write_rows("t", [{"cid": "a", "n": 1, "_write_gen": 1}])

    row = typed.read_one("t", "cid", "a")

    assert set(row) == set(typed.column_names("t"))
    assert row["f"] is None and row["vec"] is None and row["blob"] is None


def test_a_list_of_strings_column_takes_a_list_of_dicts(typed):
    """Named leniency: JSON columns take any JSON value (LanceDB refuses this one)."""
    typed.write_rows("t", [{"cid": "a", "tags": [{"k": 1}], "_write_gen": 1}])
    assert typed.read_one("t", "cid", "a")["tags"] == [{"k": 1}]


def test_nan_reads_back_as_none(typed):
    typed.write_rows("t", [{"cid": "a", "f": math.nan, "_write_gen": 1}])
    assert typed.read_one("t", "cid", "a")["f"] is None


def test_numpy_values_are_stored_as_plain_python(typed):
    np = pytest.importorskip("numpy")
    typed.write_rows("t", [{"cid": "a", "n": np.int64(3), "vec": np.array([0.5, 1.5]), "_write_gen": 1}])
    row = typed.read_one("t", "cid", "a")
    assert row["n"] == 3 and type(row["n"]) is int
    assert row["vec"] == [0.5, 1.5]


@pytest.mark.parametrize(
    ("row", "column", "declared"),
    [
        ({"cid": "d", "_status": 5, "_write_gen": 1}, "_status", "utf8"),
        ({"cid": "d", "n": "7", "_write_gen": 1}, "n", "int64"),
        ({"cid": "d", "b": 1, "_write_gen": 1}, "b", "bool"),
        ({"cid": "d", "blob": "text", "_write_gen": 1}, "blob", "large_binary"),
        ({"cid": "d", "tags": {b"bytes"}, "_write_gen": 1}, "tags", "JSON"),
    ],
)
def test_a_value_of_the_wrong_type_is_refused_and_nothing_is_written(typed, row, column, declared):
    typed.write_rows("t", [{"cid": "keep", "_write_gen": 1}])
    ok_row = {"cid": "ok", "_write_gen": 1}

    with pytest.raises(TypeError) as caught:
        typed.write_rows("t", [ok_row, row])

    message = str(caught.value)
    assert repr(column) in message and "'t'" in message, "names the column and the table"
    assert declared in message, "names the column's declared type"
    assert "How to fix:" in message
    assert message.count("\n\n") >= 2, "problem, context, how to fix"
    assert typed.count("t") == 1, "the whole call is one transaction: the valid row is not written either"


# --- write keys ---


def test_a_key_that_is_not_a_column_is_ignored_and_the_callers_row_is_unchanged(typed):
    row = {"cid": "a", "n": 1, "zzz": "ignored", "_write_gen": 1}
    before = dict(row)

    typed.write_rows("t", [row])

    assert "zzz" not in typed.read_one("t", "cid", "a")
    assert row == before, "write_rows must not mutate the caller's dict"


class SpokenUtterance(TypedDict):
    utterance_id: str
    text: str
    speaker: str


@node(output_name="utterances")
def split_spoken(transcript: str) -> list[SpokenUtterance]:
    return [SpokenUtterance(utterance_id=f"u{i}", text=word, speaker="Alice") for i, word in enumerate(transcript.split())]


@node(output_name="loud")
def shout(text: str) -> str:
    return text.upper()


def test_a_mapped_item_field_the_child_graph_does_not_consume_inserts_and_reads_back(store):
    """HyperTable writes every item field; ``speaker`` has no child column, so the store must drop it."""
    per_utterance = Graph([shout], name="per_utterance").as_node().map_over("utterances", identity="utterance_id")
    table = Graph([split_spoken, per_utterance]).as_table(identity="doc_id", store=store, runner=SyncRunner())

    receipt = table.insert(doc_id="d1", transcript="hello world")

    assert receipt.error is None
    children = table.child(table.child_table_names[0]).rows(parent="d1")
    assert [(row["utterance_id"], row["loud"]) for row in children] == [("u0", "HELLO"), ("u1", "WORLD")]


# --- predicates ---


def test_a_predicate_on_an_unknown_column_matches_nothing(numbers):
    assert numbers.read_rows("t", [("nope", "eq", 1)]) == []
    assert numbers.delete_rows("t", [("nope", "eq", 1)]) == 0
    assert numbers.count("t") == 5


@pytest.mark.parametrize("call", ["read_rows", "delete_rows"])
def test_an_unknown_operator_is_refused(numbers, call):
    with pytest.raises(ValueError, match="startswith") as caught:
        getattr(numbers, call)("t", [("n", "startswith", "x")])
    assert "eq, ne, lt, lte, gt, gte, in" in str(caught.value)
    assert numbers.count("t") == 5


def test_limit_applies_after_the_predicate(numbers):
    assert [row["cid"] for row in numbers.read_rows("t", [("n", "eq", 3)], limit=1)] == ["c"]


def test_in_with_no_values_matches_nothing(numbers):
    assert numbers.read_rows("t", [("n", "in", [])]) == []
    assert numbers.delete_rows("t", [("n", "in", [])]) == 0


def test_in_with_5000_values_stays_under_sqlites_variable_limit(numbers):
    values = list(range(3, 5003))
    assert [row["cid"] for row in numbers.read_rows("t", [("n", "in", values)])] == ["c", "d", "e"]
    assert numbers.delete_rows("t", [("cid", "in", [f"x{i}" for i in range(5000)] + ["a"])]) == 1


def test_in_on_a_bytes_column(store):
    store.open(_spec("t", ColumnSpec("blob", role="source", arrow_type=pa.large_binary())), [])
    store.write_rows("t", [{"cid": "a", "blob": b"\x00", "_write_gen": 1}, {"cid": "b", "blob": b"\xff", "_write_gen": 1}])
    assert [row["cid"] for row in store.read_rows("t", [("blob", "in", [b"\xff"])])] == ["b"]


def test_in_on_a_text_column_matches_like_eq(store):
    """SQLite gives a text column's comparison text affinity; an ``in`` list gets the same coercion."""
    store.open(_spec("t", ColumnSpec("txt", role="source", arrow_type=pa.utf8())), [])
    store.write_rows("t", [{"cid": "a", "txt": "123", "_write_gen": 1}, {"cid": "b", "txt": "1.5", "_write_gen": 1}])
    for value, want in ((123, ["a"]), (1.5, ["b"]), ("123", ["a"])):
        assert [row["cid"] for row in store.read_rows("t", [("txt", "eq", value)])] == want
        assert [row["cid"] for row in store.read_rows("t", [("txt", "in", [value])])] == want


def test_bytes_in_an_in_list_on_a_text_column_is_refused_naming_the_column(numbers):
    with pytest.raises(TypeError, match="'cid'") as caught:
        numbers.read_rows("t", [("cid", "in", ["a", b"b"])])
    assert "How to fix:" in str(caught.value)
    assert "JSON serializable" not in str(caught.value)


def test_read_rows_returns_rows_in_insertion_order(numbers):
    numbers.delete_rows("t", [("cid", "eq", "b")])
    numbers.write_rows("t", [{"cid": "b", "n": 2, "_write_gen": 2}])
    assert [row["cid"] for row in numbers.read_rows("t")] == ["a", "c", "d", "e", "b"]


def test_read_one_breaks_a_generation_tie_toward_the_first_written_row(store):
    """LanceDBStore's stable sort returns the earliest row on a ``_write_gen`` tie."""
    store.open(_spec("t", ColumnSpec("v", role="source", arrow_type=pa.utf8())), [])
    store.write_rows("t", [{"cid": "a", "v": "first", "_write_gen": 3}])
    store.write_rows("t", [{"cid": "a", "v": "second", "_write_gen": 3}])
    assert store.read_one("t", "cid", "a")["v"] == "first"


@pytest.mark.parametrize("name", ["rowid", "ROWID", "_rowid_", "oid"])
def test_a_column_named_like_a_rowid_alias_changes_neither_row_order_nor_the_tie_break(store, name):
    """A user column called rowid hides SQLite's own rowid; the store orders by an alias it does not hide."""
    store.open(_spec("t", ColumnSpec(name, role="source", arrow_type=pa.utf8())), [])
    store.write_rows("t", [{"cid": cid, name: value, "_write_gen": 1} for cid, value in (("z", "3"), ("a", "1"), ("m", "2"))])
    assert [row["cid"] for row in store.read_rows("t")] == ["z", "a", "m"]

    store.write_rows("t", [{"cid": "tie", name: "9", "_write_gen": 5}])
    store.write_rows("t", [{"cid": "tie", name: "0", "_write_gen": 5}])
    assert store.read_one("t", "cid", "tie")[name] == "9", "a generation tie goes to the first-written row"
    assert store.compare_and_set("t", "cid", "tie", {name: "9"}, {name: "claimed"}, {}) is True
    assert store.read_one("t", "cid", "tie")[name] == "claimed"


def test_a_table_holding_all_three_rowid_aliases_is_refused(store):
    three = [ColumnSpec(name, role="source", arrow_type=pa.utf8()) for name in ("rowid", "_rowid_", "OID")]
    with pytest.raises(ValueError, match="How to fix:") as caught:
        store.open(_spec("t", *three), [])
    assert "cannot create table 't'" in str(caught.value)
    assert "rowid, _rowid_ and oid" in str(caught.value)
    assert store.column_names("t") == []

    store.open(_spec("u", *three[:2]), [])
    with pytest.raises(ValueError, match="rowid, _rowid_ and oid") as caught:
        store.evolve_schema("u", {"oid": pa.utf8()})
    assert "cannot add column 'oid' to table 'u'" in str(caught.value)
    assert "oid" not in store.column_names("u")


def test_a_table_name_starting_with_sqlite_is_refused(store):
    with pytest.raises(ValueError, match="sqlite_") as caught:
        Table(identity="sqlite_job_id", store=store)
    assert "How to fix:" in str(caught.value)


@pytest.mark.parametrize("value", [2**63, -(2**63) - 1, 2**70])
def test_an_int_outside_64_bits_is_refused_on_write_and_in_filters(numbers, value):
    with pytest.raises(TypeError, match="'n'") as caught:
        numbers.write_rows("t", [{"cid": "big", "n": value, "_write_gen": 1}])
    assert "'t'" in str(caught.value) and "How to fix:" in str(caught.value)
    for where in ([("n", "eq", value)], [("n", "in", [1, value])]):
        with pytest.raises(TypeError, match="'n'"):
            numbers.read_rows("t", where)
    assert numbers.count("t") == 5


def test_the_64_bit_int_bounds_round_trip(numbers):
    numbers.write_rows("t", [{"cid": "max", "n": 2**63 - 1, "_write_gen": 1}, {"cid": "min", "n": -(2**63), "_write_gen": 1}])
    assert numbers.read_one("t", "cid", "max")["n"] == 2**63 - 1
    assert [row["cid"] for row in numbers.read_rows("t", [("n", "in", [-(2**63)])])] == ["min"]


def _reject_json5(token: str) -> None:
    raise AssertionError(f"the in-list JSON carries {token}, which only a JSON5-capable SQLite parses")


def test_non_finite_floats_in_an_in_list_match_like_eq_without_json5(store):
    store.open(_spec("t", ColumnSpec("f", role="source", arrow_type=pa.float64()), ColumnSpec("txt", role="source", arrow_type=pa.utf8())), [])
    store.write_rows(
        "t",
        [
            {"cid": "pos", "f": math.inf, "txt": "Inf", "_write_gen": 1},
            {"cid": "neg", "f": -math.inf, "txt": "-Inf", "_write_gen": 1},
            {"cid": "one", "f": 1.0, "txt": "1.0", "_write_gen": 1},
            {"cid": "nan", "f": math.nan, "_write_gen": 1},
        ],
    )
    expected = {math.inf: ["pos"], -math.inf: ["neg"], 1.0: ["one"]}
    for column in ("f", "txt"):
        for value in (math.inf, -math.inf, math.nan, 1.0):
            eq = [row["cid"] for row in store.read_rows("t", [(column, "eq", value)])]
            assert eq == expected.get(value, []), (column, value)
            assert [row["cid"] for row in store.read_rows("t", [(column, "in", [value])])] == eq, (column, value)

    _sql, params = _where("t", {"f": "real"}, [("f", "in", [math.inf, -math.inf, math.nan, 1.0])])
    json.loads(params[0], parse_constant=_reject_json5)


def test_a_projection_naming_an_unknown_column_fails_loudly(numbers):
    with pytest.raises(KeyError, match="nope"):
        numbers.read_rows("t", columns=["cid", "nope"])
    with pytest.raises(KeyError, match="nope"):
        numbers.read_one("t", "cid", "a", columns=["nope"])


def test_identifiers_are_quoted(store):
    store.open(_spec('odd "table"', ColumnSpec('we"ird col', role="source", arrow_type=pa.utf8())), [])
    store.write_rows('odd "table"', [{"cid": "a", 'we"ird col': "x'); DROP TABLE t; --", "_write_gen": 1}])
    assert store.read_rows('odd "table"', [('we"ird col', "eq", "x'); DROP TABLE t; --")], columns=["cid"]) == [{"cid": "a"}]


# --- schema ---


def test_reads_on_a_table_never_created_are_empty(store):
    assert store.count("never") == 0
    assert store.read_rows("never") == []
    assert store.read_one("never", "cid", "a") is None
    assert store.max_write_gen("never") == 0
    assert store.column_names("never") == []
    assert store.delete_rows("never", [("cid", "eq", "a")]) == 0


def test_evolve_schema_on_a_table_never_opened_raises(store):
    with pytest.raises(KeyError, match="not open"):
        store.evolve_schema("never", {"x": pa.utf8()})


def test_open_leaves_an_existing_table_as_it_is(numbers):
    wider = _spec("t", ColumnSpec("n", role="source", arrow_type=pa.int64()), ColumnSpec("later", role="derived", arrow_type=pa.utf8()))
    assert "later" not in numbers.open(wider, [])["t"]
    assert numbers.count("t") == 5


def test_answer_columns_are_not_created_by_open(store):
    spec = _spec("t", ColumnSpec("decision", role="answer", arrow_type=pa.utf8()))
    assert "decision" not in store.open(spec, [])["t"]


def test_names_that_differ_only_by_case_are_refused(store):
    store.open(_spec("V", ColumnSpec("tag", role="source", arrow_type=pa.utf8())), [])
    with pytest.raises(ValueError, match="letter case"):
        store.open(_spec("v"), [])
    with pytest.raises(ValueError, match="letter case"):
        store.evolve_schema("V", {"TAG": pa.utf8()})
    with pytest.raises(ValueError, match="letter case"):
        store.open(_spec("w", ColumnSpec("A", role="source"), ColumnSpec("a", role="source")), [])


def test_a_column_named_twice_is_refused_as_a_duplicate_not_a_case_clash(store):
    with pytest.raises(ValueError, match="names column 'a' twice") as refused:
        store.open(_spec("w", ColumnSpec("a", role="source"), ColumnSpec("a", role="source")), [])
    assert "letter case" not in str(refused.value)
    assert store.column_names("w") == []


# --- compare-and-set ---


def test_compare_and_set_evolves_writes_and_drops_the_old_generation_in_one_step(store):
    table = Table(identity="user_id", store=store)
    table.append(user_id="u1", used=0)

    assert table.compare_and_set("u1", expected={"used": 0}, used=1, note="first") is True

    assert table.get("u1") == {"user_id": "u1", "used": 1, "note": "first"}
    assert store.count("user") == 1


def test_a_failed_compare_and_set_changes_nothing(store):
    table = Table(identity="user_id", store=store)
    table.append(user_id="u1", used=0)

    assert table.compare_and_set("u1", expected={"used": 5}, used=6, note="never") is False

    assert "note" not in store.column_names("user")
    assert store.count("user") == 1


def test_a_compare_and_set_that_writes_a_wrong_type_rolls_back(store):
    table = Table(identity="user_id", store=store)
    table.append(user_id="u1", used=0)

    with pytest.raises(TypeError, match="used"):
        store.compare_and_set("user", "user_id", "u1", {"used": 0}, {"used": "one", "note": "x"}, {"note": pa.utf8()})

    assert "note" not in store.column_names("user"), "the evolve in the same transaction is rolled back too"
    assert table.get("u1") == {"user_id": "u1", "used": 0}


def _claim(path, barrier, index, queue):
    table = Table(identity="upload_id", store=SqliteTableStore(path))
    barrier.wait()
    queue.put((f"w{index}", table.compare_and_set("u1", expected={"state": "pending"}, state="claimed", owner=f"w{index}")))


def _increment(path, times, barrier):
    table = Table(identity="k", store=SqliteTableStore(path))
    barrier.wait()
    done = 0
    while done < times:
        row = table.get("c")
        if table.compare_and_set("c", expected={"v": row["v"]}, v=row["v"] + 1):
            done += 1


def _run_processes(context, target, argument_sets, timeout=120):
    processes = [context.Process(target=target, args=args) for args in argument_sets]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=timeout)
        assert process.exitcode == 0
        process.close()


def test_eight_processes_contend_and_exactly_one_compare_and_set_wins(tmp_path):
    path = tmp_path / "cas.db"
    seed = SqliteTableStore(path)
    try:
        Table(identity="upload_id", store=seed).append(upload_id="u1", state="pending")
        context = multiprocessing.get_context("spawn")
        barrier, queue = context.Barrier(8), context.Queue()

        _run_processes(context, _claim, [(path, barrier, index, queue) for index in range(8)])
        results = [queue.get(timeout=5) for _ in range(8)]
        queue.close()
        queue.join_thread()

        winners = [owner for owner, won in results if won]
        assert len(winners) == 1
        assert Table(identity="upload_id", store=seed).get("u1") == {"upload_id": "u1", "state": "claimed", "owner": winners[0]}
        assert len(seed.read_rows("upload")) == 1
    finally:
        seed.close()


def test_read_then_compare_and_set_increments_lose_no_update_across_processes(tmp_path):
    path = tmp_path / "counter.db"
    seed = SqliteTableStore(path)
    try:
        Table(identity="k", store=seed).append(k="c", v=0)
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(4)

        _run_processes(context, _increment, [(path, 50, barrier)] * 4)

        assert Table(identity="k", store=seed).get("c")["v"] == 200
    finally:
        seed.close()


def test_threads_sharing_one_memory_store_lose_no_update(store):
    table = Table(identity="k", store=store)
    table.append(k="c", v=0)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            done = 0
            while done < 100:
                row = table.get("c")
                if table.compare_and_set("c", expected={"v": row["v"]}, v=row["v"] + 1):
                    done += 1
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert table.get("c")["v"] == 800
    assert store.count("k") == 1


# --- instances, files, construction ---


def test_two_memory_stores_share_nothing():
    first, second = SqliteTableStore(), SqliteTableStore()
    try:
        Table(identity="k", store=first).append(k="x", v=1)
        assert Table(identity="k", store=second).get("x") is None
    finally:
        first.close()
        second.close()


def test_two_file_stores_on_one_path_see_each_others_writes_and_evolve_idempotently(tmp_path):
    path = tmp_path / "shared.db"
    writer, other = SqliteTableStore(path), SqliteTableStore(path)
    try:
        spec = _spec("t", ColumnSpec("n", role="source", arrow_type=pa.int64()))
        writer.open(spec, [])
        other.open(spec, [])
        writer.write_rows("t", [{"cid": "a", "n": 1, "_write_gen": 1}])
        assert other.read_one("t", "cid", "a")["n"] == 1
        assert other.count("t") == 1 and other.max_write_gen("t") == 1

        writer.evolve_schema("t", {"tag": pa.utf8()})
        assert "tag" in other.column_names("t")
        assert other.evolve_schema("t", {"tag": pa.utf8()}).count("tag") == 1
        other.write_rows("t", [{"cid": "b", "n": 2, "tag": "y", "_write_gen": 2}])
        assert writer.read_one("t", "cid", "b")["tag"] == "y"
    finally:
        writer.close()
        other.close()


def test_construction_and_empty_reads_do_no_io_and_open_creates_the_file_in_wal_mode(tmp_path):
    path = tmp_path / "x" / "y.db"
    store = SqliteTableStore(path)
    try:
        assert store.count("t") == 0
        assert store.read_rows("t") == []
        assert store.read_one("t", "cid", "a") is None
        assert store.column_names("t") == []
        assert store.delete_rows("t", [("cid", "eq", "a")]) == 0
        assert not (tmp_path / "x").exists(), "construction and reads of a missing file create nothing"

        store.open(_spec(), [])

        assert path.exists()
    finally:
        store.close()
    with sqlite3.connect(path) as raw:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    raw.close()


def test_close_is_idempotent_and_a_closed_store_refuses_use():
    store = SqliteTableStore()
    store.close()
    store.close()
    with pytest.raises(RuntimeError, match="closed"):
        store.count("t")


# --- async HyperTable ---


@node(output_name="clean_text")
async def clean(text: str) -> str:
    return text.strip().lower()


@node(output_name="word_count")
def count_words(clean_text: str) -> int:
    return len(clean_text.split())


class ThreadRecordingStore(SqliteTableStore):
    def __init__(self, path: Path, seen: list[int]) -> None:
        super().__init__(path)
        self.seen = seen

    def write_rows(self, *args, **kwargs):
        self.seen.append(threading.get_ident())
        return super().write_rows(*args, **kwargs)


@pytest.mark.asyncio
async def test_an_async_hypertable_writes_off_the_event_loop(tmp_path):
    seen: list[int] = []
    store = ThreadRecordingStore(tmp_path / "async.db", seen)
    try:
        table = Graph([clean, count_words]).as_table(identity="doc_id", store=store, runner=AsyncRunner())
        loop_thread = threading.get_ident()

        await table.insert(doc_id="d1", text="Hello World")
        result = await table.sync([{"doc_id": "d1", "text": "Hello World"}, {"doc_id": "d2", "text": "one two three"}])

        assert result.receipts
        assert table.get("d2")["word_count"] == 3
        assert seen, "no store writes recorded"
        assert all(thread != loop_thread for thread in seen)
    finally:
        store.close()
